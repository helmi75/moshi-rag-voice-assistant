"""Un faux resOS, calqué sur la documentation publique de l'API (v1.2, Postman, 23/07/2025).

resOS n'a ni bac à sable ni compte de test, et son API est une option payante
(constaté le 25/09/2026). Ce serveur la remplace pour développer et tester
`app/connecteurs/resos.py` sans rien payer.

DOCUMENTÉ, et reproduit à la forme près des exemples de la doc :
- authentification Basic, la clé en nom d'utilisateur, sans mot de passe — 401 sinon ;
- `POST /bookings` rend l'identifiant seul, en chaîne JSON ; `PUT` rend `true` ;
  `POST /bookings/{id}/restaurantNote` rend l'identifiant de la note ;
- `GET /bookings` filtre par `fromDateTime`, `toDateTime` et `customQuery`
  (`guest.phone:"33612345678"` : indicatif en tête, sans le +) ;
- `GET /bookingFlow/times` rend un objet par service, avec `availableTimes` et
  `unavailableTimes` ;
- statut par défaut `request`, source par défaut `other`.

SUPPOSÉ, faute de doc — à vérifier au premier vrai appel (docs/RESOS.md) :
- un créneau complet ACCEPTE quand même la réservation. C'est le cas le plus dangereux,
  donc celui contre lequel le connecteur doit se protéger ;
- le texte d'une 404 et d'une 422 ;
- une capacité simple : `capacite` réservations actives par créneau de 15 minutes.

À la main, pour un banc local :
    cd api && python -m uvicorn tests.faux_resos:app --port 8765
puis RESOS_API_URL=http://localhost:8765/v1 et RESOS_API_KEYS="<id>=cle-de-test-resos".
"""
import asyncio
import base64
import itertools
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CLE_DE_TEST = "cle-de-test-resos"
FUSEAU = ZoneInfo("Europe/Paris")

# Deux services, des créneaux de 15 min, fin incluse — la forme de l'exemple de la doc.
SERVICES = (("12:00", "14:00"), ("19:00", "22:00"))
_ACTIFS = ("request", "approved", "waitlist", "arrived", "seated")
_MODIFIABLES = {"date", "time", "dateTime", "people", "tables", "duration", "status",
                "metadata", "source", "referrer", "languageCode", "openingHourId",
                "canceledAt", "deletedAt", "declinedAt", "openingHourName", "customer",
                "guest", "customFields"}


def _heures(debut: str, fin: str) -> list[str]:
    def minutes(h):
        return int(h[:2]) * 60 + int(h[3:])
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(minutes(debut), minutes(fin) + 1, 15)]


def _identifiant() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(17))


@dataclass
class Etat:
    """Tout ce qu'un test peut régler ou relire."""
    cle: str = CLE_DE_TEST
    capacite: int = 3
    en_panne: bool = False          # toutes les routes répondent 503
    retard: float = 0.0             # secondes d'attente avant chaque réponse
    fermes: set = field(default_factory=set)       # dates sans aucun service
    reservations: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    requetes: list = field(default_factory=list)   # (méthode, chemin, paramètres)

    def reserver(self, *, date, time, people=2, nom="Client", phone=None,
                 status="approved") -> str:
        """Une réservation déjà présente dans le carnet du restaurant."""
        identifiant = _identifiant()
        self.reservations[identifiant] = _booking(identifiant, {
            "date": date, "time": time, "people": people, "status": status,
            "guest": {"name": nom, "phone": phone}})
        return identifiant

    def occupes(self, date: str, heure: str, sauf: str | None = None) -> int:
        return sum(1 for b in self.reservations.values()
                   if b["date"] == date and b["time"] == heure and b["status"] in _ACTIFS
                   and b["_id"] != sauf)


def _booking(identifiant: str, corps: dict) -> dict:
    invite = corps.get("guest") or {}
    date, heure = corps["date"], corps["time"]
    local = datetime.fromisoformat(f"{date}T{heure}").replace(tzinfo=FUSEAU)
    return {
        "_id": identifiant,
        "restaurantId": "fauxRestoTest",
        "date": date,
        "time": heure,
        "dateTime": local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "people": corps.get("people"),
        "duration": corps.get("duration", 120),
        "status": corps.get("status") or "request",
        "source": corps.get("source") or "other",
        "languageCode": corps.get("languageCode") or "en",
        "guest": {"name": invite.get("name"), "phone": invite.get("phone") or "",
                  "email": invite.get("email") or "",
                  "notificationSms": bool(invite.get("notificationSms")),
                  "notificationEmail": bool(invite.get("notificationEmail"))},
        "comment": corps.get("comment"),
        "note": corps.get("note"),
        "noteAuthor": corps.get("noteAuthor") or "API",
        "metadata": corps.get("metadata") or {},
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def _authentifie(request: Request, cle: str) -> bool:
    entete = request.headers.get("authorization", "")
    if not entete.startswith("Basic "):
        return False
    try:
        utilisateur, _, mot_de_passe = base64.b64decode(entete[6:]).decode().partition(":")
    except ValueError:
        return False
    return secrets.compare_digest(utilisateur, cle) and mot_de_passe == ""


def _filtre(booking: dict, expression: str) -> bool:
    """`customQuery` : conditions séparées par des virgules. Seule l'égalité `champ:"v"`
    est reproduite — le connecteur n'utilise qu'elle."""
    for condition in filter(None, (c.strip() for c in expression.split(","))):
        champ, _, valeur = condition.partition(":")
        valeur = valeur.strip().strip('"')
        actuel = booking
        for morceau in champ.strip().split("."):
            actuel = actuel.get(morceau) if isinstance(actuel, dict) else None
        if champ.strip() == "guest.phone":
            # « the country code must be included as the first 2 digits. The + sign is
            # not required » — la doc, mot pour mot.
            actuel = str(actuel or "").lstrip("+")
        if str(actuel) != valeur:
            return False
    return True


def creer_app(etat: Etat | None = None) -> FastAPI:
    etat = etat or Etat()
    app = FastAPI()
    app.state.etat = etat

    @app.middleware("http")
    async def garde(request: Request, call_next):
        etat.requetes.append((request.method, request.url.path, dict(request.query_params)))
        if etat.retard:
            await asyncio.sleep(etat.retard)
        if etat.en_panne:
            return JSONResponse("Service unavailable", status_code=503)
        if not _authentifie(request, etat.cle):
            return JSONResponse("Unauthorized", status_code=401)
        return await call_next(request)

    @app.get("/v1/healthcheck")
    def healthcheck():
        return {"status": "Now we're cooking!", "restaurantId": "fauxRestoTest"}

    @app.get("/v1/bookingFlow/times")
    def creneaux(date: str, people: int = 2):
        if date in etat.fermes:
            return []
        services = []
        for numero, (debut, fin) in enumerate(SERVICES, start=1):
            heures = _heures(debut, fin)
            libres = [h for h in heures if etat.occupes(date, h) < etat.capacite]
            services.append({
                "_id": f"service{numero}", "name": "", "note": "",
                "payment": {"active": False},
                "availableTimes": libres,
                "unavailableTimes": [h for h in heures if h not in libres],
                "waitlistTimes": [], "useWaitlist": False, "activeCustomFields": [],
            })
        return services

    @app.post("/v1/bookings")
    async def creer(request: Request):
        corps = await request.json()
        manquants = [c for c in ("date", "time", "people") if not corps.get(c)]
        if not (corps.get("guest") or {}).get("name"):
            manquants.append("guest.name")
        if manquants:
            return JSONResponse(f"Missing required parameter: {', '.join(manquants)}",
                                status_code=422)
        identifiant = _identifiant()
        # Aucun contrôle de capacité ici : c'est l'hypothèse prudente (voir en tête).
        etat.reservations[identifiant] = _booking(identifiant, corps)
        return JSONResponse(identifiant)

    @app.get("/v1/bookings")
    def lister(fromDateTime: str = "", toDateTime: str = "", customQuery: str = "",
               limit: int = 100, skip: int = 0, sort: str = ""):
        trouves = [b for b in etat.reservations.values()
                   if (not fromDateTime or b["date"] >= fromDateTime[:10])
                   and (not toDateTime or b["date"] <= toDateTime[:10])
                   and _filtre(b, customQuery)]
        if sort:
            champ, _, sens = sort.split(",")[0].partition(":")
            trouves.sort(key=lambda b: str(b.get(champ, "")), reverse=sens.strip() == "-1")
        return list(itertools.islice(trouves, skip, skip + min(limit, 100)))

    @app.get("/v1/bookings/{identifiant}")
    def lire(identifiant: str):
        booking = etat.reservations.get(identifiant)
        return booking if booking else JSONResponse("Not found", status_code=404)

    @app.put("/v1/bookings/{identifiant}")
    async def modifier(identifiant: str, request: Request):
        booking = etat.reservations.get(identifiant)
        if booking is None:
            return JSONResponse("Not found", status_code=404)
        corps = await request.json()
        for champ, valeur in corps.items():
            if champ in _MODIFIABLES:
                booking[champ] = valeur
        return JSONResponse(True)

    @app.post("/v1/bookings/{identifiant}/restaurantNote")
    async def noter(identifiant: str, request: Request):
        if identifiant not in etat.reservations:
            return JSONResponse("Not found", status_code=404)
        etat.notes.append((identifiant, (await request.json()).get("text")))
        return JSONResponse(_identifiant())

    return app


app = creer_app()
