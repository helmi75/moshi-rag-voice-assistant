"""La page d'accueil du site (ASSISTANTE-119).

Ce qu'on tient ici :
- la page est servie à tout le monde, sans compte et sans rien lire en base ;
- elle ne contient rien que la politique de sécurité de Caddy bloquerait (script en
  ligne, ressource d'un autre site) — sinon elle s'afficherait sans ses polices ni son
  formulaire, et personne ne le verrait en test ;
- ses prix sont ceux de `app/plans.py`, pas une copie : l'ancienne page de vente
  annonçait encore 249 € pour 1 200 appels après que la grille eut changé.
"""
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app import plans, site
from app.main import app

RACINE = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def page(client) -> str:
    reponse = client.get("/")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"].startswith("text/html")
    return reponse.text.replace("&#39;", "'")


class TestLaPageEstPublique:
    def test_elle_s_affiche_sans_compte(self, page):
        assert "Marie décroche." in page and "Rappelez-moi" in page
        assert '<html lang="fr">' in page

    def test_elle_ne_pose_aucun_cookie(self, client):
        assert "set-cookie" not in client.get("/").headers

    def test_une_sonde_en_head_recoit_200(self, client):
        assert client.head("/").status_code == 200

    def test_l_admin_reste_derriere_sa_connexion(self, client):
        r = client.get("/admin/", follow_redirects=False)
        assert r.status_code in (302, 303, 307) and "/admin/login" in r.headers["location"]

    def test_robots_ecarte_l_admin(self, client):
        r = client.get("/robots.txt")
        assert r.status_code == 200 and "Disallow: /admin" in r.text

    def test_elle_ne_lit_pas_la_base(self, client, monkeypatch):
        """Une page que n'importe qui peut recharger en boucle ne doit pas ouvrir SQLite :
        la boucle d'événements porte l'audio des appels en cours."""
        from app import db

        def interdit(*_a, **_k):
            raise AssertionError("la page d'accueil a ouvert la base")

        monkeypatch.setattr(db, "get_conn", interdit)
        assert client.get("/").status_code == 200


class TestLaPolitiqueDeSecurite:
    """La CSP de `caddy/Caddyfile` : script-src 'self', font-src 'self', connect-src 'self'."""

    def test_aucun_script_en_ligne(self, page):
        scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, flags=re.S)
        assert scripts, "la page n'a plus de script"
        for attributs, contenu in scripts:
            assert "src=" in attributs and not contenu.strip()
        assert not re.search(r"\son[a-z]+\s*=", page), "gestionnaire d'événement en ligne"
        assert "javascript:" not in page

    def test_aucune_ressource_d_un_autre_site(self, page):
        externes = re.findall(r"""(?:src|href|action)\s*=\s*["'](https?:)?//[^"']+""", page)
        assert externes == []
        feuille = (site.STATIC_DIR / "site.css").read_text(encoding="utf-8")
        assert "http://" not in feuille and "https://" not in feuille and "@import" not in feuille
        script = (site.STATIC_DIR / "site.js").read_text(encoding="utf-8")
        assert "http://" not in script and "https://" not in script

    def test_la_politique_de_caddy_autorise_ce_que_la_page_utilise(self):
        politique = (RACINE / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        if "Content-Security-Policy" not in politique:
            pytest.skip("Caddyfile hors de portée")
        for directive in ("script-src 'self'", "font-src 'self'", "connect-src 'self'",
                          "style-src 'self' 'unsafe-inline'", "img-src 'self' data:"):
            assert directive in politique

    def test_les_fichiers_statiques_sont_servis_avec_leur_empreinte(self, client, page):
        adresses = re.findall(r'(/site/static/[\w.-]+\?v=[0-9a-f]{10})', page)
        assert {a.split("?")[0].rsplit("/", 1)[1] for a in adresses} == {
            "site.css", "site.js", "favicon.svg"}
        for adresse in adresses:
            assert client.get(adresse).status_code == 200
        for police in re.findall(r'url\("([\w.-]+\.woff2)"\)',
                                 (site.STATIC_DIR / "site.css").read_text(encoding="utf-8")):
            reponse = client.get(f"/site/static/{police}")
            assert reponse.status_code == 200 and reponse.content[:4] == b"wOF2"

    def test_le_script_n_ecrit_que_du_texte(self):
        """La réponse du serveur s'affiche par `textContent` : jamais interprétée."""
        script = (site.STATIC_DIR / "site.js").read_text(encoding="utf-8")
        assert "textContent = message" in script
        # Le seul `innerHTML` restitue le libellé d'origine du bouton, lu dans la page.
        assert script.count("innerHTML") == 2 and "bouton.innerHTML = libelle" in script
        assert "eval(" not in script and "document.write" not in script


class TestLesPrixSontCeuxDeLaGrille:
    @staticmethod
    def _bloc(page: str, formule) -> str:
        bloc = re.search(rf"<h3>{formule.label}</h3>.*?</article>", page, flags=re.S)
        assert bloc, formule.label
        return bloc.group(0)

    def test_chaque_abonnement_affiche_son_prix_et_ses_minutes(self, page):
        abonnements = [f for f in plans.catalogue() if not f.sans_forfait]
        assert len(abonnements) >= 3
        for formule in abonnements:
            bloc = self._bloc(page, formule)
            assert f"{formule.prix_mensuel_eur} €<small>par mois" in bloc
            assert f"{site._euros(formule.minutes_incluses)} minutes par mois" in bloc
            assert formule.argument in bloc
            assert f"Au-delà : {site._euros(formule.minute_supp_eur)} € la minute" in bloc

    def test_la_formule_sans_abonnement_affiche_sa_minute(self, page):
        libres = [f for f in plans.catalogue() if f.sans_forfait]
        assert libres, "plus de formule sans abonnement : le test ne vérifie rien"
        for formule in libres:
            bloc = self._bloc(page, formule)
            assert f"{site._euros(formule.minute_supp_eur)} €<small>la minute" in bloc
            assert "Aucun abonnement" in bloc and "par mois" not in bloc
            assert (f"Mise en service : {formule.mise_en_service_eur} € par numéro, "
                    "une fois") in bloc

    def test_chaque_forfait_est_traduit_en_appels(self, page):
        """Demandé par Helmi le 03/10/2026 : « 250 minutes » ne dit rien à un
        restaurateur. L'équivalent vient de `plans.py`, et la page dit sur quoi il repose."""
        for formule in (f for f in plans.catalogue() if not f.sans_forfait):
            attendu = site._euros(plans.appels_equivalents(formule.minutes_incluses))
            assert f"soit environ {attendu} appels" in self._bloc(page, formule)
        assert "calculé sur 3 min 30 par appel" in page
        assert "nos appels d'essai" in page  # un ordre de grandeur, pas une promesse

    def test_l_equivalent_suit_la_duree_retenue(self, client, monkeypatch):
        monkeypatch.setattr(plans, "DUREE_APPEL_MIN", 2.0)
        page = client.get("/").text
        essentiel = plans.get("essentiel")
        assert f"soit environ {essentiel.minutes_incluses // 2 // 10 * 10} appels" in page
        assert "calculé sur 2 min par appel" in page

    def test_la_page_ne_vend_plus_des_appels(self, page):
        """La grille compte en minutes depuis le 02/10/2026 : un « appels par mois » ou
        un « par appel » resté sur la page vendrait une formule qui n'existe plus."""
        formules = page[page.index('id="formules"'):]
        assert "appels par mois" not in formules and "€ par appel" not in formules

    def test_le_hors_taxes_la_seconde_et_le_seuil_d_alerte(self, page):
        assert "Prix hors taxes" in page
        assert "décomptées à la seconde" in page
        assert f"Alerte à {round(plans.SEUIL_ALERTE * 100)} %" in page

    def test_un_changement_de_grille_change_la_page(self, client, monkeypatch):
        chere = plans.Formule(id="essentiel", label="Essentiel", prix_mensuel_eur=95,
                              minutes_incluses=1180, minute_supp_eur=0.55,
                              etablissements_inclus=1, argument="Test.")
        autres = tuple(f for f in plans.CATALOGUE if f.id != "essentiel")
        monkeypatch.setattr(plans, "CATALOGUE", (chere,) + autres)
        page = client.get("/").text
        assert "95 €" in page and "0,55 €" in page
        assert f"{site._euros(1180)} minutes par mois" in page
        assert "89 €" not in page and "0,35 €" not in page
        # Le calcul du manque à gagner retranche la même formule.
        assert 'data-formule="95"' in page and "− 95 €" in page

    def test_le_plus_d_une_adresse(self, page):
        maison = plans.get("maison")
        assert f"Jusqu'à {maison.etablissements_inclus} établissements" in page

    @pytest.mark.parametrize("montant,attendu", [(0.3, "0,30"), (89, "89"), (7111, "7 111"),
                                                 (0.45, "0,45")])
    def test_les_montants_s_ecrivent_a_la_francaise(self, montant, attendu):
        assert site._euros(montant) == attendu


class TestLesMentionsLegales:
    """Écrites, mais pas publiées tant que l'éditeur n'est pas identifié (Helmi, 04/10/2026 :
    « on va laisser pour plus tard ») : des mentions sans éditeur n'en sont pas."""

    @pytest.fixture()
    def editeur(self, monkeypatch):
        monkeypatch.setitem(site.EDITEUR, "nom", "Jeanne Exemple")
        monkeypatch.setitem(site.EDITEUR, "siren", "000 000 000")
        monkeypatch.setitem(site.EDITEUR, "email", "contact@exemple.test")

    @pytest.fixture()
    def mentions(self, client, editeur) -> str:
        reponse = client.get("/mentions-legales")
        assert reponse.status_code == 200
        return reponse.text.replace("&#39;", "'")

    def test_sans_editeur_ni_page_ni_lien(self, client, page):
        assert site.EDITEUR["nom"] == "", "l'éditeur est renseigné : retirer ce test"
        assert client.get("/mentions-legales").status_code == 404
        assert "mentions-legales" not in page

    def test_avec_l_editeur_l_accueil_y_mene(self, client, editeur):
        assert '<a href="/mentions-legales">Mentions légales</a>' in client.get("/").text

    def test_publique_sans_cookie_ni_base(self, client, editeur, monkeypatch):
        from app import db

        def interdit(*_a, **_k):
            raise AssertionError("les mentions légales ont ouvert la base")

        monkeypatch.setattr(db, "get_conn", interdit)
        reponse = client.get("/mentions-legales")
        assert reponse.status_code == 200 and "set-cookie" not in reponse.headers
        assert client.head("/mentions-legales").status_code == 200

    def test_ni_script_en_ligne_ni_ressource_d_un_autre_site(self, mentions):
        assert not re.search(r"<script\b", mentions)
        assert not re.search(r"\son[a-z]+\s*=", mentions)
        assert re.findall(r"""(?:src|href|action)\s*=\s*["'](https?:)?//[^"']+""", mentions) == []

    def test_l_hebergeur(self, mentions):
        assert "Hostinger International Limited" in mentions
        assert "Larnaca" in mentions and "Paris (France)" in mentions

    def test_les_durees_sont_celles_que_la_purge_applique(self, client, editeur, monkeypatch):
        """La page ne doit pas promettre une durée que `rgpd.py` n'applique pas."""
        monkeypatch.setenv("RETENTION_RAPPEL_JOURS", "12")
        monkeypatch.setenv("RETENTION_NUMERO_JOURS", "45")
        page = client.get("/mentions-legales").text
        assert "effacé au bout de 12 jours" in page and "votre numéro 45" in page

    def test_ni_cookie_ni_mesure_d_audience_annonces(self, mentions):
        assert "ne dépose aucun cookie" in mentions

    def test_l_editeur_affiche_ce_qui_est_rempli_et_rien_d_autre(self, mentions):
        assert "None" not in mentions and "Statut :" not in mentions
        assert "<b>Jeanne Exemple</b>" in mentions and "SIREN : 000 000 000" in mentions
        assert "en écrivant à contact@exemple.test" in mentions


class TestLeTheme:
    """Sombre, et seulement sombre (Helmi, 04/10/2026)."""

    def test_la_feuille_n_a_plus_de_theme_clair(self, page):
        feuille = (site.STATIC_DIR / "site.css").read_text(encoding="utf-8")
        assert "prefers-color-scheme" not in feuille and "data-theme" not in feuille
        assert "color-scheme: dark" in feuille
        assert '<meta name="color-scheme" content="dark">' in page


class TestLeLogo:
    """La toque (choisie par Helmi le 04/10/2026) est un seul dessin : celui de l'icône de
    l'onglet. L'en-tête le reprend trait pour trait, pour qu'une retouche de l'un ne laisse
    pas l'autre en arrière."""

    def test_l_en_tete_porte_le_dessin_de_l_icone(self, page):
        icone = (site.STATIC_DIR / "favicon.svg").read_text(encoding="utf-8")
        traces = re.findall(r' d="([^"]+)"', icone)
        assert len(traces) == 1
        logo = re.search(r'<span class="marque__logo"[^>]*>(.*?)</span>', page, re.S).group(1)
        assert f'd="{traces[0]}"' in logo

    def test_le_dessin_prend_la_couleur_du_theme(self, page):
        """Dans la page, il suit l'encre de sa pastille ; seule l'icône de l'onglet, qui
        n'a pas de feuille de style, porte ses couleurs."""
        logo = re.search(r'<span class="marque__logo"[^>]*>(.*?)</span>', page, re.S).group(1)
        assert 'fill="currentColor"' in logo and "#" not in logo


class TestLEnTeteSurTelephone:
    def test_l_espace_client_reste_accessible_en_bouton(self, page):
        """Sur téléphone les liens de l'en-tête s'effacent : sans ce bouton, un client
        n'avait plus aucun chemin vers sa connexion."""
        assert 'class="btn btn--line btn--small btn--client" href="/admin/login"' in page
        feuille = (site.STATIC_DIR / "site.css").read_text(encoding="utf-8")
        assert ".top nav .btn--client { display: none; }" in feuille
        assert ".top nav .btn--client { display: inline-flex; }" in feuille


class TestCeQueLaPagePromet:
    def test_elle_ne_promet_pas_le_restaurant_du_visiteur(self, page):
        """Marie rappelle en répondant pour l'établissement de démonstration : la page ne
        doit pas annoncer qu'on l'entendra « pour votre restaurant »."""
        assert "répondre pour votre restaurant" not in page
        assert "au nom de votre restaurant" not in page
        assert "comme pour un vrai restaurant" in page

    def test_plus_aucune_trace_de_la_maquette(self, page):
        assert "maquette" not in page.lower()

    def test_plus_d_essai_de_14_jours(self, page):
        """Retiré le 03/10/2026 à la demande de Helmi : rien ne l'applique."""
        assert "14 jours" not in page and "jours d'essai" not in page

    def test_ni_tarif_fondateur_ni_sans_engagement_sous_les_formules(self, page):
        """Retirés le 04/10/2026 à la demande de Helmi."""
        formules = page[page.index('id="formules"'):page.index("</section>", page.index('id="formules"'))]
        assert "fondateur" not in page
        assert "engagement" not in formules

    def test_ni_temoignage_ni_chiffre_de_clientele(self, page):
        for invente in ("témoignage", "restaurants nous font confiance", "clients satisfaits",
                        "SMS de confirmation", "ligne prioritaire"):
            assert invente not in page

    def test_les_deux_formulaires_et_leur_champ_cache(self, page):
        assert page.count('<form class="rappel"') == 2
        assert page.count('class="rappel__piege"') == 2
        assert page.count('type="tel"') == 2
