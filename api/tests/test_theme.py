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


# ---- Recette du 05/10/2026 : ce qui se lisait mal, ou sortait de l'écran -------------------

def _clair() -> dict:
    css = CSS.read_text()
    bloc = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    return dict(re.findall(r"(--app-[\w-]+):\s*([^;]+);", bloc))


def _sombre() -> dict:
    bloc = re.search(r':root\[data-theme="dark"\] \{([^}]*)\}', CSS.read_text()).group(1)
    return dict(re.findall(r"(--app-[\w-]+):\s*([^;]+);", bloc))


def _rvb(couleur: str) -> tuple:
    couleur = couleur.strip()
    if couleur.startswith("#"):
        return tuple(int(couleur[i:i + 2], 16) for i in (1, 3, 5)) + (1.0,)
    r, v, b, a = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", couleur).groups()
    return int(r), int(v), int(b), float(a)


def _pose(couleur: str, fond: tuple) -> tuple:
    """La couleur telle qu'on la voit, une fois posée sur ce fond opaque."""
    r, v, b, a = _rvb(couleur)
    return tuple(round(a * c + (1 - a) * f) for c, f in zip((r, v, b), fond[:3]))


def _contraste(encre: tuple, fond: tuple) -> float:
    def clarte(rvb):
        canaux = [c / 255 for c in rvb[:3]]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in canaux]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    haut, bas = sorted((clarte(encre), clarte(fond)), reverse=True)
    return (haut + 0.05) / (bas + 0.05)


@pytest.mark.parametrize("statut", ["good", "warn", "bad"])
def test_une_pastille_se_lit_sur_sa_teinte_en_clair(statut):
    """Le mot d'une pastille est écrit en 11,5 px sur la teinte douce de son statut, posée
    sur blanc, sur crème ou sur le fond d'une carte. Les encres d'avant tombaient à 3,9:1."""
    jetons = _clair()
    for fond in ("--app-surface", "--app-bg", "--app-surface-2"):
        teinte = _pose(jetons[f"--app-{statut}-soft"], _rvb(jetons[fond]))
        assert _contraste(_rvb(jetons[f"--app-{statut}"]), teinte) >= 4.5, (statut, fond)


@pytest.mark.parametrize("jetons", [_clair(), _sombre()], ids=["clair", "sombre"])
def test_l_accent_pose_sur_une_teinte_reste_lisible(jetons):
    """L'entrée active du menu, la pastille d'accent : de l'orange sur de l'orange pâle.
    En clair l'orange brûlé des liens n'y tenait que 4,2:1."""
    for fond in ("--app-surface", "--app-bg", "--app-surface-2"):
        teinte = _pose(jetons["--app-accent-soft"], _rvb(jetons[fond]))
        assert _contraste(_rvb(jetons["--app-accent-teinte"]), teinte) >= 4.5, fond
    css = CSS.read_text()
    assert "background: var(--app-accent-soft); color: var(--app-accent);" not in css


def test_l_encre_pale_tient_sur_le_fond_d_une_carte_en_clair():
    jetons = _clair()
    for fond in ("--app-surface", "--app-bg", "--app-surface-2"):
        assert _contraste(_rvb(jetons["--app-faint"]), _rvb(jetons[fond])) >= 4.5, fond


def test_sur_une_ligne_selectionnee_l_heure_et_la_pastille_restent_lisibles():
    css = CSS.read_text()
    assert '.card-row[aria-current="true"] .row-when { color: var(--app-muted); }' in css
    assert '.card-row[aria-current="true"] .chip { background: var(--app-surface); }' in css
    for jetons in (_clair(), _sombre()):
        ligne = _pose(jetons["--app-accent-soft"], _rvb(jetons["--app-surface"]))
        assert _contraste(_rvb(jetons["--app-muted"]), ligne) >= 4.5
        for statut in ("good", "warn", "bad"):
            assert _contraste(_rvb(jetons[f"--app-{statut}"]), _rvb(jetons["--app-surface"])) >= 4.5


def test_le_titre_d_un_encart_suit_son_ton_et_son_lien_prend_l_encre_du_texte():
    """Un titre vert sur un encart d'alerte disait « tout va bien », en 4,2:1 ; un lien
    orange sur la teinte, 3,9:1."""
    css = CSS.read_text()
    assert '.callout[style*="--app-warn-soft"] b { color: var(--app-warn); }' in css
    assert '.callout[style*="--app-bad-soft"] b { color: var(--app-bad); }' in css
    assert ".callout a { color: inherit;" in css


def test_les_filtres_passent_a_la_ligne_au_lieu_de_sortir_de_l_ecran():
    """Six filtres sur la liste des appels : à 390 px, « Pannes » était hors de l'écran et
    toute la page défilait en largeur."""
    css = CSS.read_text()
    barre = re.search(r"\.segmented \{([^}]*)\}", css).group(1)
    assert "flex-wrap: wrap" in barre and "max-width: 100%" in barre
    assert "white-space: nowrap" in re.search(r"\.segmented a \{([^}]*)\}", css).group(1)


def test_les_actions_d_une_enseigne_passent_sous_son_nom_sur_telephone():
    css = CSS.read_text()
    assert ".enseignes-liste { container-type: inline-size; }" in css
    assert ".enseignes-liste .card-row { flex-wrap: wrap; }" in css
    assert 'class="card-list enseignes-liste"' in _client().get("/admin/tenants").text


def test_les_listes_deroulantes_gardent_leur_fleche():
    """Le raccourci `background` effaçait l'image de fond : plus de flèche, ni d'icône
    sur les champs de date."""
    css = CSS.read_text()
    champs = css[css.index('input:not([type="checkbox"]):not([type="radio"]), select, textarea {'):]
    champs = champs[:champs.index("}")]
    assert "background-color: var(--app-surface) !important" in champs
    assert not re.search(r"background:\s", champs)


def test_un_message_d_attente_passe_a_la_ligne():
    css = CSS.read_text()
    assert ('[aria-busy="true"]:not(button, [role="button"], input, select, textarea, html, form) '
            "{ white-space: normal; }") in css


def test_quatre_indicateurs_ne_laissent_pas_un_orphelin():
    css = CSS.read_text()
    assert (".kpi-grid:has(> :nth-child(4):last-child) "
            "{ grid-template-columns: repeat(4, minmax(0, 1fr)); }") in css


def test_la_page_de_connexion_porte_la_marque():
    page = TestClient(app).get("/admin/login").text
    icone = (CSS.parents[2] / "site" / "static" / "favicon.svg").read_text(encoding="utf-8")
    trace = re.search(r' d="([^"]+)"', icone).group(1)
    marque = re.search(r'<p class="marque-connexion">(.*?)</p>', page, re.S).group(1)
    assert f'd="{trace}"' in marque and "Helmane" in marque


def test_les_montants_s_ecrivent_avec_une_virgule():
    from app.admin import deps
    assert deps.nombre(21.15) == "21,15" and deps.nombre(0.072, 3) == "0,072"
    assert deps.nombre(None) == "0,00" and deps.nombre(1.94, 1) == "1,9"


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
