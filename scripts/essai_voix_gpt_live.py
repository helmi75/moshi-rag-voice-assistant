"""Essai des voix de GPT-Live en français : un extrait par voix, écrit en WAV 24 kHz.

Chaque voix dit la même phrase d'accueil et de réservation. C'est à l'oreille que se
juge l'accent : ce script ne note rien, il produit de quoi écouter (docs/TEMPS_REEL.md).

La clé vient de l'environnement (`OPENAI_API_KEY`) et n'est jamais écrite ni affichée.
Dans un conteneur jetable, avec un dossier monté sur /out :

    docker run --rm -i -e OPENAI_API_KEY -v "$PWD/local/essai-gpt-live:/out" \
        moshi-rag-voice-assistant-api python - marin,cedar < scripts/essai_voix_gpt_live.py

Avec `sonde` en second argument, aucune phrase n'est dite : on vérifie seulement que la
voix est ouverte au compte (gratuit si le compte n'a pas de crédit, la session étant
refusée avant le premier son).
"""
import asyncio
import base64
import json
import os
import sys
import time
import wave

from websockets.asyncio.client import connect

CLE = os.environ["OPENAI_API_KEY"].strip()
URL = "wss://api.openai.com/v1/live/sessions"
SORTIE = "/out"
SILENCE = base64.b64encode(b"\x00" * 960).decode()  # 20 ms de silence, PCM 16 bits 24 kHz

CONSIGNES = (
    "Tu es Marie, l'assistante téléphonique du restaurant Le Bouchon Doré, à Paris. "
    "Tu parles un français de France, standard, parfaitement naturel et fluide, comme une "
    "Parisienne dont c'est la langue maternelle. Aucun accent étranger, aucune intonation "
    "anglophone. Ton chaleureux et posé, débit naturel."
)
PHRASE = (
    "Dis maintenant, mot pour mot et rien d'autre : « Bonjour, Le Bouchon Doré. Je suis "
    "Marie, l'assistante vocale du restaurant. Vous souhaitez réserver une table ? Très "
    "bien. Pour combien de personnes, et à quelle heure ? Je vérifie tout de suite : "
    "jeudi quatorze août, vingt heures trente, six personnes au nom de Lefèvre. »"
)


TEXTE = ("Bonjour, Le Bouchon Doré. Je suis Marie, l'assistante vocale du restaurant. Vous "
         "souhaitez réserver une table ? Très bien. Pour combien de personnes, et à quelle "
         "heure ? Je vérifie tout de suite : jeudi quatorze août, vingt heures trente, six "
         "personnes au nom de Lefèvre.")


async def essayer(voix: str, sonde: bool) -> dict:
    resultat = {"voix": voix, "etat": "?", "secondes": 0.0, "texte": ""}
    audio = bytearray()
    try:
        async with connect(URL, additional_headers={"Authorization": f"Bearer {CLE}"},
                           open_timeout=20, max_size=None) as ws:
            await ws.send(json.dumps({"type": "session.start", "session": {
                "model": "gpt-live-1",
                "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": voix}},
                "instructions": CONSIGNES, "input": []}}))

            demarre = False
            demande = False
            dernier_son = None
            dernier_texte = None
            relance = False
            t_demande = 0.0
            debut = time.monotonic()

            async def silence():
                while True:
                    await ws.send(json.dumps({"type": "session.input_audio.append",
                                              "audio": SILENCE}))
                    await asyncio.sleep(0.02)

            fond = None
            try:
                while time.monotonic() - debut < 30:
                    try:
                        brut = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        brut = None
                    if brut is not None:
                        evt = json.loads(brut)
                        genre = evt.get("type", "")
                        if genre == "session.started":
                            demarre = True
                            resultat["etat"] = "acceptée"
                            if sonde:
                                break
                            fond = asyncio.create_task(silence())
                        elif genre == "error":
                            err = evt.get("error") or {}
                            resultat["etat"] = f"refus : {err.get('code') or err.get('type')} — {str(err.get('message'))[:120]}"
                            break
                        elif genre == "session.output_audio.delta":
                            audio += base64.b64decode(evt.get("audio") or evt.get("delta") or "")
                            dernier_son = time.monotonic()
                        elif genre == "session.output_transcript.delta":
                            resultat["texte"] += evt.get("delta", "")
                            dernier_texte = time.monotonic()
                        elif genre == "session.closed":
                            break
                    # Muette 3 s après la consigne : la phrase lui est redonnée comme un
                    # propos à dire (session.commentary.append), une seule fois.
                    if demande and not relance and not resultat["texte"] and time.monotonic() - t_demande > 3:
                        await ws.send(json.dumps({"type": "session.commentary.append",
                                                  "event_id": "phrase_2", "delegation_id": None,
                                                  "content": TEXTE}))
                        relance = True
                        resultat["relance"] = True
                    if demarre and not demande and not sonde:
                        await ws.send(json.dumps({"type": "session.instructions.append",
                                                  "event_id": "phrase_1", "delegation_id": None,
                                                  "content": PHRASE}))
                        demande = True
                        t_demande = time.monotonic()
                    # Le flux est continu, silences compris : la fin de l'extrait se lit sur
                    # la transcription, quand plus rien n'est dit depuis 2,5 s.
                    if dernier_texte and time.monotonic() - dernier_texte > 2.5:
                        break
            finally:
                if fond:
                    fond.cancel()
            try:
                await ws.send(json.dumps({"type": "session.close", "event_id": "fin_1"}))
            except Exception:
                pass
    except Exception as exc:
        etat = getattr(getattr(exc, "response", None), "status_code", None)
        resultat["etat"] = f"connexion refusée : {type(exc).__name__} {etat or ''}".strip()
    # Retire le silence du début et de la fin (seuil : 1 % de la pleine échelle).
    import numpy as np
    echantillons = np.frombuffer(bytes(audio[: len(audio) // 2 * 2]), dtype="<i2")
    parle = np.flatnonzero(np.abs(echantillons.astype(np.int32)) > 330)
    if parle.size:
        debut = max(0, int(parle[0]) - 2400)
        fin = min(echantillons.size, int(parle[-1]) + 4800)
        audio = bytearray(echantillons[debut:fin].tobytes())
    if audio:
        with wave.open(f"{SORTIE}/gpt-live-{voix}.wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(audio))
        resultat["secondes"] = round(len(audio) / 48000, 1)
    return resultat


async def main():
    voix = sys.argv[1].split(",")
    sonde = len(sys.argv) > 2 and sys.argv[2] == "sonde"
    for v in voix:
        r = await essayer(v, sonde)
        print(json.dumps(r, ensure_ascii=False), flush=True)


asyncio.run(main())
