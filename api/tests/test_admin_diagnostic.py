"""Écran de diagnostic : réécouter un appel, et surtout ne pas réécouter celui d'un autre (#88).

Cet écran sert **la voix** d'un appelant — la donnée la plus sensible du produit. Les
tests d'autorisation passent donc avant les tests de rendu.
"""
import asyncio
import base64
import json as _json
import struct

import pytest
from fastapi.testclient import TestClient

from app import calls, db, tenants, users
from app.main import app
from app.voice import enregistrement


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def dossier_audio(tmp_path, monkeypatch):
    monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("ENREGISTREMENT_APPELS", "1")
    monkeypatch.setenv("RGPD_MENTION", "1")
    yield


def _connexion(client, email="admin@test.local", mot_de_passe="test-admin-pass"):
    assert client.post("/admin/login", data={"email": email, "password": mot_de_passe},
                       follow_redirects=False).status_code == 303
    return client


JOURNAL = {
    "version": 1, "tronque": False,
    "enregistrement": {"actif": True, "octets": 1000, "chunks_perdus": 0, "raison": None},
    "compteurs": {"tours": 1, "interruptions": 1, "revisions_stt": 2, "finales_vides": 0},
    "tours": [{"n": 1, "t_ms": 72400, "blanc_ms": 2000, "stt_ms": 200, "llm_ms": 500,
               "tts_ms": 1100, "outil_ms": 0, "non_attribue_ms": 200,
               "entendu": "une table pour deux", "dit": "Bien sûr.", "confiance": 0.62,
               "interruption": "client_coupe_bot",
               "smart_turn": {"complet": True, "proba": 0.62, "ms": 34}}],
    "evenements": [],
}


def _appel(tenant_id, *, sid, journal=None, octets=None, audio=False):
    call_id = calls.start_call(sid, tenant_id, "+33612345678")
    calls.finish_call(sid, "completed", [{"role": "user", "content": "bonjour"}],
                      None, [2000], journal, octets)
    if audio:
        for piste in enregistrement.PISTES:
            chemin = enregistrement.chemin(tenant_id, call_id, piste)
            chemin.parent.mkdir(parents=True, exist_ok=True)
            chemin.write_bytes(bytes(range(256)) * 4)  # 1024 octets µ-law
    return call_id


class TestPersonneNEcouteLAppelDUnAutre:
    """Le test qui compte. Un enregistrement, c'est la voix d'un client — celle d'un
    restaurant concurrent, le cas échéant."""

    @pytest.fixture()
    def deux_etablissements(self):
        a = tenants.create_tenant("Chez A", "+33199001001")
        b = tenants.create_tenant("Chez B", "+33199001002")
        user_b = users.create_user(f"b-{b.id}@test.fr", "resto-pass-diag",
                                   users.ROLE_RESTAURATEUR, b.id)
        yield a, b, user_b
        tenants.delete_tenant(a.id)
        tenants.delete_tenant(b.id)

    def test_le_diagnostic_d_un_autre_est_refuse(self, client, deux_etablissements):
        a, _, user_b = deux_etablissements
        chez_a = _appel(a.id, sid="CA-diag-a", journal=JOURNAL, octets=1000, audio=True)
        _connexion(client, user_b.email, "resto-pass-diag")
        assert client.get(f"/admin/calls/{chez_a}/diagnostic").status_code == 403

    def test_l_audio_d_un_autre_est_refuse(self, client, deux_etablissements):
        a, _, user_b = deux_etablissements
        chez_a = _appel(a.id, sid="CA-audio-a", journal=JOURNAL, octets=1000, audio=True)
        _connexion(client, user_b.email, "resto-pass-diag")
        reponse = client.get(f"/admin/calls/{chez_a}/audio.wav?piste=stereo")
        assert reponse.status_code == 403
        assert b"RIFF" not in reponse.content

    @pytest.mark.parametrize("piste", ["", "autre", "../appelant", "APPELANT", "stereo2"])
    def test_une_piste_hors_liste_blanche_donne_404(self, client, piste):
        """404 et surtout PAS 422 : `test_admin_security.py` compte un 422 comme
        « l'autorisation n'a pas été atteinte », donc comme une fuite non prouvée.
        C'est pourquoi `piste` est un paramètre de requête validé à la main."""
        tenant = tenants.create_tenant("Chez Piste", "+33199001003")
        try:
            appel = _appel(tenant.id, sid="CA-piste", audio=True)
            _connexion(client)
            r = client.get(f"/admin/calls/{appel}/audio.wav?piste={piste}")
            assert r.status_code == 404
        finally:
            tenants.delete_tenant(tenant.id)

    def test_un_appel_inexistant_donne_404(self, client):
        _connexion(client)
        assert client.get("/admin/calls/999999/diagnostic").status_code == 404
        assert client.get("/admin/calls/999999/audio.wav").status_code == 404


class TestLAudioServi:
    @pytest.fixture()
    def tenant(self):
        t = tenants.create_tenant("Chez Audio", "+33199001010")
        yield t
        tenants.delete_tenant(t.id)

    def test_le_stereo_porte_les_deux_pistes(self, client, tenant):
        appel = _appel(tenant.id, sid="CA-son", journal=JOURNAL, octets=2048, audio=True)
        _connexion(client)
        r = client.get(f"/admin/calls/{appel}/audio.wav?piste=stereo")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WAVE"
        canaux = struct.unpack("<H", r.content[22:24])[0]
        assert canaux == 2
        # 1024 octets µ-law → 1024 échantillons → 2 canaux × 2 octets.
        assert struct.unpack("<I", r.content[40:44])[0] == 1024 * 2 * 2

    def test_une_piste_seule_est_mono(self, client, tenant):
        appel = _appel(tenant.id, sid="CA-mono", octets=1024, audio=True)
        _connexion(client)
        r = client.get(f"/admin/calls/{appel}/audio.wav?piste=appelant")
        assert struct.unpack("<H", r.content[22:24])[0] == 1
        assert struct.unpack("<I", r.content[40:44])[0] == 1024 * 2

    def test_sans_fichier_c_est_404_pas_un_wav_vide(self, client, tenant):
        """Un WAV de zéro octet se lirait comme un enregistrement silencieux — donc
        comme un appel où personne n'a parlé."""
        appel = _appel(tenant.id, sid="CA-sansaudio", journal=JOURNAL)
        _connexion(client)
        assert client.get(f"/admin/calls/{appel}/audio.wav").status_code == 404


class TestLaPageDeDiagnostic:
    @pytest.fixture()
    def tenant(self):
        t = tenants.create_tenant("Chez Page", "+33199001020")
        yield t
        tenants.delete_tenant(t.id)

    def test_la_chronologie_montre_la_decomposition(self, client, tenant):
        """Le point du chantier : voir QUEL maillon a pris le temps."""
        appel = _appel(tenant.id, sid="CA-page", journal=JOURNAL, octets=1000, audio=True)
        _connexion(client)
        page = client.get(f"/admin/calls/{appel}/diagnostic")
        assert page.status_code == 200
        texte = page.text
        assert "Transcription" in texte and "Compréhension" in texte and "Voix" in texte
        assert "1:12" in texte            # horodatage de 72 400 ms
        assert "une table pour deux" in texte
        assert "Le client a coupé" in texte
        assert "Transcription peu sûre" in texte  # confiance 0.62

    def test_le_lecteur_audio_est_servi_depuis_l_origine(self, client, tenant):
        """La CSP autorise `media-src 'self'` : un blob ou un CDN ne passerait pas."""
        appel = _appel(tenant.id, sid="CA-lecteur", journal=JOURNAL, octets=1000, audio=True)
        _connexion(client)
        texte = client.get(f"/admin/calls/{appel}/diagnostic").text
        assert f'src="/admin/calls/{appel}/audio.wav?piste=stereo"' in texte
        assert "<audio controls" in texte

    def test_sans_journal_on_dit_pas_de_mesure_pas_des_zeros(self, client, tenant):
        """Les appels antérieurs à la mesure n'en ont pas. Afficher des zéros donnerait
        à croire à un appel parfait."""
        appel = _appel(tenant.id, sid="CA-vieux")
        _connexion(client)
        texte = client.get(f"/admin/calls/{appel}/diagnostic").text
        assert "Pas de mesure pour cet appel" in texte
        assert "Pas d'enregistrement pour cet appel" in texte

    def test_la_raison_de_l_absence_d_enregistrement_est_affichee(self, client, tenant):
        """« Pas d'enregistrement » sans motif ne se diagnostique pas."""
        journal = {**JOURNAL, "enregistrement": {"actif": False, "octets": 0,
                                                 "chunks_perdus": 0, "raison": "disque"}}
        appel = _appel(tenant.id, sid="CA-raison", journal=journal)
        _connexion(client)
        assert "disque" in client.get(f"/admin/calls/{appel}/diagnostic").text

    def test_un_journal_corrompu_ne_casse_pas_la_page(self, client, tenant):
        """C'est la page qu'on ouvre quand quelque chose va mal : elle doit tenir."""
        appel = _appel(tenant.id, sid="CA-corrompu")
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET journal = ? WHERE id = ?",
                         ("{ceci n'est pas du json", appel))
        _connexion(client)
        assert client.get(f"/admin/calls/{appel}/diagnostic").status_code == 200

    def test_le_panneau_ne_propose_le_lien_que_s_il_y_a_a_voir(self, client, tenant):
        avec = _appel(tenant.id, sid="CA-avec", journal=JOURNAL, octets=1000)
        sans = _appel(tenant.id, sid="CA-sans")
        _connexion(client)
        assert f"/admin/calls/{avec}/diagnostic" in client.get(f"/admin/calls/{avec}").text
        assert f"/admin/calls/{sans}/diagnostic" not in client.get(f"/admin/calls/{sans}").text
