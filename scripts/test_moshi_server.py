#!/usr/bin/env python3
"""Test de fumée du serveur Rust moshi-server (voix Moshi 1.6B) — sans Twilio ni l'app.

Envoie une phrase française au serveur en websocket (même protocole que le service
client api/app/voice/moshi_server_tts.py), récupère l'audio, mesure le FACTEUR TEMPS
RÉEL (audio produit / temps écoulé ; ≥ 1 = fluide, la vraie voix d'unmute) et écrit un
WAV 24 kHz pour l'écouter.

Prérequis : pip install websockets msgpack numpy

Exemples :
    python scripts/test_moshi_server.py \
        --url https://helmi75--moshi-server-tts-server.modal.run

    python scripts/test_moshi_server.py --url wss://...modal.run \
        --text "Bonjour, bienvenue au restaurant. Souhaitez-vous réserver une table ?" \
        --out /tmp/moshi.wav
"""
import argparse
import asyncio
import time
import wave
from urllib.parse import quote

import numpy as np

_NATIVE_RATE = 24000  # le serveur renvoie du PCM 24 kHz
_DEFAULT_VOICE = "unmute-prod-website/developpeuse-3.wav"
_DEFAULT_TEXT = (
    "Bonjour et bienvenue. Je suis l'assistant vocal du restaurant. "
    "Souhaitez-vous réserver une table pour ce soir ?"
)


def _ws_base(url: str) -> str:
    """Normalise http(s):// ou ws(s):// vers ws(s):// (Modal expose en https -> wss)."""
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


async def _run(args: argparse.Namespace) -> int:
    import msgpack
    import websockets

    uri = (
        f"{_ws_base(args.url)}/api/tts_streaming"
        f"?voice={quote(args.voice)}&format=PcmMessagePack"
    )
    headers = {"kyutai-api-key": args.api_key}
    print(f"→ connexion : {uri}")
    print(f"  voix       : {args.voice}")
    print(f"  texte      : {args.text!r}")
    print(
        f"  (handshake : jusqu'à {args.open_timeout}s — le 1er appel réveille la box "
        f"GPU Modal et recharge le modèle, patiente)"
    )

    chunks: list[np.ndarray] = []
    started = time.monotonic()
    first_at: float | None = None

    try:
        async with websockets.connect(
            uri,
            additional_headers=headers,
            max_size=None,
            open_timeout=args.open_timeout,
        ) as ws:

            async def _send():
                for word in args.text.split():
                    await ws.send(msgpack.packb({"type": "Text", "text": word}))
                await ws.send(msgpack.packb({"type": "Eos"}))

            send_task = asyncio.create_task(_send())
            try:
                async for message_bytes in ws:
                    msg = msgpack.unpackb(message_bytes)
                    if msg.get("type") != "Audio":
                        continue
                    pcm = np.clip(np.array(msg["pcm"], dtype=np.float32), -1.0, 1.0)
                    if pcm.size == 0:
                        continue
                    if first_at is None:
                        first_at = time.monotonic() - started
                        print(f"← 1er chunk audio en {first_at:.2f}s")
                    chunks.append(pcm)
            finally:
                send_task.cancel()
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ ERREUR : {type(e).__name__}: {e}")
        return 1

    wall = time.monotonic() - started
    if not chunks:
        print("\n❌ Aucun audio reçu (voix introuvable ? clé API ? modèle non chargé ?).")
        return 1

    audio = np.concatenate(chunks)
    audio_sec = audio.shape[0] / _NATIVE_RATE
    rtf = audio_sec / wall if wall else 0.0

    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # int16
        w.setframerate(_NATIVE_RATE)
        w.writeframes((audio * 32767).astype("<i2").tobytes())

    print(
        f"\n✅ {audio_sec:.2f}s d'audio en {wall:.2f}s "
        f"→ x{rtf:.2f} temps réel "
        f"({'FLUIDE ✔' if rtf >= 1.0 else 'SOUS le temps réel ✗ (sacade)'})"
    )
    print(f"   WAV écrit : {args.out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="URL du serveur Modal (https/wss).")
    p.add_argument("--api-key", default="public_token", help="Clé (défaut public_token).")
    p.add_argument("--voice", default=_DEFAULT_VOICE, help="Voix (chemin dans tts-voices).")
    p.add_argument("--text", default=_DEFAULT_TEXT, help="Texte à synthétiser.")
    p.add_argument("--out", default="/tmp/moshi_test.wav", help="Fichier WAV de sortie.")
    p.add_argument(
        "--open-timeout", type=float, default=180.0,
        help="Délai max du handshake websocket (s) — large pour absorber le cold start.",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
