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
        self.envois: list[tuple[float, int, int, bytes]] = []   # (instant, déjà reçu, position, son)
        self.debut = time.monotonic()            # le décroché : le flux de Twilio tourne dès là
        self.ferme = False
        self._en_cours = b""
        self._suivante = 0
        self._tours_vus = 0
        self.forcees: list[int] = []
        self._prochaine_trame = self.debut       # pendant que la session s'ouvre, les trames s'accumulent
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
        # Ce que la ligne fait d'un son : elle le joue à son arrivée, ou à la suite du
        # précédent s'il n'est pas fini. C'est la vérité à laquelle on compare l'enregistrement.
        instant = time.monotonic() - self.debut
        deja = sum(len(e[3]) for e in self.envois[-1:]) + (self.envois[-1][1] if self.envois else 0)
        position = max(len(self.piste_assistante), int(instant * 8000))
        self.piste_assistante += b"\xff" * (position - len(self.piste_assistante))
        self.envois.append((instant, deja, position, charge))
        self.piste_assistante += charge

    async def close(self, code: int = 1000) -> None:
        self.ferme = True


def calage(twilio: "FauxTwilio", tenant_id: int, call_id: int) -> None:
    """Le flux d'OpenAI tel qu'il est arrivé, puis l'écart entre l'instant où chaque son a
    été entendu et sa place dans le fichier de l'assistante — sans calage (les sons mis
    bout à bout) et tel que l'enregistreur l'a écrit."""
    import numpy as np

    from app.voice import enregistrement, ulaw

    envois = twilio.envois
    if not envois:
        print("\n--- calage --- aucun son reçu")
        return
    tailles = sorted(len(e[3]) for e in envois)
    recu = sum(tailles) / 8000
    ecoule = envois[-1][0] - envois[0][0]
    attentes = [b[0] - a[0] - len(a[3]) / 8000 for a, b in zip(envois, envois[1:])]
    print("\n--- le flux d'OpenAI ---")
    print(f"premier son {envois[0][0]:.2f} s après le décroché ; {len(envois)} envois de "
          f"{tailles[0]} à {tailles[-1]} octets (médiane {tailles[len(tailles) // 2]})")
    print(f"{recu:.1f} s de son reçus en {ecoule:.1f} s ; plus long retard d'un envoi sur le "
          f"précédent : {max(attentes, default=0) * 1000:.0f} ms ; retards de plus de 100 ms : "
          f"{sum(1 for a in attentes if a > 0.1)}")

    fichier = enregistrement.chemin(tenant_id, call_id, "assistante")
    bande = fichier.read_bytes() if fichier.exists() else b""
    client = enregistrement.chemin(tenant_id, call_id, "appelant")
    print("\n--- calage de l'enregistrement ---")
    print(f"fichiers : appelant {client.stat().st_size / 8000:.1f} s, assistante {len(bande) / 8000:.1f} s "
          f"; entendu : appelant {len(twilio.piste_client) / 8000:.1f} s, "
          f"assistante {len(twilio.piste_assistante) / 8000:.1f} s")
    bout_a_bout, ecrit, curseur = [], [], 0
    for instant, deja, position, son in envois:
        pcm = np.frombuffer(ulaw.decoder(son), dtype=np.int16).astype(np.float64)
        if len(pcm) < 80 or np.sqrt(np.mean(pcm ** 2)) < 500:      # un silence ne se retrouve pas
            continue
        bout_a_bout.append((deja - position) / 8)
        trouve = bande.find(ulaw.encoder(ulaw.decoder(son)), curseur)
        if trouve >= 0:
            ecrit.append((trouve - position) / 8)
            curseur = trouve
    def resume(ecarts):
        if not ecarts:
            return "aucun son retrouvé"
        tries = sorted(ecarts)
        return (f"au premier mot {ecarts[0]:+.0f} ms, à la fin {ecarts[-1]:+.0f} ms, médiane "
                f"{tries[len(tries) // 2]:+.0f} ms, extrêmes {tries[0]:+.0f} / {tries[-1]:+.0f} ms "
                f"({len(ecarts)} sons)")
    print("sans calage (sons mis bout à bout) :", resume(bout_a_bout))
    print("dans le fichier enregistré         :", resume(ecrit))
    print(json.dumps({"CALAGE": {"premier_son_s": round(envois[0][0], 2),
                                 "bout_a_bout_ms": [round(e) for e in bout_a_bout[::25]],
                                 "fichier_ms": [round(e) for e in ecrit[::25]]}}), flush=True)


async def converser() -> None:
    os.environ["ENREGISTREMENT_APPELS"] = "1"          # pour comparer le fichier à ce qui a été entendu
    os.environ["ENREGISTREMENT_DIR"] = f"{SORTIE}/enregistrements"
    os.environ["ENREGISTREMENT_DISQUE_MINIMUM_MO"] = "0"
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

    twilio = FauxTwilio()                              # le client a décroché : sa piste tourne
    tenant_live, prompt = await live.preparer(tenant, numero)
    t0 = time.monotonic()
    session = await live.ouvrir(tenant_live, prompt, True)
    if session is None:
        print("la session ne s'est pas ouverte")
        return
    print(f"session ouverte en {time.monotonic() - t0:.2f} s — voix « {live.voix()} », "
          f"arrière-plan {live.modele_arriere()}", flush=True)

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

    # Un seul processus porte tous les appels : une boucle d'événements bloquée, c'est du son
    # qui n'avance plus, pour tout le monde. On note chaque à-coup de plus de 100 ms.
    blocages: list[tuple[float, float]] = []

    async def veiller():
        while True:
            avant = time.monotonic()
            await asyncio.sleep(0.02)
            retard = time.monotonic() - avant - 0.02
            if retard > 0.1:
                blocages.append((round(avant - twilio.debut, 2), round(retard, 2)))

    veilleur = asyncio.create_task(veiller())
    try:
        await asyncio.wait_for(live.run_live(twilio, session, "MZ-essai", call_sid, tenant_live,
                                             caller_number=numero, call_id=call_id,
                                             prompt_systeme=prompt), timeout=150)
    except asyncio.TimeoutError:
        print("⚠ conversation interrompue au bout de 150 s")
    veilleur.cancel()
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

    print("boucle bloquée plus de 100 ms (instant depuis le décroché, durée en s) :", blocages or "jamais")
    print("file d'attente de la ligne (ms) : au pire", appel._file_ms("max"), "; en fin d'appel", appel._file_ms("fin"))
    calage(twilio, tenant.id, call_id)

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
    if sys.argv[1:] == ["repliques"]:
        asyncio.run(dire_les_repliques())
    else:
        # « boucle » : asyncio nomme lui-même ce qui a tenu la boucle plus de 100 ms.
        import logging
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(converser(), debug=sys.argv[1:] == ["boucle"])
