"""Thème clair ou sombre (ASSISTANTE-114).

Ce qui compte : le choix est posé par le SERVEUR sur <html> (aucun flash du mauvais thème
au chargement), il survit d'une page à l'autre, « comme l'appareil » revient au réglage
automatique, le formulaire ne renvoie jamais hors de l'admin, et le sombre choisi est
exactement le sombre automatique."""
import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

CSS = Path(__file__).resolve().parents[1] / "app" / "admin" / "static" / "admin.css"


def _client():
    client = TestClient(app)
    assert client.post("/admin/login", data={"email": "admin@test.local",
                                             "password": "test-admin-pass"},
                       follow_redirects=False).status_code == 303
    client.get("/admin/")
    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    client.headers["X-CSRF-Token"] = json.loads(base64.b64decode(raw))["csrf"]
    return client


def _html(page: str) -> str:
    return re.search(r"<html[^>]*>", page).group(0)


class TestLeChoixEstRetenu:
    def test_par_defaut_la_page_suit_l_appareil(self):
        page = _client().get("/admin/").text
        assert "data-theme" not in _html(page)
        assert '<meta name="color-scheme" content="light dark">' in page
        assert re.search(r'value="auto"[^>]*aria-pressed="true"', page)
        assert re.search(r'value="dark"[^>]*aria-pressed="false"', page)

    def test_sombre_puis_clair_puis_comme_l_appareil(self):
        client = _client()
        resp = client.post("/admin/theme", data={"theme": "dark", "retour": "/admin/calls"},
                           follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/admin/calls"
        page = client.get("/admin/reservations").text
        assert 'data-theme="dark"' in _html(page)
        assert '<meta name="color-scheme" content="dark">' in page

        client.post("/admin/theme", data={"theme": "light"}, follow_redirects=False)
        assert 'data-theme="light"' in _html(client.get("/admin/").text)

        client.post("/admin/theme", data={"theme": "auto"}, follow_redirects=False)
        assert "data-theme" not in _html(client.get("/admin/").text)

    def test_la_page_de_connexion_aussi(self):
        client = _client()
        client.post("/admin/theme", data={"theme": "dark"}, follow_redirects=False)
        client.post("/admin/logout", follow_redirects=False)
        assert 'data-theme="dark"' in _html(client.get("/admin/login").text)

    def test_avec_htmx_rien_a_recharger(self):
        client = _client()
        resp = client.post("/admin/theme", data={"theme": "dark"},
                           headers={"HX-Request": "true"}, follow_redirects=False)
        assert resp.status_code == 204 and "theme=dark" in resp.headers["set-cookie"]


class TestRienDeDouteux:
    def test_une_valeur_inconnue_n_atteint_pas_la_page(self):
        client = _client()
        client.cookies.set("theme", '"><script>alert(1)</script>', path="/admin")
        page = client.get("/admin/").text
        assert "data-theme" not in _html(page) and "<script>alert" not in page

    def test_le_retour_reste_dans_l_admin(self):
        for ailleurs in ("https://exemple.com/", "//exemple.com/", "/twilio/voice"):
            resp = _client().post("/admin/theme", data={"theme": "dark", "retour": ailleurs},
                                  follow_redirects=False)
            assert resp.headers["location"] == "/admin/", ailleurs

    def test_sans_jeton_csrf_rien_ne_change(self):
        client = _client()
        del client.headers["X-CSRF-Token"]
        resp = client.post("/admin/theme", data={"theme": "dark"}, follow_redirects=False)
        assert resp.status_code == 403 and "set-cookie" not in resp.headers

    def test_il_faut_etre_connecte(self):
        resp = TestClient(app).post("/admin/theme", data={"theme": "dark"},
                                    follow_redirects=False)
        assert resp.status_code in (303, 401, 403) and "theme=dark" not in resp.headers.get(
            "set-cookie", "")


def test_le_sombre_choisi_est_le_sombre_automatique():
    """Le jeu de couleurs sombre est recopié (une @media ne se partage pas avec un
    sélecteur) : les deux copies doivent rester identiques."""
    css = CSS.read_text()

    def declarations(bloc: str) -> list[str]:
        return sorted(" ".join(d.split()) for d in bloc.split(";") if d.strip())

    auto = re.search(r':root:not\(\[data-theme="light"\]\) \{([^}]*)\}', css).group(1)
    choisi = re.search(r':root\[data-theme="dark"\] \{([^}]*)\}', css).group(1)
    assert declarations(auto) == declarations(choisi)
    assert len(declarations(auto)) > 15
    # Les couleurs des graphiques sont recopiées de la même façon, plus bas.
    auto = re.search(r':root:not\(\[data-theme="light"\]\) \.viz-root \{([^}]*)\}', css).group(1)
    choisi = re.search(r':root\[data-theme="dark"\] \.viz-root \{([^}]*)\}', css).group(1)
    assert declarations(auto) == declarations(choisi) and len(declarations(auto)) == 2


def test_la_page_bascule_sans_recharger():
    script = (CSS.parent / "admin.js").read_text()
    assert ".theme-choix button[value]" in script and 'setAttribute("data-theme"' in script


def test_un_choix_non_retenu_ne_reste_pas_affiche():
    """La page bascule avant la réponse du serveur. Si l'enregistrement échoue (jeton
    périmé, réseau), elle revient au thème d'avant : sinon elle afficherait un thème que
    la page suivante perd sans rien dire."""
    script = (CSS.parent / "admin.js").read_text()
    for echec in ("htmx:responseError", "htmx:sendError", "htmx:timeout"):
        assert echec in script
    assert "dataset.avant" in script and "appliquerTheme(formulaire, formulaire.dataset.avant)" in script


def test_le_numero_et_les_notes_d_une_carte_restent_selectionnables():
    """Le lien de la fiche couvre toute la carte : le texte à copier passe au-dessus."""
    css = CSS.read_text()
    assert re.search(r"\.cal-carte \.kb-body, \.cal-carte \.card-sub \{[^}]*z-index: 1", css)


def test_l_anneau_de_focus_ne_disparait_pas_sans_has():
    """`outline: none` sur le lien n'est permis que là où la carte peut le reprendre."""
    css = CSS.read_text()
    hors_supports = re.sub(r"@supports selector\(:has\(a\)\) \{.*?\n\}", "", css, flags=re.S)
    assert ".cal-lien:focus-visible { outline: none; }" in css
    assert ".cal-lien:focus-visible { outline: none; }" not in hors_supports


# ---- Aux couleurs du site, et le menu du téléphone (04/10/2026) ---------------------------

def test_l_admin_porte_la_palette_du_site():
    """Même fond brun nuit et même orange que la page d'accueil en sombre ; en clair,
    l'orange est assombri pour rester lisible en texte sur blanc (4,9:1)."""
    css = CSS.read_text()
    site = (CSS.parents[2] / "site" / "static" / "site.css").read_text()
    sombre = re.search(r':root\[data-theme="dark"\] \{([^}]*)\}', css).group(1)
    for couleur in ("#14100D", "#241D18", "#FF9B3D", "#FBF6EF"):
        assert couleur in sombre and couleur in site, couleur
    clair = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    assert "#FFF8F0" in clair and "--app-accent:      #B8520A" in clair


def test_l_encre_sur_l_accent_suit_le_theme():
    """Blanche sur l'orange brûlé du clair, brune sur l'orange vif du sombre : un `#fff`
    écrit en dur donnait du blanc sur orange vif, illisible (2,1:1)."""
    css = CSS.read_text()
    assert not re.search(r"background: var\(--app-accent\); color: #fff", css)
    sombre = re.search(r':root\[data-theme="dark"\] \{([^}]*)\}', css).group(1)
    assert "--app-accent-ink:  #22130A" in sombre


def test_les_variables_de_pico_ne_reprennent_pas_la_main():
    """Pico pose les siennes sous `:root:not([data-theme=dark])` : un simple `:root`
    perdait, et les boutons-liens restaient bleus en clair."""
    css = CSS.read_text()
    pont = re.search(r":root:root:root \{([^}]*)\}", css).group(1)
    for variable in ("--pico-primary-background: var(--app-accent)",
                     "--pico-primary-inverse: var(--app-accent-ink)",
                     "--pico-primary-underline"):
        assert variable in pont, variable


def test_les_polices_du_site_sont_servies_d_ici():
    css = CSS.read_text()
    polices = re.findall(r'url\("([^"]+\.woff2)"\)', css)
    assert polices and all(p.startswith("/site/static/") for p in polices)
    for chemin in polices:
        assert (CSS.parents[2] / "site" / "static" / chemin.rsplit("/", 1)[1]).is_file()
    assert "http://" not in css and "https://" not in css


def test_l_admin_porte_le_logo_du_site():
    """Une seule marque : la barre latérale reprend trait pour trait le dessin de l'icône
    du site, et l'onglet de l'admin affiche cette même icône."""
    client = _client()
    page = client.get("/admin/").text
    icone = (CSS.parents[2] / "site" / "static" / "favicon.svg").read_text(encoding="utf-8")
    trace = re.search(r' d="([^"]+)"', icone).group(1)
    marque = re.search(r'<span class="brand-mark"[^>]*>(.*?)</span>', page, re.S).group(1)
    assert f'd="{trace}"' in marque and 'fill="currentColor"' in marque
    assert '<link rel="icon" href="/site/static/favicon.svg"' in page
    reponse = client.get("/site/static/favicon.svg")
    assert reponse.status_code == 200 and "svg" in reponse.headers["content-type"]


class TestLeMenuDuTelephone:
    """Sur un téléphone, la navigation se range derrière un bouton aux trois barres."""

    @pytest.fixture()
    def page(self):
        client = TestClient(app)
        assert client.post("/admin/login", data={"email": "admin@test.local",
                                                 "password": "test-admin-pass"},
                           follow_redirects=False).status_code == 303
        return client.get("/admin/").text

    def test_le_bouton_et_le_panneau_qu_il_commande(self, page):
        bouton = re.search(r"<button[^>]*data-menu[^>]*>", page).group(0)
        assert 'aria-expanded="false"' in bouton and 'aria-controls="menu"' in bouton
        assert 'type="button"' in bouton and "outline" in bouton
        assert '<div class="menu" id="menu">' in page
        # La navigation, le thème et la déconnexion sont DANS le panneau.
        panneau = page[page.index('<div class="menu" id="menu">'):page.index("</aside>")]
        for contenu in ('class="nav"', 'class="theme-choix"', "/admin/logout"):
            assert contenu in panneau, contenu

    def test_sans_script_la_navigation_reste_depliee(self, page):
        assert "<noscript><style>.menu { display: flex !important; }" in page

    def test_la_feuille_et_le_script_se_repondent(self):
        css = CSS.read_text()
        script = (CSS.parent / "admin.js").read_text()
        assert "button.menu-bouton { display: none; }" in css       # écran large : pas de bouton
        assert ".sidebar[data-ouvert] .menu { display: flex; }" in css
        assert 'toggleAttribute("data-ouvert", ouvrir)' in script
        assert 'setAttribute("aria-expanded"' in script and '"Escape"' in script

    def test_la_page_de_connexion_n_a_pas_de_menu(self):
        assert "data-menu" not in TestClient(app).get("/admin/login").text
