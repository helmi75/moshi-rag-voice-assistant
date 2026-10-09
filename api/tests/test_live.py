"""Essai GPT-Live : la voix-à-voix d'OpenAI sur des établissements choisis (voice/live.py).

OpenAI n'est jamais appelé : la session est une doublure qui joue le protocole relevé
dans le service de Pipecat 1.12 (session.start → session.started, audio dans les deux
sens, délégations enveloppées dans `response.event`).

Ce qu'on tient :
- un établissement qui n'est pas nommé ne passe JAMAIS par GPT-Live ;
- une session qui ne s'ouvre pas (clé, crédit, réseau) rend l'appel au pipeline habituel ;
- l'audio est relayé tel quel dans les deux sens, au format du téléphone ;
- le cerveau est le nôtre (`llm.MODEL`) : GPT-Live confie le travail, notre modèle
  raisonne sur la conversation entendue, et ce qu'il rend est donné à dire ;
- un outil passe toujours par `llm.run_tool`, avec le numéro de l'appelant ; confié à un
  modèle d'OpenAI (`GPT_LIVE_MODELE`), son résultat lui est rendu avant de continuer ;
- l'appel est au journal : transcription, blancs, coût aux tarifs d'OpenAI ;
- à la réécoute, la voix de l'assistante tombe à l'instant où elle a parlé.
"""
import asyncio
import base64
import json
import time
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import calls, db, llm, reservations, tenants
from app.main import app
from app.voice import live, ulaw

DEMO_NUMBER = "+33100000000"
SON_CLIENT = base64.b64encode(b"\xff" * 160).decode()       # 20 ms de silence µ-law
SON_MARIE = base64.b64encode(b"\x7f" * 160).decode()


class FausseSession:
    """Ce qu'OpenAI répondrait. `scenario(evt)` rend les événements à émettre en réponse
    à chaque message reçu de l'application."""

    def __init__(self, scenario=None, refuse: str = ""):
        self.recus: list[dict] = []
        self.fermee = False
        self._scenario = scenario or (lambda evt, session: [])
        self._refuse = refuse
        self._file = None

    def _sortie(self):
        if self._file is None:
            self._file = asyncio.Queue()
        return self._file

    def emettre(self, *evenements):
        for evt in evenements:
            self._sortie().put_nowait(json.dumps(evt))

    async def send(self, texte):
        evt = json.loads(texte)
        self.recus.append(evt)
        if evt["type"] == "session.start":
            if self._refuse:
                self.emettre({"type": "error", "error": {"code": self._refuse, "message": "non"}})
            else:
                self.emettre({"type": "session.started", "session": {"id": "sess_1"}})
        elif evt["type"] == "session.close":
            self.emettre({"type": "session.closed", "reason": "close_requested",
                          "usage": {"seconds": 42.0}})
        self.emettre(*self._scenario(evt, self))

    async def recv(self):
        return await self._sortie().get()

    async def close(self):
        self.fermee = True

    def de_type(self, genre: str) -> list[dict]:
        return [e for e in self.recus if e["type"] == genre]


def _fragment(role: str, texte: str, debut: int, fin: int) -> dict:
    genre = "session.input_transcript.delta" if role == "user" else "session.output_transcript.delta"
    return {"type": genre, "delta": texte, "start_ms": debut, "end_ms": fin}


def _appel_d_outil(nom: str, arguments: dict, call_id: str = "call_1", delegation: str = "del_1") -> dict:
    return {"type": "response.event", "delegation_id": delegation, "event": {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "status": "completed", "call_id": call_id,
                 "name": nom, "arguments": json.dumps(arguments)}}}


def _reponse_finie(delegation: str = "del_1", entree: int = 4200, cache: int = 4000, sortie: int = 60) -> dict:
    return {"type": "response.event", "delegation_id": delegation, "event": {
        "type": "response.completed", "response": {"usage": {
            "input_tokens": entree, "output_tokens": sortie,
            "input_tokens_details": {"cached_tokens": cache}}}}}


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def tenant():
    return tenants.get_by_phone(DEMO_NUMBER)


@pytest.fixture(autouse=True)
def _essai(monkeypatch, tenant):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-jamais-utilisee")
    monkeypatch.setenv("GPT_LIVE_ETABLISSEMENTS", str(tenant.id))
    monkeypatch.delenv("GPT_LIVE_VOIX", raising=False)
    monkeypatch.delenv("GPT_LIVE_MODELE", raising=False)
    monkeypatch.setenv("ENREGISTREMENT_APPELS", "0")
    # La base est commune à toute la suite, et le serveur refuse une seconde réservation
    # au même créneau pour le même numéro : chaque test repart sans table à ce numéro.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM reservations WHERE tenant_id = ? AND customer_phone = ?",
                     (tenant.id, "+33612345678"))
    # L'assistante raccroche 2 s après son « au revoir » : les tests n'attendent pas.
    monkeypatch.setattr(live, "_SILENCE_AVANT_DE_RACCROCHER", 0.05)


@pytest.fixture()
def cerveau_openai(monkeypatch):
    """Le raisonnement confié à un modèle hébergé par OpenAI, pour comparer."""
    monkeypatch.setenv("GPT_LIVE_MODELE", "gpt-6-luna")


def _brancher(monkeypatch, session: FausseSession):
    async def connecter():
        return session

    monkeypatch.setattr(live, "_connecter", connecter)


def _debut(call_sid: str, appelant: str = "+33612345678") -> str:
    return json.dumps({"event": "start", "streamSid": "MZ-live", "start": {
        "streamSid": "MZ-live", "callSid": call_sid,
        "customParameters": {"To": DEMO_NUMBER, "From": appelant, "CallSid": call_sid}}})


def _appeler(client, call_sid: str, pendant_l_appel=None) -> list[dict]:
    """Joue un appel jusqu'à ce que l'application raccroche. Rend ce que Twilio a reçu."""
    recus = []
    run_bot = AsyncMock()
    with patch("app.main._get_bot_runner", return_value=run_bot):
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_text(json.dumps({"event": "connected"}))
            ws.send_text(_debut(call_sid))
            if pendant_l_appel:
                pendant_l_appel(ws)
            try:
                while True:
                    recus.append(json.loads(ws.receive_text()))
            except WebSocketDisconnect:
                pass
    assert run_bot.await_count == 0, "l'appel est passé par le pipeline habituel"
    return recus


def _ligne(call_sid: str) -> dict:
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,)).fetchone())


class TestQuiPasseParGptLive:
    def test_personne_par_defaut(self, monkeypatch, tenant):
        monkeypatch.delenv("GPT_LIVE_ETABLISSEMENTS")
        assert live.etablissements() == set() and live.actif(tenant) is False

    def test_seulement_les_etablissements_nommes(self, monkeypatch, tenant):
        monkeypatch.setenv("GPT_LIVE_ETABLISSEMENTS", f"{tenant.id + 1}, {tenant.id + 2}")
        assert live.actif(tenant) is False
        monkeypatch.setenv("GPT_LIVE_ETABLISSEMENTS", f" {tenant.id} ,99;abc,²")
        assert live.etablissements() == {tenant.id, 99} and live.actif(tenant) is True

    def test_jamais_sans_cle(self, monkeypatch, tenant):
        monkeypatch.delenv("OPENAI_API_KEY")
        assert live.actif(tenant) is False
        # Même choisi dans l'admin : sans clé, c'est la chaîne classique qui sert.
        assert live.actif(replace(tenant, moteur_voix="gpt_live")) is False

    def test_le_choix_de_l_admin_l_emporte_sur_la_liste_du_env(self, monkeypatch, tenant):
        """Le moteur se règle dans la fiche de l'établissement (05/10/2026). La liste du
        .env ne vaut plus que pour qui n'a aucun choix enregistré."""
        # Nommé dans la liste, mais réglé sur la chaîne classique : il n'y passe plus.
        classique = replace(tenant, moteur_voix="classique")
        assert live.moteur(classique) == live.CLASSIQUE and live.actif(classique) is False
        # Absent de la liste, mais réglé sur GPT-Live : il y passe.
        monkeypatch.delenv("GPT_LIVE_ETABLISSEMENTS")
        choisi = replace(tenant, moteur_voix="gpt_live")
        assert live.moteur(choisi) == live.GPT_LIVE and live.actif(choisi) is True

    def test_sans_choix_enregistre_l_ancien_reglage_vaut_encore(self, monkeypatch, tenant):
        """La migration ne change le moteur de personne : l'établissement de l'essai reste
        sur GPT-Live, les autres sur la chaîne classique."""
        assert tenant.moteur_voix is None and live.moteur(tenant) == live.GPT_LIVE
        monkeypatch.setenv("GPT_LIVE_ETABLISSEMENTS", str(tenant.id + 1))
        assert live.moteur(tenant) == live.CLASSIQUE

    def test_un_moteur_inconnu_ne_choisit_rien(self, monkeypatch, tenant):
        monkeypatch.delenv("GPT_LIVE_ETABLISSEMENTS")
        assert live.moteur(replace(tenant, moteur_voix="autre")) == live.CLASSIQUE
        assert live.moteur(None) == live.CLASSIQUE and live.actif(None) is False

    def test_un_etablissement_regle_sur_la_chaine_classique_ne_va_pas_chez_openai(
            self, client, monkeypatch, tenant):
        """De bout en bout : nommé dans la liste du .env, mais la fiche dit « classique »."""
        tenants.update_tenant(tenant.id, moteur_voix="classique")
        session = FausseSession()
        _brancher(monkeypatch, session)
        run_bot = AsyncMock()
        try:
            with patch("app.main._get_bot_runner", return_value=run_bot):
                with client.websocket_connect("/ws/voice") as ws:
                    ws.send_text(json.dumps({"event": "connected"}))
                    ws.send_text(_debut("CA-live-classique"))
                    fin = time.monotonic() + 2
                    while run_bot.await_count == 0 and time.monotonic() < fin:
                        time.sleep(0.01)
        finally:
            tenants.update_tenant(tenant.id, moteur_voix=None)
        assert run_bot.await_count == 1 and session.recus == []

    def test_un_etablissement_hors_essai_suit_le_pipeline_habituel(self, client, monkeypatch, tenant):
        monkeypatch.setenv("GPT_LIVE_ETABLISSEMENTS", str(tenant.id + 1))
        session = FausseSession()
        _brancher(monkeypatch, session)
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(_debut("CA-live-hors-essai"))
                fin = time.monotonic() + 2
                while run_bot.await_count == 0 and time.monotonic() < fin:
                    time.sleep(0.01)
        assert run_bot.await_count == 1 and session.recus == []

    @pytest.mark.parametrize("refus", ["credit_balance_exhausted", "forbidden"])
    def test_une_session_refusee_rend_l_appel_au_pipeline_habituel(self, client, monkeypatch, refus):
        session = FausseSession(refuse=refus)
        _brancher(monkeypatch, session)
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(_debut(f"CA-live-{refus}"))
                fin = time.monotonic() + 2
                while run_bot.await_count == 0 and time.monotonic() < fin:
                    time.sleep(0.01)
        assert run_bot.await_count == 1
        assert session.fermee is True

    def test_openai_injoignable_rend_l_appel_au_pipeline_habituel(self, client, monkeypatch, tenant):
        async def connecter():
            raise OSError("réseau")

        monkeypatch.setattr(live, "_connecter", connecter)
        assert asyncio.run(live.ouvrir(tenant, "prompt")) is None


class SessionQuiTombe(FausseSession):
    """La session s'ouvre, reçoit l'accueil à dire, puis la connexion à OpenAI se perd."""

    async def recv(self):
        if self.de_type("session.commentary.append"):
            raise OSError("connexion perdue")
        return await super().recv()


class TestLaChaineClassiqueEstLeSecours:
    """« Par défaut GPT-Live, en secours l'ancienne version » (Helmi, 05/10/2026). Une
    session qui ne s'ouvre pas rendait déjà l'appel à la chaîne classique ; ce qui manquait,
    c'est la suite — que les appelants suivants n'attendent pas chacun à leur tour, et
    qu'une session tombée en cours d'appel n'envoie pas tout l'établissement au restaurant
    alors que la chaîne classique fonctionne."""

    def _decrocher(self, client, call_sid: str) -> AsyncMock:
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(_debut(call_sid))
                fin = time.monotonic() + 2
                while run_bot.await_count == 0 and time.monotonic() < fin:
                    time.sleep(0.01)
        return run_bot

    def test_rien_n_est_a_l_ecart_tant_que_tout_va_bien(self, client, monkeypatch):
        session = FausseSession(lambda evt, s: [_fragment("assistant", " Bonjour. Au revoir.", 0, 900)]
                                if evt["type"] == "session.commentary.append" else [])
        _brancher(monkeypatch, session)
        _appeler(client, "CA-live-sain")
        assert live.a_l_ecart() is None and live.dernier_echec() is None

    def test_apres_un_refus_les_appels_suivants_ne_retentent_pas_openai(self, client, monkeypatch):
        ouvertures = []

        async def connecter():
            ouvertures.append(1)
            return FausseSession(refuse="credit_balance_exhausted")

        monkeypatch.setattr(live, "_connecter", connecter)
        assert self._decrocher(client, "CA-live-refus-1").await_count == 1
        assert "credit_balance_exhausted" in live.a_l_ecart()
        # Le deuxième appelant est servi par la chaîne classique sans attendre OpenAI.
        assert self._decrocher(client, "CA-live-refus-2").await_count == 1
        assert len(ouvertures) == 1
        echec = live.dernier_echec()
        assert echec["a_l_ecart"] is True and "refusée" in echec["motif"] and echec["le"].endswith("Z")

    def test_passe_le_delai_un_appel_retente_gpt_live(self, client, monkeypatch):
        live.mettre_a_l_ecart("session refusée (essai)")
        live._echec["depuis"] -= live.MISE_A_L_ECART_SECONDES + 1
        assert live.a_l_ecart() is None
        # L'admin sait encore qu'il y a eu un échec, mais plus rien n'est à l'écart.
        assert live.dernier_echec()["a_l_ecart"] is False
        session = FausseSession(lambda evt, s: [_fragment("assistant", " Bonjour. Au revoir.", 0, 900)]
                                if evt["type"] == "session.commentary.append" else [])
        _brancher(monkeypatch, session)
        _appeler(client, "CA-live-retente")
        assert session.de_type("session.start")

    def test_openai_injoignable_ou_trop_lent_met_aussi_a_l_ecart(self, monkeypatch, tenant):
        async def connecter():
            raise OSError("réseau")

        monkeypatch.setattr(live, "_connecter", connecter)
        assert asyncio.run(live.ouvrir(tenant, "prompt")) is None
        assert "OSError" in live.a_l_ecart()

        live.reinitialiser()
        monkeypatch.setenv("GPT_LIVE_DELAI_OUVERTURE", "1")

        class Muette(FausseSession):
            async def send(self, texte):
                self.recus.append(json.loads(texte))      # ne répond jamais « prête »

        _brancher(monkeypatch, Muette())
        assert asyncio.run(live.ouvrir(tenant, "prompt")) is None
        assert "trop longue" in live.a_l_ecart()

    def test_une_session_tombee_en_cours_d_appel_n_envoie_pas_les_suivants_au_restaurant(
            self, client, monkeypatch, tenant):
        from app import renvoi

        _brancher(monkeypatch, SessionQuiTombe())
        _appeler(client, "CA-live-tombe")
        # Ce client-là est passé au restaurant : on ne reprend pas un appel en route.
        assert renvoi.motif_a_la_fin_du_flux("CA-live-tombe") == renvoi.PIPELINE
        # Mais la chaîne classique n'a rien montré : l'établissement n'est pas « en panne »,
        # c'est GPT-Live qui est mis de côté.
        assert renvoi.panne_recente(tenant.id) is None
        assert "appel interrompu" in live.a_l_ecart()
        assert self._decrocher(client, "CA-live-apres-la-chute").await_count == 1

    def test_la_chaine_classique_qui_tombe_reste_une_panne(self, client, monkeypatch, tenant):
        """Contre-épreuve : c'est seulement quand GPT-Live servait que la panne lui revient."""
        from app import renvoi

        tenants.update_tenant(tenant.id, moteur_voix="classique")
        run_bot = AsyncMock(side_effect=RuntimeError("pipeline"))
        try:
            with patch("app.main._get_bot_runner", return_value=run_bot):
                with client.websocket_connect("/ws/voice") as ws:
                    ws.send_text(json.dumps({"event": "connected"}))
                    ws.send_text(_debut("CA-classique-tombe"))
                    try:
                        while True:
                            ws.receive_text()
                    except WebSocketDisconnect:
                        pass
        finally:
            tenants.update_tenant(tenant.id, moteur_voix=None)
        assert renvoi.panne_recente(tenant.id) == renvoi.PIPELINE and live.a_l_ecart() is None


class TestLeClientRaccrochePendantQuElleParle:
    """La voix arrive d'OpenAI alors que Twilio a déjà fermé la ligne : l'envoi échoue. Ce
    n'est pas une panne. Levée, l'erreur faisait renvoyer l'appel vers le restaurant, et
    mettait GPT-Live de côté trois minutes pour tous les établissements."""

    @pytest.mark.parametrize("erreur", [WebSocketDisconnect(1006),
                                        RuntimeError('Cannot call "send" once a close message has been sent.')])
    def test_l_ecoute_de_la_session_finit_sans_lever(self, erreur):
        class LigneFermee:
            async def send_text(self, texte):
                raise erreur

        session = FausseSession()
        session.emettre({"type": "session.output_audio.delta", "delta": SON_MARIE})
        appel = live.Appel(LigneFermee(), session, "MZ", "CA", None, None, None)
        asyncio.run(asyncio.wait_for(appel.ecouter_la_session(), timeout=2))
        assert appel._ecrits["assistante"] == 0          # rien n'est noté comme dit
        assert live.a_l_ecart() is None


class TestLaSession:
    def test_le_format_du_telephone_la_voix_et_les_outils_delegues(self, tenant, monkeypatch, cerveau_openai):
        monkeypatch.setenv("GPT_LIVE_VOIX", "cedar")
        config = live.configuration(tenant, "PROMPT")
        assert config["model"] == "gpt-live-1"
        assert config["audio"] == {"format": {"type": "audio/pcmu", "rate": 8000},
                                   "output": {"voice": "cedar"}}
        delegation = config["delegation"]
        assert delegation["type"] == "responses"
        assert delegation["responses"]["model"] == "gpt-6-luna"
        assert delegation["responses"]["parallel_tool_calls"] is False
        noms = [o["name"] for o in delegation["responses"]["tools"]]
        assert noms == [o["name"] for o in llm.TOOLS] and len(noms) == 6
        for outil in delegation["responses"]["tools"]:
            assert outil["type"] == "function" and outil["parameters"]["type"] == "object"

    def test_les_deux_modeles_recoivent_nos_regles(self, tenant, cerveau_openai):
        prompt = llm.build_system_prompt(tenant)
        config = live.configuration(tenant, prompt)
        voix, arriere = config["instructions"], config["delegation"]["responses"]["instructions"]
        assert voix.startswith(prompt) and arriere.startswith(prompt)
        # La voix n'annonce rien avant le retour du travail délégué.
        assert "DÉLÈGUE" in voix and "N'annonce jamais un résultat avant le retour" in voix
        assert "français de France" in voix
        assert "tu ne parles pas au client" in arriere

    def test_le_numero_connu_n_est_pas_redemande(self, tenant, cerveau_openai):
        """Essai du 04/10/2026 : avec `callback_number` dans le schéma, le modèle
        d'arrière-plan réclamait un numéro au lieu d'enregistrer la table."""
        def champs(config, nom):
            (outil,) = [o for o in config["delegation"]["responses"]["tools"] if o["name"] == nom]
            return set(outil["parameters"]["properties"])

        connu = live.configuration(tenant, "PROMPT", numero_connu=True)
        assert "callback_number" not in champs(connu, "create_reservation")
        assert "callback_number" not in champs(connu, "take_message")
        assert "ne le demande jamais" in connu["delegation"]["responses"]["instructions"]
        # Numéro masqué : le champ reste, c'est le seul moyen de rappeler le client.
        masque = live.configuration(tenant, "PROMPT", numero_connu=False)
        assert "callback_number" in champs(masque, "create_reservation")
        assert "ne le demande jamais" not in masque["delegation"]["responses"]["instructions"]
        # Le catalogue d'origine n'est pas modifié au passage.
        assert "callback_number" in llm.TOOLS[1]["input_schema"]["properties"]

    def test_par_defaut_la_voix_est_marin_et_le_cerveau_est_le_notre(self, tenant):
        assert live.voix() == "marin"
        assert live.modele_openai() == "" and live.modele_arriere() == llm.MODEL
        config = live.configuration(tenant, "PROMPT")
        # GPT-Live nous confie le travail : ni modèle ni outils chez OpenAI.
        assert config["delegation"] == {"type": "client"}
        assert "DÉLÈGUE" in config["instructions"]

    def test_l_accueil_porte_la_mention_d_information(self, tenant):
        from app import rgpd

        assert live.accueil(tenant) == rgpd.accueil(tenant)
        assert "MOT POUR MOT" in live.configuration(tenant, "PROMPT")["instructions"]


class TestUnAppel:
    def test_l_audio_est_relaye_dans_les_deux_sens_et_l_assistante_ouvre(self, client, monkeypatch, tenant):
        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [{"type": "session.output_audio.delta", "delta": SON_MARIE},
                        _fragment("assistant", " Bonjour, Le Bouchon Doré.", 200, 1500)]
            if evt["type"] == "session.input_audio.append" and len(session.de_type("session.input_audio.append")) == 2:
                return [_fragment("user", " Rien, merci.", 2000, 2800),
                        _fragment("assistant", " Très bien, au revoir.", 3400, 4200)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)

        def parler(ws):
            for _ in range(2):
                ws.send_text(json.dumps({"event": "media", "media": {"payload": SON_CLIENT}}))

        recus = _appeler(client, "CA-live-audio", parler)

        depart = session.de_type("session.start")[0]["session"]
        assert depart["audio"]["format"] == {"type": "audio/pcmu", "rate": 8000}
        # L'accueil est donné à dire dès l'ouverture, hors de toute délégation.
        from app import rgpd

        (accueil,) = session.de_type("session.commentary.append")
        assert accueil["delegation_id"] is None and accueil["content"] == rgpd.accueil(tenant)
        # La voix du client arrive telle quelle chez OpenAI, celle de Marie telle quelle chez Twilio.
        assert [e["audio"] for e in session.de_type("session.input_audio.append")] == [SON_CLIENT] * 2
        assert recus == [{"event": "media", "streamSid": "MZ-live", "media": {"payload": SON_MARIE}}]
        # L'assistante a pris congé : l'application raccroche et ferme la session.
        assert session.de_type("session.close") and session.fermee

        ligne = _ligne("CA-live-audio")
        assert json.loads(ligne["transcript"]) == [
            {"role": "assistant", "content": "Bonjour, Le Bouchon Doré."},
            {"role": "user", "content": "Rien, merci."},
            {"role": "assistant", "content": "Très bien, au revoir."}]
        # Le blanc ressenti : fin de la phrase du client (2 800 ms) → début de la réponse (3 400 ms).
        assert json.loads(ligne["turn_latencies"]) == [600]
        assert ligne["status"] == "completed" and ligne["voix_fournisseur"] == "gpt-live"

    def test_un_outil_passe_par_run_tool_et_son_resultat_revient_avant_de_continuer(
            self, client, monkeypatch, tenant, cerveau_openai):
        jour = (date.today() + timedelta(days=3)).isoformat()
        arguments = {"customer_name": "Lefèvre", "date": jour, "time": "20:30", "party_size": 6}

        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_fragment("assistant", " Bonjour.", 0, 500),
                        _fragment("user", " Une table pour six.", 1000, 2500),
                        _appel_d_outil("create_reservation", arguments), _reponse_finie()]
            if evt["type"] == "response.create":
                return [_fragment("assistant", " C'est enregistré. Au revoir.", 3200, 4800)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch("app.voice.live.llm.run_tool", wraps=llm.run_tool) as run_tool:
            _appeler(client, "CA-live-outil")

        # L'outil est exécuté par NOTRE code, avec le numéro venu du réseau téléphonique.
        (appel,) = run_tool.await_args_list
        assert appel.args[1:4] == ("create_reservation", arguments, "+33612345678")
        resa = [r for r in reservations.list_reservations(tenant.id) if r["customer_name"] == "Lefèvre"]
        assert len(resa) == 1 and resa[0]["customer_phone"] == "+33612345678"

        # Le résultat est rendu au modèle d'arrière-plan, PUIS on lui dit de continuer.
        types = [e["type"] for e in session.recus]
        rendu = session.de_type("response.item.create")[0]["item"]
        assert rendu["type"] == "function_call_output" and rendu["call_id"] == "call_1"
        assert json.loads(rendu["output"])["reservation_id"] == resa[0]["id"]
        assert types.index("response.item.create") < types.index("response.create")
        assert len(session.de_type("response.create")) == 1

        ligne = _ligne("CA-live-outil")
        assert ligne["reservation_id"] == resa[0]["id"]
        journal = json.loads(ligne["journal"])
        assert journal["voix"] == {"fournisseur": "gpt-live", "voix": "marin", "modele": "gpt-live-1",
                                   "arriere_plan": "gpt-6-luna", "cerveau": "openai"}
        assert journal["consommation"] == {"generations": 1, "jetons_entree": 4200, "jetons_cache": 4000,
                                           "jetons_sortie": 60, "secondes_voix": 42.0,
                                           "cout_cerveau_annonce": None}
        # Chiffré aux tarifs d'OpenAI : 200 jetons frais, 4 000 en cache, 60 en sortie.
        assert ligne["cout_comprehension"] == pytest.approx(200 * 0.10e-6 + 4000 * 0.01e-6 + 60 * 0.50e-6)

    def test_un_refus_du_serveur_est_rendu_tel_quel_au_modele(self, client, monkeypatch, tenant, cerveau_openai):
        """Les refus restent décidés par `run_tool` : ici une date passée."""
        hier = (date.today() - timedelta(days=1)).isoformat()

        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_appel_d_outil("create_reservation", {
                    "customer_name": "Passé", "date": hier, "time": "20:00", "party_size": 2}),
                    _reponse_finie()]
            if evt["type"] == "response.create":
                return [_fragment("assistant", " Ce jour est passé. Au revoir.", 100, 900)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        _appeler(client, "CA-live-refus")
        rendu = json.loads(session.de_type("response.item.create")[0]["item"]["output"])
        assert "error" in rendu
        assert [r for r in reservations.list_reservations(tenant.id) if r["customer_name"] == "Passé"] == []

    def test_un_appel_d_outil_annonce_deux_fois_n_est_execute_qu_une_fois(self, client, monkeypatch, cerveau_openai):
        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                appel = _appel_d_outil("check_availability", {
                    "date": (date.today() + timedelta(days=2)).isoformat(), "time": "20:00", "party_size": 2})
                return [appel, appel, _reponse_finie()]
            if evt["type"] == "response.create":
                return [_fragment("assistant", " Au revoir.", 100, 600)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch("app.voice.live.llm.run_tool", new=AsyncMock(return_value="{}")) as run_tool:
            _appeler(client, "CA-live-doublon")
        assert run_tool.await_count == 1 and len(session.de_type("response.item.create")) == 1

    def test_on_ne_dit_pas_de_continuer_avant_la_fin_de_la_reponse(self, client, monkeypatch, cerveau_openai):
        """`response.create` trop tôt est refusé par l'API (function_call_outputs_required)."""
        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_appel_d_outil("check_availability", {
                    "date": (date.today() + timedelta(days=2)).isoformat(), "time": "20:00", "party_size": 2})]
            if evt["type"] == "response.item.create":
                # Le résultat est rendu AVANT que la réponse ait fini : rien ne doit partir.
                assert session.de_type("response.create") == []
                return [_reponse_finie()]
            if evt["type"] == "response.create":
                return [_fragment("assistant", " Au revoir.", 100, 600)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch("app.voice.live.llm.run_tool", new=AsyncMock(return_value="{}")):
            _appeler(client, "CA-live-ordre")
        assert len(session.de_type("response.create")) == 1

    def test_le_client_raccroche_et_l_appel_est_quand_meme_clos(self, client, monkeypatch):
        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_fragment("assistant", " Bonjour.", 0, 400)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(_debut("CA-live-raccroche"))
                ws.send_text(json.dumps({"event": "media", "media": {"payload": SON_CLIENT}}))
                ws.send_text(json.dumps({"event": "stop"}))
                fin = time.monotonic() + 3
                while not session.fermee and time.monotonic() < fin:
                    time.sleep(0.01)
        assert session.fermee and session.de_type("session.close")
        fin = time.monotonic() + 3
        while _ligne("CA-live-raccroche")["ended_at"] is None and time.monotonic() < fin:
            time.sleep(0.02)
        ligne = _ligne("CA-live-raccroche")
        assert ligne["ended_at"] and json.loads(ligne["transcript"]) == [
            {"role": "assistant", "content": "Bonjour."}]


def _generation(texte=None, outil=None, entree=6000, cache=5000, sortie=40):
    """Une réponse de notre modèle, telle que le client OpenRouter la rend."""
    appels = None
    if outil:
        nom, arguments = outil
        appels = [SimpleNamespace(id="call_g1", function=SimpleNamespace(
            name=nom, arguments=json.dumps(arguments)))]
    usage = SimpleNamespace(prompt_tokens=entree, completion_tokens=sortie,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cache))
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=texte, tool_calls=appels))],
                           usage=usage)


def _notre_modele(*generations):
    creer = AsyncMock(side_effect=list(generations))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=creer))), creer


CONFIE = {"type": "session.delegation.created", "delegation": {"id": "del_9", "target": "client"}}


class TestLeCerveauEstLeNotre:
    def test_notre_modele_raisonne_sur_la_conversation_et_son_resultat_est_donne_a_dire(
            self, client, monkeypatch, tenant):
        jour = (date.today() + timedelta(days=3)).isoformat()
        arguments = {"customer_name": "Dupont", "date": jour, "time": "20:30", "party_size": 4}
        modele, creer = _notre_modele(_generation(outil=("create_reservation", arguments)),
                                      _generation("C'est enregistré pour quatre personnes."))

        def scenario(evt, session):
            dits = session.de_type("session.commentary.append")
            if evt["type"] == "session.commentary.append" and len(dits) == 1:      # l'accueil
                return [_fragment("assistant", " Bonjour.", 0, 500),
                        _fragment("user", " Une table pour quatre, au nom de Dupont.", 1000, 3000), CONFIE]
            if evt["type"] == "session.commentary.append" and len(dits) == 2:      # le résultat
                return [_fragment("assistant", " C'est enregistré. Au revoir.", 4000, 5200)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch.object(llm, "get_client", return_value=modele):
            _appeler(client, "CA-live-cerveau")

        # GPT-Live nous confie le travail : pas de modèle chez OpenAI, pas d'outil déclaré là-bas.
        assert session.de_type("session.start")[0]["session"]["delegation"] == {"type": "client"}
        # Notre modèle, sans raisonnement (sinon six secondes de silence), avec nos outils.
        premier = creer.await_args_list[0].kwargs
        assert premier["model"] == llm.MODEL
        assert premier["extra_body"] == {"reasoning": {"max_tokens": 0}}
        assert [o["function"]["name"] for o in premier["tools"]] == [o["name"] for o in llm.TOOLS]
        systeme, demande = premier["messages"][0], premier["messages"][1]
        assert systeme["role"] == "system" and "tu ne parles pas au client" in systeme["content"]
        assert systeme["content"].startswith(llm.build_system_prompt(tenant)[:200])
        assert "Client : Une table pour quatre, au nom de Dupont." in demande["content"]
        assert "Assistante : Bonjour." in demande["content"]
        # L'outil est passé par run_tool, avec le numéro venu du réseau.
        resa = [r for r in reservations.list_reservations(tenant.id) if r["customer_name"] == "Dupont"]
        assert len(resa) == 1 and resa[0]["customer_phone"] == "+33612345678"
        second = creer.await_args_list[1].kwargs["messages"]
        assert second[-1]["role"] == "tool" and json.loads(second[-1]["content"])["reservation_id"] == resa[0]["id"]
        # Ce que le modèle rend est donné à dire, rattaché au travail confié.
        resultat = session.de_type("session.commentary.append")[1]
        assert resultat["delegation_id"] == "del_9"
        assert resultat["content"] == "C'est enregistré pour quatre personnes."
        assert session.de_type("response.create") == []      # rien de l'autre mode

        ligne = _ligne("CA-live-cerveau")
        journal = json.loads(ligne["journal"])
        assert journal["voix"]["cerveau"] == "le nôtre" and journal["voix"]["arriere_plan"] == llm.MODEL
        assert journal["consommation"]["generations"] == 2
        assert journal["consommation"]["jetons_entree"] == 12000
        # Chiffré aux tarifs d'OpenRouter : 2 000 jetons frais, 10 000 en cache, 80 en sortie.
        assert ligne["cout_comprehension"] == pytest.approx(2000 * 0.30e-6 + 10000 * 0.03e-6 + 80 * 2.50e-6)
        assert ligne["reservation_id"] == resa[0]["id"]

    def test_ce_que_les_outils_ont_deja_rendu_est_rappele_au_tour_suivant(self, tenant):
        appel = live.Appel(None, None, "MZ", "CA", tenant, "+33612345678", None, "PROMPT")
        appel.fragments = [{"role": "user", "texte": "Samedi à vingt heures ?", "debut": 0, "fin": 900}]
        appel.outils_appeles.append({"nom": "check_availability",
                                     "arguments": {"date": "2026-10-10", "time": "20:00", "party_size": 2},
                                     "resultat": '{"available": true}'})
        demande = appel._demande_au_cerveau()
        assert "Client : Samedi à vingt heures ?" in demande
        assert 'check_availability({"date": "2026-10-10", "time": "20:00", "party_size": 2})' in demande
        assert '→ {"available": true}' in demande
        assert appel.consignes_du_cerveau.startswith("PROMPT") and "ne le demande jamais" in appel.consignes_du_cerveau

    @pytest.mark.parametrize("panne", [RuntimeError("OpenRouter 502"), asyncio.TimeoutError()])
    def test_un_cerveau_en_panne_rend_quand_meme_une_phrase(self, client, monkeypatch, panne):
        """Sans réponse à son travail confié, l'assistante garderait le silence."""
        modele = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(side_effect=panne))))

        def scenario(evt, session):
            dits = session.de_type("session.commentary.append")
            if evt["type"] == "session.commentary.append" and len(dits) == 1:
                return [_fragment("user", " Une table.", 0, 900), CONFIE]
            if evt["type"] == "session.commentary.append" and len(dits) == 2:
                return [_fragment("assistant", " Je n'y arrive pas. Au revoir.", 1500, 2600)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch.object(llm, "get_client", return_value=modele):
            _appeler(client, "CA-live-cerveau-" + type(panne).__name__)
        rendu = session.de_type("session.commentary.append")[1]
        assert rendu["delegation_id"] == "del_9" and "n'a pas pu être fait" in rendu["content"]
        # Redemandé avant d'abandonner (ASSISTANTE-148).
        assert modele.chat.completions.create.await_count == 3
        confie = json.loads(_ligne("CA-live-cerveau-" + type(panne).__name__)["journal"])["delegations"][0]
        assert confie["generations_ms"] == [None, None, None]

    def _une_table(self, client, monkeypatch, call_sid, *reponses):
        """Un client demande une table ; `reponses` sont ce que le modèle fait à chaque
        requête : une génération, une exception, ou « muet » (ne revient jamais)."""
        monkeypatch.setattr(live, "_delai_generation", lambda: 0.1)
        suite = list(reponses)

        async def creer(**kwargs):
            reponse = suite.pop(0)
            if reponse == "muet":
                await asyncio.sleep(30)
            if isinstance(reponse, Exception):
                raise reponse
            return reponse

        modele = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=creer)))

        def scenario(evt, session):
            dits = session.de_type("session.commentary.append")
            if evt["type"] == "session.commentary.append" and len(dits) == 1:
                return [_fragment("user", " Une table pour deux, au nom de Tenace.", 0, 900), CONFIE]
            if evt["type"] == "session.commentary.append" and len(dits) == 2:
                return [_fragment("assistant", " C'est enregistré. Au revoir.", 2000, 3000)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch.object(llm, "get_client", return_value=modele):
            _appeler(client, call_sid)
        assert not suite, "le modèle n'a pas été redemandé"
        return session, json.loads(_ligne(call_sid)["journal"])["delegations"][0]

    def test_une_generation_qui_ne_revient_pas_est_redemandee(self, client, monkeypatch, tenant):
        """Appels 268 et 269 du 09/10/2026 : une requête sans réponse mangeait les vingt
        secondes, et le client s'entendait dire de rappeler."""
        jour = (date.today() + timedelta(days=3)).isoformat()
        arguments = {"customer_name": "Tenace", "date": jour, "time": "20:30", "party_size": 2}
        session, confie = self._une_table(
            client, monkeypatch, "CA-live-muet", "muet",
            _generation(outil=("create_reservation", arguments)), "muet",
            _generation("C'est enregistré pour deux personnes."))
        assert session.de_type("session.commentary.append")[1]["content"] == "C'est enregistré pour deux personnes."
        # L'outil n'a tourné qu'une fois : seule la requête au modèle est rejouée.
        assert len([r for r in reservations.list_reservations(tenant.id) if r["customer_name"] == "Tenace"]) == 1
        durees = confie["generations_ms"]
        assert [d is None for d in durees] == [True, False, True, False]
        assert confie["outils"] == ["create_reservation"] and confie["ms"] >= 200

    def test_une_erreur_du_fournisseur_est_redemandee(self, client, monkeypatch):
        session, confie = self._une_table(
            client, monkeypatch, "CA-live-502", RuntimeError("OpenRouter 502"),
            _generation("Il me manque le jour et l'heure."))
        assert session.de_type("session.commentary.append")[1]["content"] == "Il me manque le jour et l'heure."
        assert [d is None for d in confie["generations_ms"]] == [True, False]

    def test_un_travail_confie_a_openai_ne_reveille_pas_notre_modele(self, client, monkeypatch, cerveau_openai):
        modele, creer = _notre_modele(_generation("jamais"))

        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [{"type": "session.delegation.created",
                         "delegation": {"id": "del_1", "target": "responses"}},
                        _fragment("assistant", " Au revoir.", 100, 600)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch.object(llm, "get_client", return_value=modele):
            _appeler(client, "CA-live-pas-le-notre")
        creer.assert_not_awaited()


class TestUneReservationAnnonceeSansEtreEnregistree:
    """ASSISTANTE-137, appel 265 du 08/10/2026 : « C'est réservé » et aucune table en base."""

    def _appel_265(self, client, monkeypatch, call_sid):
        """La voix annonce d'elle-même, sans rien confier au cerveau."""
        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_fragment("user", " Oui.", 0, 300),
                        _fragment("assistant", " Très bien, j'enregistre ça. C'est réservé. Au revoir.", 500, 2500)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch("app.notifications.planifier") as planifier:
            _appeler(client, call_sid)
        return planifier

    def test_l_appel_est_marque_a_verifier_et_le_restaurant_est_prevenu(self, client, monkeypatch, tenant):
        planifier = self._appel_265(client, monkeypatch, "CA-live-265")
        ligne = _ligne("CA-live-265")
        assert json.loads(ligne["journal"])["a_verifier"] == {
            "motif": "annonce_sans_trace", "phrase": "C'est réservé."}
        assert ligne["reservation_id"] is None
        (resto, evenement, donnees), _ = planifier.call_args
        assert resto.id == tenant.id and evenement == "annonce_sans_trace"
        assert donnees == {"phrase": "C'est réservé.", "caller_number": "+33612345678",
                           "appel_id": ligne["id"]}

    def test_la_fiche_de_l_appel_le_dit_au_restaurateur(self, client, monkeypatch):
        self._appel_265(client, monkeypatch, "CA-live-265-admin")
        identifiant = _ligne("CA-live-265-admin")["id"]
        admin = TestClient(app)
        admin.post("/admin/login", data={"email": "admin@test.local", "password": "test-admin-pass"})
        page = admin.get(f"/admin/calls/{identifiant}").text.replace("&#39;", "'")
        assert "À vérifier : annoncé au client, rien d'enregistré" in page
        assert "« C'est réservé. »" in page
        assert "À vérifier" in admin.get("/admin/calls").text

    def test_une_reservation_reellement_creee_ne_declenche_rien(self, client, monkeypatch, tenant):
        jour = (date.today() + timedelta(days=3)).isoformat()
        modele, _ = _notre_modele(
            _generation(outil=("create_reservation", {"customer_name": "Vrai", "date": jour,
                                                      "time": "20:30", "party_size": 2})),
            _generation("C'est enregistré pour deux personnes."))

        def scenario(evt, session):
            dits = session.de_type("session.commentary.append")
            if evt["type"] == "session.commentary.append" and len(dits) == 1:
                return [_fragment("user", " Une table pour deux, au nom de Vrai.", 0, 900), CONFIE]
            if evt["type"] == "session.commentary.append" and len(dits) == 2:
                return [_fragment("assistant", " C'est enregistré. Au revoir.", 2000, 3000)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch.object(llm, "get_client", return_value=modele), \
                patch("app.notifications.planifier") as planifier:
            _appeler(client, "CA-live-vraie")
        journal = json.loads(_ligne("CA-live-vraie")["journal"])
        assert "a_verifier" not in journal
        assert [a.args[1] for a in planifier.call_args_list] == ["reservation_creee"]
        assert session.de_type("session.commentary.append")[1]["content"] == "C'est enregistré pour deux personnes."
        confie = journal["delegations"][0]
        assert len(journal["delegations"]) == 1 and len(confie["generations_ms"]) == 2
        assert {c: v for c, v in confie.items() if c not in ("t_ms", "ms", "generations_ms")} == {
            "outils": ["create_reservation"],
            "rendu": "C'est enregistré pour deux personnes.", "dementi": False}

    def test_le_cerveau_qui_annonce_sans_avoir_rien_enregistre_n_est_pas_repete(self, client, monkeypatch):
        """Il rend « c'est enregistré » sans avoir appelé l'outil : ce n'est pas donné à dire."""
        modele, _ = _notre_modele(_generation("C'est enregistré pour quatre personnes."))

        def scenario(evt, session):
            dits = session.de_type("session.commentary.append")
            if evt["type"] == "session.commentary.append" and len(dits) == 1:
                return [_fragment("user", " Oui, c'est bien ça.", 0, 900), CONFIE]
            if evt["type"] == "session.commentary.append" and len(dits) == 2:
                return [_fragment("assistant", " Je l'enregistre maintenant. Au revoir.", 2000, 3000)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        with patch.object(llm, "get_client", return_value=modele):
            _appeler(client, "CA-live-dementi")
        rendu = session.de_type("session.commentary.append")[1]["content"]
        assert "Rien n'a été enregistré" in rendu and "C'est enregistré pour quatre" not in rendu
        journal = json.loads(_ligne("CA-live-dementi")["journal"])
        assert journal["delegations"][0]["dementi"] is True
        assert journal["delegations"][0]["rendu"] == "C'est enregistré pour quatre personnes."
        # Elle a promis d'enregistrer et rien n'a suivi : le client attend sa table.
        assert journal["a_verifier"]["phrase"] == "Je l'enregistre maintenant."
        # Le filtre « À vérifier » de la liste ne sort que des appels qui portent le constat.
        filtres = calls.list_calls(outcome="a_verifier", limit=200)
        assert _ligne("CA-live-dementi")["id"] in [c["id"] for c in filtres]
        assert all("a_verifier" in json.loads(c["journal"]) for c in filtres)
        admin = TestClient(app)
        admin.post("/admin/login", data={"email": "admin@test.local", "password": "test-admin-pass"})
        assert admin.get("/admin/calls?outcome=a_verifier").status_code == 200

    def test_au_second_dementi_on_ne_tourne_pas_en_rond(self, tenant):
        appel = live.Appel(None, None, "MZ", "CA", tenant, "+33612345678", None, "PROMPT")
        premier = appel._dementi("C'est enregistré.")
        assert "confie de nouveau ce travail" in premier
        appel.delegations.append({"t_ms": 0, "outils": [], "rendu": "C'est enregistré.", "dementi": True})
        assert "Constat du serveur : RIEN n'est enregistré" in appel._demande_au_cerveau()
        second = appel._dementi("C'est enregistré.")
        assert "n'a pas pu être enregistrée" in second and "confie" not in second

    def test_apres_une_vraie_ecriture_le_cerveau_est_cru(self, tenant):
        appel = live.Appel(None, None, "MZ", "CA", tenant, "+33612345678", None, "PROMPT")
        appel.outils_appeles.append({"nom": "create_reservation", "arguments": {},
                                     "resultat": '{"status": "confirmed", "reservation_id": 7}'})
        assert appel._dementi("C'est enregistré.") is None
        assert appel._dementi("Le créneau est disponible.") is None


class TestLAccueilEtLEnregistrement:
    def test_si_elle_n_ouvre_pas_l_appel_l_accueil_lui_est_redonne_une_fois(self, client, monkeypatch, tenant):
        from app import rgpd

        monkeypatch.setattr(live, "_ATTENTE_DE_L_ACCUEIL", 0.1)

        def scenario(evt, session):
            # Le premier accueil reste sans effet ; redonné, il la fait parler.
            if evt["type"] == "session.commentary.append" and len(session.de_type("session.commentary.append")) == 2:
                return [_fragment("assistant", " Bonjour. Au revoir.", 3000, 3900)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        _appeler(client, "CA-live-muette")
        donnes = session.de_type("session.commentary.append")
        assert [d["content"] for d in donnes] == [rgpd.accueil(tenant)] * 2      # deux fois, pas trois
        accueil = json.loads(_ligne("CA-live-muette")["journal"])["accueil"]
        assert accueil == {"premiere_parole_ms": 3000, "relance": True}

    def test_elle_n_est_pas_relancee_si_elle_a_parle(self, client, monkeypatch):
        monkeypatch.setattr(live, "_ATTENTE_DE_L_ACCUEIL", 0.3)
        monkeypatch.setattr(live, "_SILENCE_AVANT_DE_RACCROCHER", 0.6)   # l'appel dure plus que le délai

        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_fragment("assistant", " Bonjour. Au revoir.", 100, 900)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        _appeler(client, "CA-live-bavarde")
        assert len(session.de_type("session.commentary.append")) == 1

    def test_les_deux_pistes_sont_enregistrees(self, client, monkeypatch, tmp_path, tenant):
        monkeypatch.setenv("ENREGISTREMENT_APPELS", "1")
        monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path))

        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [{"type": "session.output_audio.delta", "delta": SON_MARIE}] * 12
            if evt["type"] == "session.input_audio.append" and len(session.de_type("session.input_audio.append")) == 12:
                return [_fragment("assistant", " Au revoir.", 100, 600)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)

        def parler(ws):
            for _ in range(12):
                ws.send_text(json.dumps({"event": "media", "media": {"payload": SON_CLIENT}}))

        _appeler(client, "CA-live-enregistre", parler)
        ligne = _ligne("CA-live-enregistre")
        from app.voice import enregistrement

        # 12 trames de 160 octets par piste, écrites en µ-law : rien n'est resté en tampon.
        # Celle de l'assistante peut en plus porter le silence qui la cale sur l'appelant.
        tailles = {piste: enregistrement.chemin(tenant.id, ligne["id"], piste).stat().st_size
                   for piste in enregistrement.PISTES}
        assert tailles["appelant"] == 12 * 160
        assert 12 * 160 <= tailles["assistante"] <= 24 * 160
        assert ligne["recording_bytes"] == sum(tailles.values())


class _Bandes:
    """Ce que l'enregistreur écrirait, gardé en mémoire : une suite d'octets par piste."""

    def __init__(self):
        self.pistes = {"appelant": bytearray(), "assistante": bytearray()}

    def ecrire(self, piste: str, pcm16: bytes) -> None:
        self.pistes[piste] += ulaw.encoder(pcm16)


TRAME = 160                                   # 20 ms au téléphone
VOIX = b"\x10" * TRAME                        # un son net, que µ-law rend à l'identique
AUTRE_VOIX = b"\x20" * TRAME


class TestLesDeuxVoixSontCalees:
    """Les deux pistes sont rejouées côte à côte depuis leur premier octet. Celle de
    l'appelant avance sans trou depuis le décroché ; OpenAI n'envoie rien tant que la
    session s'ouvre. Sans calage, la voix de l'assistante était collée au début de son
    fichier : à la réécoute, elle répondait avant la question (Helmi, 04/10/2026)."""

    def _appel(self):
        appel = live.Appel(None, None, "MZ", "CA", None, None, None)
        appel.enregistreur = _Bandes()
        self.heure = 1000.0                       # l'horloge de l'appel, qu'on avance à la main
        appel._horloge = lambda: self.heure
        return appel

    def _le_client(self, appel, trames: int, en_retard: float = 0.0) -> None:
        """Des trames de 20 ms, chacune reçue à l'instant où elle finit — ou `en_retard`."""
        self.heure += en_retard
        for _ in range(trames):
            self.heure += 0.02
            appel._enregistrer("appelant", base64.b64encode(b"\xff" * TRAME).decode())
        self.heure -= en_retard

    def _elle_dit(self, appel, son: bytes, trames: int, a_la_cadence: bool = True) -> None:
        for _ in range(trames):
            appel._enregistrer("assistante", base64.b64encode(son).decode())
            if a_la_cadence:
                self.heure += 0.02

    def _bande(self, appel) -> bytes:
        appel.vider_les_tampons()
        return bytes(appel.enregistreur.pistes["assistante"])

    def test_son_premier_mot_tombe_a_l_instant_ou_elle_l_a_dit(self):
        appel = self._appel()
        self._le_client(appel, 100)                                # 2 s depuis le décroché
        self._elle_dit(appel, VOIX, 10)
        bande = self._bande(appel)
        assert bande.find(VOIX) == 100 * TRAME
        assert bande[:100 * TRAME] == b"\xff" * (100 * TRAME)

    def test_les_trames_accumulees_pendant_l_ouverture_ne_faussent_pas_le_decroche(self):
        """La session met 1,5 s à s'ouvrir : les 75 premières trames arrivent d'un bloc."""
        appel = self._appel()
        self.heure += 1.5
        for _ in range(75):
            appel._enregistrer("appelant", base64.b64encode(b"\xff" * TRAME).decode())
        self._le_client(appel, 25)                                 # puis à la cadence
        self._elle_dit(appel, VOIX, 5)
        assert self._bande(appel).find(VOIX) == 100 * TRAME

    def test_un_silence_d_openai_ne_decale_pas_ce_qui_suit(self):
        appel = self._appel()
        self._le_client(appel, 1)
        self._elle_dit(appel, VOIX, 10, a_la_cadence=False)
        self._le_client(appel, 60)                                 # 1,2 s sans rien d'OpenAI
        self._elle_dit(appel, AUTRE_VOIX, 5)
        bande = self._bande(appel)
        assert bande.find(VOIX) == 1 * TRAME
        assert bande.find(AUTRE_VOIX) == 61 * TRAME

    def test_des_trames_du_client_en_retard_ne_decalent_pas_l_assistante(self):
        """Mesuré contre le vrai GPT-Live : après un à-coup d'une seconde, les sons
        d'OpenAI étaient traités avant les trames du client. Caler une piste sur l'autre
        plaçait alors l'assistante une seconde trop tôt, jusqu'à la fin de l'appel."""
        appel = self._appel()
        self._le_client(appel, 50)                                 # 1 s, à l'heure
        self.heure += 1.0                                          # 1 s d'à-coup : rien n'est traité
        self._elle_dit(appel, VOIX, 5, a_la_cadence=False)         # ses sons passent d'abord
        self._le_client(appel, 50, en_retard=0.0)
        assert self._bande(appel).find(VOIX) == 100 * TRAME        # 2 s après le décroché, pas 1

    def test_un_flux_regulier_n_est_pas_troue(self):
        appel = self._appel()
        for _ in range(50):
            self._le_client(appel, 1)
            appel._enregistrer("assistante", base64.b64encode(VOIX).decode())
        assert self._bande(appel) == b"\xff" * TRAME + VOIX * 50   # un seul calage, au début

    def test_un_flux_en_avance_n_est_ni_coupe_ni_retarde(self):
        appel = self._appel()
        self._le_client(appel, 1)
        self._elle_dit(appel, VOIX, 30, a_la_cadence=False)        # OpenAI envoie d'un bloc
        self._le_client(appel, 5)
        self._elle_dit(appel, AUTRE_VOIX, 2, a_la_cadence=False)
        assert self._bande(appel) == b"\xff" * TRAME + VOIX * 30 + AUTRE_VOIX * 2

    def test_un_son_arrive_d_un_bloc_laisse_une_file_sur_la_ligne_et_on_la_mesure(self):
        """Le flux d'OpenAI est continu : ce retard-là ne se rattrape pas tout seul."""
        appel = self._appel()
        self._le_client(appel, 50)
        self._elle_dit(appel, VOIX, 50, a_la_cadence=False)        # 1 s de son en un instant
        self._le_client(appel, 25)                                 # 0,5 s plus tard…
        self._elle_dit(appel, AUTRE_VOIX, 1, a_la_cadence=False)   # …la ligne a encore 0,5 s à jouer
        compteurs = appel.journal({})["compteurs"]
        assert compteurs["file_ligne_max_ms"] == 980               # avant le dernier son du bloc
        assert compteurs["file_ligne_fin_ms"] == 500

    def test_un_flux_a_la_cadence_ne_laisse_pas_de_file(self):
        appel = self._appel()
        self._le_client(appel, 10)
        for _ in range(50):
            appel._enregistrer("assistante", base64.b64encode(VOIX).decode())
            self._le_client(appel, 1)
        compteurs = appel.journal({})["compteurs"]
        assert compteurs["file_ligne_max_ms"] == 0 and compteurs["file_ligne_fin_ms"] == 0

    def test_sans_enregistrement_la_file_n_est_pas_mesuree_et_on_le_dit(self):
        appel = live.Appel(None, None, "MZ", "CA", None, None, None)
        assert appel.journal({})["compteurs"]["file_ligne_max_ms"] is None

    def test_un_enregistreur_en_panne_ne_touche_pas_l_appel(self):
        appel = self._appel()
        appel.enregistreur.ecrire = None                           # appeler None lèverait
        self._le_client(appel, 20)
        self._elle_dit(appel, VOIX, 20)


class TestLesTours:
    def _appel(self, fragments):
        appel = live.Appel(None, None, "MZ", "CA", None, None, None)
        appel.fragments = [dict(zip(("role", "texte", "debut", "fin"), f)) for f in fragments]
        return appel

    def test_les_fragments_proches_font_un_tour(self):
        appel = self._appel([("user", " Bonjour,", 0, 400), ("user", " une table", 500, 900),
                             ("user", " pour deux.", 2000, 2600),      # 1,1 s plus tard : autre tour
                             ("assistant", " Bien sûr.", 3100, 3600)])
        assert [t["content"] for t in appel.transcription()] == [
            "Bonjour, une table", "pour deux.", "Bien sûr."]

    def test_le_blanc_se_mesure_entre_le_client_et_la_reponse(self):
        appel = self._appel([("assistant", "Bonjour.", 0, 500), ("user", "Une table.", 1000, 2000),
                             ("assistant", "Pour combien ?", 2700, 3300),
                             ("user", "Deux.", 4000, 4400), ("assistant", "Parfait.", 4300, 4900)])
        # Le second tour chevauche la fin du client (elle écoute en parlant) : pas de blanc.
        assert appel.blancs_ms() == [700]

    def test_elle_ne_raccroche_pas_tant_qu_on_parle(self, monkeypatch):
        monkeypatch.setattr(live, "_SILENCE_AVANT_DE_RACCROCHER", 5.0)
        appel = self._appel([("assistant", "Merci, au revoir.", 0, 900)])
        appel._dernier_son["assistant"] = time.monotonic()
        assert appel._a_pris_conge() is False
        appel._dernier_son["assistant"] = time.monotonic() - 6
        assert appel._a_pris_conge() is True
        # Un « bonne journée » suivi d'une question du client n'est pas une fin d'appel.
        appel.fragments.append({"role": "user", "texte": "Attendez !", "debut": 5000, "fin": 5600})
        appel.fragments.append({"role": "assistant", "texte": "Oui ?", "debut": 6000, "fin": 6300})
        assert appel._a_pris_conge() is False

    def test_une_ligne_ou_plus_personne_ne_parle_est_rendue(self, monkeypatch):
        """Un combiné posé sans raccrocher : 0,05 $ la minute jusqu'à ce que Twilio coupe."""
        appel = self._appel([("assistant", "Autre chose ?", 0, 900)])
        appel._dernier_son["assistant"] = time.monotonic() - 44
        assert appel._a_pris_conge() is False
        appel._dernier_son["assistant"] = time.monotonic() - 46
        assert appel._a_pris_conge() is True
        # Personne n'a jamais rien dit : le délai court depuis le début de l'appel.
        muet = self._appel([])
        assert muet._a_pris_conge() is False
        muet._debut = time.monotonic() - 46
        assert muet._a_pris_conge() is True


class TestLeCout:
    def test_un_appel_gpt_live_est_chiffre_aux_tarifs_d_openai(self):
        """0,05 $ la minute de session, à la seconde ; gpt-6-luna à 0,10 / 0,01 / 0,50 $ le
        million de jetons (relevé du 04/10/2026) ; pas de poste de transcription."""
        conso = {"generations": 3, "jetons_entree": 12_000, "jetons_cache": 8_000, "jetons_sortie": 200,
                 "secondes_voix": 150.0}
        couts = calls.couts_appel(155, {"voix": {"fournisseur": "gpt-live", "cerveau": "openai"},
                                        "consommation": conso})
        assert couts["voix"] == pytest.approx(150 / 60 * 0.05)
        assert couts["transcription"] == 0.0
        assert couts["comprehension"] == pytest.approx(4_000 * 0.10e-6 + 8_000 * 0.01e-6 + 200 * 0.50e-6)
        # Notre cerveau : les mêmes jetons, aux tarifs d'OpenRouter (0,30 / 0,03 / 2,50 $).
        notre = calls.couts_appel(155, {"voix": {"fournisseur": "gpt-live", "cerveau": "le nôtre"},
                                        "consommation": conso})
        assert notre["comprehension"] == pytest.approx(4_000 * 0.30e-6 + 8_000 * 0.03e-6 + 200 * 2.50e-6)
        assert notre["voix"] == couts["voix"] and notre["transcription"] == 0.0
        assert couts["telephonie"] == pytest.approx(3 * 0.01)
        assert couts["fournisseur"] == "gpt-live"

    def test_sans_decompte_de_session_on_prend_la_duree_de_l_appel(self):
        couts = calls.couts_appel(120, {"voix": {"fournisseur": "gpt-live"}, "consommation": {}})
        assert couts["voix"] == pytest.approx(2 * 0.05) and couts["comprehension"] == 0.0

    def test_un_appel_habituel_n_est_pas_touche(self):
        normal = calls.couts_appel(120, {"voix": {"fournisseur": "voxtral"},
                                         "consommation": {"caracteres_voix": 500}})
        assert normal["transcription"] > 0 and normal["voix"] == pytest.approx(500 * 0.016 / 1000)


class TestDansLAdmin:
    def test_la_fiche_et_le_diagnostic_d_un_appel_gpt_live_s_affichent(self, client, monkeypatch):
        def scenario(evt, session):
            if evt["type"] == "session.commentary.append":
                return [_fragment("assistant", " Bonjour. Au revoir.", 0, 900)]
            return []

        session = FausseSession(scenario)
        _brancher(monkeypatch, session)
        _appeler(client, "CA-live-admin")
        identifiant = _ligne("CA-live-admin")["id"]
        admin = TestClient(app)
        admin.post("/admin/login", data={"email": "admin@test.local", "password": "test-admin-pass"})
        for adresse in (f"/admin/calls/{identifiant}", f"/admin/calls/{identifiant}/diagnostic",
                        "/admin/calls", "/admin/health"):
            assert admin.get(adresse).status_code == 200, adresse
