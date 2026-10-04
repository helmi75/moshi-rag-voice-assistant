"""Le site vitrine : la page d'accueil d'Helmane et « Rappelez-moi » (ASSISTANTE-119).

Public, sans compte : la page ne lit rien en base, et la seule chose qu'un visiteur peut
y déclencher est un rappel — dont tous les refus sont dans `app/rappel.py`.

Les prix affichés viennent de `app/plans.py`. L'ancienne page de vente annonçait la
formule Maison à 249 € pour 1 200 appels, des semaines après que la grille l'eut passée à
349 € pour 750 : une page qui recopie des prix finit par mentir. Le 02/10/2026 la grille
est passée des appels aux minutes, et la page a suivi sans qu'on y recopie un chiffre.
"""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from .. import plans, rappel, rgpd
from ..admin import throttle

STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter()

# La formule mise en avant sur la page, et l'exemple chiffré du calcul (6 appels manqués
# par jour, la moitié qui auraient réservé, 2,5 couverts à 32 € : les curseurs au départ).
PHARE = "service"
_CA_EXEMPLE = 7200
# Une demande de rappel tient en cinquante octets.
_CORPS_MAX = 2000


@lru_cache(maxsize=None)
def _empreinte(nom: str) -> str:
    return hashlib.sha1((STATIC_DIR / nom).read_bytes()).hexdigest()[:10]


def statique(nom: str) -> str:
    """L'adresse d'un fichier du site, suivie de l'empreinte de son contenu : après un
    déploiement, le navigateur ne garde pas l'ancienne feuille de style."""
    return f"/site/static/{nom}?v={_empreinte(nom)}"


templates.env.globals["statique"] = statique


def _euros(montant: float) -> str:
    """0.3 → « 0,30 », 1200 → « 1 200 » : comme on l'écrit en français."""
    if float(montant).is_integer():
        return f"{int(montant):,}".replace(",", " ")
    return f"{montant:.2f}".replace(".", ",")


templates.env.filters["euros"] = _euros


def _duree(minutes: float) -> str:
    """3.5 → « 3 min 30 », 4 → « 4 min »."""
    secondes = round(minutes * 60)
    return f"{secondes // 60} min {secondes % 60:02d}" if secondes % 60 else f"{secondes // 60} min"


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def accueil(request: Request):
    premiere = plans.defaut()
    catalogue = plans.catalogue()
    return templates.TemplateResponse(request, "accueil.html", {
        # Les abonnements font les trois cartes ; ce qui se vend sans forfait a son
        # bandeau à part : ce n'est pas « une carte de plus » mais un autre choix.
        "formules": [f for f in catalogue if not f.sans_forfait],
        "sans_forfait": [f for f in catalogue if f.sans_forfait],
        "phare": PHARE,
        "premiere": premiere,
        "en_appels": plans.appels_equivalents,
        "duree_appel": _duree(plans.DUREE_APPEL_MIN),
        "seuil": round(plans.SEUIL_ALERTE * 100),
        "net_exemple": _euros(_CA_EXEMPLE - premiere.prix_mensuel_eur),
    })


# L'éditeur du site (loi pour la confiance dans l'économie numérique, art. 6-III). Rien
# n'est inventé ici : chaque champ vide est à fournir par Helmi, et la page n'affiche que
# ce qui est rempli.
EDITEUR = {
    "nom": "",        # nom et prénom, ou raison sociale
    "statut": "",     # « entrepreneur individuel », « SAS au capital de … € »…
    "siren": "",
    "adresse": "",
    "email": "",      # sert aussi aux demandes sur les données personnelles
    "directeur": "",  # directeur de la publication
}

# Relevé le 03/10/2026 dans les conditions générales de Hostinger (l'entité qui sert la
# France) ; le lieu du serveur est celui du registre de docs/RGPD.md.
HEBERGEUR = {
    "nom": "Hostinger International Limited",
    "adresse": "61 Lordou Vironos Street, 6023 Larnaca, Chypre",
    "site": "hostinger.fr",
    "serveur": "Paris (France)",
}


@router.api_route("/mentions-legales", methods=["GET", "HEAD"], response_class=HTMLResponse)
def mentions_legales(request: Request):
    """Publique et sans base, comme l'accueil. Les durées de conservation sont celles que
    `rgpd.py` applique vraiment : la page ne peut pas en promettre d'autres."""
    return templates.TemplateResponse(request, "mentions.html", {
        "editeur": EDITEUR,
        "hebergeur": HEBERGEUR,
        "jours_rappel": rgpd.jours_rappel(),
        "jours_enregistrement": rgpd.jours_enregistrement(),
        "jours_transcript": rgpd.jours_transcript(),
        "jours_numero": rgpd.jours_numero(),
    })


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /admin\n"


def _meme_origine(request: Request) -> bool:
    """La demande vient-elle de NOTRE page ? Un navigateur dit d'où part une requête
    (`Sec-Fetch-Site`, `Origin`) et une page tierce ne peut pas le falsifier. Sans ces
    en-têtes (un script, un vieux navigateur), on ne refuse pas ici : les plafonds de
    `rappel` s'appliquent à tout le monde."""
    provenance = request.headers.get("sec-fetch-site")
    if provenance and provenance not in ("same-origin", "none"):
        return False
    origine = request.headers.get("origin")
    if origine and urlsplit(origine).netloc != request.headers.get("host", ""):
        return False
    return True


async def _corps_borne(request: Request) -> Optional[bytes]:
    """Le corps de la demande s'il tient dans `_CORPS_MAX`, sinon None — sans le charger.
    `request.body()` lirait tout avant qu'on regarde la taille : sur une route ouverte à
    tout le monde, un envoi de plusieurs centaines de mégaoctets finirait dans la mémoire
    du processus qui porte les appels."""
    annonce = request.headers.get("content-length", "")
    if annonce and (not annonce.isdigit() or int(annonce) > _CORPS_MAX):
        return None
    morceaux, total = [], 0
    async for morceau in request.stream():
        total += len(morceau)
        if total > _CORPS_MAX:
            return None
        morceaux.append(morceau)
    return b"".join(morceaux)


def _reponse(resultat: rappel.Reponse) -> JSONResponse:
    return JSONResponse({"etat": resultat.etat, "message": resultat.message},
                        status_code=resultat.code)


@router.post("/rappel")
async def demander_un_rappel(request: Request):
    """Un visiteur laisse son numéro : Marie l'appelle. Tout ce qui décide si l'appel
    part est dans `rappel.demander`.

    Du JSON seulement : un formulaire posé sur un autre site ne peut pas en envoyer sans
    que le navigateur demande d'abord la permission — que personne ne lui donne."""
    type_envoye = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if type_envoye != "application/json":
        return JSONResponse({"etat": "format", "message": "Demande illisible."}, status_code=415)
    if not _meme_origine(request):
        return JSONResponse({"etat": "origine", "message": "Demande refusée."}, status_code=403)
    try:
        brut = await _corps_borne(request)
        corps = json.loads(brut) if brut is not None else None
    except ValueError:
        corps = None
    if not isinstance(corps, dict):
        return JSONResponse({"etat": "format", "message": "Demande illisible."}, status_code=400)
    if str(corps.get("site") or "").strip():
        # Le champ caché n'est rempli que par un robot : on lui répond comme à tout le
        # monde, et personne n'est appelé.
        return _reponse(rappel.OK)
    return _reponse(await rappel.demander(corps.get("numero"), throttle.adresse(request)))
