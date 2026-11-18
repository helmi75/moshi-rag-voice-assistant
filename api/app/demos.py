"""Démonstration personnalisée (ASSISTANTE-139) : un établissement d'essai au nom du restaurant
démarché, préparé avant un rendez-vous commercial.

Helmi donne un nom et une ville (l'adresse du site est facultative). Helmane cherche d'abord la
fiche Google Maps du restaurant (`fiche_maps.py`, préproduction seulement : adresse, téléphone,
horaires, catégorie…), puis LIT le site (celui de la fiche, sinon celui saisi) ; le modèle en
extrait ce que Maps ne dit pas (carte, présentation, paiement). Chaque information porte sa
source, Helmi relit et corrige, puis un établissement de démonstration est créé : l'assistante
répond « Bonjour, <restaurant> ». Décisions du 14/11/2026 : rien d'inventé ; conservation 30
jours puis effacement automatique, effacement manuel possible à tout moment. Décision du
15/11/2026 : la fiche Maps vient de gosom, en préproduction seulement.

Trois protections, qui sont le cœur du module :
  1. `lire_le_site` ne fait jamais lire à Helmane une adresse de son propre réseau : l'adresse
     vient d'un formulaire, et sans ce refus ce formulaire serait un moyen de sonder le serveur
     (métadonnées du fournisseur, base, API interne) — SSRF.
  2. `extraire` ne garde une information que si la phrase du site qui la fonde (sa citation) se
     retrouve MOT POUR MOT dans le texte lu : un modèle qui invente ne passe pas.
  3. Un établissement de démonstration ne prévient personne et n'écrit jamais dans un vrai
     carnet (`tenants.est_demo`, lu par `notifications.destinataires` et `connecteurs`).

Toute la fonction n'existe que si `DEMOS_ACTIVES=1` : elle tourne en préproduction, pas en
production.
"""
import asyncio
import ipaddress
import json
import os
import re
import socket
import threading
import time
import unicodedata
from collections import deque
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from . import db, disponibilite, fiche_maps, horloge, llm, tenants

# --- Lecture du site -----------------------------------------------------------------

DELAI_SECONDES = 10.0
RESOLUTION_SECONDES = 5.0
LECTURE_SECONDES = 30.0   # borne de TOUTE la lecture, redirections comprises
MODELE_SECONDES = 60.0
TAILLE_MAX = 2 * 1024 * 1024
TEXTE_MAX = 30_000
REDIRECTIONS_MAX = 3
PORTS_ADMIS = (80, 443)
_REDIRECTIONS = (301, 302, 303, 307, 308)
_TYPES_ADMIS = ("text/html", "application/xhtml+xml")

# Un transport de remplacement, pour les tests : la suite ne touche jamais le réseau.
_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None


def actives() -> bool:
    """`DEMOS_ACTIVES=1` : lue à chaque appel, pour qu'un test (ou un redémarrage) la change."""
    return os.getenv("DEMOS_ACTIVES", "").strip().lower() in ("1", "true", "oui", "on")


class SiteRefuse(Exception):
    """Le site n'a pas été lu, et la phrase dit pourquoi (affichée telle quelle à Helmi)."""


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _adresse_publique(texte: str) -> bool:
    """Vrai seulement pour une adresse d'Internet : ni privée, ni boucle locale, ni
    lien-local, ni multicast, ni réservée. `is_global` les refuse toutes ; une IPv6 qui
    emballe une IPv4 (::ffff:127.0.0.1) est jugée sur l'IPv4."""
    try:
        adresse = ipaddress.ip_address(texte.split("%", 1)[0])
    except ValueError:
        return False
    emballee = getattr(adresse, "ipv4_mapped", None)
    if emballee is not None:
        adresse = emballee
    # NAT64 : Python tient ces préfixes pour globaux, mais ils emballent une IPv4 (peut-être
    # interne) que la passerelle du fournisseur traduirait.
    if adresse.version == 6 and any(adresse in r for r in _NAT64):
        return False
    return adresse.is_global


async def _verifier(url: str) -> tuple[str, str, int]:
    """(nom d'hôte, adresse vérifiée, port) ou `SiteRefuse`. Chaque adresse que le nom
    résout doit être publique : un seul enregistrement privé suffit à refuser."""
    try:
        morceaux = urlsplit(url)
        port = morceaux.port
    except ValueError:
        raise SiteRefuse("Cette adresse de site est illisible.") from None
    if morceaux.scheme not in ("http", "https"):
        raise SiteRefuse("Seules les adresses http et https sont lues.")
    if not morceaux.hostname:
        raise SiteRefuse("Cette adresse de site est illisible.")
    if morceaux.username is not None or morceaux.password is not None:
        raise SiteRefuse("Une adresse avec identifiant n'est pas lue.")
    port = port or (443 if morceaux.scheme == "https" else 80)
    if port not in PORTS_ADMIS:
        raise SiteRefuse("Seuls les ports 80 et 443 sont lus.")
    hote = morceaux.hostname
    try:
        reponses = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, hote, port, type=socket.SOCK_STREAM),
            RESOLUTION_SECONDES)
    except asyncio.TimeoutError:
        raise SiteRefuse("Le nom de ce site n'a pas été résolu à temps.") from None
    except OSError:
        raise SiteRefuse("Ce nom de site est introuvable.") from None
    adresses = [str(r[4][0]) for r in reponses]
    if not adresses:
        raise SiteRefuse("Ce nom de site est introuvable.")
    if not all(_adresse_publique(a) for a in adresses):
        logger.warning(f"[demos] adresse non publique refusée : {hote}")
        raise SiteRefuse("Ce site pointe vers une adresse qui n'est pas publique : il n'est pas lu.")
    return hote, adresses[0], port


class _Texte(HTMLParser):
    """Le texte visible d'une page : ni script, ni style, ni gabarit, ni dessin (`<head>` n'est pas ignoré : une page sans `</head>` ne rendrait rien)."""
    _IGNORES = {"script", "style", "noscript", "template", "svg"}
    _BLOCS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
              "article", "header", "footer", "ul", "ol", "table", "address", "td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._profondeur_ignoree = 0
        self.morceaux: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORES:
            self._profondeur_ignoree += 1
        elif tag in self._BLOCS:
            self.morceaux.append("\n")

    def handle_endtag(self, tag):
        if tag in self._IGNORES and self._profondeur_ignoree:
            self._profondeur_ignoree -= 1
        elif tag in self._BLOCS:
            self.morceaux.append("\n")

    def handle_data(self, data):
        if not self._profondeur_ignoree:
            self.morceaux.append(data)


def html_en_texte(html: str) -> str:
    parseur = _Texte()
    parseur.feed(html)
    parseur.close()
    lignes = (" ".join(ligne.split()) for ligne in "".join(parseur.morceaux).splitlines())
    return "\n".join(ligne for ligne in lignes if ligne)[:TEXTE_MAX]


def _charset(type_contenu: str) -> str:
    m = re.search(r"charset=([\w-]+)", type_contenu, re.IGNORECASE)
    return m.group(1) if m else "utf-8"


async def lire_le_site(url: str) -> str:
    """Le texte de la page, ou `SiteRefuse` ; toute la lecture tient en `LECTURE_SECONDES`."""
    try:
        return await asyncio.wait_for(_lire(url), LECTURE_SECONDES)
    except asyncio.TimeoutError:
        raise SiteRefuse("Le site a mis trop de temps à répondre : rien n'a été lu.") from None


async def _lire(url: str) -> str:
    """Le texte de la page, ou `SiteRefuse`. Les redirections sont suivies à la main, trois
    au plus, et CHACUNE repasse par la vérification d'adresse : un site public qui renvoie
    vers 169.254.169.254 ne contourne rien. On se connecte à l'adresse vérifiée (pas au nom,
    qu'un second résolveur pourrait faire pointer ailleurs entre-temps), en gardant le nom
    pour l'en-tête Host et le certificat."""
    url = (url or "").strip()
    async with httpx.AsyncClient(timeout=DELAI_SECONDES, follow_redirects=False,
                                 transport=_TRANSPORT, headers={"User-Agent": "Helmane-demo/1.0"}) as client:
        for _ in range(REDIRECTIONS_MAX + 1):
            hote, ip, port = await _verifier(url)
            cible = httpx.URL(url).copy_with(host=ip)
            try:
                async with client.stream("GET", cible, headers={"Host": hote},
                                         extensions={"sni_hostname": hote}) as reponse:
                    if reponse.status_code in _REDIRECTIONS and reponse.headers.get("location"):
                        url = urljoin(url, reponse.headers["location"])
                        continue
                    if reponse.status_code != 200:
                        raise SiteRefuse(f"Le site a répondu {reponse.status_code} : rien n'a été lu.")
                    type_contenu = reponse.headers.get("content-type", "")
                    if type_contenu.split(";", 1)[0].strip().lower() not in _TYPES_ADMIS:
                        raise SiteRefuse("Cette adresse ne rend pas une page web (HTML) : rien n'a été lu.")
                    annonce = reponse.headers.get("content-length", "")
                    if annonce.isascii() and annonce.isdigit() and int(annonce) > TAILLE_MAX:
                        raise SiteRefuse("Cette page est trop volumineuse pour être lue.")
                    recu = bytearray()
                    async for bloc in reponse.aiter_bytes():
                        recu += bloc
                        if len(recu) > TAILLE_MAX:
                            raise SiteRefuse("Cette page est trop volumineuse pour être lue.")
            except httpx.TimeoutException:
                raise SiteRefuse("Le site n'a pas répondu à temps.") from None
            except httpx.HTTPError:
                raise SiteRefuse("Le site n'a pas pu être joint.") from None
            try:
                brut = bytes(recu).decode(_charset(type_contenu), errors="replace")
            except LookupError:   # charset inventé par le site
                brut = bytes(recu).decode("utf-8", errors="replace")
            texte = html_en_texte(brut)
            if not texte.strip():
                raise SiteRefuse("Cette page ne contient aucun texte lisible.")
            return texte
    raise SiteRefuse("Trop de redirections : le site n'a pas été lu.")


# --- Extraction par le modèle, avec sa source -----------------------------------------

CHAMPS = (
    ("adresse", "Adresse"),
    ("telephone", "Téléphone"),
    ("cuisine", "Cuisine"),
    ("horaires_texte", "Horaires (texte)"),
    ("carte", "Carte et prix"),
    ("paiement", "Paiement"),
    ("acces", "Accès"),
    ("presentation", "Présentation"),
)
HORAIRES = "horaires"   # les horaires structurés, au format de disponibilite.py
# Relevé en préproduction (ASSISTANTE-188) : un modèle qui raisonne dépensait les 2000 jetons
# en réflexion et rendait une réponse VIDE (finish_reason « length »), facturée quand même.
# On borne donc le raisonnement (512 jetons, `max_tokens` : OpenRouter le traduit en effort pour
# les modèles qui n'en connaissent qu'un) et on laisse de la place à la réponse JSON.
MAX_JETONS = 3000
RAISONNEMENT_MAX_JETONS = 512
SOURCE_MAPS = "Google Maps"
SOURCE_SITE = "site du restaurant"

CONSIGNE = (
    "Tu lis le texte du site web d'un restaurant et tu en extrais des informations, SANS RIEN "
    "INVENTER. Pour chacune, donne `valeur` (une phrase courte, en français) et `citation` : "
    "la phrase ou le fragment du site, recopié MOT POUR MOT, qui la fonde. Si le site ne dit "
    "rien sur un sujet, mets \"valeur\" et \"citation\" vides : ne devine jamais. "
    "Réponds par un seul objet JSON, sans commentaire, avec les clés : "
    + ", ".join(nom for nom, _ in CHAMPS)
    + " (chacune {\"valeur\": \"…\", \"citation\": \"…\"}), et `horaires` : "
    "{\"valeur\": {\"semaine\": {\"lundi\": [], \"mardi\": [[\"12:00\", \"14:30\"], [\"19:00\", "
    "\"22:30\"]], …}}, \"citation\": \"…\"}, où chaque jour de la semaine (lundi à dimanche) "
    "porte ses plages d'ouverture au format HH:MM (liste vide = fermé). `horaires` reste vide "
    "si le site ne donne pas les horaires clairement."
)


def _normaliser(texte) -> str:
    """Pour comparer une citation au texte lu : casse, espaces, apostrophes et espaces
    insécables ramenés à une forme commune. On ne tolère rien d'autre."""
    t = unicodedata.normalize("NFKC", str(texte or "")).casefold()
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    return " ".join(t.split())


def citation_trouvee(citation, texte_normalise: str) -> bool:
    """Garde anti-invention : la citation, normalisée, est-elle dans le texte du site ?"""
    c = _normaliser(citation)
    return len(c) >= CITATION_MIN and c in texte_normalise


def _nombres(texte) -> set[str]:
    """Les nombres entiers d'un texte, sans zéros de tête (« 04 » = « 4 »)."""
    return {n.lstrip("0") or "0" for n in re.findall(r"\d+", str(texte))}


def _sans_accents(texte) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", _normaliser(texte))
                   if not unicodedata.combining(c))


def _nombres_fondes(valeur: str, citation: str) -> bool:
    """Chaque nombre de la valeur (numéro, prix, heure) est un nombre de la citation — des
    ensembles de nombres entiers, pas des sous-chaînes : « 1 » ne passe pas parce que la
    citation contient « 12 »."""
    return _nombres(valeur) <= _nombres(citation)


def _valeur_fondee(valeur: str, citation: str) -> bool:
    """La valeur dit ce que la citation dit : au moins la moitié de ses mots pleins (quatre
    lettres ou plus) figurent dans la citation. Sans cela, une phrase vraie du site (« Lyon »)
    pouvait cautionner n'importe quelle valeur (« Cuisine japonaise »)."""
    pleins = set(re.findall(r"[^\W\d_]{4,}", _sans_accents(valeur)))
    if not pleins:
        return True   # que des nombres : _nombres_fondes les contrôle
    c = _sans_accents(citation)
    return sum(1 for m in pleins if m in c) * 2 >= len(pleins)


CITATION_MIN = 10


def _vide(trouve: bool = False) -> dict:
    return {"valeur": "", "citation": "", "trouve": trouve}


def _garder(brut, texte_normalise: str) -> dict:
    """Un champ extrait → {valeur, citation, trouve}. Non trouvé : valeur vide."""
    if not isinstance(brut, dict):
        return _vide()
    valeur = " ".join(str(brut.get("valeur") or "").split())
    citation = " ".join(str(brut.get("citation") or "").split())
    if not valeur or not citation_trouvee(citation, texte_normalise) \
            or not _nombres_fondes(valeur, citation) or not _valeur_fondee(valeur, citation):
        return _vide()
    return {"valeur": valeur, "citation": citation, "trouve": True}


_SEPARATEUR_JOURS = r"\s*(?:au|à|a|-|–|—|jusqu'au|jusqu'à|jusqu'a)\s*"


def _jours_cites(citation: str) -> set[str]:
    """Les jours que la citation nomme, plus ceux qu'une plage couvre (« du mardi au samedi »,
    « mardi-samedi », « mardi à samedi », en tournant sur la semaine)."""
    c = _normaliser(citation)
    jours = horloge.JOURS
    cites = {j for j in jours if j in c}
    motif = "(" + "|".join(jours) + ")" + _SEPARATEUR_JOURS + "(" + "|".join(jours) + ")"
    for debut, fin in re.findall(motif, c):
        i = jours.index(debut)
        while True:
            cites.add(jours[i])
            if jours[i] == fin:
                break
            i = (i + 1) % 7
    return cites


def _horaires_fondes(charges: dict, citation: str) -> bool:
    """Chaque jour ouvert est nommé (ou couvert par une plage de jours) dans la citation, et
    chaque heure et minute non nulle des plages est un nombre de la citation : la structure ne
    dit rien que la phrase du site ne dise."""
    nombres = _nombres(citation)
    cites = _jours_cites(citation)
    for jour, plages in charges["semaine"].items():
        if plages and jour not in cites:
            return False
        for debut, fin in plages:
            for h in (debut, fin):
                for valeur in (h.hour, h.minute):
                    if valeur and str(valeur) not in nombres:
                        return False
    return True


def _garder_horaires(brut, texte_normalise: str) -> dict:
    """Les horaires structurés : acceptés par `disponibilite.charger`, au moins un jour
    ouvert, ET citation retrouvée dans le texte. Sinon vides."""
    if not isinstance(brut, dict) or not isinstance(brut.get("valeur"), dict):
        return _vide()
    if not citation_trouvee(brut.get("citation"), texte_normalise):
        return _vide()
    donnees = {"semaine": brut["valeur"].get("semaine") or {}}
    texte_json = json.dumps(donnees, ensure_ascii=False)
    charges = disponibilite.charger(texte_json)
    if charges is None or disponibilite.toujours_ferme(charges):
        return _vide()
    if not _horaires_fondes(charges, str(brut["citation"])):
        return _vide()
    return {"valeur": texte_json, "citation": " ".join(str(brut["citation"]).split()),
            "trouve": True}


def _json_de(texte: str) -> dict:
    debut, fin = texte.find("{"), texte.rfind("}")
    if debut < 0 or fin <= debut:
        raise ValueError("pas d'objet JSON dans la réponse")
    objet = json.loads(texte[debut:fin + 1])
    if not isinstance(objet, dict):
        raise ValueError("la réponse n'est pas un objet")
    return objet


def _cout_de(usage) -> Optional[float]:
    """Le coût que le fournisseur annonce, s'il en annonce un. Jamais estimé."""
    if usage is None:
        return None
    brut = getattr(usage, "cost", None)
    if brut is None:
        brut = (getattr(usage, "model_extra", None) or {}).get("cost")
    return float(brut) if isinstance(brut, (int, float)) else None


class ExtractionImpossible(Exception):
    """Le modèle n'a rien rendu d'exploitable. La phrase dit pourquoi ; les jetons et le coût
    sont ceux que le fournisseur a quand même facturés (ASSISTANTE-188)."""

    def __init__(self, message: str, jetons: Optional[int] = None, cout_usd: Optional[float] = None):
        super().__init__(message)
        self.jetons, self.cout_usd = jetons, cout_usd


def _jetons_de_raisonnement(usage) -> Optional[int]:
    brut = getattr(usage, "reasoning_tokens", None)
    if brut is None:
        brut = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    return brut if isinstance(brut, int) else None


async def extraire(texte: str) -> dict:
    """{"champs": {nom: {valeur, citation, trouve}}, "jetons": int|None, "cout_usd": float|None}.
    Lève `ExtractionImpossible` (cause, jetons et coût) si le modèle ne rend pas de JSON, une
    autre exception s'il est injoignable : `preparer` en fait un message."""
    from . import resume

    reponse = await llm.get_client().with_options(
        timeout=MODELE_SECONDES, max_retries=0).chat.completions.create(
        model=resume.MODELE,
        max_tokens=MAX_JETONS,
        temperature=0,
        messages=[{"role": "system", "content": CONSIGNE},
                  {"role": "user", "content": texte}],
        extra_body={"usage": {"include": True},
                    "reasoning": {"max_tokens": RAISONNEMENT_MAX_JETONS}},
    )
    usage = getattr(reponse, "usage", None)
    jetons = getattr(usage, "total_tokens", None)
    jetons = jetons if isinstance(jetons, int) else None
    cout = _cout_de(usage)
    choix = reponse.choices[0]
    contenu = choix.message.content or ""
    try:
        brut = _json_de(contenu)
    except ValueError as exc:
        if getattr(choix, "finish_reason", None) == "length":
            raisonne = _jetons_de_raisonnement(usage)
            detail = f" ({raisonne} jetons de raisonnement)" if raisonne else ""
            cause = f"Réponse du modèle coupée par la limite de jetons{detail}"
        elif not contenu.strip():
            cause = "Le modèle n'a rien répondu"
        elif isinstance(exc, json.JSONDecodeError):
            cause = "Réponse du modèle illisible (JSON invalide)"
        else:
            cause = "Réponse du modèle sans JSON"
        raise ExtractionImpossible(cause, jetons, cout) from None
    normalise = _normaliser(texte)
    champs = {nom: _garder(brut.get(nom), normalise) for nom, _ in CHAMPS}
    champs[HORAIRES] = _garder_horaires(brut.get(HORAIRES), normalise)
    return {"champs": champs, "jetons": jetons, "cout_usd": cout}


# --- La démonstration ------------------------------------------------------------------

ETATS = ("a_relire", "prete", "effacee")
# Une lecture « en cours » depuis plus longtemps que ça a été interrompue (redémarrage).
LECTURE_PERIMEE_SECONDES = 300


class DemoIntrouvable(Exception):
    pass


class PasRelue(Exception):
    """La validation est refusée tant que la relecture n'a pas été enregistrée une fois."""


class Refus(Exception):
    """Une saisie refusée, avec la phrase française à afficher."""


def _champs_vides() -> dict:
    return {nom: _vide() for nom, _ in CHAMPS} | {HORAIRES: _vide()}


def _vers_dict(row) -> dict:
    try:
        contenu = json.loads(row["champs"] or "{}")
    except ValueError:
        contenu = {}
    # `choix` : les fiches Google Maps proposées à Helmi (lecture « choix »), puis la fiche
    # retenue pendant la lecture du site ; `debut` : l'instant où la lecture en cours a commencé.
    base = {"champs": _champs_vides(), "lecture": "terminee", "message": "", "relu": False,
            "choix": [], "debut": ""}
    base.update({k: v for k, v in contenu.items() if k in base})
    base["champs"] = _champs_vides() | (base["champs"] or {})
    return {"id": row["id"], "created_at": row["created_at"], "nom": row["nom"],
            "ville": row["ville"], "site_url": row["site_url"], "etat": row["etat"],
            "tenant_id": row["tenant_id"], "validee_at": row["validee_at"],
            "cout_usd": row["cout_usd"], "jetons": row["jetons"],
            "champs_brut": row["champs"], **base}


def _ecrire(demo_id: int, contenu: dict, **colonnes) -> None:
    sets = ", ".join(["champs = ?"] + [f"{k} = ?" for k in colonnes])
    with db.get_conn() as conn:
        conn.execute(f"UPDATE demonstrations SET {sets} WHERE id = ?",  # noqa: S608 — noms du code, valeurs liées
                     (json.dumps(contenu, ensure_ascii=False), *colonnes.values(), demo_id))


def _contenu(demo: dict) -> dict:
    return {k: demo[k] for k in ("champs", "lecture", "message", "relu", "choix", "debut")}


def obtenir(demo_id: int) -> Optional[dict]:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM demonstrations WHERE id = ?", (demo_id,)).fetchone()
    return _vers_dict(row) if row else None


def lister() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM demonstrations ORDER BY id DESC").fetchall()
    return [_vers_dict(r) for r in rows]


def _nettoyer(valeur, nom_champ: str, maxi: int) -> str:
    texte = " ".join(str(valeur or "").split())
    if not 1 <= len(texte) <= maxi:
        raise Refus(f"{nom_champ} : entre 1 et {maxi} caractères.")
    return texte


def adresse_du_site(brute: str) -> str:
    """« exemple.fr » devient « https://exemple.fr » ; la forme est vérifiée ici, l'adresse
    elle-même (réseau) seulement à la lecture."""
    texte = (brute or "").strip()
    if "://" not in texte:
        texte = "https://" + texte
    if len(texte) > 500 or any(c.isspace() for c in texte):
        raise Refus("Adresse du site : illisible (500 caractères au plus, sans espace).")
    if urlsplit(texte).scheme not in ("http", "https"):
        raise Refus("Adresse du site : seules les adresses http et https sont admises.")
    return texte


def creer(nom: str, ville: str, site_url: str = "") -> int:
    """Le site est facultatif : vide, Helmane s'en remet à la fiche Google Maps (colonne vide)."""
    nom = _nettoyer(nom, "Nom du restaurant", 100)
    ville = _nettoyer(ville, "Ville", 100)
    url = adresse_du_site(site_url) if (site_url or "").strip() else ""
    contenu = {"champs": _champs_vides(), "lecture": "en_cours", "message": "", "relu": False}
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO demonstrations (nom, ville, site_url, champs) VALUES (?, ?, ?, ?)",
            (nom, ville, url, json.dumps(contenu, ensure_ascii=False)))
        return cur.lastrowid


def _champs_de_maps(fiche: dict) -> dict:
    """Les champs que la fiche Google Maps renseigne, source « Google Maps » : la valeur brute
    de la fiche est sa propre citation (aucune garde de citation, il n'y a pas de texte lu)."""
    champs = _champs_vides()

    def poser(nom: str, valeur, citation=None) -> None:
        valeur = " ".join(str(valeur or "").split())
        if valeur:
            champs[nom] = {"valeur": valeur, "citation": " ".join(str(citation or valeur).split()),
                           "trouve": True, "source": SOURCE_MAPS}
    poser("adresse", fiche.get("adresse"))
    poser("telephone", fiche.get("telephone"))
    poser("cuisine", fiche.get("categorie"))
    poser("presentation", fiche.get("description"))
    poser("acces", fiche.get("acces"))
    horaires = fiche.get("horaires") or ""
    charges = disponibilite.charger(horaires) if horaires else None
    if charges is not None:
        poser(HORAIRES, horaires, fiche.get("horaires_brut"))
        poser("horaires_texte", disponibilite.en_toutes_lettres(charges), fiche.get("horaires_brut"))
    return champs


def _completer(maps: dict, site: dict) -> dict:
    """Un champ que Maps a trouvé n'est jamais écrasé par le site ; le site comble le reste."""
    fusion = {}
    for nom, champ in maps.items():
        if champ["trouve"]:
            fusion[nom] = champ
        elif site[nom]["trouve"]:
            fusion[nom] = site[nom] | {"source": SOURCE_SITE}
        else:
            fusion[nom] = champ
    return fusion


def _ajouter(message: str, phrase: str) -> str:
    return f"{message} {phrase}" if message else phrase


async def preparer(demo_id: int) -> None:
    """Cherche la fiche Google Maps, puis lit le site et en extrait le reste. Ne lève jamais :
    un échec laisse les champs vides et un message, la démonstration reste à relire (Helmi peut
    tout saisir à la main). Plusieurs fiches possibles : la démonstration attend le choix de
    Helmi (`choisir`)."""
    demo = await db.hors_boucle(obtenir, demo_id)
    if demo is None:
        return
    fiches, message = [], ""
    if fiche_maps.actif():
        try:
            fiches = await fiche_maps.chercher(demo["nom"], demo["ville"])
            if not fiches:
                message = "Aucune fiche Google Maps trouvée pour ce nom et cette ville."
        except fiche_maps.MapsIndisponible as exc:
            message = str(exc)
        except Exception as exc:
            logger.warning(f"[demos] recherche Maps impossible pour la démonstration {demo_id} "
                           f"({type(exc).__name__})")
            message = "La recherche Google Maps a échoué."
    if len(fiches) > 1:
        contenu = {"champs": _champs_vides(), "lecture": "choix", "relu": False, "debut": "",
                   "message": "Plusieurs fiches Google Maps correspondent : choisissez la bonne.",
                   "choix": fiches}
        await db.hors_boucle(_ecrire_lecture, demo_id, contenu, {})
        return
    await _lire_et_ranger(demo_id, demo, fiches[0] if fiches else None, message)


async def _lire_et_ranger(demo_id: int, demo: dict, fiche: Optional[dict], message: str) -> None:
    """Les champs de la fiche (s'il y en a une), complétés par ce que le site dit : celui de la
    fiche, sinon celui que Helmi a saisi."""
    maps = _champs_de_maps(fiche) if fiche else _champs_vides()
    champs, colonnes = maps, {}
    site_url = (fiche or {}).get("site") or demo["site_url"]
    if not site_url:
        message = _ajouter(message, (
            "La fiche Google Maps n'indique pas de site : carte et paiement sont à saisir à la main."
            if fiche else
            "Aucun site n'est connu : indiquez l'adresse du site du restaurant (nouvelle "
            "démonstration) ou saisissez les champs à la main."))
    else:
        try:
            texte = await lire_le_site(adresse_du_site(site_url))
            resultat = await extraire(texte)
            colonnes = {"jetons": resultat["jetons"], "cout_usd": resultat["cout_usd"]}
            champs = _completer(maps, resultat["champs"])
            if not any(c["trouve"] for c in champs.values()):
                message = _ajouter(message, "Le site a été lu, mais rien n'a pu en être extrait avec certitude.")
        except (SiteRefuse, Refus) as exc:
            message = _ajouter(message, str(exc))
        except ExtractionImpossible as exc:
            colonnes = {"jetons": exc.jetons, "cout_usd": exc.cout_usd}
            logger.warning(f"[demos] extraction sans résultat pour la démonstration {demo_id} : {exc}")
            message = _ajouter(message, f"{exc}. Les champs du site sont à saisir à la main.")
        except Exception as exc:
            logger.warning(f"[demos] extraction impossible pour la démonstration {demo_id} "
                           f"({type(exc).__name__}: {exc})")
            message = _ajouter(message, "Le modèle n'a pas pu lire ce site. Les champs sont à saisir à la main.")
    contenu = {"champs": champs, "lecture": "terminee", "message": message, "relu": False,
               "choix": [], "debut": ""}
    await db.hors_boucle(_ecrire_lecture, demo_id, contenu, colonnes)


def choisir(demo_id: int, indice: int) -> None:
    """Helmi a choisi une des fiches Google Maps proposées : la démonstration repasse en lecture
    (le site de cette fiche est lu en tâche de fond par `preparer_apres_choix`). Écriture
    conditionnelle : un double clic ne lance pas deux lectures."""
    demo = obtenir(demo_id)
    if demo is None:
        raise DemoIntrouvable(demo_id)
    if demo["etat"] != "a_relire" or demo["lecture"] != "choix" or demo["relu"]:
        raise Refus("Cette démonstration n'attend pas de choix de fiche.")
    if not 0 <= indice < len(demo["choix"]):
        raise Refus("Cette fiche ne fait pas partie de celles proposées.")
    with db.get_conn() as conn:
        # Comparé à l'état LU plus haut (celui sur lequel les contrôles ont porté), pas à une
        # seconde lecture : deux POST quasi simultanés, un seul passe.
        avant = demo["champs_brut"]
        demo.update({"lecture": "en_cours", "choix": [demo["choix"][indice]], "message": "",
                     "debut": horloge.utc_iso()})
        pris = conn.execute(
            "UPDATE demonstrations SET champs = ? WHERE id = ? AND etat = 'a_relire' AND champs IS ?",
            (json.dumps(_contenu(demo), ensure_ascii=False), demo_id, avant)).rowcount
    if not pris:
        raise Refus("Le choix a déjà été pris.")


async def preparer_apres_choix(demo_id: int) -> None:
    demo = await db.hors_boucle(obtenir, demo_id)
    if demo is None or demo["lecture"] != "en_cours" or not demo["choix"]:
        return
    await _lire_et_ranger(demo_id, demo, demo["choix"][0], "")


def _ecrire_lecture(demo_id: int, contenu: dict, colonnes: dict) -> None:
    """N'écrit le résultat que si la démonstration attend encore sa lecture : une démonstration
    effacée, ou déjà relue par Helmi, n'est pas écrasée par une lecture qui finit tard."""
    conn = db.get_conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM demonstrations WHERE id = ?", (demo_id,)).fetchone()
        courant = _vers_dict(row) if row else None
        if courant and courant["etat"] == "a_relire" and not courant["relu"] \
                and courant["lecture"] == "en_cours":
            sets = ", ".join(["champs = ?"] + [f"{k} = ?" for k in colonnes])
            conn.execute(f"UPDATE demonstrations SET {sets} WHERE id = ?",  # noqa: S608 — noms du code, valeurs liées
                         (json.dumps(contenu, ensure_ascii=False), *colonnes.values(), demo_id))
        conn.execute("COMMIT")
    finally:
        conn.close()


def lecture_en_cours(demo: dict) -> bool:
    """Vrai tant que la lecture tourne (et n'est pas périmée : un redémarrage la tue)."""
    if demo["lecture"] != "en_cours" or demo["etat"] != "a_relire":
        return False
    # Après un choix de fiche, la lecture recommence : on compte depuis ce moment-là.
    return (demo["debut"] or demo["created_at"]) > horloge.il_y_a(LECTURE_PERIMEE_SECONDES / 86400)


def enregistrer_relecture(demo_id: int, valeurs: dict) -> dict:
    """Enregistre ce que Helmi a relu (valeur de chaque champ ; la citation-source est
    gardée telle quelle). Marque la relecture faite : c'est elle que `valider` exige."""
    demo = obtenir(demo_id)
    if demo is None:
        raise DemoIntrouvable(demo_id)
    if demo["etat"] != "a_relire":
        raise Refus("Cette démonstration n'est plus à relire.")
    if demo["lecture"] == "choix":
        raise Refus("Choisissez d'abord la fiche Google Maps : il n'y a encore rien à relire.")
    for nom in [n for n, _ in CHAMPS]:
        if nom in valeurs:
            valeur = " ".join(str(valeurs[nom] or "").split())[:1000]
            demo["champs"][nom] = demo["champs"][nom] | {"valeur": valeur}
    if HORAIRES in valeurs:
        # Les horaires structurés ne se retapent pas : on garde ou on retire, jamais d'édition
        # libre d'un JSON ; vider la case les retire.
        if not str(valeurs[HORAIRES] or "").strip():
            demo["champs"][HORAIRES] = _vide()
    demo["relu"] = True
    demo["lecture"] = "terminee"
    _ecrire(demo_id, _contenu(demo))
    return demo


def horaires_lisibles(demo: dict) -> str:
    """Les horaires structurés comme l'assistante les dira (« Du lundi au vendredi : … »).
    La page de relecture montrait le JSON brut, illisible sur téléphone (ASSISTANTE-190)."""
    h = demo["champs"][HORAIRES]
    if not (h["trouve"] and h["valeur"]):
        return ""
    return disponibilite.en_toutes_lettres(disponibilite.charger(h["valeur"]))


def citation_horaires(demo: dict) -> str:
    """La source des horaires : la phrase du site telle quelle ; la fiche Google Maps,
    elle, les rend en JSON ({"lundi": ["07:00–02:00"]}), écrit ici jour par jour."""
    citation = str(demo["champs"][HORAIRES]["citation"] or "")
    try:
        brut = json.loads(citation)
    except (ValueError, RecursionError):   # mille crochets imbriqués : RecursionError, pas un 500
        return citation
    if not isinstance(brut, dict):
        return citation
    return " ; ".join(f"{jour} : {', '.join(map(str, plages)) if isinstance(plages, list) else plages}"
                      for jour, plages in brut.items())


def cout_lisible(cout_usd: Optional[float]) -> str:
    """Le coût annoncé par le fournisseur, au dix-millième : une lecture coûte quelques
    dixièmes de centime, que deux décimales affichaient « 0,00 $ » (ASSISTANTE-190)."""
    if cout_usd is None:
        return ""
    if 0 < cout_usd < 0.0001:
        return "moins de 0,0001 $"
    return f"{cout_usd:.4f} $".replace(".", ",")


def _fiches(demo: dict) -> list[dict]:
    c = {nom: demo["champs"][nom]["valeur"] for nom, _ in CHAMPS}
    resto = [f"{demo['nom']}, {demo['ville']}."]
    resto += [c[n] for n in ("presentation", "cuisine") if c[n]]
    if c["adresse"]:
        resto.append(f"Adresse : {c['adresse']}")
    if c["telephone"]:
        resto.append(f"Téléphone : {c['telephone']}")
    fiches = [{"title": "Restaurant", "body": "\n".join(resto)}]
    for titre, cle in (("Horaires", "horaires_texte"), ("Menus", "carte"),
                       ("Paiement", "paiement"), ("Accès", "acces")):
        if c[cle]:
            fiches.append({"title": titre, "body": c[cle]})
    return fiches


def valider(demo_id: int) -> tenants.Tenant:
    """Crée l'établissement de démonstration. Refusée (`PasRelue`) tant que la relecture n'a
    pas été enregistrée au moins une fois : la décision est ici, côté serveur, pas dans un
    bouton grisé. Aucun numéro réel, aucun compte restaurateur : rien n'est créé qui puisse
    sonner ou écrire chez quelqu'un."""
    demo = obtenir(demo_id)
    if demo is None:
        raise DemoIntrouvable(demo_id)
    if demo["etat"] != "a_relire":
        raise Refus("Cette démonstration n'est plus à valider.")
    if not demo["relu"]:
        raise PasRelue(demo_id)
    # Prise de la démonstration par une écriture conditionnelle : un double clic (ou deux
    # onglets) ne crée pas deux établissements, le second reçoit un refus.
    with db.get_conn() as conn:
        pris = conn.execute(
            "UPDATE demonstrations SET validee_at = ? "
            "WHERE id = ? AND etat = 'a_relire' AND validee_at IS NULL",
            (horloge.utc_iso(), demo_id)).rowcount
    if not pris:
        raise Refus("Cette démonstration est déjà en cours de validation ou validée.")
    try:
        tenant = tenants.create_tenant(
            demo["nom"], f"{tenants.PREFIXE_DEMO}{demo_id}",
            greeting=f"Bonjour, {demo['nom']}.",
            knowledge_base=tenants.assembler_fiches(_fiches(demo)))
        try:
            horaires = demo["champs"][HORAIRES]
            if horaires["trouve"] and horaires["valeur"]:
                tenants.update_tenant(tenant.id, opening_hours=horaires["valeur"])
            with db.get_conn() as conn:
                conn.execute("UPDATE demonstrations SET etat = 'prete', tenant_id = ? WHERE id = ?",
                             (tenant.id, demo_id))
        except Exception:
            tenants.delete_tenant(tenant.id)   # pas d'établissement orphelin
            raise
    except Exception:
        with db.get_conn() as conn:
            conn.execute("UPDATE demonstrations SET validee_at = NULL "
                         "WHERE id = ? AND etat = 'a_relire'", (demo_id,))
        raise
    return tenant


def effacer(demo_id: int) -> bool:
    """Efface l'établissement (et ses réservations d'essai), vide les champs. La ligne reste,
    marquée « effacée », jusqu'à la purge. Faux si la démonstration n'existe pas."""
    demo = obtenir(demo_id)
    if demo is None:
        return False
    if demo["tenant_id"]:
        tenants.delete_tenant(demo["tenant_id"])
    with db.get_conn() as conn:
        conn.execute("UPDATE demonstrations SET etat = 'effacee', champs = '{}', tenant_id = NULL "
                     "WHERE id = ?", (demo_id,))
    return True


def purger(jours: int) -> int:
    """Efface les démonstrations plus vieilles que la durée de conservation (app/rgpd.py) :
    l'établissement d'essai, puis la ligne elle-même, avec celles déjà « effacées » à la main
    depuis aussi longtemps. Rend le nombre de démonstrations vivantes effacées."""
    limite = horloge.il_y_a(jours)
    with db.get_conn() as conn:
        anciennes = [r["id"] for r in conn.execute(
            "SELECT id FROM demonstrations WHERE created_at < ? AND etat != 'effacee'", (limite,))]
    for identifiant in anciennes:
        effacer(identifiant)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM demonstrations WHERE etat = 'effacee' AND created_at < ?", (limite,))
    return len(anciennes)


# --- Démo 5 : faire rappeler le gérant (ASSISTANTE-145) -----------------------------------
#
# En rendez-vous, Helmi saisit le portable du gérant et clique : Twilio compose le numéro, et
# quand le gérant décroche il entend « Bonjour, <son restaurant> », puis la mention que l'appel
# est enregistré (décision du 18/11/2026 : enregistré et annoncé, par `ENREGISTREMENT_APPELS`
# comme tout appel). Il joue un client et réserve ; la réservation d'essai reste dans
# l'établissement de démonstration, qui ne prévient personne (protection 3 ci-dessus).
#
# Ce que l'appel ne doit JAMAIS faire : servir un vrai établissement. `/twilio/demo` reçoit
# l'identifiant d'une démonstration dans son adresse (signée par Twilio), et
# `etablissement_a_rappeler` ne rend qu'un établissement d'essai d'une démonstration prête.

_verrou_rappels = threading.Lock()
_rappels_lances: deque = deque()


def rappels_par_jour() -> int:
    """Le plafond des appels de démonstration sur 24 h glissantes : c'est lui qui borne la
    facture si un bouton est cliqué en boucle. En mémoire du processus (un seul worker) :
    seul le super-admin y a accès, un redémarrage qui le remet à zéro ne coûte qu'un
    plafond de plus."""
    try:
        return max(0, int(os.getenv("DEMO_RAPPELS_PAR_JOUR", "").strip() or 20))
    except ValueError:
        return 20


def ligne_presentee() -> Optional[str]:
    """Le numéro Twilio qui s'affiche chez le gérant (`DEMO_LIGNE`), au format international,
    ou None. Il doit appartenir au compte Twilio : c'est lui qui appelle."""
    brut = "".join(os.getenv("DEMO_LIGNE", "").split())
    return brut if re.fullmatch(r"\+\d{8,15}", brut) else None


def rappel_indisponible() -> str:
    """Pourquoi le bouton ne peut pas appeler (texte affiché sur la page), ou "" s'il peut."""
    from . import rappel, twilio_region

    manque = []
    if not twilio_region.identifiants():
        manque.append("les identifiants Twilio")
    if not rappel._base_publique():
        manque.append("PUBLIC_URL")
    if ligne_presentee() is None:
        manque.append("DEMO_LIGNE (le numéro Twilio présenté)")
    return ("Rappel impossible : il manque " + ", ".join(manque) + " dans le .env.") if manque else ""


def etablissement_a_rappeler(demo_id: int) -> Optional[tenants.Tenant]:
    """L'établissement d'essai d'une démonstration PRÊTE, ou None. Seul chemin par lequel un
    appel de démonstration trouve son établissement : jamais un vrai restaurant, même si
    l'identifiant d'une démonstration désignait un jour un établissement réel."""
    if not actives():
        return None
    demo = obtenir(demo_id)
    if demo is None or demo["etat"] != "prete" or not demo["tenant_id"]:
        return None
    tenant = tenants.get_by_id(demo["tenant_id"])
    if tenant is None or not tenants.est_demo(tenant):
        return None
    return tenant


def _compter_un_rappel() -> bool:
    """Inscrit un appel s'il reste de la place sous le plafond du jour ; faux sinon."""
    maintenant = time.monotonic()
    with _verrou_rappels:
        while _rappels_lances and maintenant - _rappels_lances[0] > 86400:
            _rappels_lances.popleft()
        if len(_rappels_lances) >= rappels_par_jour():
            return False
        _rappels_lances.append(maintenant)
        return True


def oublier_les_rappels() -> None:
    """Remet le plafond à zéro — réservé aux tests."""
    with _verrou_rappels:
        _rappels_lances.clear()


async def faire_rappeler(demo_id: int, brut) -> tuple[bool, str]:
    """Fait sonner le portable du gérant sur l'établissement d'essai de la démonstration.
    Rend (parti, phrase à afficher). Ne lève jamais : la page est montrée en rendez-vous."""
    from . import rappel

    if not actives():
        return False, "Les démonstrations ne sont pas actives ici."
    indisponible = rappel_indisponible()
    if indisponible:
        return False, indisponible
    numero = rappel.normaliser(brut)
    if numero is None:
        return False, "Ce numéro n'est pas un numéro français de métropole. Exemple : 06 12 34 56 78."
    tenant = await db.hors_boucle(etablissement_a_rappeler, demo_id)
    if tenant is None:
        return False, "Créez d'abord l'établissement de démonstration."
    if not _compter_un_rappel():
        return False, (f"Plafond atteint : {rappels_par_jour()} appels de démonstration en 24 heures "
                       "(DEMO_RAPPELS_PAR_JOUR).")
    try:
        # L'accueil pré-rendu : sans lui, le premier appel de la démonstration commence par
        # un blanc, le temps de synthétiser « Bonjour, <restaurant> ».
        from .voice import greeting as greeting_mod
        await asyncio.wait_for(greeting_mod.ensure_greeting_wav(tenant), timeout=20)
    except Exception as exc:
        logger.warning(f"[demo] accueil non pré-rendu, il sera dit en direct ({type(exc).__name__})")
    try:
        call_sid = await rappel._composer(numero, ligne_presentee(), f"/twilio/demo?d={int(demo_id)}")
    except Exception as exc:
        # Le type seulement : le message d'une erreur HTTP porte le numéro composé.
        logger.warning(f"[demo] Twilio n'a pas composé le numéro ({type(exc).__name__})")
        return False, "L'appel n'a pas pu partir. Réessayez dans un instant."
    logger.info(f"[demo] démonstration {demo_id} : appel d'un numéro en …{numero[-2:]} ({call_sid})")
    return True, f"Le téléphone du {rappel.lisible(numero)} va sonner dans quelques secondes."
