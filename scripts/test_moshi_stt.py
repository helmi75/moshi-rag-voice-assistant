#!/usr/bin/env python3
"""Test de fumée du module STT (ASR) du serveur moshi-server — sans Twilio ni l'app.

Boucle autonome TTS -> STT : synthétise une phrase française via /api/tts_streaming
(24 kHz), la renvoie au module ASR /api/asr-streaming (même serveur, même protocole
msgpack que le futur client api/app/voice/kyutai_stt.py), puis mesure la latence du
flush de fin de tour (Marker + silence) et affiche le transcript.

Protocole ASR vérifié (kyutai-labs/delayed-streams-modeling, moshi rust/moshi-server) :
    URI    : {ws_base}/api/asr-streaming
    header : {"kyutai-api-key": <clé>}                 (défaut serveur : "public_token")
    envoi  : msgpack {"type": "Audio", "pcm": [f32...]}  24 kHz mono, chunks de 1920
             puis {"type": "Marker", "id": n} suivi de ~0,8 s de zéros (draine le delay)
    retour : {"type":"Ready"} | {"type":"Word","text","start_time"} | {"type":"EndWord"}
             | {"type":"Step","prs":[...]} | {"type":"Marker","id"} | {"type":"Error"}
    packb DOIT utiliser use_single_float=True (Vec<f32> côté Rust) + use_bin_type=True.

Prérequis : pip install websockets msgpack numpy

Exemples :
    python scripts/test_moshi_stt.py --url https://helmi75--moshi-server-tts-server.modal.run
    python scripts/test_moshi_stt.py --url wss://...modal.run --idle 120   # test session idle
"""
import argparse
import asyncio
import time
from urllib.parse import quote

import numpy as np

_NATIVE_RATE = 24000  # le serveur ASR attend (et le TTS renvoie) du PCM 24 kHz
_FRAME = 1920  # 80 ms à 24 kHz : taille de chunk canonique côté Kyutai
_DEFAULT_VOICE = "unmute-prod-website/developpeuse-3.wav"
_DEFAULT_TEXT = (
    "Bonjour, je voudrais réserver une table pour quatre personnes demain soir "
    "à vingt heures, au nom de Martin, sur la terrasse s'il vous plaît."
)


def _ws_base(url: str) -> str:
    """Normalise http(s):// ou ws(s):// vers ws(s):// (Modal expose en https -> wss)."""
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


async def _synthesize(base: str, api_key: str, voice: str, text: str) -> np.ndarray:
    """Synthétise `text` via le module TTS et retourne le PCM float32 24 kHz."""
    import msgpack
    import websockets

    uri = f"{base}/api/tts_streaming?voice={quote(voice)}&format=PcmMessagePack"
    chunks: list[np.ndarray] = []
    async with websockets.connect(
        uri, additional_headers={"kyutai-api-key": api_key}, max_size=None, open_timeout=180.0
    ) as ws:
        async def _send():
            for word in text.split():
                await ws.send(msgpack.packb({"type": "Text", "text": word}))
            await ws.send(msgpack.packb({"type": "Eos"}))

        send_task = asyncio.create_task(_send())
        try:
            async for message in ws:
                msg = msgpack.unpackb(message)
                if msg.get("type") != "Audio":
                    continue
                pcm = np.array(msg["pcm"], dtype=np.float32)
                if pcm.size:
                    chunks.append(pcm)
        finally:
            send_task.cancel()
    if not chunks:
        raise RuntimeError("aucun audio TTS reçu (voix ? clé ? modèle non chargé ?)")
    return np.concatenate(chunks)


async def _transcribe(base: str, api_key: str, pcm: np.ndarray, idle: float) -> tuple[str, float]:
    """Envoie `pcm` (24 kHz f32) au module ASR, retourne (transcript, latence_flush_s)."""
    import msgpack
    import websockets

    uri = f"{base}/api/asr-streaming"
    words: list[str] = []
    marker_at: asyncio.Future = asyncio.get_event_loop().create_future()

    async with websockets.connect(
        uri, additional_headers={"kyutai-api-key": api_key}, max_size=None, open_timeout=180.0
    ) as ws:

        async def _recv():
            async for message in ws:
                msg = msgpack.unpackb(message)
                t = msg.get("type")
                if t == "Word":
                    words.append(msg["text"])
                    print(f"  · Word {msg['text']!r} @ {msg.get('start_time', 0):.2f}s")
                elif t == "Marker":
                    if not marker_at.done():
                        marker_at.set_result(time.monotonic())
                    return
                elif t == "Error":
                    print(f"  ❌ Error: {msg.get('message')}")
                    return

        recv_task = asyncio.create_task(_recv())

        async def _send_audio(samples: np.ndarray):
            await ws.send(msgpack.packb(
                {"type": "Audio", "pcm": [float(x) for x in samples]},
                use_single_float=True, use_bin_type=True,
            ))

        # Test optionnel de session idle : silence prolongé AVANT la parole (reproduit le
        # mute de l'accueil/musique, jusqu'à 90 s) pour vérifier que la session ASR survit.
        if idle > 0:
            print(f"  (session idle : {idle:.0f}s de silence avant la parole…)")
            for _ in range(int(idle / 0.08)):
                await _send_audio(np.zeros(_FRAME, dtype=np.float32))

        # Streame l'audio réel en chunks de 80 ms, au fil du temps réel.
        for i in range(0, len(pcm), _FRAME):
            frame = pcm[i:i + _FRAME]
            if len(frame) < _FRAME:
                frame = np.pad(frame, (0, _FRAME - len(frame)))
            await _send_audio(frame)

        # Flush de fin de tour : Marker puis ~0,8 s de zéros pour drainer le delay (0,5 s).
        flush_start = time.monotonic()
        await ws.send(msgpack.packb({"type": "Marker", "id": 0}, use_single_float=True))
        for _ in range(10):
            await _send_audio(np.zeros(_FRAME, dtype=np.float32))

        try:
            marker_time = await asyncio.wait_for(marker_at, timeout=15.0)
        except asyncio.TimeoutError:
            recv_task.cancel()
            raise RuntimeError("pas d'écho Marker en 15 s (flush non confirmé)")
        recv_task.cancel()

    return " ".join(words), marker_time - flush_start


async def _run(args: argparse.Namespace) -> int:
    base = _ws_base(args.url)
    print(f"→ serveur : {base}")
    print(f"  texte de référence : {args.text!r}")
    print("  [1/2] synthèse TTS…")
    try:
        pcm = await _synthesize(base, args.api_key, args.voice, args.text)
    except Exception as e:  # noqa: BLE001
        print(f"❌ TTS : {type(e).__name__}: {e}")
        return 1
    print(f"  → {len(pcm) / _NATIVE_RATE:.2f}s d'audio synthétisé")

    print("  [2/2] transcription ASR…")
    try:
        transcript, flush_s = await _transcribe(base, args.api_key, pcm, args.idle)
    except Exception as e:  # noqa: BLE001
        print(f"❌ ASR : {type(e).__name__}: {e}")
        return 1

    print(f"\n✅ Transcript : {transcript!r}")
    print(f"   Latence flush (Marker→écho) : {flush_s * 1000:.0f} ms "
          f"({'✔ < 500 ms' if flush_s < 0.5 else '⚠ > 500 ms'})")
    if not transcript.strip():
        print("   ⚠ transcript vide — vérifier le module ASR / le format audio.")
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="URL du serveur Modal (https/wss).")
    p.add_argument("--api-key", default="public_token", help="Clé (défaut public_token).")
    p.add_argument("--voice", default=_DEFAULT_VOICE, help="Voix TTS pour générer l'audio test.")
    p.add_argument("--text", default=_DEFAULT_TEXT, help="Phrase de référence à transcrire.")
    p.add_argument("--idle", type=float, default=0.0,
                   help="Secondes de silence avant la parole (test de session idle, ex. 120).")
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
