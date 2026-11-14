"""Démonstration personnalisée (ASSISTANTE-139) : un établissement d'essai au nom du restaurant
démarché, préparé avant un rendez-vous commercial.

Helmi donne un nom, une ville et l'adresse du site du restaurant. Helmane LIT ce site (et lui
seul : pas Google), le modèle en extrait les informations, Helmi les relit et les corrige, puis
un établissement de démonstration est créé : l'assistante répond « Bonjour, <restaurant> ».
Décisions du 14/11/2026 : source = le site du restaurant, rien d'inventé ; conservation 30 jours
puis effacement automatique, effacement manuel possible à tout moment.

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
import unicodedata
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

from . import db, disponibilite, horloge, llm, tenants

# --- Lecture du site -----------------------------------------------------------------

DELAI_SECONDES = 10.0
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
        reponses = await asyncio.to_thread(socket.getaddrinfo, hote, port, type=socket.SOCK_STREAM)
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
    """Le texte visible d'une page : ni script, ni style, ni gabarit, ni dessin."""
    _IGNORES = {"script", "style", "noscript", "template", "svg", "head"}
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
            texte = html_en_texte(bytes(recu).decode(_charset(type_contenu), errors="replace"))
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
MAX_JETONS = 2000

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
    return len(c) >= 3 and c in texte_normalise


def _nombres_fondes(valeur: str, citation: str) -> bool:
    """Chaque nombre de la valeur (numéro, prix, heure) figure dans la citation : le modèle
    ne peut pas citer une phrase vraie et y accrocher un chiffre de son cru."""
    c = _normaliser(citation)
    return all(n in c for n in re.findall(r"\d+", _normaliser(valeur)))


def _vide(trouve: bool = False) -> dict:
    return {"valeur": "", "citation": "", "trouve": trouve}


def _garder(brut, texte_normalise: str) -> dict:
    """Un champ extrait → {valeur, citation, trouve}. Non trouvé : valeur vide."""
    if not isinstance(brut, dict):
        return _vide()
    valeur = " ".join(str(brut.get("valeur") or "").split())
    citation = " ".join(str(brut.get("citation") or "").split())
    if not valeur or not citation_trouvee(citation, texte_normalise) \
            or not _nombres_fondes(valeur, citation):
        return _vide()
    return {"valeur": valeur, "citation": citation, "trouve": True}


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


async def extraire(texte: str) -> dict:
    """{"champs": {nom: {valeur, citation, trouve}}, "jetons": int|None, "cout_usd": float|None}.
    Lève si le modèle est injoignable ou ne rend pas de JSON : `preparer` en fait un message."""
    from . import resume

    reponse = await llm.get_client().chat.completions.create(
        model=resume.MODELE,
        max_tokens=MAX_JETONS,
        temperature=0,
        messages=[{"role": "system", "content": CONSIGNE},
                  {"role": "user", "content": texte}],
        extra_body={"usage": {"include": True}},
    )
    brut = _json_de(reponse.choices[0].message.content or "")
    normalise = _normaliser(texte)
    champs = {nom: _garder(brut.get(nom), normalise) for nom, _ in CHAMPS}
    champs[HORAIRES] = _garder_horaires(brut.get(HORAIRES), normalise)
    usage = getattr(reponse, "usage", None)
    jetons = getattr(usage, "total_tokens", None)
    return {"champs": champs, "jetons": jetons if isinstance(jetons, int) else None,
            "cout_usd": _cout_de(usage)}


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
    base = {"champs": _champs_vides(), "lecture": "terminee", "message": "", "relu": False}
    base.update({k: v for k, v in contenu.items() if k in base})
    base["champs"] = _champs_vides() | (base["champs"] or {})
    return {"id": row["id"], "created_at": row["created_at"], "nom": row["nom"],
            "ville": row["ville"], "site_url": row["site_url"], "etat": row["etat"],
            "tenant_id": row["tenant_id"], "validee_at": row["validee_at"],
            "cout_usd": row["cout_usd"], "jetons": row["jetons"], **base}


def _ecrire(demo_id: int, contenu: dict, **colonnes) -> None:
    sets = ", ".join(["champs = ?"] + [f"{k} = ?" for k in colonnes])
    with db.get_conn() as conn:
        conn.execute(f"UPDATE demonstrations SET {sets} WHERE id = ?",  # noqa: S608 — noms du code, valeurs liées
                     (json.dumps(contenu, ensure_ascii=False), *colonnes.values(), demo_id))


def _contenu(demo: dict) -> dict:
    return {k: demo[k] for k in ("champs", "lecture", "message", "relu")}


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


def creer(nom: str, ville: str, site_url: str) -> int:
    nom = _nettoyer(nom, "Nom du restaurant", 100)
    ville = _nettoyer(ville, "Ville", 100)
    url = adresse_du_site(site_url)
    contenu = {"champs": _champs_vides(), "lecture": "en_cours", "message": "", "relu": False}
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO demonstrations (nom, ville, site_url, champs) VALUES (?, ?, ?, ?)",
            (nom, ville, url, json.dumps(contenu, ensure_ascii=False)))
        return cur.lastrowid


async def preparer(demo_id: int) -> None:
    """Lit le site et en extrait les informations. Ne lève jamais : un échec laisse les champs
    vides et un message, la démonstration reste à relire (Helmi peut tout saisir à la main)."""
    demo = await db.hors_boucle(obtenir, demo_id)
    if demo is None:
        return
    message, champs, colonnes = "", _champs_vides(), {}
    try:
        texte = await lire_le_site(demo["site_url"])
        resultat = await extraire(texte)
        champs = resultat["champs"]
        colonnes = {"jetons": resultat["jetons"], "cout_usd": resultat["cout_usd"]}
        if not any(c["trouve"] for c in champs.values()):
            message = "Le site a été lu, mais rien n'a pu en être extrait avec certitude."
    except SiteRefuse as exc:
        message = str(exc)
    except Exception as exc:
        logger.warning(f"[demos] extraction impossible pour la démonstration {demo_id} "
                       f"({type(exc).__name__}: {exc})")
        message = "Le modèle n'a pas pu lire ce site. Les champs sont à saisir à la main."
    contenu = {"champs": champs, "lecture": "terminee", "message": message, "relu": False}
    await db.hors_boucle(_ecrire, demo_id, contenu, **colonnes)


def lecture_en_cours(demo: dict) -> bool:
    """Vrai tant que la lecture tourne (et n'est pas périmée : un redémarrage la tue)."""
    if demo["lecture"] != "en_cours" or demo["etat"] != "a_relire":
        return False
    return demo["created_at"] > horloge.il_y_a(LECTURE_PERIMEE_SECONDES / 86400)


def enregistrer_relecture(demo_id: int, valeurs: dict) -> dict:
    """Enregistre ce que Helmi a relu (valeur de chaque champ ; la citation-source est
    gardée telle quelle). Marque la relecture faite : c'est elle que `valider` exige."""
    demo = obtenir(demo_id)
    if demo is None:
        raise DemoIntrouvable(demo_id)
    if demo["etat"] != "a_relire":
        raise Refus("Cette démonstration n'est plus à relire.")
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
    tenant = tenants.create_tenant(
        demo["nom"], f"{tenants.PREFIXE_DEMO}{demo_id}",
        greeting=f"Bonjour, {demo['nom']}.",
        knowledge_base=tenants.assembler_fiches(_fiches(demo)))
    horaires = demo["champs"][HORAIRES]
    if horaires["trouve"] and horaires["valeur"]:
        tenants.update_tenant(tenant.id, opening_hours=horaires["valeur"])
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE demonstrations SET etat = 'prete', tenant_id = ?, validee_at = ? WHERE id = ?",
            (tenant.id, horloge.utc_iso(), demo_id))
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
