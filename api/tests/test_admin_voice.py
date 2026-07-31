"""Config voix admin : aperçu greeting, statut, upload musique d'attente."""
import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import tenants, users
from app.main import app
from app.voice import greeting as greeting_mod
from app.voice import voices


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def resto(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLD_MUSIC_DIR", str(tmp_path / "hold"))
    monkeypatch.setenv("GREETING_CACHE_DIR", str(tmp_path / "greet"))
    tenant = tenants.create_tenant("Voix Test", f"+3362{id(object()) % 10_000_000:07d}")
    yield tenant
    tenants.delete_tenant(tenant.id)


def _login(client):
    resp = client.post("/admin/login",
                       data={"email": "admin@test.local", "password": "test-admin-pass"},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def _csrf(client) -> str:
    client.get("/admin/")
    import base64, json

    raw = client.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.b64decode(raw))["csrf"]


def _wav_bytes(rate=44100, channels=2, seconds=0.2) -> bytes:
    n = int(rate * seconds)
    samples = (np.sin(np.linspace(0, 50, n * channels)) * 20000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


class TestVoiceSettings:
    def test_page_renders(self, client, resto):
        _login(client)
        resp = client.get(f"/admin/tenants/{resto.id}/voice")
        assert resp.status_code == 200
        assert "Musique d'attente" in resp.text

    def test_greeting_wav_404_when_not_rendered(self, client, resto):
        _login(client)
        assert client.get(f"/admin/tenants/{resto.id}/greeting.wav").status_code == 404

    def test_greeting_wav_served_from_cache(self, client, resto, tmp_path):
        # Simule un WAV déjà rendu dans le cache.
        path = greeting_mod._cache_path(resto)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        _login(client)
        resp = client.get(f"/admin/tenants/{resto.id}/greeting.wav")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/wav")

    def test_greeting_status_fragment(self, client, resto):
        _login(client)
        resp = client.get(f"/admin/tenants/{resto.id}/greeting/status")
        assert resp.status_code == 200
        assert "Rendu de la voix en cours" in resp.text


class TestChoixDeVoix:
    """Le sélecteur de voix : ce qu'il propose, ce qu'il accepte d'enregistrer, et
    surtout ce qu'il refuse — une voix hors catalogue serait servie sans erreur par
    moshi-server, avec sa voix de repli, sans que rien ne le signale."""

    def _autre_voix(self):
        return next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)

    def _poster(self, client, tenant_id, **champs):
        return client.post(
            f"/admin/tenants/{tenant_id}/voice",
            data=champs,
            headers={"X-CSRF-Token": _csrf(client)},
            follow_redirects=False,
        )

    def test_le_selecteur_liste_le_catalogue(self, client, resto):
        _login(client)
        page = client.get(f"/admin/tenants/{resto.id}/voice")
        for voice in voices.catalogue():
            assert voice.label in page.text
        # Un restaurateur doit lire un nom, jamais un chemin de fichier.
        assert voices.DEFAULT_VOICE not in page.text.replace(
            f'value="{voices.DEFAULT_VOICE}"', ""
        )

    def test_enregistre_une_voix_du_catalogue(self, client, resto):
        _login(client)
        autre = self._autre_voix()
        assert self._poster(client, resto.id, voice=autre.id).status_code == 303
        assert tenants.get_by_id(resto.id).voice == autre.id

    def test_refuse_une_voix_hors_catalogue(self, client, resto):
        _login(client)
        autre = self._autre_voix()
        self._poster(client, resto.id, voice=autre.id)
        self._poster(client, resto.id, voice="dossier-bidon/inconnue.wav")
        assert tenants.get_by_id(resto.id).voice == autre.id, (
            "une valeur forgée ne doit pas remplacer un réglage valide"
        )

    def test_changer_la_voix_ne_touche_pas_a_l_accueil(self, client, resto):
        _login(client)
        self._poster(client, resto.id, greeting="Bonjour, ici Le Test.")
        self._poster(client, resto.id, voice=self._autre_voix().id)
        assert tenants.get_by_id(resto.id).greeting == "Bonjour, ici Le Test."

    def test_changer_l_accueil_ne_reinitialise_pas_la_voix(self, client, resto):
        _login(client)
        autre = self._autre_voix()
        self._poster(client, resto.id, voice=autre.id)
        self._poster(client, resto.id, greeting="Nouvel accueil.")
        assert tenants.get_by_id(resto.id).voice == autre.id

    def test_la_voix_choisie_invalide_l_accueil_deja_rendu(self, client, resto):
        """Sans ça, l'établissement entendrait l'ancienne voix au décroché et la
        nouvelle dès la première réponse."""
        _login(client)
        avant = greeting_mod._cache_path(resto)
        self._poster(client, resto.id, voice=self._autre_voix().id)
        apres = greeting_mod._cache_path(tenants.get_by_id(resto.id))
        assert avant != apres


class TestHoldMusicUpload:
    def test_upload_stereo_44k_converted_to_mono_8k(self, client, resto):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            f"/admin/tenants/{resto.id}/hold-music",
            files={"file": ("musique.wav", _wav_bytes(44100, 2), "audio/wav")},
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        dest = greeting_mod.hold_music_dir() / f"tenant{resto.id}.wav"
        with wave.open(str(dest), "rb") as w:
            assert w.getframerate() == 8000
            assert w.getnchannels() == 1
        # La résolution par tenant préfère désormais ce fichier.
        assert greeting_mod.hold_music_path(resto.id) == str(dest)

    def test_upload_garbage_rejected(self, client, resto):
        _login(client)
        token = _csrf(client)
        resp = client.post(
            f"/admin/tenants/{resto.id}/hold-music",
            files={"file": ("fake.wav", b"pas un wav du tout", "audio/wav")},
            headers={"X-CSRF-Token": token},
        )
        assert resp.status_code == 422

    def test_delete_returns_to_default(self, client, resto):
        _login(client)
        token = _csrf(client)
        client.post(
            f"/admin/tenants/{resto.id}/hold-music",
            files={"file": ("m.wav", _wav_bytes(8000, 1), "audio/wav")},
            headers={"X-CSRF-Token": token},
        )
        resp = client.post(
            f"/admin/tenants/{resto.id}/hold-music/delete",
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert greeting_mod.hold_music_path(resto.id) == greeting_mod.hold_music_path(None)
