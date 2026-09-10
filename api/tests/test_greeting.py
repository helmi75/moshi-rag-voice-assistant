"""Tests du module d'accueil pré-rendu (Phase 3), logique hors-réseau.

Le rendu TTS et le warmup dépendent du réseau (moshi-server) ; on couvre ici la
partie déterministe : chemin de cache et son invalidation, découpage en frames.
"""
import wave

import pytest

from app.tenants import Tenant
from app.voice import greeting as g


def _tenant(greeting="Bonjour, restaurant, que puis-je pour vous ?", voice=None):
    return Tenant(
        id=1,
        name="Resto",
        business_type="restaurant",
        phone_number="+33100000000",
        language="fr-FR",
        greeting=greeting,
        knowledge_base="",
        voice=voice,
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


def test_cache_path_changes_with_text_and_voice():
    """C'est ce qui rend le changement de voix sûr : l'accueil rendu dans l'ancienne
    voix n'est plus jamais retrouvé, donc jamais rejoué par-dessus la nouvelle."""
    from app.voice import voices

    p1 = g._cache_path(_tenant("Bonjour A"))
    p2 = g._cache_path(_tenant("Bonjour B"))
    assert p1 != p2, "un texte différent doit donner un fichier de cache différent"

    autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
    p3 = g._cache_path(_tenant("Bonjour A", voice=autre.id))
    assert p3 != p1, "une voix différente doit invalider le cache"


def test_cache_path_ignores_a_voice_outside_catalogue():
    """Une voix inconnue ne DOIT PAS changer le cache : le serveur la remplacerait en
    silence par sa voix de repli, et on servirait un accueil qui n'est plus celui de
    l'appel. On reste sur la voix par défaut, donc sur l'accueil déjà rendu."""
    p1 = g._cache_path(_tenant("Bonjour A"))
    p2 = g._cache_path(_tenant("Bonjour A", voice="dossier-bidon/inconnue.wav"))
    assert p1 == p2


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


# --- GPU chaud : ni réveil ni musique au décroché ------------------------------------


def test_gpu_chaud_seulement_dans_la_fenetre(monkeypatch):
    import time

    from app.voice import moshi_server_tts as m

    monkeypatch.delenv("MOSHI_CHAUD_SECONDES", raising=False)
    monkeypatch.setattr(m, "_dernier_son", None)
    assert not m.gpu_chaud(), "sans aucun son rendu, on ne sait pas : on réveille"
    m.noter_son()
    assert m.gpu_chaud()
    # Au-delà de 75 s, on n'est plus sûr que Modal (120 s) ne l'a pas éteint.
    monkeypatch.setattr(m, "_dernier_son", time.monotonic() - 80)
    assert not m.gpu_chaud()
    monkeypatch.setenv("MOSHI_CHAUD_SECONDES", "0")
    m.noter_son()
    assert not m.gpu_chaud(), "0 désactive le raccourci"


def test_texte_de_reprise_selon_l_attente(monkeypatch):
    monkeypatch.delenv("MOSHI_RESUME_TEXT", raising=False)
    monkeypatch.delenv("MOSHI_RESUME_TEXT_CHAUD", raising=False)
    assert g.texte_de_reprise(True) == "Je vous écoute."
    assert g.texte_de_reprise(False) == "Merci d'avoir patienté, je vous écoute."


def _intro(monkeypatch, chaud, secondes_accueil=0.5):
    """Joue l'intro avec de faux transport/tâche ; renvoie (réveils, textes dits,
    trames audio envoyées, attentes)."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setenv("TTS_PROVIDER", "moshi_server")
    reveils, attentes = [], []

    async def faux_reveil():
        reveils.append(1)

    async def fausse_attente(secondes):
        attentes.append(secondes)

    monkeypatch.setattr(g, "warmup_moshi_server", faux_reveil)
    monkeypatch.setattr(g, "load_hold_music_chunks", lambda **kw: (8000, []))
    monkeypatch.setattr(g.asyncio, "sleep", fausse_attente)
    _write_wav(g._cache_path(_tenant()), seconds=secondes_accueil)
    task = SimpleNamespace(queue_frames=AsyncMock())
    sortie = SimpleNamespace(send_audio=AsyncMock())
    asyncio.run(g.run_switchboard_intro(task, sortie, _tenant(), None, chaud=chaud))
    frames = [f for appel in task.queue_frames.await_args_list for f in appel.args[0]]
    textes = [f.text for f in frames if type(f).__name__ == "TTSSpeakFrame"]
    assert type(frames[-1]).__name__ == "STTMuteFrame" and frames[-1].mute is False
    return reveils, textes, sortie.send_audio.await_count, attentes


def test_intro_gpu_chaud_ni_reveil_ni_musique(monkeypatch):
    reveils, textes, trames, _ = _intro(monkeypatch, chaud=True)
    assert reveils == [], "le réveil ouvrait une connexion GPU de plus par appel"
    assert textes == ["Je vous écoute."]
    assert trames == 25, "l'accueil seul (0,5 s en trames de 20 ms), aucune musique"


def test_intro_gpu_chaud_attend_la_fin_de_l_accueil(monkeypatch):
    """`send_audio` rend la main aussitôt : sans attente, la reprise partirait pendant
    l'accueil et le client pourrait parler par-dessus la mention d'information."""
    _, _, _, attentes = _intro(monkeypatch, chaud=True, secondes_accueil=2.0)
    assert len(attentes) == 1
    assert attentes[0] == pytest.approx(2.0 - g._AVANCE_REPRISE, abs=0.2)


def test_intro_gpu_froid_reveille_et_remercie(monkeypatch):
    reveils, textes, _, _ = _intro(monkeypatch, chaud=False)
    assert reveils == [1]
    assert textes == ["Merci d'avoir patienté, je vous écoute."]
