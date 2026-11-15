"""Les traces de la recette d'appels automatique (ASSISTANTE-192).

Aucun réseau : on archive des lignes écrites à la main, sur des pistes µ-law synthétiques.
On vérifie surtout deux choses : ce qui est archivé se relit (conversation et audio), et la
page n'est lisible que du super-admin (403 pour un restaurateur)."""
import asyncio
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, recettes_archive, rgpd, tenants, users
from app.main import app
from app.recette import __main__ as recette_main
from app.recette import etage2, verdicts


@pytest.fixture()
def archives(tmp_path, monkeypatch):
    monkeypatch.setenv("RECETTE_ARCHIVE_DIR", str(tmp_path / "archives"))
    monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path / "enregistrements"))
    yield tmp_path
    with db.get_conn() as conn:
        conn.execute("DELETE FROM recette_scenarios")
        conn.execute("DELETE FROM recettes")


def _piste(tmp_path, tenant_id, call_id, piste, octets=b"\xff\x7f" * 400):
    from app.voice import enregistrement

    chemin = enregistrement.chemin(tenant_id, call_id, piste)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(octets)
    return chemin


def _lignes(audio=None):
    return [
        {"etage": 2, "moteur": "gpt_live", "scenario": "t17a", "recette": "T17a", "titre": "Réserver",
         "etat": "ok", "preuve": "table créée", "cout": 0.12,
         "conversation": ["client : bonjour, une table pour deux", "elle : très bien, pour quand ?"],
         "audio": audio},
        {"etage": 1, "moteur": "classique", "scenario": "c8", "recette": "C8", "titre": "Annuler",
         "etat": "echec", "preuve": "rien annulé", "cout": 0.03, "conversation": []},
    ]


class TestL_archive:
    def test_un_passage_archive_se_relit_avec_conversation_et_audio(self, archives):
        nom = "a" * 32
        identifiant = recettes_archive.archiver(_lignes(audio=nom), 0.15, "boot-1", True)
        recette = recettes_archive.obtenir(identifiant)
        assert (recette["version"], recette["scenarios"], recette["echecs"], recette["etage2"]) == ("boot-1", 2, 1, 1)
        premier, second = recette["lignes"]
        assert premier["conversation"] == ["client : bonjour, une table pour deux", "elle : très bien, pour quand ?"]
        assert premier["audio"] == nom and second["audio"] is None
        assert [r["id"] for r in recettes_archive.lister()][0] == identifiant
        assert recettes_archive.obtenir(identifiant + 999) is None

    def test_la_purge_efface_passage_scenarios_et_audio_apres_la_conservation_seulement(self, archives):
        nom = recettes_archive.garder_audio(*self._appel(archives))
        vieux = recettes_archive.archiver(_lignes(audio=nom), 0.1, "v", True)
        recent = recettes_archive.archiver(_lignes(), 0.1, "v", False)
        ancien = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.get_conn() as conn:
            conn.execute("UPDATE recettes SET created_at = ? WHERE id = ?", (ancien, vieux))
        fichiers = [recettes_archive.chemin_audio(nom, p) for p in ("appelant", "assistante")]
        assert all(f.exists() for f in fichiers)

        assert recettes_archive.purger(60) == 0          # pas avant la durée de conservation
        assert recettes_archive.obtenir(vieux) is not None and all(f.exists() for f in fichiers)
        assert recettes_archive.purger(30) == 1
        assert recettes_archive.obtenir(vieux) is None and recettes_archive.obtenir(recent) is not None
        assert not any(f.exists() for f in fichiers)
        with db.get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM recette_scenarios WHERE recette_id = ?",
                                (vieux,)).fetchone()[0] == 0

    def test_la_purge_des_donnees_personnelles_purge_aussi_les_recettes(self, archives, monkeypatch):
        vu = []
        monkeypatch.setattr(recettes_archive, "purger", lambda jours: vu.append(jours) or 0)
        monkeypatch.setenv("RETENTION_RECETTE_JOURS", "12")
        rgpd.purger()
        assert vu == [12]

    @staticmethod
    def _appel(archives, tenant_id=77, call_id=5):
        _piste(archives, tenant_id, call_id, "appelant")
        _piste(archives, tenant_id, call_id, "assistante")
        return tenant_id, call_id


class TestLAudio:
    @pytest.mark.parametrize("nom", ["../x", "../" + "a" * 29, "A" * 32, "a" * 31, "a" * 33, "", "a" * 32 + "/.."])
    def test_un_nom_qui_n_est_pas_hexadecimal_est_refuse(self, archives, nom):
        with pytest.raises(ValueError):
            recettes_archive.chemin_audio(nom, "appelant")

    def test_une_piste_inconnue_est_refusee(self, archives):
        with pytest.raises(ValueError):
            recettes_archive.chemin_audio("a" * 32, "../appelant")

    def test_garder_audio_copie_les_pistes_et_rend_un_nom(self, archives):
        _piste(archives, 9, 3, "appelant", b"\x01\x02")
        _piste(archives, 9, 3, "assistante", b"\x03\x04")
        nom = recettes_archive.garder_audio(9, 3)
        assert len(nom) == 32
        assert recettes_archive.chemin_audio(nom, "appelant").read_bytes() == b"\x01\x02"
        assert recettes_archive.chemin_audio(nom, "assistante").read_bytes() == b"\x03\x04"

    def test_sans_piste_garder_audio_rend_rien(self, archives):
        assert recettes_archive.garder_audio(9, 404) is None

    def test_une_erreur_disque_ne_fait_pas_echouer_la_recette(self, archives, monkeypatch):
        _piste(archives, 9, 3, "appelant")

        def plein(*_a, **_k):
            raise OSError("disque plein")

        monkeypatch.setattr(recettes_archive.shutil, "copyfile", plein)
        assert recettes_archive.garder_audio(9, 3) is None


class TestLaConversationComplete:
    def test_aucun_tour_n_est_tronque(self):
        long = "mot " * 100
        appel = {"transcript": json.dumps([{"role": "user", "content": long},
                                           {"role": "assistant", "content": "ok"},
                                           {"role": "system", "content": "caché"}])}
        complete = verdicts.conversation_complete(appel)
        assert len(complete) == 2 and complete[0] == "client : " + long.strip()
        assert verdicts.conversation_complete(None) == []


@pytest.fixture()
def client():
    return TestClient(app)


def _connecter(client, email=None, mdp=None):
    r = client.post("/admin/login", data={"email": email or os.environ["ADMIN_EMAIL"],
                                          "password": mdp or os.environ["ADMIN_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 303


class TestLesPages:
    def test_le_super_admin_voit_la_liste_le_detail_et_ecoute_l_audio(self, client, archives):
        nom = recettes_archive.garder_audio(*TestL_archive._appel(archives))
        identifiant = recettes_archive.archiver(_lignes(audio=nom), 0.15, "boot-1", True)
        scenario = recettes_archive.obtenir(identifiant)["lignes"][0]
        _connecter(client)

        liste = client.get("/admin/recettes")
        assert liste.status_code == 200 and f"/admin/recettes/{identifiant}" in liste.text
        assert "❌ 1 en échec sur 2" in liste.text
        assert 'href="/admin/recettes"' in client.get("/admin/").text
        detail = client.get(f"/admin/recettes/{identifiant}")
        assert detail.status_code == 200
        assert "client : bonjour, une table pour deux" in detail.text
        assert f'src="/admin/recettes/audio/{scenario["id"]}.wav"' in detail.text
        sonore = client.get(f"/admin/recettes/audio/{scenario['id']}.wav")
        assert sonore.status_code == 200 and sonore.headers["content-type"] == "audio/wav"
        assert sonore.content[:4] == b"RIFF"

    def test_un_identifiant_inconnu_ou_sans_audio_est_404(self, client, archives):
        identifiant = recettes_archive.archiver(_lignes(), 0.1, "v", True)
        sans_audio = recettes_archive.obtenir(identifiant)["lignes"][0]["id"]
        _connecter(client)
        assert client.get("/admin/recettes/987654").status_code == 404
        assert client.get("/admin/recettes/audio/987654.wav").status_code == 404
        assert client.get(f"/admin/recettes/audio/{sans_audio}.wav").status_code == 404

    def test_un_restaurateur_recoit_403_partout(self, client, archives):
        t = tenants.create_tenant("Chez Recette", "+33100000192")
        mdp = "-".join(("compte", "recette", "sans", "valeur"))
        u = users.create_user(f"resto-{t.id}@test.fr", mdp, users.ROLE_RESTAURATEUR, t.id)
        try:
            nom = recettes_archive.garder_audio(*TestL_archive._appel(archives))
            identifiant = recettes_archive.archiver(_lignes(audio=nom), 0.1, "v", True)
            scenario = recettes_archive.obtenir(identifiant)["lignes"][0]["id"]
            _connecter(client, u.email, mdp)
            assert client.get("/admin/recettes").status_code == 403
            assert client.get(f"/admin/recettes/{identifiant}").status_code == 403
            assert client.get(f"/admin/recettes/audio/{scenario}.wav").status_code == 403
            assert 'href="/admin/recettes"' not in client.get("/admin/").text
        finally:
            tenants.delete_tenant(t.id)


class TestLePassageNeCassePas:
    @pytest.fixture()
    def carnet(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RECETTE_CARNET", str(tmp_path / "recette" / "depenses.json"))

    def test_meme_quand_l_archive_leve_le_carnet_est_note_et_le_passage_aboutit(self, carnet, monkeypatch):
        from app.recette import budget

        def casse(*_a, **_k):
            raise RuntimeError("base verrouillée")

        monkeypatch.setattr(recettes_archive, "archiver", casse)
        avant = {"lignes": [{"etage": 1, "scenario": "t17a", "etat": "ok", "cout": 0.05}]}
        resultat = asyncio.run(recette_main._etage2(avant, None, True))
        assert resultat["cout"] == 0.05
        assert budget.lire()[-1]["cout"] == 0.05

    def test_le_passage_est_archive_avec_ses_conversations(self, carnet, archives, monkeypatch):
        avant = {"lignes": [{"etage": 1, "moteur": "classique", "scenario": "t17a", "recette": "T17a",
                             "titre": "Réserver", "etat": "ok", "cout": 0.05,
                             "conversation": ["client : bonjour"]}]}
        asyncio.run(recette_main._etage2(avant, None, True))
        dernier = recettes_archive.obtenir(recettes_archive.lister()[0]["id"])
        assert dernier["etage2"] == 0 and dernier["lignes"][0]["conversation"] == ["client : bonjour"]

    def test_etage1_tiret_lit_l_entree_standard(self, carnet, archives, monkeypatch, capsys):
        avant = {"lignes": [{"etage": 1, "scenario": "t17a", "recette": "T17a", "titre": "x",
                             "etat": "ok", "cout": 0.01, "conversation": ["client : salut"]}]}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(avant)))
        assert recette_main.main(["etage2", "--etage1", "-", "--sans-appels"]) == 0
        sortie = capsys.readouterr().out
        resultat = json.loads(sortie.split("RESULTAT ", 1)[1])
        assert resultat["lignes"][0]["scenario"] == "t17a"
        assert resultat["lignes"][0]["conversation"] == ["client : salut"]


class TestLeScript:
    def test_le_resultat_de_l_etage_1_passe_par_l_entree_standard_et_le_rapport_est_ecrit(
            self, monkeypatch, tmp_path, capsys):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
        import recette_appels

        vus = []
        resultat = {"lignes": [{"etage": 1, "moteur": "classique", "scenario": "t17a", "recette": "T17a",
                                "etat": "ok", "preuve": "ok", "conversation": ["client : salut"]}],
                    "cout": 0.01, "du_mois": 1.0, "cerveau": {}}

        def faux(commande, attente, entree=None):
            vus.append((commande, entree))
            if commande.endswith("recette etat"):
                return 'RESULTAT {"du_mois": 1.0, "plafond_mois": 30, "plafond_recette": 3, "active": false}'
            if "app.recette etage2" in commande:
                return "| T17a |\nRESULTAT " + json.dumps(resultat)
            return "RESULTAT " + json.dumps({"lignes": [{"scenario": "x" * 300_000}]})

        monkeypatch.setattr(recette_appels, "sur_le_serveur", faux)
        monkeypatch.setattr(recette_appels, "__file__", str(tmp_path / "scripts" / "recette_appels.py"))
        monkeypatch.setattr(sys, "argv", ["recette_appels.py"])
        assert recette_appels.main() == 0
        commande, entree = vus[-1]
        assert commande.endswith("--etage1 - --sans-appels") and "x" * 100 not in commande
        assert len(entree) > 300_000
        rapports = list((tmp_path / "local" / "recettes").glob("recette-*.txt"))
        assert len(rapports) == 1 and "client : salut" in rapports[0].read_text(encoding="utf-8")
        assert str(rapports[0]) in capsys.readouterr().out
