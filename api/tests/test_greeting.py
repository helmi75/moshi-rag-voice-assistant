"""Tests du module d'accueil pré-rendu (Phase 3), logique hors-réseau.

Le rendu TTS et le warmup dépendent du réseau (moshi-server) ; on couvre ici la
partie déterministe : chemin de cache et son invalidation, découpage en frames.
"""
import wave

import pytest

from app.tenants import Tenant
from app.voice import greeting as g


def _tenant(greeting="Bonjour, restaurant, que puis-je pour vous ?"):
    return Tenant(
        id=1,
        name="Resto",
        business_type="restaurant",
        phone_number="+33100000000",
        language="fr-FR",
        greeting=greeting,
        knowledge_base="",
    )


def _write_wav(path, seconds=0.5, rate=8000):
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n)
    return path


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GREETING_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("MOSHI_TTS_VOICE", "unmute-prod-website/developpeuse-3.wav")
    return tmp_path


def test_cache_path_changes_with_text_and_voice(monkeypatch):
    p1 = g._cache_path(_tenant("Bonjour A"))
    p2 = g._cache_path(_tenant("Bonjour B"))
    assert p1 != p2, "un texte différent doit donner un fichier de cache différent"

    monkeypatch.setenv("MOSHI_TTS_VOICE", "autre-voix.wav")
    p3 = g._cache_path(_tenant("Bonjour A"))
    assert p3 != p1, "une voix différente doit invalider le cache"


def test_cached_greeting_path_absent_returns_none():
    assert g.cached_greeting_path(_tenant()) is None


def test_cached_greeting_path_ignores_empty_wav(_cache_dir):
    path = g._cache_path(_tenant())
    path.write_bytes(b"\x00" * 40)  # < en-tête WAV : traité comme vide/corrompu
    assert g.cached_greeting_path(_tenant()) is None


def test_cached_greeting_path_returns_valid_wav(_cache_dir):
    path = g._cache_path(_tenant())
    _write_wav(path, seconds=0.5)
    assert g.cached_greeting_path(_tenant()) == path


def test_load_greeting_frames_chunks_20ms(_cache_dir):
    path = g._cache_path(_tenant())
    _write_wav(path, seconds=0.5, rate=8000)  # 0,5 s @ 8 kHz
    frames = g.load_greeting_frames(path, chunk_ms=20)
    # 0,5 s / 20 ms = 25 frames de 160 échantillons (320 octets) chacune
    assert len(frames) == 25
    assert all(f.sample_rate == 8000 and f.num_channels == 1 for f in frames)
    assert all(len(f.audio) == 320 for f in frames)
    total = sum(len(f.audio) for f in frames)
    assert total / 2 / 8000 == pytest.approx(0.5)


def test_is_moshi_server(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "moshi_server")
    assert g.is_moshi_server() is True
    monkeypatch.setenv("TTS_PROVIDER", "pocket")
    assert g.is_moshi_server() is False
