"""Le site vitrine : la page d'accueil d'Helmane et « Rappelez-moi » (ASSISTANTE-119).

Public, sans compte : la page ne lit rien en base, et la seule chose qu'un visiteur peut
y déclencher est un rappel — dont tous les refus sont dans `app/rappel.py`.

Les prix affichés viennent de `app/plans.py`. L'ancienne page de vente annonçait la
formule Maison à 249 € pour 1 200 appels, des semaines après que la grille l'eut passée à
349 € pour 750 : une page qui recopie des prix finit par mentir. Le 02/10/2026 la grille
est passée des appels aux minutes, et la page a suivi sans qu'on y recopie un chiffre.
"""
import hashlib
from html import escape
import json
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from .. import contact, plans, rappel, rgpd
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
# Un message de contact : 2000 caractères de texte, que le JSON peut écrire sur six octets
# chacun, plus quatre champs courts.
_CORPS_CONTACT_MAX = 16000

# Les questions de la page. Elles nourrissent aussi les données structurées (FAQPage) :
# Google exige que celles-ci reprennent mot pour mot ce que le visiteur lit (ASSISTANTE-184).
FAQ = (
    ("Est-ce qu'on entend que c'est une machine ?",
     "Elle le dit elle-même dès le décroché. La voix est française et naturelle."),
    ("Je garde mon numéro ?",
     "Oui. Vous le renvoyez vers Marie, tout le temps ou seulement quand personne ne décroche."),
    ("Et si elle ne comprend pas ?",
     "Elle fait répéter. Sinon, elle prend un message avec le numéro où rappeler."),
    ("Les appels sont-ils enregistrés ?",
     "Oui, l'appelant en est prévenu. Vous les réécoutez dans votre espace, pendant une durée limitée."),
)


@lru_cache(maxsize=None)
def _empreinte(nom: str) -> str:
    return hashlib.sha1((STATIC_DIR / nom).read_bytes(), usedforsecurity=False).hexdigest()[:10]  # nosemgrep: insecure-hash-algorithm-sha1 — empreinte de cache, pas de sécurité


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


def base(request: Request) -> str:
    """L'adresse publique du site, sans « / » final : `PUBLIC_URL` (celle que Twilio et
    les e-mails utilisent déjà), sinon celle de la requête. Une adresse canonique qui
    suivrait l'en-tête Host laisserait n'importe quel domaine pointé sur le serveur se
    déclarer la bonne adresse auprès de Google."""
    return rappel._base_publique() or str(request.base_url).rstrip("/")


def donnees_structurees(adresse: str, catalogue: list) -> str:
    """Ce que Google lit de la page (schema.org, JSON-LD) : l'entreprise, le service et
    ses prix — ceux de `plans.py`, jamais recopiés — et les questions de la page.
    Un bloc de données, pas un script : le navigateur ne l'exécute pas, la politique de
    sécurité (script-src 'self') ne le concerne pas. `<` est échappé pour qu'aucune
    valeur ne puisse fermer la balise."""
    offres = []
    for f in catalogue:
        if f.sans_forfait:
            prix = {"@type": "UnitPriceSpecification", "price": f.minute_supp_eur,
                    "priceCurrency": "EUR", "unitText": "minute", "valueAddedTaxIncluded": False}
        else:
            prix = {"@type": "UnitPriceSpecification", "price": f.prix_mensuel_eur,
                    "priceCurrency": "EUR", "unitText": "mois", "valueAddedTaxIncluded": False}
        offres.append({"@type": "Offer", "name": f.label, "description": f.argument,
                       "priceSpecification": prix})
    graphe = [
        {"@type": "Organization", "@id": f"{adresse}/#helmane", "name": "Helmane",
         "url": f"{adresse}/"},
        {"@type": "Service", "name": "Standard téléphonique pour restaurants",
         "serviceType": "Assistante téléphonique de réservation",
         "description": ("Une assistante vocale décroche le téléphone du restaurant 24 h sur "
                         "24 : réservations, modifications, annulations et messages."),
         "provider": {"@id": f"{adresse}/#helmane"},
         "areaServed": {"@type": "Country", "name": "France"},
         "availableLanguage": "fr", "offers": offres},
        {"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": r}} for q, r in FAQ]},
    ]
    texte = json.dumps({"@context": "https://schema.org", "@graph": graphe}, ensure_ascii=False)
    return texte.replace("<", "\\u003c")


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def accueil(request: Request):
    premiere = plans.defaut()
    catalogue = plans.catalogue()
    adresse = base(request)
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
        "mentions_publiees": mentions_publiees(),
        "adresse": adresse,
        "faq": FAQ,
        "donnees_structurees": donnees_structurees(adresse, catalogue),
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


def mentions_publiees() -> bool:
    """La page n'existe pour le public qu'une fois l'éditeur identifié : des mentions
    légales sans éditeur ne sont pas des mentions légales (Helmi, 04/10/2026 : « on va
    laisser pour plus tard »)."""
    return bool(EDITEUR["nom"].strip())


@router.api_route("/mentions-legales", methods=["GET", "HEAD"], response_class=HTMLResponse)
def mentions_legales(request: Request):
    """Publique et sans base, comme l'accueil. Les durées de conservation sont celles que
    `rgpd.py` applique vraiment : la page ne peut pas en promettre d'autres."""
    if not mentions_publiees():
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "mentions.html", {
        "editeur": EDITEUR,
        "hebergeur": HEBERGEUR,
        "jours_rappel": rgpd.jours_rappel(),
        "jours_enregistrement": rgpd.jours_enregistrement(),
        "jours_transcript": rgpd.jours_transcript(),
        "jours_numero": rgpd.jours_numero(),
    })


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request):
    return f"User-agent: *\nDisallow: /admin\n\nSitemap: {base(request)}/sitemap.xml\n"


@router.get("/sitemap.xml")
def plan_du_site(request: Request):
    """Les pages publiques, pour les moteurs de recherche. Les mentions légales n'y sont
    que publiées : une adresse en 404 dans un plan du site est signalée en erreur."""
    adresse = base(request)
    pages = [f"{adresse}/"] + ([f"{adresse}/mentions-legales"] if mentions_publiees() else [])
    lignes = "".join(f"  <url><loc>{escape(p)}</loc></url>\n" for p in pages)
    corps = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
             f"{lignes}</urlset>\n")
    return Response(corps, media_type="application/xml")


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


async def _corps_borne(request: Request, maximum: int = _CORPS_MAX) -> Optional[bytes]:
    """Le corps de la demande s'il tient dans `_CORPS_MAX`, sinon None — sans le charger.
    `request.body()` lirait tout avant qu'on regarde la taille : sur une route ouverte à
    tout le monde, un envoi de plusieurs centaines de mégaoctets finirait dans la mémoire
    du processus qui porte les appels."""
    annonce = request.headers.get("content-length", "")
    if annonce and (not annonce.isdigit() or int(annonce) > maximum):
        return None
    morceaux, total = [], 0
    async for morceau in request.stream():
        total += len(morceau)
        if total > maximum:
            return None
        morceaux.append(morceau)
    return b"".join(morceaux)


def _reponse(resultat: rappel.Reponse | contact.Reponse) -> JSONResponse:
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


@router.post("/contact")
async def ecrire_a_helmane(request: Request):
    """Un visiteur écrit à Helmane. Le message est gardé en base (`contact.demander`) et lu
    dans l'admin. Même chaîne de refus que `/rappel` : du JSON seulement, de notre page
    seulement, un corps borné, un champ piège pour les robots."""
    type_envoye = request.headers.get("content-type", "").split(";")[0].strip().lower()
    en_json = type_envoye == "application/json"
    if not en_json:
        return JSONResponse({"etat": "format", "message": "Demande illisible."}, status_code=415)
    provenance_ok = _meme_origine(request)
    if not provenance_ok:
        return JSONResponse({"etat": "origine", "message": "Demande refusée."}, status_code=403)
    try:
        brut = await _corps_borne(request, _CORPS_CONTACT_MAX)
        corps = json.loads(brut) if brut is not None else None
    except ValueError:
        corps = None
    if not isinstance(corps, dict):
        return JSONResponse({"etat": "format", "message": "Demande illisible."}, status_code=400)
    if str(corps.get("site") or "").strip():
        # Le champ caché n'est rempli que par un robot : réponse d'un message accepté,
        # rien n'est enregistré.
        return _reponse(contact.OK)
    return _reponse(await contact.demander(corps, throttle.adresse(request)))
