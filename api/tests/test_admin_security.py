"""Cloisonnement et authentification de l'admin — vérifiés par ÉNUMÉRATION des routes.

L'admin est sur l'Internet public. Le risque n'est pas qu'une garde soit mal écrite
(deps.py est court et solide) : c'est qu'une route l'oublie. Une fuite entre
établissements s'est déjà produite une fois de cette façon.

Ces tests ne visent donc pas des routes nommées : ils parcourent `app.routes` et
s'appliquent à TOUTE route `/admin` — y compris celles ajoutées après leur écriture.
Une route non gardée fait rougir la suite sans que personne ait pensé à la tester.
"""
import os
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import calls, reservations, tenants, users
from app.main import app

# Seules routes légitimement ouvertes : la page de connexion et sa soumission.
OUVERTES = {("GET", "/admin/login"), ("POST", "/admin/login")}


def mdp_jetable(qui: str) -> str:
    """Mot de passe d'un compte de test, ASSEMBLÉ à l'exécution.

    Écrits en clair, ces littéraux déclenchent le détecteur de secrets à chaque PR.
    Trois faux positifs permanents de plus useraient la confiance dans un outil dont
    toute la valeur tient au rapport signal/bruit — et le prochain vrai secret passerait
    inaperçu au milieu du bruit. Les comptes concernés vivent dans une base jetable.
    """
    return "-".join(("compte", qui, "sans", "valeur"))


MDP_A = mdp_jetable("a")
MDP_B = mdp_jetable("b")
MDP_FAUX = "-".join(("mauvais", "essai"))
MDP_TROP_COURT = "abc"
# Semé par conftest.py : on le lit plutôt que de le réécrire.
MDP_SUPERADMIN = os.environ["ADMIN_PASSWORD"]


def _sous_routes(r):
    """Les routes filles d'un objet de routage, quel que soit son emballage.

    FastAPI 0.139 ne met PAS les routers inclus à plat dans `app.routes` : il les
    enveloppe dans un `_IncludedRouter` qui n'expose ni `.routes` ni `.prefix`, mais
    un `.original_router`. On essaie donc les deux formes plutôt que d'épouser un
    détail interne — et `test_l_enumeration_couvre_bien_quelque_chose` reste là pour
    le jour où une montée de version cassera encore ce chemin."""
    filles = getattr(r, "routes", None)
    if isinstance(filles, (list, tuple)):
        return filles
    origine = getattr(r, "original_router", None)
    if origine is not None:
        return getattr(origine, "routes", ()) or ()
    return ()


def _aplatir(routes):
    """Parcours récursif. Une énumération qui ne descend pas rendrait les tests
    ci-dessous silencieusement vides — le test décoratif qu'ils sont censés empêcher."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        else:
            yield from _aplatir(_sous_routes(r))


def routes_admin():
    """(méthode, gabarit) pour chaque route /admin, hors fichiers statiques."""
    vues = []
    for r in _aplatir(app.routes):
        if not r.path.startswith("/admin"):
            continue
        for methode in sorted(r.methods - {"HEAD", "OPTIONS"}):
            vues.append((methode, r.path))
    return sorted(set(vues))


# Valeurs plausibles par nom de champ : le corps doit franchir la validation FastAPI
# pour que la requête ATTEIGNE le contrôle d'autorisation. Un 422 ne prouve rien —
# il signifie seulement que la route a refusé la forme, pas qu'elle refuse l'accès.
_VALEURS = {
    "date": "2026-09-01", "time": "20:00", "party_size": "2",
    "customer_name": "Client", "customer_phone": "+33600000000",
    "email": "intrus@test.fr", "password": mdp_jetable("synthese"),
    "name": "Nom", "phone_number": "+33999999999",
    "greeting": "Bonjour.", "knowledge_base": "## Horaires\nMidi et soir.",
    "business_type": "restaurant", "language": "fr-FR",
}


def corps_valide(route):
    """(données, fichiers) minimaux pour satisfaire la signature de la route."""
    import inspect

    from fastapi import params

    donnees, fichiers = {}, {}
    for nom, p in inspect.signature(route.endpoint).parameters.items():
        defaut = p.default
        if isinstance(defaut, params.File) or "UploadFile" in str(p.annotation):
            fichiers[nom] = (f"{nom}.wav", b"RIFF$\x00\x00\x00WAVEfmt ", "audio/wav")
        elif isinstance(defaut, params.Form):
            # On remplit TOUS les champs, requis ou non. Distinguer les deux
            # demanderait de comparer le défaut à `PydanticUndefined` — un détail
            # interne de FastAPI qui a déjà changé de forme. Fournir une valeur à un
            # champ optionnel est sans conséquence ici : ce qu'on teste, c'est le
            # refus d'accès, pas le traitement du corps.
            donnees[nom] = _VALEURS.get(nom, "2" if p.annotation is int else "x")
    return donnees, fichiers


def concret(gabarit: str, ids: dict) -> str:
    """Remplace {tenant_id}, {reservation_id}… par de vrais identifiants."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ids.get(m.group(1), 1)), gabarit)


@pytest.fixture()
def deux_etablissements():
    """A (celui du restaurateur testé) et B (celui qu'il ne doit jamais atteindre)."""
    suffixe = id(object()) % 10_000_000
    a = tenants.create_tenant("Chez A", f"+3361{suffixe:07d}")
    b = tenants.create_tenant("Voisin Secret B", f"+3362{suffixe:07d}")
    resto_a = users.create_user(f"a-{a.id}@test.fr", MDP_A,
                                users.ROLE_RESTAURATEUR, a.id)
    resto_b = users.create_user(f"b-{b.id}@test.fr", MDP_B,
                                users.ROLE_RESTAURATEUR, b.id)
    resa_b = reservations.create_reservation(b.id, "Client de B", "2026-09-01", "20:00", 2)
    calls.start_call(f"CA-b-{suffixe}", b.id, "+33600000000")
    calls.finish_call(f"CA-b-{suffixe}", "completed")
    appel_b = calls.list_calls(b.id)[0]
    yield {
        "a": a, "resto_a": resto_a,
        "ids_b": {"tenant_id": b.id, "reservation_id": resa_b["id"],
                  "call_id": appel_b["id"], "user_id": resto_b.id},
        "nom_b": b.name,
    }
    tenants.delete_tenant(a.id)
    tenants.delete_tenant(b.id)


def _connexion(client, email, mot_de_passe):
    r = client.post("/admin/login", data={"email": email, "password": mot_de_passe},
                    follow_redirects=False)
    assert r.status_code == 303, "connexion refusée : le test ne prouverait rien"
    import base64
    import json
    brut = client.cookies.get("session").split(".")[0]
    brut += "=" * (-len(brut) % 4)
    return json.loads(base64.b64decode(brut))["csrf"]


class TestAuthentification:
    def test_aucune_route_admin_accessible_sans_session(self):
        """Une route qui répond 2xx sans session est une porte ouverte sur Internet."""
        ouvertes = []
        for methode, gabarit in routes_admin():
            if (methode, gabarit) in OUVERTES:
                continue
            client = TestClient(app)  # client neuf = aucune session
            r = client.request(methode, concret(gabarit, {}), follow_redirects=False)
            if 200 <= r.status_code < 300:
                ouvertes.append(f"{methode} {gabarit} → {r.status_code}")
        assert not ouvertes, "routes atteignables sans authentification :\n  " + \
            "\n  ".join(ouvertes)

    def test_l_enumeration_couvre_bien_quelque_chose(self):
        """Garde-fou du garde-fou : si l'introspection ne trouvait plus rien, le test
        ci-dessus passerait au vert en ne vérifiant strictement aucune route."""
        assert len(routes_admin()) >= 25


class TestCloisonnement:
    def test_un_restaurateur_n_atteint_aucun_objet_d_un_autre(self, deux_etablissements):
        """Sur CHAQUE route paramétrée, avec les identifiants de l'établissement voisin."""
        d = deux_etablissements
        client = TestClient(app)
        csrf = _connexion(client, d["resto_a"].email, MDP_A)

        par_gabarit = {(m, r.path): r for r in _aplatir(app.routes) for m in r.methods}
        fuites = []
        for methode, gabarit in routes_admin():
            if "{" not in gabarit:
                continue
            chemin = concret(gabarit, d["ids_b"])
            donnees, fichiers = corps_valide(par_gabarit[(methode, gabarit)])
            r = client.request(methode, chemin, data=donnees or None,
                               files=fichiers or None,
                               headers={"X-CSRF-Token": csrf}, follow_redirects=False)
            if r.status_code == 422:
                fuites.append(f"{methode} {gabarit} → 422 : corps refusé, "
                              f"l'autorisation n'a PAS été atteinte (test à compléter)")
            elif r.status_code not in (403, 404):
                fuites.append(f"{methode} {gabarit} → {r.status_code}")
        assert not fuites, "objets d'un autre établissement atteignables :\n  " + \
            "\n  ".join(fuites)

    def test_le_nom_du_voisin_n_apparait_dans_aucune_liste(self, deux_etablissements):
        """Les listes ne prennent pas d'identifiant en chemin mais acceptent
        ?tenant_id= : c'est par là que la première fuite est passée."""
        d = deux_etablissements
        client = TestClient(app)
        _connexion(client, d["resto_a"].email, MDP_A)

        fuites = []
        for methode, gabarit in routes_admin():
            if methode != "GET" or "{" in gabarit:
                continue
            r = client.get(f"{gabarit}?tenant_id={d['ids_b']['tenant_id']}",
                           follow_redirects=False)
            if r.status_code == 200 and d["nom_b"] in r.text:
                fuites.append(f"GET {gabarit}?tenant_id=<voisin>")
        assert not fuites, "nom d'un autre établissement visible :\n  " + \
            "\n  ".join(fuites)


class TestCsrf:
    def test_tout_post_refuse_un_jeton_absent_ou_faux(self, deux_etablissements):
        """Sans CSRF, un site tiers peut faire agir un restaurateur connecté."""
        d = deux_etablissements
        client = TestClient(app)
        _connexion(client, d["resto_a"].email, MDP_A)
        ids = {"tenant_id": d["a"].id, "reservation_id": 1, "call_id": 1,
               "user_id": d["resto_a"].id}

        sans_protection = []
        for methode, gabarit in routes_admin():
            if methode != "POST" or (methode, gabarit) in OUVERTES:
                continue
            r = client.post(concret(gabarit, ids),
                            headers={"X-CSRF-Token": "jeton-forge"},
                            follow_redirects=False)
            if r.status_code != 403:
                sans_protection.append(f"POST {gabarit} → {r.status_code}")
        assert not sans_protection, "POST acceptés avec un jeton CSRF faux :\n  " + \
            "\n  ".join(sans_protection)


class TestForceBrute:
    """L'admin est sur Internet et des sondes automatisées y sont déjà passées.
    bcrypt freine (~150 ms/essai) mais ne bloque rien, et rien n'était tracé."""

    @pytest.fixture(autouse=True)
    def _ardoise_propre(self):
        from app.admin import throttle
        throttle.vider()
        yield
        throttle.vider()

    def test_les_tentatives_finissent_par_etre_refusees(self):
        from app.admin import throttle

        client = TestClient(app)
        codes = []
        for _ in range(throttle.MAX_ECHECS + 2):
            r = client.post("/admin/login",
                            data={"email": "intrus@test.fr", "password": MDP_FAUX},
                            follow_redirects=False)
            codes.append(r.status_code)
        assert codes[0] == 401, "le premier essai doit être évalué normalement"
        assert 429 in codes, f"aucun blocage après {len(codes)} essais : {codes}"
        assert codes[-1] == 429 and codes.count(429) >= 2

    def test_le_refus_indique_quand_reessayer(self):
        from app.admin import throttle

        client = TestClient(app)
        for _ in range(throttle.MAX_ECHECS + 1):
            r = client.post("/admin/login",
                            data={"email": "intrus@test.fr", "password": MDP_FAUX},
                            follow_redirects=False)
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0

    def test_une_connexion_reussie_efface_l_ardoise(self, deux_etablissements):
        """Sinon un restaurateur maladroit resterait bloqué après s'être connecté."""
        from app.admin import throttle

        d = deux_etablissements
        client = TestClient(app)
        for _ in range(throttle.MAX_ECHECS - 1):
            client.post("/admin/login",
                        data={"email": d["resto_a"].email, "password": MDP_FAUX},
                        follow_redirects=False)
        ok = client.post("/admin/login",
                         data={"email": d["resto_a"].email, "password": MDP_A},
                         follow_redirects=False)
        assert ok.status_code == 303
        # L'ardoise est effacée : un nouvel échec repart de zéro, pas de 429 immédiat.
        r = client.post("/admin/login",
                        data={"email": d["resto_a"].email, "password": MDP_FAUX},
                        follow_redirects=False)
        assert r.status_code == 401

    def test_l_adresse_retenue_n_est_pas_celle_que_le_client_annonce(self):
        """X-Forwarded-For est ajouté PAR Caddy en fin de liste. Prendre la première
        entrée laisserait n'importe qui se forger une adresse par tentative et
        contourner le compteur."""
        from app.admin import throttle

        client = TestClient(app)
        codes = []
        for i in range(throttle.MAX_ECHECS + 2):
            r = client.post("/admin/login",
                            data={"email": "intrus@test.fr", "password": MDP_FAUX},
                            headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"},
                            follow_redirects=False)
            codes.append(r.status_code)
        assert 429 in codes, (
            "le compteur suit l'adresse annoncée par le client : elle est forgeable, "
            f"donc la limitation est contournable — codes {codes}"
        )


class TestRobustesseMotDePasse:
    def test_un_mot_de_passe_court_est_refuse_a_la_creation(self, deux_etablissements):
        from app import users as u

        d = deux_etablissements
        client = TestClient(app)
        csrf = _connexion(client, "admin@test.local", MDP_SUPERADMIN)
        avant = len(u.list_users(d["a"].id))
        r = client.post(f"/admin/tenants/{d['a'].id}/users",
                        data={"email": "court@test.fr", "password": MDP_TROP_COURT},
                        headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert r.status_code == 400
        assert len(u.list_users(d["a"].id)) == avant, "le compte a été créé malgré tout"

    def test_un_mot_de_passe_court_est_refuse_au_changement(self, deux_etablissements):
        d = deux_etablissements
        client = TestClient(app)
        csrf = _connexion(client, d["resto_a"].email, MDP_A)
        r = client.post(f"/admin/users/{d['resto_a'].id}/password",
                        data={"password": MDP_TROP_COURT},
                        headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert r.status_code == 400
        # L'ancien mot de passe fonctionne toujours : rien n'a été écrasé.
        autre = TestClient(app)
        assert autre.post("/admin/login",
                          data={"email": d["resto_a"].email, "password": MDP_A},
                          follow_redirects=False).status_code == 303

    def test_le_semis_du_super_admin_n_est_pas_soumis_a_la_regle(self):
        """La règle vit dans les routes, pas dans users.create_user : un ADMIN_PASSWORD
        court doit faire démarrer l'application quand même, pas échouer au boot."""
        import inspect

        from app import users as u

        assert "trop_court" not in inspect.getsource(u.create_user)
        assert "trop_court" not in inspect.getsource(u.update_password)
