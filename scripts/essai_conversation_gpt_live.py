"""Une vraie conversation de réservation à travers `app/voice/live.py`, contre le vrai
GPT-Live — sans téléphone : un faux Twilio joue le client, au format du téléphone.

À quoi ça sert : éprouver le relais, la délégation des outils et la clôture AVANT le
premier appel, et mesurer le blanc avant chaque réponse. La base est jetable (celle du
conteneur), `llm.run_tool` est le vrai.

    # 1. les répliques du client, dites par une voix de GPT-Live (µ-law 8 kHz) :
    python essai_conversation_gpt_live.py repliques
    # 2. la conversation :
    python essai_conversation_gpt_live.py conversation

Dans un conteneur jetable dont `/app/app` est le code de la branche et `/out` un dossier
monté ; la clé vient de `OPENAI_API_KEY` et n'est jamais affichée.
"""
import asyncio
import base64
import json
import os
import sys
import time
import wave

SORTIE = "/out"
REPLIQUES = [
    "Bonjour, je voudrais réserver une table pour deux personnes, samedi soir à vingt heures.",
    "Au nom de Martin, s'il vous plaît.",
    "Oui, c'est bien ça.",
    "Non merci, c'est tout. Au revoir.",
]
SILENCE = b"\xff" * 160                      # 20 ms de silence en µ-law 8 kHz
# Le client n'enchaîne pas à l'aveugle : chaque réplique attend ce que l'assistante doit
# avoir dit (depuis la réplique précédente) pour qu'elle ait un sens.
ATTENDUS = [
    lambda dit: True,                                                    # après l'accueil
    lambda dit: "nom" in dit,                                            # elle demande le nom
    lambda dit: "?" in dit and any(m in dit for m in ("martin", "récapitul", "bien ça", "confirm")),
    lambda dit: any(m in dit for m in ("c'est enregistré", "autre chose", "c'est réservé", "c'est noté")),
]
PATIENCE = 30.0                                # au-delà, le client parle quand même


async def dire_les_repliques() -> None:
    """Fait dire chaque réplique du client par une voix de GPT-Live, en µ-law 8 kHz."""
    from websockets.asyncio.client import connect

    import numpy as np

    from app.voice import ulaw

    for numero, phrase in enumerate(REPLIQUES, 1):
        audio = bytearray()
        texte = ""
        async with connect("wss://api.openai.com/v1/live/sessions", max_size=None,
                           additional_headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}"}) as ws:
            await ws.send(json.dumps({"type": "session.start", "session": {
                "model": "gpt-live-1",
                "instructions": "Tu es un client qui téléphone à un restaurant. Tu dis exactement la "
                                "phrase qu'on te donne, d'un ton naturel, en français, et rien d'autre.",
                "audio": {"format": {"type": "audio/pcmu", "rate": 8000}, "output": {"voice": "ash"}}}}))
            demande, dernier, debut = False, None, time.monotonic()

            async def silence():
                while True:
                    await ws.send(json.dumps({"type": "session.input_audio.append",
                                              "audio": base64.b64encode(SILENCE).decode()}))
                    await asyncio.sleep(0.02)

            fond = None
            try:
                while time.monotonic() - debut < 25:
                    try:
                        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
                    except asyncio.TimeoutError:
                        continue
                    genre = evt.get("type")
                    if genre == "session.started":
                        fond = asyncio.create_task(silence())
                        await ws.send(json.dumps({"type": "session.commentary.append", "event_id": "r",
                                                  "delegation_id": None, "content": phrase}))
                        demande = True
                    elif genre == "session.output_audio.delta" and demande:
                        audio += base64.b64decode(evt.get("delta") or "")
                    elif genre == "session.output_transcript.delta":
                        texte += evt.get("delta", "")
                        dernier = time.monotonic()
                    elif genre == "error":
                        print("erreur :", (evt.get("error") or {}).get("code"))
                        break
                    if dernier and time.monotonic() - dernier > 2.0:
                        break
            finally:
                if fond:
                    fond.cancel()
        pcm = np.frombuffer(ulaw.decoder(bytes(audio)), dtype="<i2")
        parle = np.flatnonzero(np.abs(pcm.astype(np.int32)) > 400)
        if parle.size:
            audio = audio[max(0, int(parle[0]) - 1600): int(parle[-1]) + 3200]
        with open(f"{SORTIE}/client-{numero}.ulaw", "wb") as f:
            f.write(bytes(audio))
        print(f"réplique {numero} : {len(audio) / 8000:.1f} s — {texte.strip()!r}", flush=True)


class FauxTwilio:
    """Ce que Twilio enverrait : une trame de 20 ms toutes les 20 ms, du silence tant que
    le client se tait, ses répliques quand l'assistante a fini de parler."""

    def __init__(self):
        self.appel = None
        self.repliques = []
        for numero in range(1, len(REPLIQUES) + 1):
            with open(f"{SORTIE}/client-{numero}.ulaw", "rb") as f:
                self.repliques.append(f.read())
        self.piste_client = bytearray()
        self.piste_assistante = bytearray()
        self.ferme = False
        self._en_cours = b""
        self._suivante = 0
        self._tours_vus = 0
        self.forcees: list[int] = []
        self._prochaine_trame = None
        self._fin_de_replique = time.monotonic()

    def _depuis_la_derniere_replique(self) -> str:
        dits = [f["texte"] for f in self.appel.fragments if f["role"] == "assistant"]
        return "".join(dits[self._tours_vus:]).lower()

    def _c_est_a_moi(self) -> bool:
        """L'assistante a dit ce que la réplique suivante attend, puis s'est tue depuis
        plus d'une seconde. Si elle ne le dit jamais, le client finit par parler."""
        dit = self._depuis_la_derniere_replique()
        dernier = self.appel._dernier_son["assistant"]
        if not dit or not dernier or time.monotonic() - dernier < 1.2:
            return False
        if ATTENDUS[self._suivante](dit):
            return True
        if time.monotonic() - self._fin_de_replique > PATIENCE:
            self.forcees.append(self._suivante + 1)
            return True
        return False

    async def receive_text(self) -> str:
        maintenant = time.monotonic()
        if self._prochaine_trame is None:
            self._prochaine_trame = maintenant
        await asyncio.sleep(max(0.0, self._prochaine_trame - maintenant))
        self._prochaine_trame += 0.02
        if self.ferme:
            return json.dumps({"event": "stop"})
        if not self._en_cours and self._suivante < len(self.repliques) and self._c_est_a_moi():
            self._en_cours = self.repliques[self._suivante]
            self._suivante += 1
            self._tours_vus = sum(1 for f in self.appel.fragments if f["role"] == "assistant")
        if self._en_cours:
            trame, self._en_cours = self._en_cours[:160], self._en_cours[160:]
            trame = trame.ljust(160, b"\xff")
            if not self._en_cours:
                self._fin_de_replique = time.monotonic()
        else:
            trame = SILENCE
        self.piste_client += trame
        return json.dumps({"event": "media", "media": {"payload": base64.b64encode(trame).decode()}})

    async def send_text(self, texte: str) -> None:
        charge = base64.b64decode(json.loads(texte)["media"]["payload"])
        # La piste de l'assistante commence quand la session parle : on l'aligne sur celle
        # du client, qui tourne depuis le décroché.
        if not self.piste_assistante:
            self.piste_assistante += b"\xff" * max(0, len(self.piste_client) - len(charge))
        self.piste_assistante += charge

    async def close(self, code: int = 1000) -> None:
        self.ferme = True


async def converser() -> None:
    os.environ.setdefault("ENREGISTREMENT_APPELS", "0")
    from app import calls, db, reservations, tenants
    from app.voice import bot, live, ulaw

    # Pour la mesure : OpenRouter joint à chaque réponse ce qu'il a réellement facturé.
    d_origine = bot.llm_extra_body

    def avec_le_cout():
        extra = d_origine()
        return {"extra_body": {**extra.get("extra_body", {}), "usage": {"include": True}}}

    bot.llm_extra_body = avec_le_cout

    db.init_db()
    tenants.seed_demo_tenant()
    tenant = tenants.list_all()[0]
    numero = "+33612345678"
    os.environ["GPT_LIVE_ETABLISSEMENTS"] = str(tenant.id)
    call_sid = f"CAESSAI{int(time.time())}"
    call_id = calls.start_call(call_sid, tenant.id, numero)

    tenant_live, prompt = await live.preparer(tenant, numero)
    t0 = time.monotonic()
    session = await live.ouvrir(tenant_live, prompt, True)
    if session is None:
        print("la session ne s'est pas ouverte")
        return
    print(f"session ouverte en {time.monotonic() - t0:.2f} s — voix « {live.voix()} », "
          f"arrière-plan {live.modele_arriere()}", flush=True)

    twilio = FauxTwilio()
    origine = live.Appel.__init__

    def capter(self, *a, **k):
        origine(self, *a, **k)
        twilio.appel = self

    live.Appel.__init__ = capter
    relancer = live.Appel._relancer_l_accueil_si_muette

    async def relancer_et_dire(self):
        avant = self.accueil_relance
        await relancer(self)
        if self.accueil_relance and not avant:
            print(f"(accueil redonné {time.monotonic() - self._debut:.2f} s après le début de l'appel)", flush=True)

    live.Appel._relancer_l_accueil_si_muette = relancer_et_dire
    try:
        await asyncio.wait_for(live.run_live(twilio, session, "MZ-essai", call_sid, tenant_live,
                                             caller_number=numero, call_id=call_id,
                                             prompt_systeme=prompt), timeout=150)
    except asyncio.TimeoutError:
        print("⚠ conversation interrompue au bout de 150 s")
    appel = twilio.appel

    print("\n--- conversation ---")
    for tour in appel.tours():
        qui = "CLIENT   " if tour["role"] == "user" else "ASSISTANTE"
        print(f"{(tour['debut'] or 0) / 1000:6.1f} s  {qui}  {' '.join(tour['texte'].split())}")
    print("\n--- outils ---")
    for outil in appel.outils_appeles:
        print(f"{outil['nom']}  {json.dumps(outil['arguments'], ensure_ascii=False)}")
    print("\n--- mesures ---")
    print("blancs avant réponse (ms) :", appel.blancs_ms())
    print("accueil : première parole à", appel.premiere_parole_ms(), "ms ; relance :", appel.accueil_relance)
    print("secondes de session facturées :", appel.secondes_voix, "; jetons du cerveau :", appel.jetons)
    print("ce qu'OpenRouter dit avoir facturé pour le cerveau ($) :", appel.cout_annonce)
    print("raccroché par l'assistante :", appel.raccroche_par_nous)
    print("répliques dites sans attendre ce qu'elles attendaient :", twilio.forcees or "aucune")
    with db.get_conn() as conn:
        ligne = dict(conn.execute("SELECT status, duration_seconds, estimated_cost, cout_telephonie, "
                                  "cout_comprehension, cout_voix, voix_fournisseur, reservation_id "
                                  "FROM calls WHERE call_sid = ?", (call_sid,)).fetchone())
    print("au journal :", ligne)
    minutes = ligne["duration_seconds"] / 60
    caracteres = sum(len(t["content"]) for t in appel.transcription() if t["role"] == "assistant")
    print(json.dumps({"MESURE": {
        "cerveau": live.modele_arriere(), "voix": live.voix(), "duree_s": round(ligne["duration_seconds"], 1),
        "session_s": appel.secondes_voix, "cout_total": round(ligne["estimated_cost"], 5),
        "cout_voix": ligne["cout_voix"], "cout_cerveau": ligne["cout_comprehension"],
        "cout_cerveau_annonce": appel.cout_annonce, "cout_telephonie": ligne["cout_telephonie"],
        "par_minute": round(ligne["estimated_cost"] / minutes, 4), "jetons": appel.jetons,
        "delegations": appel.jetons["generations"], "outils": [o["nom"] for o in appel.outils_appeles],
        "blancs_ms": appel.blancs_ms(), "caracteres_dits": caracteres,
        "reservation": bool(ligne["reservation_id"]), "raccroche": appel.raccroche_par_nous}},
        ensure_ascii=False), flush=True)
    print("réservations en base :", [(r["customer_name"], r["date"], r["time"], r["party_size"],
                                      r["customer_phone"]) for r in reservations.list_reservations(tenant.id)])

    # La conversation telle qu'on l'entendrait au téléphone : 8 kHz, les deux voix mêlées.
    longueur = max(len(twilio.piste_client), len(twilio.piste_assistante))
    client = bytes(twilio.piste_client).ljust(longueur, b"\xff")
    assistante = bytes(twilio.piste_assistante).ljust(longueur, b"\xff")
    with wave.open(f"{SORTIE}/conversation-gpt-live-{live.voix()}.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(ulaw.mixer(ulaw.decoder(client), ulaw.decoder(assistante)))
    print(f"\nécrit : conversation-gpt-live-{live.voix()}.wav ({longueur / 8000:.0f} s)")


if __name__ == "__main__":
    asyncio.run(dire_les_repliques() if sys.argv[1:] == ["repliques"] else converser())
