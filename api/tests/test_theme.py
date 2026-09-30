"""Thème clair ou sombre (ASSISTANTE-114).

Ce qui compte : le choix est posé par le SERVEUR sur <html> (aucun flash du mauvais thème
au chargement), il survit d'une page à l'autre, « comme l'appareil » revient au réglage
automatique, le formulaire ne renvoie jamais hors de l'admin, et le sombre choisi est
exactement le sombre automatique."""
import base64
import json
import re
from pathlib import Path

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


def test_la_page_bascule_sans_recharger():
    script = (CSS.parent / "admin.js").read_text()
    assert ".theme-choix button[value]" in script and 'setAttribute("data-theme"' in script
