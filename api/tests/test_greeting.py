"""Tests du module d'accueil pré-rendu (Phase 3), logique hors-réseau.

Le rendu TTS dépend du réseau (Mistral) ; on couvre ici la
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
    return tmp_path


def test_cache_path_changes_with_text_and_voice(monkeypatch):
    """C'est ce qui rend le changement de voix sûr : l'accueil rendu dans l'ancienne
    voix n'est plus jamais retrouvé, donc jamais rejoué par-dessus la nouvelle."""
    from app.voice import voices

    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    p1 = g._cache_path(_tenant("Bonjour A"))
    p2 = g._cache_path(_tenant("Bonjour B"))
    assert p1 != p2, "un texte différent doit donner un fichier de cache différent"

    autre = next(v for v in voices.catalogue() if v.id != voices.VOIX_PAR_DEFAUT)
    p3 = g._cache_path(_tenant("Bonjour A", voice=autre.id))
    assert p3 != p1, "une voix différente doit invalider le cache"


def test_cache_path_ignores_a_voice_outside_catalogue(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    """Une voix inconnue (ou une ancienne voix Moshi) ne DOIT PAS changer le cache : elle
    est servie par la voix par défaut, donc par l'accueil déjà rendu."""
    p1 = g._cache_path(_tenant("Bonjour A"))
    p2 = g._cache_path(_tenant("Bonjour A", voice="unmute-prod-website/developpeuse-3.wav"))
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


def test_le_pre_rendu_exige_la_cle_mistral(monkeypatch):
    """Plus de serveur de voix de secours : sans clé, rien ne se rend."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert g.pre_rendu_possible(_tenant()) is False
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    assert g.pre_rendu_possible(_tenant()) is True


def test_une_url_moshi_posee_n_a_plus_aucun_effet(monkeypatch):
    """Retrait de Moshi (ASSISTANTE-103) : MOSHI_TTS_URL ne fait plus rien rendre."""
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
    assert g.pre_rendu_possible(_tenant()) is False
    assert not hasattr(g, "is_moshi_server")


def test_le_rendu_passe_par_voxtral_jamais_par_un_websocket(monkeypatch):
    """Même avec MOSHI_TTS_URL posée, le rendu va chez Mistral : aucune connexion à
    moshi-server n'est tentée, et une ancienne voix Moshi est rendue en voix par défaut."""
    import asyncio

    import numpy as np

    from app.voice import voices, voxtral_tts

    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
    vus = []

    async def rendre(texte, voxtral_id):
        vus.append(voxtral_id)
        return np.zeros(240, dtype=np.float32)

    monkeypatch.setattr(voxtral_tts, "rendre_pcm", rendre)
    asyncio.run(g._render_pcm("Bonjour", "unmute-prod-website/developpeuse-3.wav"))
    assert vus == [voices.get(voices.VOIX_PAR_DEFAUT).voxtral_id]


def test_texte_de_reprise():
    assert g.texte_de_reprise() == "Je vous écoute."


def _jouer_intro(monkeypatch, secondes_accueil=0.5):
    """Joue l'intro avec de faux transport/tâche ; renvoie (trames mises en file, trames
    audio envoyées, attentes)."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    attentes = []

    async def fausse_attente(secondes):
        attentes.append(secondes)

    monkeypatch.setattr(g.asyncio, "sleep", fausse_attente)
    _write_wav(g._cache_path(_tenant()), seconds=secondes_accueil)
    task = SimpleNamespace(queue_frames=AsyncMock())
    sortie = SimpleNamespace(send_audio=AsyncMock())
    asyncio.run(g.run_switchboard_intro(task, sortie, _tenant(), None))
    frames = [f for appel in task.queue_frames.await_args_list for f in appel.args[0]]
    return frames, sortie.send_audio.await_count, attentes


def test_intro_accueil_puis_reprise_sans_musique(monkeypatch):
    frames, trames, _ = _jouer_intro(monkeypatch)
    textes = [f.text for f in frames if type(f).__name__ == "TTSSpeakFrame"]
    assert textes == ["Je vous écoute."]
    assert trames == 25, "l'accueil seul (0,5 s en trames de 20 ms), aucune musique"
    assert type(frames[-1]).__name__ == "STTMuteFrame" and frames[-1].mute is False


def test_intro_attend_la_fin_de_l_accueil(monkeypatch):
    """`send_audio` rend la main aussitôt : sans attente, la reprise partirait pendant
    l'accueil et le client pourrait parler par-dessus la mention d'information."""
    _, _, attentes = _jouer_intro(monkeypatch, secondes_accueil=2.0)
    assert len(attentes) == 1
    assert attentes[0] == pytest.approx(2.0 - g._AVANCE_REPRISE, abs=0.2)


def test_le_pre_rendu_ne_rend_que_l_accueil(monkeypatch):
    import asyncio

    import numpy as np

    monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")
    rendus = []

    async def rendre(texte, voix=None):
        rendus.append(texte)
        return np.zeros(2400, dtype=np.float32)

    async def convertir(_pcm):
        return b"\x00\x00" * 800

    monkeypatch.setattr(g, "_render_pcm", rendre)
    monkeypatch.setattr(g, "_to_twilio_int16", convertir)
    tenant = _tenant()
    assert asyncio.run(g.ensure_greeting_wav(tenant)) == g._cache_path(tenant)
    assert len(rendus) == 1
    # Idempotent : tout est en cache, plus aucun rendu.
    asyncio.run(g.ensure_greeting_wav(tenant))
    assert len(rendus) == 1


class TestAmorceDuContexte:
    """Ce que le modèle croit avoir déjà dit au décroché.

    Défaut du 20/09/2026 : la phrase de reprise figurait DEUX fois dans le contexte et
    dans la transcription — une fois pré-inscrite, une fois par l'agrégateur, puisqu'elle
    passe par le TTS du pipeline. Le restaurateur lisait une transcription fausse."""

    @pytest.fixture(autouse=True)
    def _cle(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "cle-de-test")

    def test_l_accueil_pre_rendu_est_inscrit_une_fois(self):
        from app.voice import bot

        tenant = _tenant()
        _write_wav(g._cache_path(tenant))
        amorce = bot.amorce_assistante(tenant)
        assert amorce == [{"role": "assistant", "content": tenant.greeting}]

    def test_la_phrase_de_reprise_n_est_jamais_pre_inscrite(self):
        """Elle est dite par le TTS du pipeline : l'agrégateur s'en charge."""
        from app.voice import bot

        tenant = _tenant()
        _write_wav(g._cache_path(tenant))
        amorce = " ".join(m["content"] for m in bot.amorce_assistante(tenant))
        assert g.texte_de_reprise() not in amorce

    def test_sans_wav_en_cache_on_n_inscrit_rien(self):
        """L'accueil repasse alors par le pipeline (repli TTS) : l'inscrire ici le
        dupliquerait à son tour."""
        from app.voice import bot

        assert bot.amorce_assistante(_tenant()) == []

    def test_sans_cle_mistral_on_n_inscrit_rien(self, monkeypatch):
        from app.voice import bot

        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        tenant = _tenant()
        _write_wav(g._cache_path(tenant))
        assert bot.amorce_assistante(tenant) == []
