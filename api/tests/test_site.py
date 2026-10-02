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
    def test_chaque_formule_affiche_son_prix_et_son_plafond(self, page):
        for formule in plans.catalogue():
            bloc = re.search(rf"<h3>{formule.label}</h3>.*?</article>", page, flags=re.S)
            assert bloc, formule.label
            assert f"{formule.prix_mensuel_eur} €" in bloc.group(0)
            assert f"{formule.appels_inclus} appels par mois" in bloc.group(0)
            assert formule.argument in bloc.group(0)
            assert "Au-delà : 0,30 € par appel" in bloc.group(0)

    def test_le_depassement_et_le_seuil_d_alerte(self, page):
        assert plans.DEPASSEMENT_EUR == 0.30 and "0,30 €" in page
        assert f"Alerte à {round(plans.SEUIL_ALERTE * 100)} %" in page

    def test_un_changement_de_grille_change_la_page(self, client, monkeypatch):
        chere = plans.Formule(id="essentiel", label="Essentiel", prix_mensuel_eur=95,
                              appels_inclus=180, etablissements_inclus=1, argument="Test.")
        monkeypatch.setattr(plans, "CATALOGUE", (chere,) + plans.CATALOGUE[1:])
        monkeypatch.setattr(plans, "DEPASSEMENT_EUR", 0.45)
        page = client.get("/").text
        assert "95 €" in page and "180 appels par mois" in page and "0,45 €" in page
        assert "89 €" not in page and "0,30 €" not in page
        # Le calcul du manque à gagner retranche la même formule.
        assert 'data-formule="95"' in page and "− 95 €" in page

    def test_le_plus_d_une_adresse(self, page):
        maison = plans.get("maison")
        assert f"Jusqu'à {maison.etablissements_inclus} établissements" in page

    @pytest.mark.parametrize("montant,attendu", [(0.3, "0,30"), (89, "89"), (7111, "7 111"),
                                                 (0.45, "0,45")])
    def test_les_montants_s_ecrivent_a_la_francaise(self, montant, attendu):
        assert site._euros(montant) == attendu


class TestCeQueLaPagePromet:
    def test_elle_ne_promet_pas_le_restaurant_du_visiteur(self, page):
        """Marie rappelle en répondant pour l'établissement de démonstration : la page ne
        doit pas annoncer qu'on l'entendra « pour votre restaurant »."""
        assert "répondre pour votre restaurant" not in page
        assert "au nom de votre restaurant" not in page
        assert "comme pour un vrai restaurant" in page

    def test_plus_aucune_trace_de_la_maquette(self, page):
        assert "maquette" not in page.lower()

    def test_ni_temoignage_ni_chiffre_de_clientele(self, page):
        for invente in ("témoignage", "restaurants nous font confiance", "clients satisfaits",
                        "SMS de confirmation", "ligne prioritaire"):
            assert invente not in page

    def test_les_deux_formulaires_et_leur_champ_cache(self, page):
        assert page.count('<form class="rappel"') == 2
        assert page.count('class="rappel__piege"') == 2
        assert page.count('type="tel"') == 2
