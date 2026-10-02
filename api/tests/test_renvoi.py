"""En cas de panne, l'appel est renvoyé vers le restaurant (ASSISTANTE-118).

Ce qui compte : un client ne reste jamais sans personne. Pendant les horaires, le
téléphone du restaurant sonne ; sinon, ou si personne ne décroche, il laisse un message.
Un appel qui se termine normalement n'est JAMAIS renvoyé, et l'appel ne tourne pas en
rond entre la ligne du restaurant et la nôtre."""
import base64
import json
import re
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import calls, db, horloge, messages, notifications, renvoi, supervision, tenants, users
from app.admin import presenters
from app.main import app

FIXE, PORTABLE, CLIENT = "+33142000000", "+33612345678", "+33611223344"
HORAIRES = {"semaine": {"lundi": [], "mardi": [["12:00", "14:30"], ["19:00", "22:30"]],
                        "mercredi": [["12:00", "14:30"], ["19:00", "22:30"]],
                        "jeudi": [["19:00", "22:30"]], "vendredi": [["19:00", "23:00"]],
                        "samedi": [["19:00", "23:00"]], "dimanche": [["12:00", "15:00"]]},
            "fermetures": ["2030-03-20"]}
# Mars 2030 : le 11 est un lundi (fermé), le 13 un mercredi.
MERCREDI_MIDI = datetime(2030, 3, 13, 12, 30, tzinfo=horloge.FUSEAU)
MERCREDI_17H = datetime(2030, 3, 13, 17, 0, tzinfo=horloge.FUSEAU)
MERCREDI_18H15 = datetime(2030, 3, 13, 18, 15, tzinfo=horloge.FUSEAU)
LUNDI_MIDI = datetime(2030, 3, 11, 12, 30, tzinfo=horloge.FUSEAU)
NUIT = datetime(2030, 3, 13, 3, 0, tzinfo=horloge.FUSEAU)

client = TestClient(app)


def _sid() -> str:
    return f"CA{uuid.uuid4().hex}"


@pytest.fixture()
def resto():
    tenant = tenants.create_tenant("Chez Secours", f"+3363{id(object()) % 10_000_000:07d}")
    tenants.update_tenant(tenant.id, numero_secours=FIXE, opening_hours=json.dumps(HORAIRES))
    yield tenants.get_by_id(tenant.id)
    tenants.delete_tenant(tenant.id)


@pytest.fixture()
def ouvert(monkeypatch):
    monkeypatch.setattr(horloge, "maintenant", lambda: MERCREDI_MIDI)


@pytest.fixture()
def ferme(monkeypatch):
    monkeypatch.setattr(horloge, "maintenant", lambda: NUIT)


def _appel(call_sid: str) -> dict:
    with db.get_conn() as conn:
        return dict(conn.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,)).fetchone())


def _poster(chemin: str, tenant, call_sid: str, **champs):
    return client.post(chemin, data={"CallSid": call_sid, "To": tenant.phone_number,
                                     "From": CLIENT, **champs})


# ---- La décision -----------------------------------------------------------------------

class TestLaDecision:
    def test_pendant_le_service_le_restaurant_sonne(self, resto):
        assert renvoi.decision(resto, appelant=CLIENT, instant=MERCREDI_MIDI) == (renvoi.RENVOI, FIXE)

    def test_une_heure_avant_l_ouverture_on_decroche_deja(self, resto):
        assert renvoi.decision(resto, instant=MERCREDI_18H15)[0] == renvoi.RENVOI
        assert renvoi.decision(resto, instant=MERCREDI_17H)[0] == renvoi.REPONDEUR

    @pytest.mark.parametrize("instant", [NUIT, LUNDI_MIDI,
                                         datetime(2030, 3, 20, 12, 30, tzinfo=horloge.FUSEAU)])
    def test_ferme_on_prend_un_message(self, resto, instant):
        """La nuit, le jour de fermeture, la fermeture exceptionnelle : personne ne
        décrocherait."""
        assert renvoi.decision(resto, instant=instant) == (renvoi.REPONDEUR, None)

    def test_sans_numero_de_secours_toujours_le_message(self, resto):
        tenants.update_tenant(resto.id, numero_secours=None)
        assert renvoi.decision(tenants.get_by_id(resto.id), instant=MERCREDI_MIDI)[0] == renvoi.REPONDEUR

    def test_sans_horaires_on_ne_reveille_personne(self, resto):
        tenants.update_tenant(resto.id, opening_hours=None)
        sans = tenants.get_by_id(resto.id)
        assert renvoi.decision(sans, instant=MERCREDI_17H)[0] == renvoi.RENVOI
        assert renvoi.decision(sans, instant=NUIT)[0] == renvoi.REPONDEUR

    @pytest.mark.parametrize("champ", ["appelant", "transfere_depuis"])
    def test_l_appel_ne_tourne_pas_en_rond(self, resto, champ):
        """Le fixe du restaurant renvoie vers nous quand personne ne décroche : le
        rappeler ramènerait l'appel ici, sans fin."""
        assert renvoi.decision(resto, instant=MERCREDI_MIDI, **{champ: FIXE})[0] == renvoi.REPONDEUR


class TestLeTwiML:
    def test_le_restaurant_voit_le_numero_du_client(self):
        twiml = renvoi.twiml_renvoi(FIXE, appelant=CLIENT, ligne="+33100000000")
        assert f'callerId="{CLIENT}"' in twiml and f"<Number>{FIXE}</Number>" in twiml
        assert 'timeout="15"' in twiml and 'action="/twilio/secours/fin"' in twiml

    @pytest.mark.parametrize("masque", ["anonymous", "+266696687", "", None])
    def test_un_appel_masque_presente_notre_ligne(self, masque):
        twiml = renvoi.twiml_renvoi(FIXE, appelant=masque, ligne="+33100000000")
        assert 'callerId="+33100000000"' in twiml

    def test_le_repondeur_enregistre_et_rappelle_quand_c_est_pret(self):
        twiml = renvoi.twiml_repondeur()
        assert "<Record" in twiml and 'action="/twilio/repondeur"' in twiml
        assert 'recordingStatusCallback="/twilio/repondeur/pret"' in twiml
        assert twiml.index("<Say") < twiml.index("<Record") < twiml.index("<Hangup/>")


class TestLaMemoireDesPannes:
    def test_une_panne_de_voix_concerne_tout_le_parc(self):
        renvoi.signaler_panne(renvoi.VOIX)
        assert renvoi.panne_recente(1) == renvoi.VOIX and renvoi.panne_recente(2) == renvoi.VOIX

    def test_une_erreur_du_pipeline_ne_concerne_que_son_etablissement(self):
        renvoi.signaler_panne(renvoi.PIPELINE, 7)
        assert renvoi.panne_recente(7) == renvoi.PIPELINE and renvoi.panne_recente(8) is None

    def test_passe_trois_minutes_on_retente_l_assistante(self, monkeypatch):
        renvoi.signaler_panne(renvoi.VOIX)
        maintenant = renvoi._temps.monotonic()
        monkeypatch.setattr(renvoi._temps, "monotonic",
                            lambda: maintenant + renvoi.MEMOIRE_PANNE_SECONDES + 1)
        assert renvoi.panne_recente(1) is None

    def test_la_fin_du_flux(self):
        assert renvoi.motif_a_la_fin_du_flux("CAjamais-vu") == renvoi.FLUX
        renvoi.flux_ouvert("CAnormal")
        assert renvoi.motif_a_la_fin_du_flux("CAnormal") is None
        renvoi.demander("CAnormal", renvoi.VOIX)
        renvoi.demander("CAnormal", renvoi.MODELE)   # la première cause reste
        assert renvoi.motif_a_la_fin_du_flux("CAnormal") == renvoi.VOIX
        assert renvoi.motif_a_la_fin_du_flux(None) is None


# ---- Au décroché -----------------------------------------------------------------------

class TestAuDecroche:
    def test_un_appel_normal_part_sur_l_assistante_avec_une_suite(self, resto):
        twiml = _poster("/twilio/voice", resto, _sid()).text
        assert "<Connect>" in twiml and "<Dial" not in twiml
        assert twiml.index("</Connect>") < twiml.index('<Redirect method="POST">/twilio/suite</Redirect>')

    def test_apres_une_panne_l_appel_suivant_sonne_au_restaurant(self, resto, ouvert):
        renvoi.signaler_panne(renvoi.VOIX)
        sid = _sid()
        twiml = _poster("/twilio/voice", resto, sid).text
        assert "<Connect>" not in twiml and f"<Number>{FIXE}</Number>" in twiml
        appel = _appel(sid)
        assert appel["secours_motif"] == renvoi.PANNE_RECENTE and appel["status"] == "unserved"
        assert appel["ended_at"] and appel["caller_number"] == CLIENT

    def test_apres_une_panne_hors_horaires_le_repondeur(self, resto, ferme):
        renvoi.signaler_panne(renvoi.VOIX)
        twiml = _poster("/twilio/voice", resto, _sid()).text
        assert "<Record" in twiml and "<Dial" not in twiml

    def test_l_appel_renvoye_par_le_fixe_ne_repart_pas_vers_lui(self, resto, ouvert):
        renvoi.signaler_panne(renvoi.VOIX)
        twiml = _poster("/twilio/voice", resto, _sid(), ForwardedFrom=FIXE).text
        assert "<Record" in twiml and "<Dial" not in twiml


# ---- À la fin du flux ------------------------------------------------------------------

class TestLaSuiteDuFlux:
    def test_un_appel_termine_normalement_est_raccroche(self, resto, ouvert):
        sid = _sid()
        renvoi.flux_ouvert(sid)
        twiml = _poster("/twilio/suite", resto, sid).text
        assert "<Hangup/>" in twiml and "<Dial" not in twiml and "<Record" not in twiml

    def test_l_assistante_a_demande_le_renvoi(self, resto, ouvert):
        sid = _sid()
        calls.start_call(sid, resto.id, CLIENT)
        calls.finish_call(sid, "completed", [{"role": "user", "content": "Bonjour"}])
        renvoi.demander(sid, renvoi.VOIX)
        twiml = _poster("/twilio/suite", resto, sid).text
        assert f"<Number>{FIXE}</Number>" in twiml
        appel = _appel(sid)
        assert appel["secours_motif"] == renvoi.VOIX and appel["status"] == "unserved"
        assert appel["transcript"]  # ce que l'assistante avait noté reste

    def test_un_flux_jamais_ouvert_est_une_panne(self, resto, ouvert):
        sid = _sid()
        twiml = _poster("/twilio/suite", resto, sid).text
        assert "<Dial" in twiml and _appel(sid)["secours_motif"] == renvoi.FLUX
        assert renvoi.panne_recente(resto.id) == renvoi.FLUX

    def test_la_cloture_de_l_appel_ne_efface_pas_le_secours(self, resto, ouvert):
        """Si le pipeline clôt l'appel APRÈS que le secours s'est ouvert, il ne doit pas
        le remettre à « terminé normalement »."""
        sid = _sid()
        calls.start_call(sid, resto.id, CLIENT)
        renvoi.demander(sid, renvoi.MODELE)
        _poster("/twilio/suite", resto, sid)
        calls.finish_call(sid, "completed", [])
        assert _appel(sid)["status"] == "unserved"


class TestLeRestaurantDecrocheOuPas:
    def _renvoye(self, resto) -> str:
        sid = _sid()
        renvoi.signaler_panne(renvoi.VOIX)
        _poster("/twilio/voice", resto, sid)
        return sid

    def test_il_decroche(self, resto, ouvert):
        sid = self._renvoye(resto)
        twiml = _poster("/twilio/secours/fin", resto, sid, DialCallStatus="completed",
                        DialCallDuration="95").text
        assert "<Hangup/>" in twiml and "<Record" not in twiml
        appel = _appel(sid)
        assert appel["status"] == "forwarded" and appel["secours_secondes"] == 95
        # L'appel reçu (1 minute entamée) + deux minutes entamées vers un fixe.
        assert appel["cout_telephonie"] == pytest.approx(0.01 + 2 * 0.0187)
        assert appel["estimated_cost"] == pytest.approx(appel["cout_telephonie"])
        assert presenters.call_view(appel)["outcome_label"] == "Renvoyé au restaurant"

    @pytest.mark.parametrize("statut", ["no-answer", "busy", "failed", "canceled"])
    def test_personne_ne_decroche_le_client_laisse_un_message(self, resto, ouvert, statut):
        sid = self._renvoye(resto)
        twiml = _poster("/twilio/secours/fin", resto, sid, DialCallStatus=statut).text
        assert "<Record" in twiml
        assert _appel(sid)["status"] == "unserved"   # tant qu'il n'a rien dit

    def test_le_cout_d_un_renvoi(self):
        assert calls.cout_renvoi(95, FIXE) == pytest.approx(2 * 0.0187)
        assert calls.cout_renvoi(95, PORTABLE) == pytest.approx(2 * 0.0404)
        assert calls.cout_renvoi(60, "+33712345678") == pytest.approx(0.0404)
        assert calls.cout_renvoi(10, "+442071234567") == pytest.approx(0.0404)  # borne haute
        assert calls.cout_renvoi(0, FIXE) == 0 and calls.cout_renvoi(None, FIXE) == 0


class TestLeMessageVocal:
    def test_le_message_est_note_et_le_restaurant_prevenu(self, resto, ferme):
        sid = _sid()
        renvoi.signaler_panne(renvoi.VOIX)
        _poster("/twilio/voice", resto, sid)
        with patch("app.main.notifications.planifier") as planifier:
            twiml = _poster("/twilio/repondeur", resto, sid, RecordingDuration="34").text
        assert renvoi.MERCI in twiml and "<Hangup/>" in twiml
        appel = _appel(sid)
        assert appel["status"] == "voicemail"
        notes = messages.messages_d_un_appel(appel["id"])
        assert len(notes) == 1 and notes[0]["caller_number"] == CLIENT
        assert "34 s" in notes[0]["details"] and not notes[0]["handled_at"]
        evenement, donnees = planifier.call_args.args[1:]
        assert evenement == "message_vocal" and donnees["caller_number"] == CLIENT
        assert donnees["appel_id"] == appel["id"] and donnees["secondes"] == 34
        assert presenters.call_view(appel)["outcome_label"] == "Message vocal"

    @pytest.mark.parametrize("duree", ["0", "", None])
    def test_sans_un_mot_pas_de_message(self, resto, ferme, duree):
        sid = _sid()
        renvoi.signaler_panne(renvoi.VOIX)
        _poster("/twilio/voice", resto, sid)
        champs = {} if duree is None else {"RecordingDuration": duree}
        twiml = _poster("/twilio/repondeur", resto, sid, **champs).text
        assert renvoi.SANS_MESSAGE in twiml
        assert messages.messages_d_un_appel(_appel(sid)["id"]) == []
        assert _appel(sid)["status"] == "unserved"

    def test_l_e_mail_dit_qui_rappeler(self, resto):
        sujet, corps = notifications.sujet_et_corps(
            "message_vocal", resto, {"secondes": 34, "caller_number": CLIENT, "appel_id": 12})
        assert "Message vocal" in sujet and CLIENT in corps and "34 s" in corps
        assert "/admin/calls/12" in corps
        masque = notifications.sujet_et_corps("message_vocal", resto, {"secondes": 5})[1]
        assert "numéro masqué" in masque

    def test_le_fichier_pret_est_rapatrie_en_tache_de_fond(self, resto):
        with patch("app.repondeur.rapatrier", new=AsyncMock(return_value=True)) as rapatrier:
            resp = client.post("/twilio/repondeur/pret", data={
                "CallSid": "CA1", "RecordingSid": "RE" + "a" * 32, "RecordingStatus": "completed"})
            assert resp.status_code == 204
        rapatrier.assert_called_once_with("CA1", "RE" + "a" * 32)

    def test_un_fichier_pas_encore_pret_n_est_pas_demande(self):
        with patch("app.repondeur.rapatrier", new=AsyncMock()) as rapatrier:
            client.post("/twilio/repondeur/pret", data={
                "CallSid": "CA1", "RecordingSid": "RE" + "a" * 32, "RecordingStatus": "in-progress"})
        rapatrier.assert_not_called()


# ---- Le pipeline qui s'effondre --------------------------------------------------------

class TestLePipelineSEffondre:
    def test_l_appel_et_les_suivants_sont_renvoyes(self, resto, ouvert):
        from test_voice_stream import _attendre_le_bot, _twilio_start_message

        sid = _sid()
        run_bot = AsyncMock(side_effect=RuntimeError("clé de transcription absente"))
        with patch("app.main._get_bot_runner", return_value=run_bot):
            try:
                with client.websocket_connect("/ws/voice") as ws:
                    ws.send_text(json.dumps(_twilio_start_message(
                        to=resto.phone_number, call_sid=sid, from_number=CLIENT)))
                    _attendre_le_bot(run_bot)
                    ws.receive_text()
            except Exception:
                pass  # le serveur ferme le flux : c'est ce qu'on attend
        assert renvoi.motif_a_la_fin_du_flux(sid) == renvoi.PIPELINE
        assert renvoi.panne_recente(resto.id) == renvoi.PIPELINE
        # Twilio vient chercher la suite : le restaurant sonne.
        assert f"<Number>{FIXE}</Number>" in _poster("/twilio/suite", resto, sid).text
        # L'appel suivant du même établissement ne retente pas le pipeline.
        assert "<Dial" in _poster("/twilio/voice", resto, _sid()).text

    def test_un_flux_normal_n_est_pas_une_panne(self, resto):
        from test_voice_stream import _attendre_le_bot, _twilio_start_message

        sid = _sid()
        run_bot = AsyncMock()
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps(_twilio_start_message(
                    to=resto.phone_number, call_sid=sid, from_number=CLIENT)))
                _attendre_le_bot(run_bot)
        assert renvoi.motif_a_la_fin_du_flux(sid) is None
        assert renvoi.panne_recente(resto.id) is None


# ---- Signature Twilio ------------------------------------------------------------------

@pytest.mark.parametrize("chemin", ["/twilio/suite", "/twilio/secours/fin", "/twilio/repondeur",
                                    "/twilio/repondeur/pret"])
def test_les_nouvelles_adresses_exigent_la_signature_de_twilio(monkeypatch, chemin):
    """Sans elle, n'importe qui ferait sonner le restaurant, ou lui écrirait des messages."""
    monkeypatch.setenv("TWILIO_SIGNATURE", "enforce")
    assert client.post(chemin, data={"CallSid": "CA1", "To": "+33100000000"}).status_code == 403


# ---- L'admin ---------------------------------------------------------------------------

def _admin(email="admin@test.local", password="test-admin-pass"):
    session = TestClient(app)
    assert session.post("/admin/login", data={"email": email, "password": password},
                        follow_redirects=False).status_code == 303
    session.get("/admin/")
    raw = session.cookies.get("session").split(".")[0]
    raw += "=" * (-len(raw) % 4)
    session.headers["X-CSRF-Token"] = json.loads(base64.b64decode(raw))["csrf"]
    return session


class TestLeNumeroDeSecours:
    @pytest.fixture()
    def restaurateur(self, resto):
        compte = users.create_user(f"secours-{resto.id}@test.fr", "resto-pass",
                                   users.ROLE_RESTAURATEUR, resto.id)
        return resto, _admin(compte.email, "resto-pass")

    def test_le_restaurateur_le_regle_lui_meme(self, restaurateur):
        resto, session = restaurateur
        page = session.get(f"/admin/tenants/{resto.id}/edit").text
        assert 'name="numero_secours"' in page and f'value="{FIXE}"' in page
        resp = session.post(f"/admin/tenants/{resto.id}", follow_redirects=False,
                            data={"name": resto.name, "numero_secours": "06 12 34 56 78"})
        assert resp.status_code == 422   # pas au format international
        assert tenants.get_by_id(resto.id).numero_secours == FIXE
        resp = session.post(f"/admin/tenants/{resto.id}", follow_redirects=False,
                            data={"name": resto.name, "numero_secours": "+33 6 12 34 56 78"})
        assert resp.status_code == 303
        assert tenants.get_by_id(resto.id).numero_secours == PORTABLE

    def test_jamais_la_ligne_de_l_assistante(self, restaurateur):
        resto, session = restaurateur
        resp = session.post(f"/admin/tenants/{resto.id}", follow_redirects=False,
                            data={"name": resto.name, "numero_secours": resto.phone_number})
        assert resp.status_code == 422 and "reviendrait ici" in resp.text
        assert tenants.get_by_id(resto.id).numero_secours == FIXE

    def test_vide_il_est_retire_absent_il_reste(self, restaurateur):
        resto, session = restaurateur
        session.post(f"/admin/tenants/{resto.id}", data={"name": resto.name})
        assert tenants.get_by_id(resto.id).numero_secours == FIXE
        session.post(f"/admin/tenants/{resto.id}", data={"name": resto.name, "numero_secours": ""})
        assert tenants.get_by_id(resto.id).numero_secours is None

    def test_sans_numero_l_admin_le_dit(self, restaurateur):
        resto, session = restaurateur
        assert "Pas de numéro de secours" not in session.get("/admin/").text
        tenants.update_tenant(resto.id, numero_secours=None)
        assert "Pas de numéro de secours" in session.get("/admin/").text
        assert "pas de numéro de secours" in _admin().get("/admin/").text

    def test_le_journal_montre_ce_qu_est_devenu_l_appel(self, restaurateur, ouvert):
        resto, session = restaurateur
        sid = _sid()
        renvoi.signaler_panne(renvoi.VOIX)
        _poster("/twilio/voice", resto, sid)
        appel = _appel(sid)
        page = session.get(f"/admin/calls/{appel['id']}").text.replace("&#39;", "'")
        assert "Panne : le client n'a eu personne" in page
        assert "une panne venait d'être constatée" in page
        _poster("/twilio/secours/fin", resto, sid, DialCallStatus="completed", DialCallDuration="42")
        page = session.get(f"/admin/calls/{appel['id']}").text
        assert "Appel renvoyé au restaurant" in page and "42 s de communication" in page
        liste = session.get("/admin/calls?outcome=secours").text
        assert "Renvoyé au restaurant" in liste and "Pannes" in liste


class TestEssayerLeRenvoi:
    """Vérifier que le téléphone du restaurant sonne, sans attendre une vraie panne."""

    def test_le_super_admin_lance_un_essai_pour_un_etablissement(self, resto, ouvert):
        session = _admin()
        page = session.get(f"/admin/tenants/{resto.id}/edit").text
        assert "Essayer le renvoi" in page and "Essai en cours" not in page
        resp = session.post(f"/admin/tenants/{resto.id}/secours/essai", follow_redirects=False)
        assert resp.status_code == 303
        assert "Essai en cours" in session.get(f"/admin/tenants/{resto.id}/edit").text
        sid = _sid()
        assert f"<Number>{FIXE}</Number>" in _poster("/twilio/voice", resto, sid).text
        assert _appel(sid)["secours_motif"] == renvoi.ESSAI
        # Les autres établissements gardent leur assistante.
        assert renvoi.panne_recente(resto.id + 1000) is None

    def test_l_essai_s_arrete_a_la_demande(self, resto):
        session = _admin()
        session.post(f"/admin/tenants/{resto.id}/secours/essai")
        session.post(f"/admin/tenants/{resto.id}/secours/essai", data={"arreter": "1"})
        assert renvoi.panne_recente(resto.id) is None
        assert "<Connect>" in _poster("/twilio/voice", resto, _sid()).text

    def test_arreter_un_essai_ne_cache_pas_une_vraie_panne(self, resto):
        renvoi.signaler_panne(renvoi.PIPELINE, resto.id)
        _admin().post(f"/admin/tenants/{resto.id}/secours/essai", data={"arreter": "1"})
        assert renvoi.panne_recente(resto.id) == renvoi.PIPELINE

    def test_un_restaurateur_ne_peut_pas_couper_son_assistante(self, resto):
        compte = users.create_user(f"essai-{resto.id}@test.fr", "resto-pass",
                                   users.ROLE_RESTAURATEUR, resto.id)
        session = _admin(compte.email, "resto-pass")
        assert "Essayer le renvoi" not in session.get(f"/admin/tenants/{resto.id}/edit").text
        resp = session.post(f"/admin/tenants/{resto.id}/secours/essai", follow_redirects=False)
        assert resp.status_code == 403 and renvoi.panne_recente(resto.id) is None

    def test_un_essai_n_alerte_pas_la_supervision(self, resto, ouvert):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET started_at = '2020-01-01T00:00:00Z' "
                         "WHERE secours_motif IS NOT NULL")
        _admin().post(f"/admin/tenants/{resto.id}/secours/essai")
        _poster("/twilio/voice", resto, _sid())
        assert supervision._controle_secours().mesure["secours_24h"] == 0


# ---- La supervision --------------------------------------------------------------------

class TestLaSupervision:
    def _controle(self):
        return supervision._controle_secours()

    def test_un_secours_tout_juste_passe_est_une_panne(self, resto, ouvert):
        sid = _sid()
        renvoi.signaler_panne(renvoi.VOIX)
        _poster("/twilio/voice", resto, sid)
        controle = self._controle()
        assert controle.niveau == supervision.PANNE
        assert controle.mesure["secours_24h"] >= 1 and controle.mesure["non_servis"] >= 1

    def test_plus_ancien_il_reste_une_attention(self, resto, ouvert):
        sid = _sid()
        renvoi.signaler_panne(renvoi.VOIX)
        _poster("/twilio/voice", resto, sid)
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET started_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', "
                         "'-3 hours') WHERE secours_motif IS NOT NULL")
        assert self._controle().niveau == supervision.ATTENTION

    def test_un_etablissement_sans_numero_est_signale(self, resto):
        with db.get_conn() as conn:
            conn.execute("UPDATE calls SET started_at = '2020-01-01T00:00:00Z' "
                         "WHERE secours_motif IS NOT NULL")
        tenants.update_tenant(resto.id, numero_secours=None)
        controle = self._controle()
        assert controle.niveau == supervision.ATTENTION
        assert controle.mesure["sans_numero_de_secours"] >= 1 and "Chez Secours" in (
            controle.detail + controle.resume) or controle.mesure["sans_numero_de_secours"] > 5
