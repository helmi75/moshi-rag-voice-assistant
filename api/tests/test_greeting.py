"""Tests du module d'accueil pré-rendu (Phase 3), logique hors-réseau.

Le rendu TTS et le warmup dépendent du réseau (moshi-server) ; on couvre ici la
partie déterministe : chemin de cache et son invalidation, découpage en frames.
"""
import wave

import pytest

from app import horloge
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
    """La garde de tout ce qui parle : un serveur de voix configuré, ou rien."""
    monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
    assert g.is_moshi_server() is True
    monkeypatch.setenv("MOSHI_TTS_URL", "   ")
    assert g.is_moshi_server() is False
    monkeypatch.delenv("MOSHI_TTS_URL", raising=False)
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


def _jouer_intro(monkeypatch, chaud, secondes_accueil=0.5, gpu_venu=True):
    """Joue l'intro avec de faux transport/tâche ; renvoie (résultat, réveils, trames
    mises en file, trames audio envoyées, attentes)."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
    reveils, attentes = [], []

    async def faux_reveil():
        reveils.append(1)
        return gpu_venu

    async def fausse_attente(secondes):
        attentes.append(secondes)

    monkeypatch.setattr(g, "warmup_moshi_server", faux_reveil)
    monkeypatch.setattr(g, "load_hold_music_chunks", lambda **kw: (8000, []))
    monkeypatch.setattr(g.asyncio, "sleep", fausse_attente)
    _write_wav(g._cache_path(_tenant()), seconds=secondes_accueil)
    task = SimpleNamespace(queue_frames=AsyncMock())
    sortie = SimpleNamespace(send_audio=AsyncMock())
    resultat = asyncio.run(g.run_switchboard_intro(task, sortie, _tenant(), None, chaud=chaud))
    frames = [f for appel in task.queue_frames.await_args_list for f in appel.args[0]]
    return resultat, reveils, frames, sortie.send_audio.await_count, attentes


def _intro(monkeypatch, chaud, secondes_accueil=0.5):
    """Le cas nominal : renvoie (réveils, textes dits, trames audio envoyées, attentes)."""
    _, reveils, frames, trames, attentes = _jouer_intro(monkeypatch, chaud, secondes_accueil)
    textes = [f.text for f in frames if type(f).__name__ == "TTSSpeakFrame"]
    assert type(frames[-1]).__name__ == "STTMuteFrame" and frames[-1].mute is False
    return reveils, textes, trames, attentes


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


class TestGpuIntrouvable:
    """Appels 158 et 159 (24/09/2026, 23 h 15) : Modal n'avait plus de GPU L4 en Europe.
    90 s de musique, puis une reprise envoyée à un serveur absent — et l'appelant dans
    le silence jusqu'à ce qu'il raccroche. Il doit entendre qu'on le rappelle à rappeler."""

    def _message_en_cache(self, secondes=3.0):
        return _write_wav(g._chemin_indisponible(_tenant()), seconds=secondes)

    def test_le_message_est_joue_puis_on_raccroche(self, monkeypatch):
        self._message_en_cache(secondes=3.0)
        resultat, _, frames, trames, _ = _jouer_intro(monkeypatch, chaud=False, gpu_venu=False)
        assert resultat == g.INDISPONIBLE
        assert trames == 25 + 150, "l'accueil (0,5 s) puis le message (3 s), en trames de 20 ms"
        noms = [type(f).__name__ for f in frames]
        assert noms[-1] == "EndFrame", "on raccroche, on ne laisse pas l'appelant en ligne"
        assert "TTSSpeakFrame" not in noms, (
            "la reprise partirait vers un serveur absent : c'est le silence des appels 158-159")

    def test_rien_ne_suit_le_raccroche(self, monkeypatch):
        """Un démute mis en file après l'EndFrame arriverait sur un pipeline qui s'arrête."""
        self._message_en_cache()
        _, _, frames, _, _ = _jouer_intro(monkeypatch, chaud=False, gpu_venu=False)
        assert [type(f).__name__ for f in frames].count("STTMuteFrame") == 1

    def test_un_gpu_venu_ne_declenche_jamais_le_message(self, monkeypatch):
        self._message_en_cache()
        resultat, _, frames, trames, _ = _jouer_intro(monkeypatch, chaud=False, gpu_venu=True)
        assert resultat is None
        assert trames == 25, "l'accueil seul"
        assert "EndFrame" not in [type(f).__name__ for f in frames]

    def test_sans_message_en_cache_la_reprise_est_tentee(self, monkeypatch):
        """Pas de WAV : on ne raccroche pas au nez de l'appelant sans un mot. On tente la
        reprise, comme avant — et la supervision signale le message manquant."""
        resultat, _, frames, _, _ = _jouer_intro(monkeypatch, chaud=False, gpu_venu=False)
        assert resultat is None
        assert [f.text for f in frames if type(f).__name__ == "TTSSpeakFrame"] == [
            "Merci d'avoir patienté, je vous écoute."]

    def test_le_message_a_son_propre_cache_suivant_la_voix(self):
        """Changer la voix de l'établissement doit rendre un nouveau message : sinon
        l'appelant entendrait deux voix différentes dans le même appel."""
        from app.voice import voices

        tenant = _tenant()
        assert g._chemin_indisponible(tenant) != g._cache_path(tenant)
        autre = next(v for v in voices.catalogue() if v.id != voices.DEFAULT_VOICE)
        assert g._chemin_indisponible(_tenant(voice=autre.id)) != g._chemin_indisponible(tenant)

    def test_le_pre_rendu_prepare_aussi_le_message(self, monkeypatch):
        import asyncio

        import numpy as np

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
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
        assert g.cached_indisponible_path(tenant) is not None
        assert rendus[-1] == g.texte_indisponible()
        # Idempotent : tout est en cache, plus aucun rendu.
        asyncio.run(g.ensure_greeting_wav(tenant))
        assert len(rendus) == 2

    def test_gpu_injoignable_on_n_attend_pas_une_deuxieme_fois(self, monkeypatch):
        """L'accueil vient d'échouer : réessayer le message coûterait 90 s de plus."""
        import asyncio

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        rendus = []

        async def echouer(texte, voix=None):
            rendus.append(texte)
            raise TimeoutError("timed out during opening handshake")

        monkeypatch.setattr(g, "_render_pcm", echouer)
        assert asyncio.run(g.ensure_greeting_wav(_tenant())) is None
        assert len(rendus) == 1


class TestKeepWarmAuxHeuresDeService:
    """Le GPU chaud coûte ≈ 0,80 $/h : on ne le garde chaud qu'aux heures où l'on appelle."""

    @staticmethod
    def _a(heure, minute=0):
        from datetime import datetime

        return datetime(2026, 9, 19, heure, minute, tzinfo=horloge.FUSEAU)

    def test_lecture_des_plages(self):
        assert g.plages_keepwarm("11:30-14:30, 18h30-23") == [(690, 870), (1110, 1380)]
        assert g.plages_keepwarm("") is None

    @pytest.mark.parametrize("brut", ["midi-14", "11-11", "25-26", "11-14;18-23"])
    def test_une_plage_illisible_garde_chaud_toute_la_journee(self, brut):
        assert g.plages_keepwarm(brut) is None

    def test_dedans_et_dehors(self):
        plages = g.plages_keepwarm("11:30-14:30,18:30-23")
        assert g.keepwarm_maintenant(plages, self._a(12))
        assert not g.keepwarm_maintenant(plages, self._a(16))
        assert not g.keepwarm_maintenant(plages, self._a(14, 30))  # fin exclue
        assert g.keepwarm_maintenant(None, self._a(4))            # pas de plage : toujours

    def test_une_plage_qui_passe_minuit(self):
        plages = g.plages_keepwarm("22-2")
        assert g.keepwarm_maintenant(plages, self._a(23))
        assert g.keepwarm_maintenant(plages, self._a(1, 59))
        assert not g.keepwarm_maintenant(plages, self._a(2))

    @pytest.mark.parametrize("heure,reveils", [(12, 1), (16, 0)])
    def test_la_boucle_ne_reveille_le_gpu_que_dans_la_fenetre(self, monkeypatch, heure, reveils):
        import asyncio

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        monkeypatch.setenv("MOSHI_KEEPWARM_SECONDS", "90")
        monkeypatch.setenv("MOSHI_KEEPWARM_HEURES", "11:30-14:30")
        monkeypatch.setattr(horloge, "maintenant", lambda: self._a(heure))
        appels = []

        async def reveil():
            appels.append(1)

        async def dormir(_secondes):
            raise asyncio.CancelledError  # un seul tour de boucle

        monkeypatch.setattr(g, "warmup_moshi_server", reveil)
        monkeypatch.setattr(g.asyncio, "sleep", dormir)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(g.keep_warm_loop())
        assert len(appels) == reveils


class TestAmorceDuContexte:
    """Ce que le modèle croit avoir déjà dit au décroché.

    Défaut du 20/09/2026 : la phrase de reprise figurait DEUX fois dans le contexte et
    dans la transcription — une fois pré-inscrite, une fois par l'agrégateur, puisqu'elle
    passe par le TTS du pipeline. Le restaurateur lisait une transcription fausse."""

    def test_l_accueil_pre_rendu_est_inscrit_une_fois(self, monkeypatch):
        from app.voice import bot

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        tenant = _tenant()
        _write_wav(g._cache_path(tenant))
        amorce = bot.amorce_assistante(tenant)
        assert amorce == [{"role": "assistant", "content": tenant.greeting}]

    def test_la_phrase_de_reprise_n_est_jamais_pre_inscrite(self, monkeypatch):
        """Elle est dite par le TTS du pipeline : l'agrégateur s'en charge."""
        from app.voice import bot

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        tenant = _tenant()
        _write_wav(g._cache_path(tenant))
        amorce = " ".join(m["content"] for m in bot.amorce_assistante(tenant))
        assert g.texte_de_reprise(True) not in amorce
        assert g.texte_de_reprise(False) not in amorce

    def test_sans_wav_en_cache_on_n_inscrit_rien(self, monkeypatch):
        """L'accueil repasse alors par le pipeline (repli TTS) : l'inscrire ici le
        dupliquerait à son tour."""
        from app.voice import bot

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        assert bot.amorce_assistante(_tenant()) == []

    def test_sans_serveur_de_voix_on_n_inscrit_rien(self, monkeypatch):
        from app.voice import bot

        monkeypatch.delenv("MOSHI_TTS_URL", raising=False)
        tenant = _tenant()
        _write_wav(g._cache_path(tenant))
        assert bot.amorce_assistante(tenant) == []


class TestTranscriptionDuMessage:
    """Le message part sans le modèle : il n'est pas dans le contexte. Il faut l'y
    inscrire, sinon l'appel se lirait comme un appelant qui raccroche sans un mot."""

    def _tache(self, resultat=None, annulee=False):
        import asyncio

        async def intro():
            if annulee:
                await asyncio.sleep(3600)
            return resultat

        async def jouer():
            tache = asyncio.ensure_future(intro())
            if annulee:
                await asyncio.sleep(0)
                tache.cancel()
            try:
                await tache
            except asyncio.CancelledError:
                pass
            return tache

        return asyncio.run(jouer())

    def test_raccroche_faute_de_gpu(self):
        from app.voice import bot

        assert bot.message_de_fin_d_intro(self._tache(g.INDISPONIBLE)) == g.texte_indisponible()

    def test_intro_normale_ou_interrompue(self):
        from app.voice import bot

        assert bot.message_de_fin_d_intro(self._tache(None)) is None
        assert bot.message_de_fin_d_intro(self._tache(annulee=True)) is None
