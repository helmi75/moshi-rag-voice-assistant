"""Accueil de l'admin : vue du parc (super-admin), salle de contrôle (restaurateur),
santé & coûts (super-admin).

Principe tenu partout ici : **rien n'est affiché qui ne soit mesuré**. Les chiffres
viennent de `calls`/`reservations`, les états viennent du cache de voix et de la
configuration réelle — aucune métrique de latence n'existe, donc aucune n'est montrée.
"""
import os
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request

from .. import calls, db, disponibilite, horloge, quotas, reservations, supervision, tenants
from ..users import User
from ..voice import greeting as greeting_mod
from ..voice import live
from . import charts, deps, presenters

router = APIRouter()

_WINDOW_DAYS = 30
_PARK_CHART_DAYS = 15


def _scope(user: User, tenant_id: Optional[int]) -> Optional[int]:
    """Périmètre des stats : restaurateur = SON tenant, super-admin = tous ou filtré."""
    if not user.is_superadmin:
        return user.tenant_id
    return tenant_id


def _delta(current: float, previous: float, *, unit: str = "%",
           good: str = "up") -> Optional[dict]:
    """Évolution entre la fenêtre courante et la précédente.

    Renvoie None quand la période précédente est vide : afficher « +100 % » face à un
    historique inexistant serait un chiffre décoratif, exactement ce qu'on s'interdit.
    """
    if not previous:
        return None
    if unit == "pts":
        change = round(current - previous)
        label = f"{change:+d} pts"
    elif unit == "$":
        change = current - previous
        label = f"{change:+.2f} $".replace(".", ",")
    else:
        change = round(100 * (current - previous) / previous)
        label = f"{change:+d} %"
    if change == 0:
        return {"label": label, "dir": "flat"}
    rising = change > 0
    if good == "none":
        direction = "flat"
    else:
        direction = "up" if rising == (good == "up") else "down"
    return {"label": label, "dir": direction}


def _fill_days(stats: list[dict], days: int, key: str) -> list[tuple[str, float, str]]:
    """Série jour par jour, trous compris — `stats_daily` ne renvoie que les jours
    actifs, et un graphique à trous mentirait sur le rythme réel. Chaque jour porte
    aussi sa date en toutes lettres, pour la bulle du survol (ASSISTANTE-115)."""
    by_day = {s["day"]: s for s in stats}
    today = horloge.aujourd_hui()
    points = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        iso = day.isoformat()
        points.append((iso[5:], by_day.get(iso, {}).get(key, 0),
                       horloge.en_toutes_lettres(day).capitalize()))
    return points


def _venue_rows(days: int = _WINDOW_DAYS) -> list[dict]:
    """Une ligne par établissement : agrégats réels + état de sa voix, de sa base et de
    son forfait."""
    per_tenant = calls.stats_by_tenant(days=days)
    liste = tenants.list_all()
    # Une seule requête pour tout le parc (cf. quotas.etat_par_tenant).
    conso = quotas.etat_par_tenant(liste)
    rows = []
    for tenant in liste:
        stats = per_tenant.get(tenant.id, {
            "n_calls": 0, "n_with_reservation": 0, "total_cost": 0.0,
            "n_reservations": 0, "capture_rate": 0,
        })
        sections = tenants.parse_knowledge_sections(tenant.knowledge_base)
        rows.append({
            "tenant": tenant,
            "initials": (tenant.name[:2] or "??").upper(),
            "stats": stats,
            "greeting_ready": greeting_mod.cached_greeting_path(tenant) is not None,
            "sections": sections,
            "gaps": [s for s in sections if not s["filled"]],
            "horaires_ok": disponibilite.est_configure(
                disponibilite.charger(tenant.opening_hours)),
            "quota": conso[tenant.id],
        })
    return rows


def _alerts(rows: list[dict]) -> list[dict]:
    """Alertes DÉDUITES de faits vérifiables — jamais de récit inventé."""
    alerts = []
    for row in rows:
        name = row["tenant"].name
        if greeting_mod.pre_rendu_possible(row["tenant"]) and not row["greeting_ready"]:
            alerts.append({
                "level": "warn", "title": f"{name} · voix d'accueil pas encore rendue",
                "detail": "Le WAV d'accueil n'est pas en cache : le décroché ne sera pas "
                          "instantané au prochain appel. Ouvrez « Voix & accueil » pour "
                          "relancer le rendu.",
            })
        if not row["sections"]:
            alerts.append({
                "level": "warn", "title": f"{name} · base de connaissances vide",
                "detail": "L'IA n'a aucun horaire ni tarif à citer : elle improvisera ou "
                          "fera rappeler.",
            })
        elif row["gaps"]:
            titles = ", ".join(s["title"] for s in row["gaps"][:3])
            alerts.append({
                "level": "warn",
                "title": f"{name} · {len(row['gaps'])} fiche(s) sans contenu",
                "detail": f"Sections vides : {titles}.",
            })
        if not row["horaires_ok"]:
            alerts.append({
                "level": "warn", "title": f"{name} · horaires d'ouverture non renseignés",
                "detail": "L'assistante ne refusera aucun créneau fermé : elle peut enregistrer "
                          "une table un jour de fermeture. Ouvrez « Horaires d'ouverture ».",
            })
        if not row["tenant"].numero_secours:
            alerts.append({
                "level": "warn", "title": f"{name} · pas de numéro de secours",
                "detail": "Si l'assistante tombe en panne, l'appel ne peut pas être renvoyé "
                          "vers le restaurant : le client laisse un message. À renseigner "
                          "dans la fiche de l'établissement.",
            })
        if live.moteur(row["tenant"]) == live.GPT_LIVE and not live.cle():
            alerts.append({
                "level": "warn", "title": f"{name} · GPT-Live choisi, clé OpenAI absente",
                "detail": "Tant que la clé manque sur le serveur, ses appels passent par la "
                          "chaîne classique. Poser OPENAI_API_KEY dans le .env, ou changer le "
                          "moteur dans la fiche de l'établissement.",
            })
        if not row["stats"]["n_calls"]:
            alerts.append({
                "level": "info", "title": f"{name} · aucun appel sur 30 jours",
                "detail": f"Vérifiez que le webhook Twilio du {row['tenant'].phone_number} "
                          "pointe bien sur ce serveur.",
            })
    # Forfait : prévenir à 80 %, pas après coup (#31). Placées en TÊTE parce qu'un
    # dépassement engage de l'argent, contrairement à une fiche de connaissance vide.
    return quotas.alertes([r["tenant"] for r in rows]) + alerts


@router.get("/admin/")
async def home(request: Request, tenant_id: Optional[int] = None,
               user: User = Depends(deps.current_user)):
    deps.ensure_csrf(request)
    # Rendu dans un thread : une dizaine de requêtes SQLite par page, et l'admin tourne
    # dans le même processus que les appels en cours (voir db.hors_boucle).
    if user.is_superadmin and tenant_id is None:
        return await db.hors_boucle(_park, request)
    return await db.hors_boucle(_control_room, request, _scope(user, tenant_id))


def _park(request: Request):
    now = calls.totals(None, days=_WINDOW_DAYS)
    before = calls.totals(None, days=_WINDOW_DAYS, offset_days=_WINDOW_DAYS)
    rows = _venue_rows()
    daily = calls.stats_daily(None, days=_PARK_CHART_DAYS)
    chart = charts.bar_chart(
        _fill_days(daily, _PARK_CHART_DAYS, "n_calls"),
        title=f"Appels du parc · {_PARK_CHART_DAYS} derniers jours",
        tone="dark", height=150, unite=("appel", "appels"),
        empty_label="Aucun appel sur la période.",
    )
    return deps.templates.TemplateResponse(
        request, "park.html",
        {
            "totals": now,
            "deltas": {
                "calls": _delta(now["n_calls"], before["n_calls"]),
                "capture": _delta(now["capture_rate"], before["capture_rate"],
                                  unit="pts") if before["n_calls"] else None,
                "reservations": _delta(now["n_reservations"], before["n_reservations"]),
                "cost": _delta(now["total_cost"], before["total_cost"],
                               unit="$", good="none"),
            },
            "venues": sorted(rows, key=lambda r: -r["stats"]["n_calls"]),
            "alerts": _alerts(rows),
            "chart": chart,
            "days": _WINDOW_DAYS,
        },
    )


def _control_room(request: Request, tenant_id: Optional[int]):
    now = calls.totals(tenant_id, days=_WINDOW_DAYS)
    before = calls.totals(tenant_id, days=_WINDOW_DAYS, offset_days=_WINDOW_DAYS)
    today = horloge.aujourd_hui().isoformat()
    recent = [presenters.call_view(c) for c in calls.list_calls(tenant_id, limit=5)]
    upcoming = reservations.list_filtered(
        tenant_id=tenant_id, date_from=today, limit=6, plus_proches_d_abord=True,
    ) if tenant_id else []
    slots = reservations.covers_by_slot(tenant_id, today) if tenant_id else []
    slots_chart = charts.bar_chart(
        [(s["time"], s["covers"], f"Créneau de {s['time']}") for s in slots],
        title="Couverts réservés par créneau, aujourd'hui", series=2, height=160,
        unite=("couvert", "couverts"),
        empty_label="Aucune réservation pour aujourd'hui.",
    )
    # Son forfait, sur le mois EN COURS — pas sur la fenêtre glissante de 30 jours des
    # autres indicateurs : c'est la maille de facturation, et afficher autre chose ferait
    # douter de la facture.
    tenant = tenants.get_by_id(tenant_id) if tenant_id else None
    conso = quotas.etat(tenant) if tenant else None
    return deps.templates.TemplateResponse(
        request, "dashboard.html",
        {
            "totals": now,
            "deltas": {
                "calls": _delta(now["n_calls"], before["n_calls"]),
                "capture": _delta(now["capture_rate"], before["capture_rate"],
                                  unit="pts") if before["n_calls"] else None,
                "covers": _delta(now["n_covers"], before["n_covers"]),
                "cost": _delta(now["total_cost"], before["total_cost"],
                               unit="$", good="none"),
            },
            "recent_calls": recent,
            "upcoming": upcoming,
            "slots_chart": slots_chart,
            "quota": conso,
            "tenant_id": tenant_id,
            "horaires_ok": tenant is None or disponibilite.est_configure(
                disponibilite.charger(tenant.opening_hours)),
            "secours_ok": tenant is None or bool(tenant.numero_secours),
            "days": _WINDOW_DAYS,
        },
    )


@router.get("/admin/health", dependencies=[Depends(deps.require_superadmin)])
async def health(request: Request):
    return await db.hors_boucle(_health, request)


def _health(request: Request):
    deps.ensure_csrf(request)
    now = calls.totals(None, days=_WINDOW_DAYS)
    rows = _venue_rows()
    ready = sum(1 for r in rows if r["greeting_ready"])
    max_cost = max((r["stats"]["total_cost"] for r in rows), default=0) or 1
    # Configuration RÉELLE de la pile (variables d'environnement du conteneur) :
    # une colonne « latence » figurerait ici si elle était mesurée — elle ne l'est pas.
    from ..voice import voices
    from ..voice.voxtral_tts import modele as modele_voxtral

    stack = [
        {"name": "Transcription (STT)", "detail": "Deepgram",
         "metric": os.getenv("DEEPGRAM_MODEL", "nova-3")},
        {"name": "Compréhension (LLM)", "detail": "OpenRouter",
         "metric": os.getenv("LLM_MODEL", "google/gemini-2.5-flash")},
        {"name": "Voix de synthèse (TTS)",
         "detail": (f"Mistral Voxtral · {len(voices.voix_voxtral())} voix au catalogue"
                    if voices.voxtral_disponible()
                    else "Clé Mistral absente : voix de secours Moshi"),
         "metric": modele_voxtral() if voices.voxtral_disponible() else "secours"},
        {"name": "Téléphonie", "detail": "Twilio Media Streams",
         "metric": f"{len(rows)} numéro(s) routé(s)"},
    ]
    # GPT-Live : combien d'établissements l'ont comme moteur, et s'il sert vraiment — clé
    # posée, pas à l'écart après un échec. La mémoire de l'échec est celle du processus.
    sur_gpt_live = sum(1 for r in rows if live.moteur(r["tenant"]) == live.GPT_LIVE)
    echec = live.dernier_echec()
    if not live.cle():
        etat_gpt_live = "clé absente"
    elif echec and echec["a_l_ecart"]:
        etat_gpt_live = "à l'écart"
    else:
        etat_gpt_live = "prêt"
    stack.append({
        "name": "Voix-à-voix (GPT-Live)",
        "detail": (f"OpenAI · moteur de {sur_gpt_live} établissement{'s' if sur_gpt_live > 1 else ''} "
                   f"sur {len(rows)} · voix {live.voix()} · cerveau {live.modele_arriere()}"),
        "metric": etat_gpt_live,
    })
    mesures = calls.par_moteur(None, days=_WINDOW_DAYS)
    moteurs = [{"libelle": libelle, **mesures[cle]}
               for cle, libelle in ((live.GPT_LIVE, "GPT-Live"),
                                    (live.CLASSIQUE, "Chaîne classique"))
               if cle in mesures]
    return deps.templates.TemplateResponse(
        request, "health.html",
        {
            "totals": now,
            "days": _WINDOW_DAYS,
            # Enfin mesurée (migration v4) : le blanc entre la fin de la phrase du
            # client et la reprise de la voix. None tant qu'aucun appel n'a été
            # instrumenté — on n'affiche alors rien plutôt qu'un chiffre inventé.
            "latency": calls.latency_stats(None, days=_WINDOW_DAYS),
            "breakdown": calls.cost_breakdown(None, days=_WINDOW_DAYS),
            # Ce que chaque moteur a coûté, mesuré sur les appels de la fenêtre, et les
            # tarifs avec lesquels un appel GPT-Live est chiffré.
            "moteurs": moteurs,
            "tarifs_gpt_live": calls.tarifs_gpt_live(cerveau_openai=bool(live.modele_openai())),
            "echec_gpt_live": echec,
            "venues": sorted(rows, key=lambda r: -r["stats"]["total_cost"]),
            "max_cost": max_cost,
            "stack": stack,
            # `force=True` : la sonde met son verdict en cache quelques secondes pour
            # rester interrogeable en boucle, mais un super-admin qui recharge la page
            # après avoir corrigé quelque chose doit voir l'état de MAINTENANT.
            # C'est la MÊME fonction que celle servie à `/supervision` : un tableau de
            # bord qui pourrait afficher « tout va bien » pendant que l'alerte crie
            # serait pire que pas de tableau de bord.
            "supervision": supervision.etat(force=True),
            "greeting_ready": ready,
            "greeting_total": len(rows),
            "voice": presenters.voice_label(),
        },
    )


@router.get("/admin/stats/charts")
async def stats_charts(request: Request, tenant_id: Optional[int] = None, days: int = 30,
                       user: User = Depends(deps.current_user)):
    return await db.hors_boucle(_stats_charts, request, _scope(user, tenant_id), days)


def _stats_charts(request: Request, scope: Optional[int], days: int):
    days = min(days, 90)
    stats = calls.stats_daily(scope, days=days)
    calls_svg = charts.bar_chart(
        _fill_days(stats, days, "n_calls"), title="Appels par jour",
        unite=("appel", "appels"),
    )
    resas_svg = charts.bar_chart(
        _fill_days(stats, days, "n_reservations"),
        title="Réservations par jour", series=2,
        unite=("réservation", "réservations"),
    )
    return deps.templates.TemplateResponse(
        request, "_charts.html", {"calls_svg": calls_svg, "resas_svg": resas_svg}
    )
