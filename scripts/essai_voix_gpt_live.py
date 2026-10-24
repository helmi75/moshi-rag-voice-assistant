"""Banc d'écoute des voix de GPT-Live en français : chaque voix dit la même phrase.

C'est à l'oreille que se juge l'accent : ce script ne note rien, il produit de quoi
écouter (docs/TEMPS_REEL.md). Deux extraits par voix :

- `tel` : demandé à GPT-Live en µ-law 8 kHz, le format exact que la ligne reçoit en
  production (`voice/live.py`). C'est ce que le client entendra, sans re-codage de notre
  part : un extrait large bande ré-échantillonné ne dirait pas ce que Twilio transmet.
- `hd` : PCM 24 kHz, pour entendre la voix elle-même.

Une voix se désigne par son nom (`marin`, `brise`), par son identifiant (`voice_…`), ou
par `prompt:<style>` : la voix est alors créée à partir d'une description (STYLES
ci-dessous), une fois. Son identifiant est gardé dans `voix-creees.json` du dossier de
sortie et réutilisé aux passages suivants, pour ne pas en créer une nouvelle à chaque
fois (100 voix au plus par organisation, création gratuite, relevé le 24/10/2026).

La clé vient de l'environnement (`OPENAI_API_KEY`) et n'est jamais écrite ni affichée.
Dans un conteneur jetable, avec un dossier monté sur /out :

    docker run --rm -i -e OPENAI_API_KEY -v "$PWD/local/essai-gpt-live:/out" \
        moshi-rag-voice-assistant-api python - marin,brise,prompt:posee < scripts/essai_voix_gpt_live.py

Second argument : `sonde` (aucune phrase n'est dite : on vérifie seulement que la voix est
ouverte au compte), ou `tel` / `hd` pour un seul des deux extraits.
"""
import asyncio
import audioop  # présent jusqu'à Python 3.12, l'image de production ; script hors appel
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import wave

from websockets.asyncio.client import connect

CLE = os.environ["OPENAI_API_KEY"].strip()
URL = "wss://api.openai.com/v1/live/sessions"
URL_VOIX = "https://api.openai.com/v1/audio/voices"
SORTIE = "/out"

# Un format par extrait : ce que la session demande, 20 ms de silence à lui envoyer (le
# flux d'entrée doit rester continu), et la fréquence du fichier écrit.
FORMATS = {
    "tel": ({"type": "audio/pcmu", "rate": 8000}, b"\xff" * 160, 8000),
    "hd": ({"type": "audio/pcm", "rate": 24000}, b"\x00" * 960, 24000),
}

# Le style de chaque voix créée par description. La description fixe le timbre ; la
# langue et l'accent sont redits dans les consignes de session.
STYLES = {
    "posee": (
        "A warm, calm woman in her late thirties, a native French speaker from Paris with "
        "a standard metropolitan French accent and no foreign accent. She answers the phone "
        "for a Parisian bistro: medium-low pitch, unhurried pace, smiling but professional, "
        "very clear diction."
    ),
    "souriante": (
        "A bright, friendly woman around thirty, a native French speaker from Paris with a "
        "standard metropolitan French accent and no foreign accent. Lively and welcoming "
        "without rushing, mid pitch, crisp articulation, sounds genuinely happy to help."
    ),
    "elegante": (
        "A refined woman in her mid forties, like the maître d'hôtel of an elegant Paris "
        "restaurant, a native French speaker with a standard metropolitan French accent and "
        "no foreign accent. Slightly low pitch, measured pace, impeccable diction, courteous "
        "and reassuring."
    ),
}

CONSIGNES = (
    "Tu es Marie, l'assistante téléphonique du restaurant Le Bouchon Doré, à Paris. "
    "Tu parles un français de France, standard, parfaitement naturel et fluide, comme une "
    "Parisienne dont c'est la langue maternelle. Aucun accent étranger, aucune intonation "
    "anglophone. Ton chaleureux et posé, débit naturel."
)
TEXTE = ("Bonjour, Le Bouchon Doré. Je suis Marie, l'assistante vocale du restaurant. Vous "
         "souhaitez réserver une table ? Très bien. Pour combien de personnes, et à quelle "
         "heure ? Je vérifie tout de suite : jeudi quatorze août, vingt heures trente, six "
         "personnes au nom de Lefèvre.")
# L'exemple de parole qui guide la création d'une voix : la phrase seule a été refusée
# (« Script hint is too short », 24/10/2026), on y ajoute une suite de conversation.
EXEMPLE = TEXTE + (" C'est noté, votre table est réservée. Vous recevrez une confirmation "
                   "par SMS. Je vous rappelle que nous sommes ouverts du mardi au samedi, "
                   "le midi et le soir. Puis-je faire autre chose pour vous ? Je vous en "
                   "prie, à jeudi soir, et très bonne journée.")
PHRASE = f"Dis maintenant, mot pour mot et rien d'autre : « {TEXTE} »"


def voix_creees() -> dict:
    try:
        with open(f"{SORTIE}/voix-creees.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def creer_voix(style: str) -> str:
    """Identifiant de la voix décrite par STYLES[style], créée au premier passage."""
    connues = voix_creees()
    if style in connues:
        return connues[style]["id"]
    corps = json.dumps({"type": "prompt", "name": f"Marie {style}", "model": "auto",
                        "prompt": STYLES[style], "script_hint": EXEMPLE}).encode()
    requete = urllib.request.Request(URL_VOIX, data=corps, method="POST", headers={
        "Authorization": f"Bearer {CLE}", "Content-Type": "application/json"})
    # La création échoue par moments côté OpenAI (« Voice generation failed. Please try
    # again. », HTTP 500, le 24/10/2026) : trois essais, pas davantage.
    for essai in range(3):
        try:
            with urllib.request.urlopen(requete, timeout=60) as r:
                voix = json.load(r)
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or essai == 2:
                raise
            time.sleep(5)
    connues[style] = {"id": voix["id"], "prompt": STYLES[style],
                      "creee_le": voix.get("created_at")}
    with open(f"{SORTIE}/voix-creees.json", "w") as f:
        json.dump(connues, f, ensure_ascii=False, indent=1)
    return voix["id"]


def designer(spec: str) -> tuple[str, object]:
    """(nom du fichier, valeur du champ `voice` de la session) pour une voix demandée."""
    if spec.startswith("prompt:"):
        style = spec.split(":", 1)[1]
        return f"prompt-{style}", {"id": creer_voix(style)}
    if spec.startswith("voice_"):
        return spec, {"id": spec}
    return spec, spec  # une voix nommée s'écrit en chaîne, une voix créée en objet


async def essayer(nom: str, voix, format_: str, sonde: bool) -> dict:
    session_format, silence_brut, frequence = FORMATS[format_]
    silence = base64.b64encode(silence_brut).decode()
    resultat = {"voix": nom, "format": format_, "etat": "?", "secondes": 0.0, "texte": ""}
    audio = bytearray()
    try:
        async with connect(URL, additional_headers={"Authorization": f"Bearer {CLE}"},
                           open_timeout=20, max_size=None) as ws:
            await ws.send(json.dumps({"type": "session.start", "session": {
                "model": "gpt-live-1",
                "audio": {"format": session_format, "output": {"voice": voix}},
                "instructions": CONSIGNES, "input": []}}))

            demarre = demande = relance = False
            dernier_texte = None
            t_demande = 0.0
            debut = time.monotonic()

            async def flux_de_silence():
                while True:
                    await ws.send(json.dumps({"type": "session.input_audio.append",
                                              "audio": silence}))
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
                            fond = asyncio.create_task(flux_de_silence())
                        elif genre == "error":
                            err = evt.get("error") or {}
                            resultat["etat"] = f"refus : {err.get('code') or err.get('type')} — {str(err.get('message'))[:120]}"
                            break
                        elif genre == "session.output_audio.delta":
                            audio += base64.b64decode(evt.get("audio") or evt.get("delta") or "")
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
    pcm = audioop.ulaw2lin(bytes(audio), 2) if format_ == "tel" else bytes(audio[: len(audio) // 2 * 2])
    # Retire le silence du début et de la fin (seuil : 1 % de la pleine échelle).
    import numpy as np
    echantillons = np.frombuffer(pcm, dtype="<i2")
    parle = np.flatnonzero(np.abs(echantillons.astype(np.int32)) > 330)
    if parle.size:
        debut = max(0, int(parle[0]) - frequence // 10)
        fin = min(echantillons.size, int(parle[-1]) + frequence // 5)
        pcm = echantillons[debut:fin].tobytes()
    if pcm:
        with wave.open(f"{SORTIE}/{nom}-{format_}.wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(frequence)
            w.writeframes(pcm)
        resultat["secondes"] = round(len(pcm) / (2 * frequence), 1)
    return resultat


async def main():
    demandees = sys.argv[1].split(",")
    option = sys.argv[2] if len(sys.argv) > 2 else ""
    sonde = option == "sonde"
    formats = ["tel"] if sonde else ([option] if option in FORMATS else ["tel", "hd"])
    for spec in demandees:
        try:
            nom, voix = designer(spec)
        except Exception as exc:
            # Le corps de la réponse dit pourquoi (description refusée, quota) : il ne
            # contient pas la clé.
            motif = exc.read().decode(errors="replace")[:300] if hasattr(exc, "read") else ""
            print(json.dumps({"voix": spec, "etat": f"création refusée : {type(exc).__name__} "
                              f"{getattr(exc, 'code', '')} {motif}"}, ensure_ascii=False),
                  flush=True)
            continue
        for format_ in formats:
            r = await essayer(nom, voix, format_, sonde)
            print(json.dumps(r, ensure_ascii=False), flush=True)


asyncio.run(main())
