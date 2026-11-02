"""Export des données d'un établissement (ASSISTANTE-155) : contenu, cloisonnement,
secrets absents, enregistrements décodés, fichier temporaire supprimé."""
import csv
import io
import json
import os
import wave
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import calls, db, export, messages, reservations, tenants, users
from app.main import app
from app.voice import enregistrement

MDP_RESTO = "-".join(("export", "resto", "jetable"))


@pytest.fixture()
def deux_restaurants(tmp_path, monkeypatch):
    monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path))
    suffixe = id(object()) % 10_000_000
    a = tenants.create_tenant("Chez Export", f"+3363{suffixe:07d}")
    b = tenants.create_tenant("Voisin Confidentiel", f"+3364{suffixe:07d}")
    resto_a = users.create_user(f"expa-{a.id}@test.fr", MDP_RESTO,
                                users.ROLE_RESTAURATEUR, a.id)
    users.create_user(f"expb-{b.id}@test.fr", MDP_RESTO, users.ROLE_RESTAURATEUR, b.id)
    resa = reservations.create_reservation(a.id, "Client Un", "2026-09-01", "20:00", 2)
    annulee = reservations.create_reservation(a.id, "Client Annulé", "2026-09-02", "19:30", 4)
    with db.get_conn() as conn:
        conn.execute("UPDATE reservations SET cancelled_at = '2026-09-01T10:00:00Z' "
                     "WHERE id = ?", (annulee["id"],))
    reservations.create_reservation(b.id, "Secret de B", "2026-09-03", "20:00", 2)
    calls.start_call(f"CA-exp-a-{suffixe}", a.id, "+33600000001")
    calls.finish_call(f"CA-exp-a-{suffixe}", "completed")
    calls.start_call(f"CA-exp-b-{suffixe}", b.id, "+33600000002")
    messages.create_message(a.id, "Rappeler M. Dupont")
    messages.create_message(b.id, "Message confidentiel de B")
    appel_a = calls.list_calls(a.id)[0]
    yield {"a": a, "b": b, "resto_a": resto_a, "appel_a": appel_a, "resa": resa}
    tenants.delete_tenant(a.id)
    tenants.delete_tenant(b.id)


def _client(email="admin@test.local", mdp="test-admin-pass") -> TestClient:
    client = TestClient(app)
    reponse = client.post("/admin/login", data={"email": email, "password": mdp},
                          follow_redirects=False)
    assert reponse.status_code == 303
    return client


def _lignes(archive: zipfile.ZipFile, nom: str) -> list[dict]:
    texte = archive.read(nom).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(texte), delimiter=";"))


def _archive(tenant_id: int) -> zipfile.ZipFile:
    chemin = export.construire(tenant_id)
    donnees = chemin.read_bytes()
    chemin.unlink()
    return zipfile.ZipFile(io.BytesIO(donnees))


class TestContenu:
    def test_l_archive_contient_les_fichiers_attendus(self, deux_restaurants):
        z = _archive(deux_restaurants["a"].id)
        assert {"LISEZMOI.txt", "etablissement.json", "reservations.csv", "appels.csv",
                "messages.csv", "comptes.csv"} <= set(z.namelist())
        assert "Chez Export" in z.read("LISEZMOI.txt").decode("utf-8")

    def test_les_csv_ont_le_bom_et_le_point_virgule(self, deux_restaurants):
        brut = _archive(deux_restaurants["a"].id).read("reservations.csv")
        assert brut.startswith("﻿".encode("utf-8"))
        assert b";" in brut.split(b"\r\n")[0]

    def test_les_reservations_annulees_figurent_dans_l_archive(self, deux_restaurants):
        lignes = _lignes(_archive(deux_restaurants["a"].id), "reservations.csv")
        assert {r["customer_name"] for r in lignes} == {"Client Un", "Client Annulé"}
        annulee = next(r for r in lignes if r["customer_name"] == "Client Annulé")
        assert annulee["cancelled_at"]

    def test_appels_et_messages_sont_relus(self, deux_restaurants):
        z = _archive(deux_restaurants["a"].id)
        assert [a["id"] for a in _lignes(z, "appels.csv")] == [
            str(deux_restaurants["appel_a"]["id"])]
        assert [m["subject"] for m in _lignes(z, "messages.csv")] == ["Rappeler M. Dupont"]

    def test_la_fiche_est_exportee_en_json(self, deux_restaurants):
        fiche = json.loads(_archive(deux_restaurants["a"].id).read("etablissement.json"))
        assert fiche["name"] == "Chez Export"

    def test_un_etablissement_resos_est_signale_dans_le_lisez_moi(self, deux_restaurants):
        tenant = deux_restaurants["a"]
        tenants.update_tenant(tenant.id, booking_provider="resos")
        texte = _archive(tenant.id).read("LISEZMOI.txt").decode("utf-8")
        assert "resOS" in texte and "font foi" in texte


class TestCloisonnementDeLArchive:
    def test_rien_d_un_autre_etablissement_dans_l_archive(self, deux_restaurants):
        z = _archive(deux_restaurants["a"].id)
        tout = b"".join(z.read(n) for n in z.namelist()).decode("utf-8", "replace")
        for intrus in ("Secret de B", "Voisin Confidentiel", "Message confidentiel de B",
                       "+33600000002", f"expb-{deux_restaurants['b'].id}@test.fr"):
            assert intrus not in tout

    def test_aucun_mot_de_passe_ni_empreinte_dans_l_archive(self, deux_restaurants):
        z = _archive(deux_restaurants["a"].id)
        assert _lignes(z, "comptes.csv")[0].keys() == {"email", "role", "created_at"}
        tout = b"".join(z.read(n) for n in z.namelist()).decode("utf-8", "replace")
        assert "password" not in tout.lower()
        assert "$2b$" not in tout and "$2a$" not in tout


class TestEnregistrements:
    def test_un_appel_avec_ses_pistes_donne_un_wav_valide(self, deux_restaurants):
        a, appel = deux_restaurants["a"], deux_restaurants["appel_a"]
        for piste in enregistrement.PISTES:
            chemin = enregistrement.chemin(a.id, appel["id"], piste)
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_bytes(b"\xff" * 800)
        z = _archive(a.id)
        nom = f"enregistrements/appel{appel['id']}.wav"
        assert nom in z.namelist()
        with wave.open(io.BytesIO(z.read(nom))) as wav:
            assert wav.getnchannels() == 2
            assert wav.getframerate() == 8000
            assert wav.getnframes() == 800

    def test_un_appel_sans_piste_n_a_pas_de_wav(self, deux_restaurants):
        z = _archive(deux_restaurants["a"].id)
        assert not [n for n in z.namelist() if n.startswith("enregistrements/")]


class TestLaRoute:
    def test_le_restaurateur_exporte_son_etablissement(self, deux_restaurants):
        client = _client(deux_restaurants["resto_a"].email, MDP_RESTO)
        a = deux_restaurants["a"]
        r = client.get(f"/admin/tenants/{a.id}/export.zip")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert f"helmane-export-{a.id}-" in r.headers["content-disposition"]
        assert zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None

    def test_le_restaurateur_n_exporte_pas_un_autre_etablissement(self, deux_restaurants):
        client = _client(deux_restaurants["resto_a"].email, MDP_RESTO)
        r = client.get(f"/admin/tenants/{deux_restaurants['b'].id}/export.zip")
        assert r.status_code == 403
        assert b"Secret de B" not in r.content

    def test_le_super_admin_exporte_n_importe_quel_etablissement(self, deux_restaurants):
        r = _client().get(f"/admin/tenants/{deux_restaurants['b'].id}/export.zip")
        assert r.status_code == 200

    def test_sans_session_on_est_renvoye_a_la_connexion(self, deux_restaurants):
        r = TestClient(app).get(f"/admin/tenants/{deux_restaurants['a'].id}/export.zip",
                                follow_redirects=False)
        assert r.status_code in (303, 307)
        assert r.headers["location"].startswith("/admin/login")

    def test_le_fichier_temporaire_est_supprime_apres_la_reponse(
            self, deux_restaurants, monkeypatch):
        faits: list[Path] = []
        originale = export.construire

        def espion(tenant_id):
            chemin = originale(tenant_id)
            faits.append(chemin)
            return chemin

        monkeypatch.setattr(export, "construire", espion)
        r = _client().get(f"/admin/tenants/{deux_restaurants['a'].id}/export.zip")
        assert r.status_code == 200
        assert len(faits) == 1 and not os.path.exists(faits[0])

    def test_la_fiche_propose_le_bouton(self, deux_restaurants):
        a = deux_restaurants["a"]
        page = _client(deux_restaurants["resto_a"].email, MDP_RESTO).get(
            f"/admin/tenants/{a.id}/edit")
        assert f"/admin/tenants/{a.id}/export.zip" in page.text
        assert "Exporter toutes les données" in page.text


class TestCeQueLArchiveTait:
    """Relecture d'ASSISTANTE-155 : l'archive ne montre pas plus que l'admin."""

    def test_ni_journal_ni_couts_par_poste_ni_fournisseur(self, deux_restaurants):
        a = deux_restaurants["a"]
        tenants.update_tenant(a.id, moteur_voix="gpt_live")
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET voix_fournisseur = 'gpt-live', journal = 'trace', "
                         "cout_voix = 0.01 WHERE tenant_id = ?", (a.id,))
        z = _archive(a.id)
        colonnes = set(_lignes(z, "appels.csv")[0])
        assert not colonnes & {"journal", "voix_fournisseur", "cout_telephonie",
                               "cout_transcription", "cout_comprehension", "cout_voix"}
        assert "estimated_cost" in colonnes
        assert "moteur_voix" not in json.loads(z.read("etablissement.json"))
        tout = b"".join(z.read(n) for n in z.namelist()).decode("utf-8", "replace").lower()
        assert "gpt" not in tout and "openai" not in tout

    def test_une_formule_dictee_ne_s_execute_pas_dans_excel(self, deux_restaurants):
        a = deux_restaurants["a"]
        reservations.create_reservation(a.id, '=HYPERLINK("http://x","clic")', "2026-09-04",
                                        "20:00", 2, customer_phone="+33 6 12 34 56 78")
        lignes = _lignes(_archive(a.id), "reservations.csv")
        piege = next(r for r in lignes if "HYPERLINK" in r["customer_name"])
        assert piege["customer_name"].startswith("'=")
        assert piege["customer_phone"] == "+33 6 12 34 56 78"     # un numéro reste un numéro

    @pytest.mark.parametrize("valeur,attendu", [
        ("@SUM(A1)", "'@SUM(A1)"), ("-2+3", "'-2+3"), ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
        ("-12.5", "-12.5"), ("Dupont", "Dupont"), (None, "")])
    def test_les_cellules_dangereuses_sont_neutralisees(self, valeur, attendu):
        assert export._cellule(valeur) == attendu


class TestFichiersTemporaires:
    def test_un_orphelin_d_une_heure_est_supprime(self, deux_restaurants, tmp_path, monkeypatch):
        monkeypatch.setattr(export, "_DOSSIER", tmp_path / "exports")
        (tmp_path / "exports").mkdir()
        vieux = tmp_path / "exports" / "helmane-export-vieux.zip"
        recent = tmp_path / "exports" / "helmane-export-recent.zip"
        vieux.write_bytes(b"x")
        recent.write_bytes(b"x")
        os.utime(vieux, (0, 0))
        export.construire(deux_restaurants["a"].id).unlink()
        assert not vieux.exists() and recent.exists()

    def test_une_erreur_pendant_la_construction_ne_laisse_rien(self, deux_restaurants,
                                                               tmp_path, monkeypatch):
        monkeypatch.setattr(export, "_DOSSIER", tmp_path / "exports")

        def panne(*_):
            raise RuntimeError("panne")

        monkeypatch.setattr(export, "_csv", panne)
        with pytest.raises(RuntimeError):
            export.construire(deux_restaurants["a"].id)
        assert list((tmp_path / "exports").iterdir()) == []

    def test_disque_trop_plein_l_export_est_refuse(self, deux_restaurants, tmp_path, monkeypatch):
        monkeypatch.setattr(export, "_DOSSIER", tmp_path / "exports")
        monkeypatch.setattr(enregistrement, "espace_libre_mo", lambda *_: 100)
        reponse = _client().get(f"/admin/tenants/{deux_restaurants['a'].id}/export.zip")
        assert reponse.status_code == 507
        assert list((tmp_path / "exports").iterdir()) == []
