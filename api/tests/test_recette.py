"""Recette d'appels automatique (app/recette/) : le client d'essai, ses verdicts, son
plafond et ses garde-fous. Aucun réseau : ni Twilio, ni OpenAI, ni Mistral.

Ce qu'on tient :
- le client ne parle qu'après avoir entendu l'assistante se taire, et finit toujours ;
- un verdict se lit en base : la table, le nom, l'absence de « À vérifier » ;
- rien ne part au-delà du plafond, la nuit, pendant un appel réel, ni vers un numéro qui
  n'est pas celui de l'établissement d'essai ;
- `/ws/recette` est fermée tant que la recette n'est pas activée, et n'obéit qu'à un jeton
  à usage unique.
"""
import asyncio
import base64
import json
import time
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import calls, db, horloge, renvoi, tenants, twilio_signature
from app.main import app
from app.recette import appelant, budget, etage2, gardes, scenarios, tarifs, verdicts, voix
from app.recette.client import SILENCE, Client, audible
from app.voice import ulaw

SECRET = "s" * 40
VOIX = ulaw.encoder((np.sin(np.arange(1600) / 5) * 9000).astype("<i2").tobytes())   # 200 ms audibles
REPLIQUE = ulaw.encoder((np.sin(np.arange(800) / 4) * 9000).astype("<i2").tobytes())  # 100 ms


class Horloge:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _client(n=2, **reglages):
    h = Horloge()
    return Client([REPLIQUE] * n, horloge=h,
                  **{"silence": 1.5, "patience": 25.0, "court": 0.0, **reglages}), h


def _trames(client, h, secondes):
    """Fait passer le temps trame par trame ; rend ce que le client a envoyé d'audible."""
    dit = 0
    for _ in range(int(secondes / 0.02)):
        h.t += 0.02
        dit += client.trame() != SILENCE
    return dit


class TestLeClientAttendSonTour:
    def test_le_silence_d_une_ligne_n_est_pas_une_voix(self):
        assert not audible(SILENCE) and not audible(b"") and audible(VOIX)

    def test_il_se_tait_tant_qu_elle_n_a_pas_parle(self):
        client, h = _client()
        assert _trames(client, h, 5) == 0 and client.dites == 0

    def test_il_attend_qu_elle_ait_fini_puis_qu_elle_se_soit_tue(self):
        client, h = _client()
        client.recu(VOIX)
        assert _trames(client, h, 1.0) == 0          # elle vient de finir : pas encore
        client.recu(VOIX)                            # elle reprend : le silence repart de zéro
        assert _trames(client, h, 1.0) == 0
        assert _trames(client, h, 1.5) > 0 and client.dites == 1

    def test_un_son_recu_d_un_bloc_dure_ce_qu_il_dure(self):
        """L'application envoie quatre secondes de voix d'un coup : elle parle encore."""
        client, h = _client()
        client.recu(VOIX * 20)
        assert _trames(client, h, 4.0) == 0
        assert _trames(client, h, 2.0) > 0

    def test_il_ne_redit_rien_tant_qu_elle_n_a_pas_repondu(self):
        client, h = _client()
        client.recu(VOIX)
        _trames(client, h, 3)
        assert client.dites == 1
        _trames(client, h, 10)
        assert client.dites == 1 and not client.fini

    def test_sans_reponse_il_finit_par_parler_et_on_le_sait(self):
        client, h = _client(patience=5.0)
        _trames(client, h, 6)
        assert client.dites == 1 and client.forcees == [1]

    def test_il_raccroche_quand_tout_est_dit_et_qu_elle_s_est_tue(self):
        client, h = _client(n=1, conge=3.0)
        client.recu(VOIX)
        _trames(client, h, 3)
        assert client.dites == 1 and not client.fini
        client.recu(VOIX)                            # « au revoir »
        _trames(client, h, 2)
        assert not client.fini
        _trames(client, h, 3)
        assert client.fini

    def test_un_appel_d_essai_ne_dure_jamais_plus_que_sa_limite(self):
        client, h = _client(duree_max=30.0)
        for _ in range(40):
            client.recu(VOIX * 5)                    # elle ne s'arrête jamais de parler
            _trames(client, h, 1)
        assert client.fini


def _appel(**champs):
    base = {"id": 7, "status": "completed", "ended_at": "2026-10-10T10:01:00Z", "reservation_id": 3,
            "journal": "{}", "transcript": json.dumps([
                {"role": "user", "content": "Oui."},
                {"role": "assistant", "content": "C'est enregistré au nom de Martin. Autre chose ?"}])}
    return {**base, **champs}


def _resa(**champs):
    return {"id": 3, "party_size": 2, "time": "20:00", "date": "2026-10-10",
            "customer_name": "Martin", "cancelled_at": None, **champs}


T17, C8, T6 = (scenarios.par_cle(c) for c in ("t17a", "c8", "t6"))


class TestLeVerdictSeLitEnBase:
    def test_une_reservation_conforme(self):
        etat, preuve = verdicts.juger(T17, _appel(), [_resa()])
        assert etat == verdicts.OK and "réservation 3" in preuve and "Martin" in preuve

    def test_un_appel_que_l_application_n_a_pas_recu(self):
        assert verdicts.juger(T17, None, [])[0] == verdicts.ECHEC

    def test_une_annonce_sans_table_echoue(self):
        etat, preuve = verdicts.juger(T17, _appel(reservation_id=None), [])
        assert etat == verdicts.ECHEC and "0 réservation" in preuve

    def test_un_appel_marque_a_verifier_echoue(self):
        journal = json.dumps({"a_verifier": {"phrase": "C'est réservé."}})
        etat, preuve = verdicts.juger(T17, _appel(journal=journal), [_resa()])
        assert etat == verdicts.ECHEC and "À vérifier" in preuve

    @pytest.mark.parametrize("champs,mot", [({"party_size": 4}, "couverts"), ({"time": "21:00"}, "21:00"),
                                             ({"customer_name": "Durand"}, "Durand"),
                                             # « lundi » réservé un mardi (vrai appel du 11/10/2026)
                                             ({"date": "2026-10-13"}, "mardi"), ({"date": "bientôt"}, "illisible")])
    def test_une_table_qui_n_est_pas_la_bonne_echoue(self, champs, mot):
        etat, preuve = verdicts.juger(T17, _appel(), [_resa(**champs)])
        assert etat == verdicts.ECHEC and mot in preuve

    def test_le_nom_corrige_ne_laisse_qu_une_reservation(self):
        lambert = _resa(party_size=3, time="20:30", customer_name="Lambert", date="2026-10-14")
        assert verdicts.juger(C8, _appel(), [lambert])[0] == verdicts.OK
        dupont = _resa(id=4, party_size=3, time="20:30", customer_name="Dupont")
        etat, preuve = verdicts.juger(C8, _appel(), [dupont, lambert])
        assert etat == verdicts.ECHEC and "2 réservation" in preuve
        # L'ancienne, annulée, ne compte pas.
        assert verdicts.juger(C8, _appel(), [{**dupont, "cancelled_at": "x"}, lambert])[0] == verdicts.OK

    def test_une_annonce_suivie_d_une_question_reste_une_annonce(self):
        # Vrai appel du 12/10/2026 : « C'est enregistré, autre chose ? »
        dite = _appel(transcript=json.dumps([{"role": "assistant", "content": "Très bien. C'est enregistré, autre chose ?"}]))
        assert verdicts.juger(T17, dite, [_resa()])[0] == verdicts.OK
        demande = _appel(transcript=json.dumps([{"role": "assistant", "content": "Rien n'est enregistré, autre chose ?"}]))
        assert verdicts.juger(T17, demande, [_resa()])[0] == verdicts.ECHEC

    def test_une_table_creee_sans_etre_annoncee_echoue(self):
        muette = _appel(transcript=json.dumps([{"role": "assistant", "content": "Au revoir."}]))
        etat, preuve = verdicts.juger(T17, muette, [_resa()])
        assert etat == verdicts.ECHEC and "jamais annoncée" in preuve

    def test_un_jour_ferme_ne_cree_rien_et_elle_le_dit(self):
        refus = _appel(reservation_id=None, transcript=json.dumps(
            [{"role": "assistant", "content": "Nous sommes fermés le dimanche."}]))
        assert verdicts.juger(T6, refus, [])[0] == verdicts.OK
        assert verdicts.juger(T6, refus, [_resa()])[0] == verdicts.ECHEC
        sans_le_dire = _appel(reservation_id=None, transcript=json.dumps(
            [{"role": "assistant", "content": "Au revoir."}]))
        assert verdicts.juger(T6, sans_le_dire, [])[0] == verdicts.ECHEC

    def test_un_appel_passe_en_secours_ou_non_clos_echoue(self):
        assert verdicts.juger(T17, _appel(secours_motif="session"), [_resa()])[0] == verdicts.ECHEC
        assert verdicts.juger(T17, _appel(ended_at=None), [_resa()])[0] == verdicts.ECHEC

    def test_le_silence_par_defaut_couvre_le_blanc_d_un_outil(self):
        """Une phrase longue puis cinq secondes de blanc : il ne répond pas dedans."""
        h = Horloge()
        client = Client([REPLIQUE], horloge=h)
        for _ in range(15):                          # trois secondes de parole
            client.recu(VOIX)
        assert _trames(client, h, 3.0 + 5.5) == 0
        assert _trames(client, h, 1.0) > 0

    def test_apres_une_phrase_courte_il_patiente_deux_fois_plus(self):
        """« Je vérifie tout de suite. » puis dix secondes de blanc (vrais appels du
        12/10/2026) : il ne dit pas « oui » à une phrase qui n'attendait rien."""
        h = Horloge()
        client = Client([REPLIQUE, REPLIQUE], horloge=h)
        for _ in range(7):                           # 1,4 s : une phrase courte
            client.recu(VOIX)
        assert _trames(client, h, 1.4 + 10.0) == 0
        for _ in range(15):                          # le récapitulatif arrive enfin
            client.recu(VOIX)
        assert _trames(client, h, 3.0 + 5.5) == 0, "courte puis longue : c'est le total qui compte"
        assert _trames(client, h, 1.0) > 0

    def test_une_table_dite_notee_est_annoncee(self):
        dite = _appel(transcript=json.dumps([{"role": "assistant", "content":
            "C'est bien noté pour vendredi seize octobre, à midi trente, au nom de Bernard."}]))
        assert verdicts.juger(T17, dite, [_resa()])[0] == verdicts.OK

    def test_un_echec_garde_la_fin_de_la_conversation(self):
        assert verdicts.conversation(_appel()) == ["client : Oui.",
                                                   "elle : C'est enregistré au nom de Martin. Autre chose ?"]
        assert verdicts.conversation(None) == []

    def test_le_client_d_essai_connait_tous_les_scenarios_des_vrais_appels(self):
        # Le 11/10/2026 : S10 composé par Twilio, puis son flux refusé (« scénario inconnu »).
        from app.recette import etage2
        for scenario in scenarios.SCENARIOS + (etage2.S10,):
            assert scenarios.par_cle(scenario.cle) is scenario

    def test_un_appel_gpt_live_servi_par_la_chaine_classique_n_est_pas_une_reussite(self):
        # Le 10/10/2026 : crédit OpenAI épuisé, la chaîne classique a servi les appels « GPT-Live ».
        appel = {"id": 4, "voix_fournisseur": "voxtral"}
        refus = verdicts.pas_le_bon_moteur("gpt_live", appel, "session refusée (credit_balance_exhausted)")
        assert "classique" in refus and "appel 4" in refus and "credit_balance_exhausted" in refus
        assert verdicts.pas_le_bon_moteur("gpt_live", {"id": 4, "voix_fournisseur": "gpt-live"}) is None
        assert verdicts.pas_le_bon_moteur("classique", appel) is None
        assert verdicts.pas_le_bon_moteur("gpt_live", None) is None

    def test_sur_la_chaine_classique_un_echec_est_a_regarder_pas_un_verdict(self):
        etat, preuve = verdicts.sur_la_chaine_classique(verdicts.ECHEC, "appel 3 : 0 réservation")
        assert etat == verdicts.A_REGARDER and "pas un verdict" in preuve and "appel 3" in preuve
        assert verdicts.sur_la_chaine_classique(verdicts.OK, "ok") == (verdicts.OK, "ok")

    def test_les_durees_du_cerveau_sont_relevees(self):
        journal = json.dumps({"delegations": [{"generations_ms": [400, None, 900]},
                                              {"generations_ms": [600]}]})
        assert verdicts.cerveau([_appel(journal=journal), _appel()]) == {
            "travaux": 2, "generations": 3, "sans_reponse": 1, "mediane_ms": 600, "max_ms": 900}


@pytest.fixture()
def carnet(tmp_path, monkeypatch):
    monkeypatch.setenv("RECETTE_CARNET", str(tmp_path / "recette" / "depenses.json"))
    monkeypatch.delenv("RECETTE_DEJA_CE_MOIS", raising=False)
    monkeypatch.delenv("RECETTE_PLAFOND_PAR_RECETTE", raising=False)
    monkeypatch.delenv("RECETTE_PLAFOND_PAR_MOIS", raising=False)


class TestLePlafond:
    def test_trois_dollars_par_recette_trente_par_mois(self, carnet):
        assert (budget.plafond_par_recette(), budget.plafond_par_mois()) == (3.0, 30.0)

    def test_un_appel_qui_ferait_depasser_la_recette_est_refuse(self, carnet):
        assert budget.refus(0.25, 2.70) is None
        assert "plafond par recette" in budget.refus(0.25, 2.80)

    def test_le_mois_compte_les_passages_deja_notes(self, carnet):
        mois = horloge.maintenant().strftime("%Y-%m")
        budget.noter({"le": f"{mois}-03T10:00", "cout": 20.0})
        budget.noter({"le": f"{mois}-05T10:00", "cout": 9.5})
        budget.noter({"le": "2020-01-05T10:00", "cout": 99.0})       # un autre mois
        assert budget.du_mois() == 29.5
        assert budget.refus(0.25, 0.0) is None
        assert "plafond du mois" in budget.refus(0.25, 0.30)

    def test_un_carnet_absent_est_vide_un_carnet_illisible_arrete_tout(self, carnet):
        """Pris pour vide, un carnet abîmé remettrait le mois à zéro et serait écrasé."""
        assert budget.lire() == [] and budget.du_mois() == 0
        budget.chemin().parent.mkdir(parents=True)
        budget.chemin().write_text("{pas du json", encoding="utf-8")
        with pytest.raises(budget.CarnetIllisible):
            budget.lire()
        with pytest.raises(budget.CarnetIllisible):
            budget.noter({"le": "2026-10-10T10:00", "cout": 1.0})
        assert budget.chemin().read_text(encoding="utf-8") == "{pas du json"

    def test_un_seul_passage_a_la_fois(self, carnet):
        with budget.verrou():
            with pytest.raises(BlockingIOError):
                with budget.verrou():
                    pass
        with budget.verrou():
            pass

    def test_l_etage_1_recoit_la_depense_du_mois_du_script(self, carnet, monkeypatch):
        monkeypatch.setenv("RECETTE_DEJA_CE_MOIS", "29.9")
        assert "plafond du mois" in budget.refus(0.25)

    def test_la_borne_d_un_vrai_appel_depasse_celle_d_un_banc(self):
        assert tarifs.borne("gpt_live", 2) > tarifs.borne("gpt_live", 1) > 0
        assert tarifs.borne("classique", 2) > tarifs.borne("classique", 1) > 0


@pytest.fixture()
def essai_actif(monkeypatch):
    resto = tenants.create_tenant("Banc d'essai (test)", f"+3397{int(time.time() * 1000) % 10_000_000:07d}")
    monkeypatch.setenv("RECETTE_JETON", SECRET)
    monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(resto.id))
    gardes._nonces_vus.clear()
    # La base est commune à la suite : d'autres tests y laissent des appels jamais clos.
    with db.get_conn() as conn:
        conn.execute("UPDATE calls SET ended_at = started_at WHERE ended_at IS NULL")
    yield resto
    tenants.delete_tenant(resto.id)


class TestLesGardeFous:
    def test_sans_secret_ou_avec_un_secret_court_rien_n_est_actif(self, monkeypatch, essai_actif):
        assert gardes.active()
        monkeypatch.setenv("RECETTE_JETON", "court")
        assert not gardes.active()
        monkeypatch.setenv("RECETTE_JETON", SECRET)
        monkeypatch.delenv("RECETTE_ETABLISSEMENT")
        assert not gardes.active()

    def test_jamais_la_nuit(self):
        assert gardes.la_nuit(datetime(2026, 10, 10, 14, 0, tzinfo=horloge.FUSEAU)) is None
        assert "pas d'appel d'essai" in gardes.la_nuit(datetime(2026, 10, 10, 23, 30, tzinfo=horloge.FUSEAU))
        assert gardes.la_nuit(datetime(2026, 10, 10, 6, 0, tzinfo=horloge.FUSEAU))

    def test_jamais_pendant_un_appel_reel(self, essai_actif):
        autre = tenants.create_tenant("Vrai resto (test)", f"+3398{int(time.time() * 1000) % 10_000_000:07d}")
        try:
            calls.start_call("CA-recette-essai-en-cours", essai_actif.id, "+33612345678")
            assert gardes.appel_reel_en_cours() is None       # un appel d'essai ne bloque pas
            calls.start_call("CA-recette-reel-en-cours", autre.id, "+33612345678")
            assert "appel réel en cours" in gardes.appel_reel_en_cours()
            calls.finish_call("CA-recette-reel-en-cours")
            assert gardes.appel_reel_en_cours() is None
        finally:
            tenants.delete_tenant(autre.id)

    def test_un_seul_passage_de_vrais_appels_par_deploiement(self):
        assert gardes.deja_joue([{"demarrage": "abc", "etage2": True}], "abc")
        assert gardes.deja_joue([{"demarrage": "abc", "etage2": False}], "abc") is None
        assert gardes.deja_joue([{"demarrage": "avant", "etage2": True}], "abc") is None

    def test_seul_le_numero_de_l_etablissement_d_essai_se_compose(self):
        nous = {"+33500000001", "+17600000002"}
        assert gardes.destinataire_refuse("+33500000001", nous, "+33500000001") is None
        assert gardes.destinataire_refuse("+17600000002", nous, "+33500000001")     # à nous, mais pas l'essai
        assert gardes.destinataire_refuse("+33612345678", nous, "+33612345678")     # pas à nous
        assert gardes.destinataire_refuse("", nous, "")

    def test_le_jeton_ne_sert_qu_une_fois_et_dix_minutes(self, essai_actif):
        nonce = gardes.nouveau_nonce()
        bon = gardes.jeton("t17a", nonce)
        assert not gardes.jeton_valide("c8", nonce, bon)            # un autre scénario
        assert not gardes.jeton_valide("t17a", nonce, "0" * 64)
        assert not gardes.jeton_valide("t17a", nonce, "é" * 64)       # non ASCII : refusé, sans lever
        assert gardes.jeton_valide("t17a", nonce, bon)
        assert not gardes.jeton_valide("t17a", nonce, bon)          # rejoué
        vieux = f"{int(time.time()) - gardes.DUREE_DU_JETON - 5}-abcd"
        assert not gardes.jeton_valide("t17a", vieux, gardes.jeton("t17a", vieux))

    def test_les_prealables_d_un_vrai_appel(self, essai_actif, monkeypatch, carnet):
        monkeypatch.setattr(gardes, "la_nuit", lambda *a: None)
        nous = {essai_actif.phone_number, "+17600000002"}
        assert etage2.prealables([], essai_actif, nous, "+17600000002") is None
        assert "pas un de nos numéros" in etage2.prealables([], essai_actif, {"+17600000002"}, "+17600000002")
        assert "ligne présentée" in etage2.prealables([], essai_actif, nous, "+33612345678")
        assert "ligne présentée" in etage2.prealables([], essai_actif, nous, essai_actif.phone_number)
        assert "n'existe pas" in etage2.prealables([], None, nous, "+17600000002")
        # Une faute de frappe dans RECETTE_ETABLISSEMENT désigne un vrai restaurant : refus.
        tenants.update_tenant(essai_actif.id, name="Chez Vrai")
        assert "ne s'appelle pas" in etage2.prealables([], tenants.get_by_id(essai_actif.id), nous, "+17600000002")
        tenants.update_tenant(essai_actif.id, name="Banc d'essai (test)")
        monkeypatch.delenv("RECETTE_JETON")
        assert "non activée" in etage2.prealables([], essai_actif, nous, "+17600000002")

    def test_la_purge_refuse_un_etablissement_qui_ne_s_appelle_pas_essai(self, essai_actif):
        """`RECETTE_ETABLISSEMENT` mal saisi : les appels d'un vrai restaurant ne s'effacent pas."""
        from app import essai

        calls.start_call("CA-recette-vrai-resto", essai_actif.id, "+33612345678")
        calls.finish_call("CA-recette-vrai-resto")
        tenants.update_tenant(essai_actif.id, name="Chez Vrai")
        assert essai.purger()["appels"] == 0 and calls.par_sid("CA-recette-vrai-resto") is not None
        tenants.update_tenant(essai_actif.id, name="Banc d'essai (test)")
        assert essai.purger()["appels"] >= 1 and calls.par_sid("CA-recette-vrai-resto") is None

    def test_le_twiml_branche_la_jambe_appelante_sur_notre_flux_avec_un_jeton(self, essai_actif, monkeypatch):
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://app.exemple.fr/ws/voice")
        twiml = etage2.twiml("t17a")
        assert '<Stream url="wss://app.exemple.fr/ws/recette">' in twiml and twiml.endswith("<Hangup/></Response>")
        assert 'name="scenario" value="t17a"' in twiml and SECRET not in twiml


def _debut(scenario, nonce, jeton):
    return json.dumps({"event": "start", "streamSid": "MZ-recette", "start": {
        "streamSid": "MZ-recette", "callSid": "CA-recette",
        "customParameters": {"scenario": scenario, "nonce": nonce, "jeton": jeton}}})


class TestLaJambeAppelante:
    @pytest.fixture()
    def sans_signature(self, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "off")

    def test_fermee_tant_que_la_recette_n_est_pas_activee(self, monkeypatch, sans_signature):
        monkeypatch.delenv("RECETTE_JETON", raising=False)
        with pytest.raises(WebSocketDisconnect):
            with TestClient(app).websocket_connect("/ws/recette"):
                pass

    def test_refusee_sans_la_signature_de_twilio(self, essai_actif, monkeypatch):
        monkeypatch.setenv("TWILIO_SIGNATURE", "enforce")
        with pytest.raises(WebSocketDisconnect):
            with TestClient(app).websocket_connect("/ws/recette"):
                pass
        signature = twilio_signature.signature_attendue("test-auth-token", appelant.url_du_flux(), {})
        with TestClient(app).websocket_connect(
                "/ws/recette", headers={twilio_signature.EN_TETE: signature}) as ws:
            ws.send_text(_debut("t17a", "x", "y"))        # signée, mais sans jeton valable
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_un_jeton_forge_ne_fait_rien_jouer(self, essai_actif, sans_signature, monkeypatch, tmp_path):
        # Les répliques sont sur le disque : seul le jeton empêche qu'elles soient jouées.
        monkeypatch.setenv("RECETTE_REPLIQUES_DIR", str(tmp_path))
        monkeypatch.setattr(voix, "voix_du_client", lambda langue: "voxtral/essai")
        for texte in scenarios.par_cle("t17a").repliques:
            voix._chemin(texte, "voxtral/essai").write_bytes(REPLIQUE)
        with TestClient(app).websocket_connect("/ws/recette") as ws:
            ws.send_text(_debut("t17a", gardes.nouveau_nonce(), "0" * 64))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_avec_un_jeton_valable_le_client_joue_ses_repliques(self, essai_actif, sans_signature,
                                                               monkeypatch, tmp_path):
        monkeypatch.setenv("RECETTE_REPLIQUES_DIR", str(tmp_path))
        monkeypatch.setattr(voix, "voix_du_client", lambda langue: "voxtral/essai")
        scenario = scenarios.par_cle("t6")
        for texte in scenario.repliques:
            voix._chemin(texte, "voxtral/essai").write_bytes(REPLIQUE)
        monkeypatch.setattr(appelant, "Client", lambda repliques: Client(
            repliques, silence=0.05, patience=0.3, conge=0.1))
        nonce = gardes.nouveau_nonce()
        audibles = 0
        with TestClient(app).websocket_connect("/ws/recette") as ws:
            ws.send_text(_debut("t6", nonce, gardes.jeton("t6", nonce)))
            ws.send_text(json.dumps({"event": "media", "media": {"payload": base64.b64encode(VOIX).decode()}}))
            try:
                while True:
                    message = json.loads(ws.receive_text())
                    assert message["event"] == "media" and message["streamSid"] == "MZ-recette"
                    audibles += audible(base64.b64decode(message["media"]["payload"]))
            except WebSocketDisconnect:
                pass
        assert audibles >= 2 * (len(REPLIQUE) // 160)       # ses deux répliques, puis il raccroche

    @pytest.mark.parametrize("message", ["[1, 2]", "pas du json", '{"event": "start", "start": "texte"}'])
    def test_un_message_mal_forme_ferme_le_flux_sans_erreur(self, essai_actif, sans_signature, message):
        with TestClient(app).websocket_connect("/ws/recette") as ws:
            for _ in range(10):
                ws.send_text(message)
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_sans_repliques_sur_le_disque_rien_n_est_joue(self, essai_actif, sans_signature, monkeypatch, tmp_path):
        monkeypatch.setenv("RECETTE_REPLIQUES_DIR", str(tmp_path / "vide"))
        monkeypatch.setattr(voix, "voix_du_client", lambda langue: "voxtral/essai")
        nonce = gardes.nouveau_nonce()
        with TestClient(app).websocket_connect("/ws/recette") as ws:
            ws.send_text(_debut("t6", nonce, gardes.jeton("t6", nonce)))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


class TestLesScenarios:
    def test_chaque_scenario_tient_dans_les_horaires_de_l_etablissement_d_essai(self):
        from app import disponibilite

        assert disponibilite.charger(json.dumps(scenarios.HORAIRES)) is not None
        for scenario in scenarios.SCENARIOS + (etage2.S10,):
            if scenario.attendu:
                heure = scenario.attendu["time"]
                assert any(debut <= heure <= fin for debut, fin in scenarios.HORAIRES["semaine"]["lundi"]), scenario.cle
        assert scenarios.HORAIRES["semaine"]["dimanche"] == []          # ce que T6 éprouve

    def test_les_creneaux_sont_tous_differents(self):
        """À l'étage 2 tous les appels viennent du même numéro : le serveur refuserait une
        seconde réservation au même créneau."""
        vus = [s.repliques[0] for s in scenarios.SCENARIOS + (etage2.S10,) if s.attendu]
        assert len(vus) == len(set(vus))
        assert len({s.cle for s in scenarios.SCENARIOS}) == len(scenarios.SCENARIOS)


def _porte(action, nonce=None, jeton=None, **en_plus):
    nonce = nonce or gardes.nouveau_nonce()
    jeton = gardes.jeton("secours-" + action, nonce) if jeton is None else jeton
    return TestClient(app).post("/recette/secours", data={
        "action": action, "nonce": nonce, "jeton": jeton, **en_plus})


class TestLaPorteDuRepondeur:
    """S6 à S8 sans numéro de secours : la recette met l'établissement d'essai sur répondeur
    par une porte, parce que la mémoire des pannes est celle du processus de production."""

    def test_porte_du_repondeur_fermee_tant_que_la_recette_n_est_pas_activee(self, essai_actif, monkeypatch):
        monkeypatch.delenv("RECETTE_JETON")
        assert _porte("essayer").status_code == 404

    def test_porte_du_repondeur_fermee_sans_etablissement_d_essai(self, essai_actif, monkeypatch):
        monkeypatch.delenv("RECETTE_ETABLISSEMENT")
        assert _porte("essayer").status_code == 404

    def test_porte_du_repondeur_refuse_un_etablissement_qui_ne_s_appelle_pas_essai(self, essai_actif, monkeypatch):
        vrai = tenants.create_tenant("Chez Marcel", f"+3396{int(time.time() * 1000) % 10_000_000:07d}")
        try:
            monkeypatch.setenv("RECETTE_ETABLISSEMENT", str(vrai.id))
            assert _porte("essayer").status_code == 404
            assert not renvoi.panne_recente(vrai.id)
        finally:
            tenants.delete_tenant(vrai.id)

    def test_porte_du_repondeur_refuse_sans_jeton(self, essai_actif):
        assert _porte("essayer", jeton="").status_code == 403
        assert _porte("essayer", jeton="0" * 64).status_code == 403
        assert not renvoi.essai_en_cours(essai_actif.id)

    def test_porte_du_repondeur_refuse_le_jeton_d_une_autre_action(self, essai_actif):
        nonce = gardes.nouveau_nonce()
        assert _porte("essayer", nonce, gardes.jeton("secours-arreter", nonce)).status_code == 403
        assert _porte("essayer", nonce, gardes.jeton("t6", nonce)).status_code == 403
        assert not renvoi.essai_en_cours(essai_actif.id)

    def test_porte_du_repondeur_refuse_un_jeton_perime_ou_deja_servi(self, essai_actif):
        vieux = f"{int(time.time()) - gardes.DUREE_DU_JETON - 5}-abcd"
        assert _porte("essayer", vieux).status_code == 403
        nonce = gardes.nouveau_nonce()
        assert _porte("essayer", nonce).status_code == 204
        assert _porte("essayer", nonce).status_code == 403

    def test_porte_du_repondeur_refuse_une_action_inconnue(self, essai_actif):
        assert _porte("detruire").status_code == 400

    def test_porte_du_repondeur_demarre_puis_arrete_l_essai(self, essai_actif):
        assert _porte("essayer").status_code == 204
        assert renvoi.essai_en_cours(essai_actif.id)
        assert _porte("arreter").status_code == 204
        assert not renvoi.essai_en_cours(essai_actif.id)

    def test_porte_du_repondeur_n_agit_que_sur_l_etablissement_d_essai(self, essai_actif):
        autre = tenants.create_tenant("Chez Odette", f"+3395{int(time.time() * 1000) % 10_000_000:07d}")
        try:
            assert _porte("essayer", tenant_id=autre.id, id=autre.id).status_code == 204
            assert renvoi.essai_en_cours(essai_actif.id)
            assert not renvoi.panne_recente(autre.id)
        finally:
            tenants.delete_tenant(autre.id)


def _message(**champs):
    return {"subject": "Message vocal à rappeler", **champs}


def _appel_repondeur(**champs):
    return _appel(**{"secours_motif": "essai", "status": calls.SECOURS_MESSAGE,
                     "reservation_id": None, "recording_bytes": 24000, **champs})


class TestLeVerdictDuRepondeur:
    def test_un_message_note_avec_son_audio_est_vert(self):
        assert verdicts.juger_repondeur(_appel_repondeur(), [_message()], [])[0] == verdicts.OK

    @pytest.mark.parametrize("appel, messages, resas, mot", [
        (None, [], [], "aucun appel"),
        (_appel_repondeur(), [], [], "Message vocal"),
        (_appel_repondeur(recording_bytes=None), [_message()], [], "audio"),
        (_appel_repondeur(secours_motif=None), [_message()], [], "secours d'essai"),
        (_appel_repondeur(status="completed"), [_message()], [], "message vocal"),
        (_appel_repondeur(), [_message()], [_resa()], "réservation"),
    ])
    def test_il_est_rouge_quand_un_element_manque(self, appel, messages, resas, mot):
        etat, preuve = verdicts.juger_repondeur(appel, messages, resas)
        assert etat == verdicts.ECHEC and mot in preuve


class TestLeRepondeurEtLaRecetteDAppels:
    def test_le_scenario_regle_le_client_pour_parler_dans_les_cinq_secondes_du_bip(self):
        r = scenarios.REPONDEUR
        assert r not in scenarios.SCENARIOS, "l'étage 1 n'a pas de Twilio pour enregistrer"
        assert scenarios.par_cle("repondeur") is r
        assert r.client["silence"] < 5 and r.client["silence_court"] < 5
        assert scenarios.par_cle("t6").client == {}

    def test_les_reglages_du_scenario_sont_transmis_au_client(self, essai_actif, monkeypatch, tmp_path):
        monkeypatch.setenv("TWILIO_SIGNATURE", "off")
        monkeypatch.setenv("RECETTE_REPLIQUES_DIR", str(tmp_path))
        monkeypatch.setattr(voix, "voix_du_client", lambda langue: "voxtral/essai")
        for texte in scenarios.REPONDEUR.repliques:
            voix._chemin(texte, "voxtral/essai").write_bytes(REPLIQUE)
        recus = []

        def faux_client(repliques, **reglages):
            recus.append(reglages)
            return Client(repliques, silence=0.05, patience=0.2, conge=0.1)

        monkeypatch.setattr(appelant, "Client", faux_client)
        nonce = gardes.nouveau_nonce()
        with TestClient(app).websocket_connect("/ws/recette") as ws:
            ws.send_text(_debut("repondeur", nonce, gardes.jeton("repondeur", nonce)))
            try:
                while True:
                    ws.receive_text()
            except WebSocketDisconnect:
                pass
        assert recus == [scenarios.REPONDEUR.client]

    def test_l_essai_est_arrete_meme_quand_le_passage_leve(self, monkeypatch):
        actions = []

        async def porte(action):
            actions.append(action)
            return True

        monkeypatch.setattr(etage2, "_porte", porte)
        etat = etage2.Essai()

        async def passage():
            async with etage2.repondeur_ouvert(etat):
                raise RuntimeError("le passage casse")

        with pytest.raises(RuntimeError):
            asyncio.run(passage())
        assert actions == ["essayer", "arreter"] and etat.arrete is True

    def test_un_essai_qui_ne_demarre_pas_n_appelle_personne_et_s_arrete_quand_meme(self, monkeypatch):
        actions = []

        async def porte(action):
            actions.append(action)
            return action != "essayer"

        monkeypatch.setattr(etage2, "_porte", porte)

        async def passage():
            async with etage2.repondeur_ouvert(etage2.Essai()):
                actions.append("appel")

        with pytest.raises(RuntimeError):
            asyncio.run(passage())
        assert actions == ["essayer", "arreter"]

    def test_s8_est_vert_quand_l_appel_suivant_est_servi_par_l_assistante(self):
        servi = {"etat": verdicts.OK, "appel": {"id": 9, "secours_motif": None}}
        assert etage2.ligne_s8("gpt_live", True, servi)["etat"] == verdicts.OK
        # Le verdict propre de T6 n'est pas celui de S8.
        assert etage2.ligne_s8("gpt_live", True, {**servi, "etat": verdicts.ECHEC})["etat"] == verdicts.OK

    @pytest.mark.parametrize("arret, t6", [
        (False, {"etat": "ok", "appel": {"id": 9, "secours_motif": None}}),
        (None, {"etat": "ok", "appel": {"id": 9, "secours_motif": None}}),
        (True, {"etat": "echec", "appel": {"id": 9, "secours_motif": "essai"}}),
        (True, {"etat": "echec", "appel": None}),
    ])
    def test_s8_est_rouge_si_l_arret_n_est_pas_confirme_ou_si_l_appel_part_en_secours(self, arret, t6):
        assert etage2.ligne_s8("gpt_live", arret, t6)["etat"] == verdicts.ECHEC

    def test_s8_n_est_pas_joue_sans_t6(self):
        assert etage2.ligne_s8("gpt_live", True, None)["etat"] == verdicts.NON_JOUE
