"""Config voix admin : aperçu greeting, statut, upload musique d'attente."""
import io
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import tenants, users
from app.main import app
from app.voice import greeting as greeting_mod


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
