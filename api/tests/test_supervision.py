"""Supervision : chaque contrôle doit pouvoir DEVENIR ROUGE.

Un tableau de bord vert ne prouve rien tant qu'on n'a pas vu le rouge. La panne du
30/07/2026 — appels muets, `/health` répondant « ok » du début à la fin — n'a pas été
manquée faute de supervision, mais parce que la supervision n'observait rien.

Chaque classe ci-dessous fabrique donc la panne, puis exige le verdict correspondant.
Un contrôle qui ne saurait pas passer au rouge serait une décoration, et il vaut mieux
pas de voyant qu'un voyant qui ment.
"""
import html
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db, supervision, tenants
from app.main import app


@pytest.fixture(autouse=True)
def cache_vide():
    """`etat()` met son résultat en cache : sans ça, un test lirait le verdict du test
    précédent et passerait au vert pour de mauvaises raisons."""
    supervision.vider_cache()
    yield
    supervision.vider_cache()


@pytest.fixture()
def base(tmp_path):
    chemin = str(tmp_path / "supervision.db")
    with patch.object(db, "DB_PATH", chemin):
        db.init_db()
        yield chemin


@pytest.fixture()
def etablissement(base):
    return tenants.create_tenant("Chez Test", "+33199000001")


def _il_y_a(minutes: float) -> str:
    date = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return date.strftime("%Y-%m-%dT%H:%M:%SZ")


def _appel(tenant_id: int, sid: str, *, minutes: float = 60, duree: float = 60,
           status: str = "completed", transcript=None, cloture: bool = True,
           latences=None) -> None:
    """Écrit un appel directement : `finish_call` calcule la durée depuis `started_at`,
    donc il ne permet pas de fabriquer un appel ancien ou volontairement inachevé."""
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO calls (call_sid, tenant_id, started_at, ended_at,
                                  duration_seconds, status, transcript, turn_latencies)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, tenant_id, _il_y_a(minutes), _il_y_a(minutes - 1) if cloture else None,
             duree, status,
             json.dumps(transcript, ensure_ascii=False) if transcript else None,
             json.dumps(latences) if latences else None),
        )


def _controle(cle: str) -> dict:
    for c in supervision.etat(force=True)["controles"]:
        if c["cle"] == cle:
            return c
    raise AssertionError(f"contrôle « {cle} » absent de l'état")


DIALOGUE = [{"role": "user", "content": "bonjour"},
            {"role": "assistant", "content": "Bonjour, Chez Test j'écoute."}]
MONOLOGUE = [{"role": "user", "content": "bonjour ?"}]
# L'accueil pré-rendu, tel qu'il est inscrit en tête de transcription : il part SANS GPU.
ACCUEIL = {"role": "assistant", "content": "Bonjour, Chez Test, un instant s'il vous plaît."}


class TestEnumeration:
    """Filet de sécurité : un contrôle supprimé par mégarde ferait passer la sonde au
    vert sans que rien ne le signale. Le même piège que les tests de sécurité vides."""

    ATTENDUS = ["base", "configuration", "signatures", "jeton_voix", "appels_muets",
                "appels_echoues", "appels_inacheves", "latence", "twilio", "accueils",
                "sauvegarde", "purge", "disque", "enregistrements"]

    def test_tous_les_controles_sont_presents(self, base):
        assert [c["cle"] for c in supervision.etat(force=True)["controles"]] == self.ATTENDUS

    def test_chaque_controle_a_un_titre_lisible(self, base):
        for c in supervision.etat(force=True)["controles"]:
            assert c["titre"] and c["titre"] != c["cle"]
            assert c["niveau"] in (supervision.OK, supervision.ATTENTION, supervision.PANNE)


class TestVerdictGlobal:
    def test_le_niveau_est_le_pire_des_controles(self):
        assert supervision.pire("ok", "attention", "ok") == "attention"
        assert supervision.pire("attention", "panne") == "panne"
        assert supervision.pire() == "ok"

    def test_un_controle_qui_explose_ne_fait_pas_tomber_la_sonde(self, base):
        """Une panne de supervision ne doit pas se déguiser en panne d'application —
        ni, surtout, en feu vert."""
        with patch.object(supervision, "_controle_configuration",
                          side_effect=RuntimeError("boum")):
            etat = supervision.etat(force=True)
        config = next(c for c in etat["controles"] if c["cle"] == "configuration")
        assert config["niveau"] == supervision.ATTENTION
        assert "INCONNU" in config["detail"]

    def test_une_base_illisible_ne_passe_pas_pour_calme(self, base):
        """Si la lecture des appels échoue, les contrôles d'appels ne doivent PAS
        conclure « aucun appel, donc pas d'anomalie » : ils ne savent rien.

        Sans ce garde-fou, la sonde tombait aussi en HTTP 500 — l'alerte aurait dit
        « supervision cassée » là où il fallait lire « base cassée »."""
        with patch.object(supervision, "_appels_fenetre",
                          side_effect=RuntimeError("base injoignable")):
            etat = supervision.etat(force=True)
        par_cle = {c["cle"]: c for c in etat["controles"]}
        assert len(par_cle) == len(TestEnumeration.ATTENDUS)  # la sonde a répondu
        for cle in ("appels_muets", "appels_echoues", "appels_inacheves"):
            assert par_cle[cle]["niveau"] == supervision.ATTENTION
            assert "INCONNU" in par_cle[cle]["detail"]

    def test_le_cache_expire_sur_demande(self, base):
        premier = supervision.etat()
        assert supervision.etat() is premier  # servi depuis le cache
        supervision.vider_cache()
        assert supervision.etat() is not premier

    def test_resume_nomme_les_fautifs(self):
        etat = {"controles": [
            {"cle": "a", "titre": "Base", "niveau": "ok"},
            {"cle": "b", "titre": "Latence", "niveau": "panne"},
        ]}
        assert supervision.resume(etat) == "Latence (panne)"
        assert supervision.resume({"controles": []}) == "Tout est vert"


class TestBase:
    def test_ok_quand_la_base_ecrit(self, etablissement):
        assert _controle("base")["niveau"] == supervision.OK

    def test_panne_quand_l_ecriture_echoue(self, base):
        """Lire ne suffit pas : un volume en lecture seule laisse passer les SELECT et
        fait perdre les réservations. Le contrôle écrit vraiment."""
        with patch.object(supervision, "noter", side_effect=OSError("disque plein")):
            controle = _controle("base")
        assert controle["niveau"] == supervision.PANNE
        assert "disque" in controle["detail"].lower()


class TestConfiguration:
    def test_panne_si_la_cle_llm_manque(self, base, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        controle = _controle("configuration")
        assert controle["niveau"] == supervision.PANNE
        assert "OPENROUTER_API_KEY" in controle["resume"]

    def test_le_chemin_d_appel_exige_ses_quatre_variables(self, base, monkeypatch):
        """Un seul chemin d'appel (flux média, Deepgram, moshi-server) : les quatre
        variables sont exigées sans condition. L'exigence dépendait autrefois d'un mode
        `gather` qui n'existe plus."""
        monkeypatch.delenv("PUBLIC_WS_URL", raising=False)
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        monkeypatch.delenv("MOSHI_TTS_URL", raising=False)
        controle = _controle("configuration")
        assert controle["niveau"] == supervision.PANNE
        assert set(controle["mesure"]["manquantes"]) == {
            "PUBLIC_WS_URL", "DEEPGRAM_API_KEY", "MOSHI_TTS_URL"}

    @pytest.mark.parametrize("url", ["wss://exemple.fr/ws voice",
                                     "wss://exemple.fr/ws\xa0",
                                     "https://exemple.fr/ws"])
    def test_une_url_de_flux_malformee_est_une_panne(self, base, monkeypatch, url):
        """Une seule espace insécable collée depuis un navigateur, et Twilio ne joint
        jamais le flux : l'appel raccroche sans un mot. Vécu."""
        monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        monkeypatch.setenv("PUBLIC_WS_URL", url)
        controle = _controle("configuration")
        assert controle["niveau"] == supervision.PANNE
        assert "PUBLIC_WS_URL" in controle["resume"]

    def test_ok_quand_tout_est_la(self, base, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        monkeypatch.setenv("PUBLIC_WS_URL", "wss://app.exemple.fr/ws/voice")
        assert _controle("configuration")["niveau"] == supervision.OK


class TestAppelsMuets:
    """LE contrôle qui aurait vu la panne du 30/07 : appel `completed`, HTTP 200
    partout, et pas un mot prononcé."""

    def test_pas_de_mesure_sans_appel(self, etablissement):
        controle = _controle("appels_muets")
        assert controle["niveau"] == supervision.OK
        assert "pas de mesure" in controle["resume"]

    def test_un_appel_repondu_reste_vert(self, etablissement):
        _appel(etablissement.id, "CA-ok", transcript=DIALOGUE)
        assert _controle("appels_muets")["niveau"] == supervision.OK

    def test_un_appel_muet_isole_alerte(self, etablissement):
        _appel(etablissement.id, "CA-muet", transcript=MONOLOGUE)
        _appel(etablissement.id, "CA-ok1", transcript=DIALOGUE)
        _appel(etablissement.id, "CA-ok2", transcript=DIALOGUE)
        controle = _controle("appels_muets")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"] == {"candidats": 3, "muets": 1, "d_affilee": False}

    def test_une_majorite_d_appels_muets_est_une_panne(self, etablissement):
        for i in range(3):
            _appel(etablissement.id, f"CA-muet{i}", transcript=None)
        assert _controle("appels_muets")["niveau"] == supervision.PANNE

    def test_un_raccroche_immediat_n_est_pas_une_panne(self, etablissement):
        """Trois secondes sans réponse, c'est un appelant qui a raccroché — pas une
        assistante muette. Sinon la supervision crierait à chaque faux numéro."""
        _appel(etablissement.id, "CA-court", duree=3, transcript=None)
        assert _controle("appels_muets")["niveau"] == supervision.OK

    def test_un_appel_hors_fenetre_est_oublie(self, etablissement, monkeypatch):
        """Une alerte doit parler du présent : un incident d'il y a trois semaines
        n'a pas à maintenir le voyant au rouge."""
        monkeypatch.setenv("SUPERVISION_FENETRE_JOURS", "7")
        _appel(etablissement.id, "CA-vieux", minutes=60 * 24 * 30, transcript=None)
        assert _controle("appels_muets")["niveau"] == supervision.OK


class TestLesDeuxPannesQueLeControleNAvaitPasVues:
    """Le contrôle comptait n'importe quel tour de l'assistante. Deux pannes réelles
    lui ont échappé, parce que le « décor » — ce que l'assistante dit sans le modèle —
    remplissait la transcription à la place d'une vraie réponse."""

    def test_l_accueil_seul_n_est_pas_une_reponse(self, etablissement):
        """23/09/2026 : plus de GPU L4 en Europe. L'accueil, un WAV pré-rendu joué sans
        GPU, était la SEULE ligne de l'assistante — et le voyant restait vert. Cinq
        appels perdus, dont personne n'aurait rien su."""
        _appel(etablissement.id, "CA-gpu", transcript=[ACCUEIL])
        assert _controle("appels_muets")["mesure"]["muets"] == 1

    def test_la_reprise_et_les_relances_ne_sont_pas_des_reponses(self, etablissement):
        """30/07/2026 : le modèle levait à chaque tour. La reprise et les relances,
        dites sans lui, faisaient croire qu'il parlait."""
        from app.voice import bot, greeting

        _appel(etablissement.id, "CA-llm", transcript=[
            ACCUEIL,
            {"role": "assistant", "content": greeting.texte_de_reprise(False)},
            {"role": "user", "content": "Je voudrais réserver une table"},
            {"role": "assistant", "content": bot.relance(1, "fr")[0]},
            {"role": "user", "content": "Allô ?"},
            {"role": "assistant", "content": bot.relance(2, "fr")[0]},
        ])
        assert _controle("appels_muets")["mesure"]["muets"] == 1

    def test_le_message_d_indisponibilite_n_est_pas_une_reponse(self, etablissement):
        """25/09/2026 : GPU introuvable, l'assistante demande de rappeler et raccroche.
        L'appel est perdu pour le restaurant : il doit compter parmi les muets, sinon
        le filet de sécurité masquerait la panne qu'il amortit."""
        from app.voice import greeting

        _appel(etablissement.id, "CA-indispo", transcript=[
            ACCUEIL, {"role": "assistant", "content": greeting.texte_indisponible()}])
        assert _controle("appels_muets")["mesure"]["muets"] == 1

    def test_des_relances_fusionnees_en_un_seul_tour_restent_du_decor(self, etablissement):
        """L'agrégateur fusionne les sorties consécutives : l'appel 138 portait les trois
        relances bout à bout dans UN tour. Une comparaison exacte l'aurait pris pour une
        vraie réponse."""
        from app.voice import bot

        fusion = " ".join(bot.relance(n, "fr")[0] for n in (1, 2, 3))
        _appel(etablissement.id, "CA-fusion", transcript=[
            ACCUEIL, {"role": "user", "content": "…"},
            {"role": "assistant", "content": fusion}])
        assert _controle("appels_muets")["mesure"]["muets"] == 1

    def test_une_vraie_reponse_collee_a_une_relance_compte(self, etablissement):
        """Le retrait est chirurgical : il ôte le décor, pas le tour qui le contient."""
        from app.voice import bot

        tour = bot.relance(1, "fr")[0] + " Oui, nous avons une table à vingt heures."
        _appel(etablissement.id, "CA-mixte", transcript=[
            ACCUEIL, {"role": "user", "content": "Une table ce soir ?"},
            {"role": "assistant", "content": tour}])
        assert _controle("appels_muets")["mesure"]["muets"] == 0

    def test_les_relances_anglaises_sont_aussi_du_decor(self, etablissement):
        from app.voice import bot

        _appel(etablissement.id, "CA-en", transcript=[
            ACCUEIL, {"role": "user", "content": "Hello?"},
            {"role": "assistant", "content": bot.relance(1, "en")[0]}])
        assert _controle("appels_muets")["mesure"]["muets"] == 1

    def test_trois_appels_muets_d_affilee_sont_une_panne_meme_dilues(self, etablissement):
        """Le 23/09, cinq appels perdus pesaient 5 sur 20 dans la semaine : la proportion
        disait « attention ». Mais les derniers appels n'obtenaient plus RIEN : la ligne
        était muette à cet instant, et c'est une panne."""
        for i in range(10):
            _appel(etablissement.id, f"CA-ok{i}", minutes=600 + i, transcript=DIALOGUE)
        for i in range(3):
            _appel(etablissement.id, f"CA-gpu{i}", minutes=10 + i, transcript=[ACCUEIL])
        controle = _controle("appels_muets")
        assert controle["niveau"] == supervision.PANNE
        assert controle["mesure"]["d_affilee"] is True
        assert "AUCUNE réponse" in controle["resume"]

    def test_une_panne_terminee_ne_hurle_plus(self, etablissement):
        """Trois appels perdus, puis la ligne répond de nouveau. La proportion garde la
        trace de l'incident (attention), mais ce n'est plus une panne en cours."""
        for i in range(3):
            _appel(etablissement.id, f"CA-gpu{i}", minutes=60 + i, transcript=[ACCUEIL])
        for i in range(7):
            _appel(etablissement.id, f"CA-ok{i}", minutes=10 + i, transcript=DIALOGUE)
        controle = _controle("appels_muets")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"]["d_affilee"] is False


class TestAppelsEchoues:
    def test_un_echec_isole_alerte(self, etablissement):
        _appel(etablissement.id, "CA-ko", status="failed")
        _appel(etablissement.id, "CA-ok1", transcript=DIALOGUE)
        _appel(etablissement.id, "CA-ok2", transcript=DIALOGUE)
        assert _controle("appels_echoues")["niveau"] == supervision.ATTENTION

    def test_une_majorite_d_echecs_est_une_panne(self, etablissement):
        for i in range(3):
            _appel(etablissement.id, f"CA-ko{i}", status="failed")
        assert _controle("appels_echoues")["niveau"] == supervision.PANNE


class TestAppelsInacheves:
    def test_un_appel_en_cours_n_est_pas_une_anomalie(self, etablissement):
        """Décrocher il y a deux minutes et ne pas avoir raccroché, c'est un appel —
        pas un worker mort."""
        _appel(etablissement.id, "CA-live", minutes=2, cloture=False)
        controle = _controle("appels_inacheves")
        assert controle["niveau"] == supervision.OK
        assert controle["mesure"]["appels"] == 0

    def test_un_appel_jamais_cloture_alerte(self, etablissement):
        _appel(etablissement.id, "CA-zombie", minutes=120, cloture=False)
        _appel(etablissement.id, "CA-ok1", minutes=120, transcript=DIALOGUE)
        _appel(etablissement.id, "CA-ok2", minutes=120, transcript=DIALOGUE)
        controle = _controle("appels_inacheves")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"] == {"appels": 3, "inacheves": 1}


class TestLatence:
    def test_pas_de_mesure_sans_tour_instrumente(self, etablissement):
        controle = _controle("latence")
        assert controle["niveau"] == supervision.OK
        assert "pas de mesure" in controle["resume"]

    def test_une_latence_normale_reste_verte(self, etablissement):
        _appel(etablissement.id, "CA-vif", transcript=DIALOGUE, latences=[900, 1100, 1200])
        assert _controle("latence")["niveau"] == supervision.OK

    def test_une_derive_alerte(self, etablissement, monkeypatch):
        monkeypatch.setenv("SUPERVISION_LATENCE_ATTENTION_MS", "2500")
        _appel(etablissement.id, "CA-lent", transcript=DIALOGUE, latences=[2600, 2800, 3000])
        assert _controle("latence")["niveau"] == supervision.ATTENTION

    def test_une_derive_grave_est_une_panne(self, etablissement, monkeypatch):
        monkeypatch.setenv("SUPERVISION_LATENCE_PANNE_MS", "4000")
        _appel(etablissement.id, "CA-fige", transcript=DIALOGUE, latences=[5000, 6000, 7000])
        assert _controle("latence")["niveau"] == supervision.PANNE


class TestJetonVoix:
    """La clé de démonstration de Kyutai ouvre le GPU à qui connaît l'URL."""

    @pytest.mark.parametrize("valeur", ["", "public_token", "  public_token  "])
    def test_cle_publique_ou_absente_est_signalee(self, base, monkeypatch, valeur):
        monkeypatch.setenv("MOSHI_TTS_API_KEY", valeur)
        controle = _controle("jeton_voix")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"]["cle_privee"] is False

    def test_cle_privee_est_verte(self, base, monkeypatch):
        monkeypatch.setenv("MOSHI_TTS_API_KEY", "0f" * 32)
        controle = _controle("jeton_voix")
        assert controle["niveau"] == supervision.OK
        # La valeur de la clé n'apparaît nulle part dans la sonde, qui sort du serveur.
        assert "0f" * 32 not in json.dumps(controle)


class TestSauvegarde:
    def test_sans_jeton_on_ne_pretend_pas_que_tout_va_bien(self, base, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERVISION_BACKUP_STAMP", str(tmp_path / "absent"))
        assert _controle("sauvegarde")["niveau"] == supervision.ATTENTION

    def test_un_jeton_frais_et_sa_copie_distante_sont_verts(self, base, monkeypatch, tmp_path):
        jeton = tmp_path / "derniere-sauvegarde"
        jeton.write_text(_il_y_a(60), encoding="utf-8")
        (tmp_path / "derniere-sauvegarde-distante").write_text(_il_y_a(58), encoding="utf-8")
        monkeypatch.setenv("SUPERVISION_BACKUP_STAMP", str(jeton))
        assert _controle("sauvegarde")["niveau"] == supervision.OK

    def test_sans_copie_distante_on_le_dit(self, base, monkeypatch, tmp_path):
        """Des copies sur la même machine ne survivent pas à un incident disque."""
        jeton = tmp_path / "derniere-sauvegarde"
        jeton.write_text(_il_y_a(60), encoding="utf-8")
        monkeypatch.setenv("SUPERVISION_BACKUP_STAMP", str(jeton))
        controle = _controle("sauvegarde")
        assert controle["niveau"] == supervision.ATTENTION
        assert "hors du serveur" in controle["resume"]
        assert controle["mesure"]["distante"] is False

    def test_copie_distante_arretee_est_signalee(self, base, monkeypatch, tmp_path):
        jeton = tmp_path / "derniere-sauvegarde"
        jeton.write_text(_il_y_a(60), encoding="utf-8")
        (tmp_path / "derniere-sauvegarde-distante").write_text(_il_y_a(60 * 50),
                                                                 encoding="utf-8")
        monkeypatch.setenv("SUPERVISION_BACKUP_STAMP", str(jeton))
        controle = _controle("sauvegarde")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"]["age_distante_heures"] >= 49

    def test_deux_nuits_manquees_sont_une_panne(self, base, monkeypatch, tmp_path):
        jeton = tmp_path / "derniere-sauvegarde"
        jeton.write_text(_il_y_a(60 * 80), encoding="utf-8")
        monkeypatch.setenv("SUPERVISION_BACKUP_STAMP", str(jeton))
        assert _controle("sauvegarde")["niveau"] == supervision.PANNE

    def test_un_jeton_illisible_ne_passe_pas_pour_frais(self, base, monkeypatch, tmp_path):
        jeton = tmp_path / "derniere-sauvegarde"
        jeton.write_text("pas une date", encoding="utf-8")
        monkeypatch.setenv("SUPERVISION_BACKUP_STAMP", str(jeton))
        assert _controle("sauvegarde")["niveau"] == supervision.ATTENTION


class TestTwilio:
    """L'angle mort : quand Twilio n'arrive pas à nous joindre, l'application n'en
    sait rien du tout — aucune ligne dans `calls`, aucun journal."""

    def test_sans_releve_le_controle_le_dit(self, base):
        controle = _controle("twilio")
        assert "Pas encore relevé" in controle["resume"]

    def test_des_erreurs_relevees_alertent(self, base):
        supervision.noter("twilio", {"erreurs": 4, "codes": {"11200": 4}})
        controle = _controle("twilio")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"]["erreurs"] == 4

    def test_zero_erreur_relevee_est_vert(self, base):
        supervision.noter("twilio", {"erreurs": 0, "codes": {}})
        assert _controle("twilio")["niveau"] == supervision.OK

    def test_une_releve_impossible_n_est_pas_un_feu_vert(self, base):
        supervision.noter("twilio", {"erreur": "ConnectTimeout"})
        assert _controle("twilio")["niveau"] == supervision.ATTENTION

    def test_un_401_est_nomme_pour_ce_qu_il_est(self, base):
        """Twilio A répondu : dire « l'API n'a pas répondu » enverrait chercher un
        problème de réseau là où il faut renouveler un jeton. Constaté en production
        le 24/08 — jeton révoqué depuis l'incident #20, jamais reporté dans le `.env`,
        et rien ne l'avait signalé parce que les appels ENTRANTS n'en dépendent pas."""
        supervision.noter("twilio", {"erreur": "HTTP 401"})
        controle = _controle("twilio")
        assert controle["niveau"] == supervision.ATTENTION
        assert "401" in controle["resume"]
        assert "identifiants" in controle["detail"]
        assert controle["mesure"]["cause"] == "HTTP 401"

    def test_une_reponse_d_erreur_porte_son_code(self, base, monkeypatch):
        """Le code HTTP doit survivre jusqu'à l'ardoise, sinon le diagnostic se perd."""
        import asyncio

        import httpx

        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-de-test")

        class _Reponse:
            status_code = 401

            def raise_for_status(self):
                raise httpx.HTTPStatusError("refusé", request=None, response=self)

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                return _Reponse()

        with patch("httpx.AsyncClient", lambda **k: _Client()):
            asyncio.run(supervision.rafraichir_twilio())
        valeur, _ = supervision.relire("twilio")
        assert valeur == {"erreur": "HTTP 401"}

    def test_une_releve_figee_est_signalee(self, base):
        """Une tâche de fond morte laisserait un « 0 erreur » éternellement vert."""
        supervision.noter("twilio", {"erreurs": 0, "codes": {}})
        with db.get_conn() as conn:
            conn.execute("UPDATE supervision SET maj_le = ? WHERE cle = 'twilio'",
                         (_il_y_a(60 * 12),))
        controle = _controle("twilio")
        assert controle["niveau"] == supervision.ATTENTION
        assert "figée" in controle["resume"]

    def test_la_releve_ne_compte_que_les_vraies_erreurs(self, base, monkeypatch):
        """Les alertes Twilio de niveau « notice » ne décrivent pas une panne : les
        compter reviendrait à sonner en permanence, donc à ne plus être écouté."""
        import asyncio

        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-de-test")

        class _Reponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"alerts": [
                    {"error_code": "11200"}, {"error_code": "11200"},
                    {"error_code": "12100"}, {"error_code": None}, {},
                ]}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                return _Reponse()

        with patch("httpx.AsyncClient", lambda **k: _Client()):
            asyncio.run(supervision.rafraichir_twilio())
        valeur, _ = supervision.relire("twilio")
        assert valeur == {"erreurs": 3, "codes": {"11200": 2, "12100": 1}}

    def test_la_releve_est_desactivable(self, monkeypatch):
        """`SUPERVISION_TWILIO_SECONDES=0` doit rendre la boucle inerte — c'est ce qui
        garantit que la suite de tests ne touche jamais au réseau (cf. conftest)."""
        import asyncio

        monkeypatch.setenv("SUPERVISION_TWILIO_SECONDES", "0")
        with patch.object(supervision, "rafraichir_twilio") as releve:
            asyncio.run(supervision.boucle_twilio())
        releve.assert_not_called()

    def test_la_releve_est_arretee_a_l_extinction(self, base, monkeypatch):
        """Une tâche de fond abandonnée empêche la boucle d'événements de se fermer :
        le processus — ou un `TestClient` qui sort en erreur — attend indéfiniment.

        Vécu le 23/08 : le contrôle de mutation s'est figé une demi-heure exactement
        là-dessus. Le scénario est joué à la main plutôt qu'avec `TestClient`, pour
        qu'une régression échoue au lieu de bloquer la suite."""
        import asyncio

        from app import main as main_mod

        monkeypatch.setenv("SUPERVISION_TWILIO_SECONDES", "900")
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")

        monkeypatch.setenv("RETENTION_INTERVALLE_SECONDES", "3600")

        from app import taches as registre

        async def scenario():
            await main_mod._demarrer_taches_de_fond()
            boucles = [t for t in registre.retenues()
                       if t.get_name() in ("relève des alertes Twilio",
                                           "purge des données personnelles")]
            assert len(boucles) == 2, "les deux boucles doivent être retenues par le registre"
            assert all(not t.done() for t in boucles)
            await asyncio.sleep(0)
            await main_mod._arreter_taches_de_fond()
            return boucles

        boucles = asyncio.run(scenario())
        # TOUTES doivent être arrêtées, pas seulement la première : c'est précisément
        # ce qu'un motif copié-collé par tâche finit par oublier.
        assert all(t.done() for t in boucles)
        assert registre.en_cours() == 0

    def test_sans_identifiants_la_releve_se_declare_impossible(self, base, monkeypatch):
        import asyncio

        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
        asyncio.run(supervision.rafraichir_twilio())
        assert _controle("twilio")["niveau"] == supervision.ATTENTION


ENTETE = {"X-Supervision-Token": "le-bon-jeton"}


class TestSonde:
    """La sonde HTTP : c'est elle que le surveillant extérieur interroge, et son CODE
    HTTP — pas le corps de la réponse — décide de l'alerte."""

    def test_invisible_sans_jeton_configure(self, monkeypatch):
        monkeypatch.delenv("SUPERVISION_TOKEN", raising=False)
        with TestClient(app) as client:
            assert client.get("/supervision").status_code == 404

    def test_refuse_un_mauvais_jeton(self, monkeypatch):
        monkeypatch.setenv("SUPERVISION_TOKEN", "le-bon-jeton")
        with TestClient(app) as client:
            assert client.get("/supervision").status_code == 401
            assert client.get("/supervision?token=faux").status_code == 401

    def test_accepte_l_en_tete(self, monkeypatch):
        monkeypatch.setenv("SUPERVISION_TOKEN", "le-bon-jeton")
        with TestClient(app) as client:
            par_entete = client.get("/supervision", headers=ENTETE)
        assert par_entete.status_code in (200, 503)
        assert par_entete.json()["niveau"] in ("ok", "attention", "panne")

    def test_refuse_le_jeton_dans_l_url(self, monkeypatch):
        """Un jeton dans l'URL finit dans les journaux du proxy : il n'est plus secret."""
        monkeypatch.setenv("SUPERVISION_TOKEN", "le-bon-jeton")
        with TestClient(app) as client:
            assert client.get("/supervision?token=le-bon-jeton").status_code == 401

    def test_503_uniquement_en_panne(self, monkeypatch):
        monkeypatch.setenv("SUPERVISION_TOKEN", "le-bon-jeton")
        faux = {"niveau": "attention", "mesure_le": "", "fenetre_jours": 7, "controles": []}
        with patch.object(supervision, "etat", return_value=faux):
            with TestClient(app) as client:
                assert client.get("/supervision", headers=ENTETE).status_code == 200
        faux["niveau"] = "panne"
        with patch.object(supervision, "etat", return_value=faux):
            with TestClient(app) as client:
                assert client.get("/supervision", headers=ENTETE).status_code == 503

    def test_health_reste_une_sonde_de_vie(self, monkeypatch):
        """`/health` sert la porte de déploiement : une sauvegarde en retard ne doit
        pas empêcher de déployer un correctif."""
        monkeypatch.setenv("SUPERVISION_TOKEN", "le-bon-jeton")
        faux = {"niveau": "panne", "mesure_le": "", "fenetre_jours": 7, "controles": []}
        with patch.object(supervision, "etat", return_value=faux):
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200


class TestEcranAdmin:
    """La page « Santé & coûts » lit la MÊME fonction que la sonde. C'est délibéré :
    deux calculs séparés finiraient par diverger, et on croirait le plus rassurant."""

    @pytest.fixture()
    def client(self):
        return TestClient(app)

    def _login(self, client, email="admin@test.local", password="test-admin-pass"):
        assert client.post("/admin/login", data={"email": email, "password": password},
                           follow_redirects=False).status_code == 303
        return client

    def test_le_verdict_est_affiche(self, client):
        self._login(client)
        page = client.get("/admin/health")
        assert page.status_code == 200
        # Jinja échappe l'apostrophe en `&#39;` : on compare le texte rendu, pas la
        # source HTML, sinon le test échoue sur un détail d'échappement.
        rendu = html.unescape(page.text)
        assert "État de la pile" in rendu
        for titre in ("Configuration du chemin d'appel", "Appels muets",
                      "Sauvegarde de la base", "Alertes Twilio", "Blanc ressenti"):
            assert titre in rendu

    def test_le_niveau_n_est_pas_porte_par_la_seule_couleur(self, client):
        """Une pastille colorée ne se lit ni en noir et blanc, ni par un daltonien."""
        self._login(client)
        page = client.get("/admin/health")
        assert "chip-good" in page.text or "chip-warn" in page.text
        assert ">Vert<" in page.text or ">Attention<" in page.text or ">Panne<" in page.text

    def test_l_ecran_suit_la_sonde(self, client):
        """Si la sonde crie « panne », la page ne doit pas afficher un bandeau vert."""
        self._login(client)
        faux = {"niveau": "panne", "mesure_le": "2026-08-23T12:00:00Z", "fenetre_jours": 7,
                "controles": [{"cle": "base", "titre": "Base de données", "niveau": "panne",
                               "resume": "Écriture impossible.", "detail": "", "mesure": {}}]}
        with patch.object(supervision, "etat", return_value=faux):
            page = client.get("/admin/health")
        assert "Panne en cours" in page.text
        assert "Tout est vert" not in page.text

    def test_un_restaurateur_n_y_a_pas_acces(self, client):
        from app import users

        tenant = tenants.create_tenant("Chez Sonde", "+33199000042")
        user = users.create_user(f"sonde-{tenant.id}@test.fr", "resto-pass-sonde",
                                 users.ROLE_RESTAURATEUR, tenant.id)
        try:
            self._login(client, user.email, "resto-pass-sonde")
            assert client.get("/admin/health").status_code == 403
        finally:
            tenants.delete_tenant(tenant.id)


class TestCablage:
    """Une variable lue par le code doit être TRANSMISE au conteneur.

    Compose lit `.env` pour la substitution, pas pour peupler l'environnement d'un
    service : une variable absente du bloc `environment:` n'atteint jamais l'application.
    Le 24/08, le jeton était bien dans le `.env` du VPS, la sonde répondait 404, et le
    déploiement s'était déclaré « vérifié ». Ce test rend l'oubli impossible à répéter.
    """

    @staticmethod
    def _compose() -> str:
        import pathlib

        chemin = pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml"
        if not chemin.exists():
            pytest.skip("docker-compose.yml hors de portée (tests montés seuls dans le "
                        "conteneur) — ce contrôle mord en CI, où le dépôt est complet.")
        return chemin.read_text(encoding="utf-8")

    @staticmethod
    def _variables_lues() -> set[str]:
        """TOUTES les variables lues par l'application, pas une liste de préfixes : le
        19/09/2026, 23 réglages (GPU chaud, coûts, fuseau…) documentés dans env.example
        n'atteignaient pas le conteneur, parce que le test ne regardait que certains
        modules."""
        import pathlib
        import re

        app = pathlib.Path(__file__).resolve().parents[1] / "app"
        # Lecture directe, et lecture par les petits utilitaires `_entier`/`_jours` des
        # modules de seuils, qui reçoivent le nom en paramètre.
        motifs = (r'os\.(?:getenv|environ\.get)\(\s*["\']([A-Z0-9_]+)["\']',
                  r'\b_(?:entier|jours)\(\s*["\']([A-Z0-9_]+)["\']')
        lues: set[str] = set()
        for source in app.rglob("*.py"):
            texte = source.read_text(encoding="utf-8")
            for motif in motifs:
                lues |= set(re.findall(motif, texte))
        return lues

    def test_toutes_les_variables_lues_sont_transmises(self):
        compose = self._compose()
        lues = self._variables_lues()
        assert {"SUPERVISION_TOKEN", "SUPERVISION_FENETRE_JOURS",
                "MOSHI_KEEPWARM_SECONDS"} <= lues, (
            "la lecture des variables ne trouve plus ce qu'elle devrait : le test ne "
            "vérifie rien")
        oubliees = sorted(v for v in lues if f"{v}:" not in compose)
        assert not oubliees, (
            "ces variables sont lues par l'application mais absentes de "
            f"docker-compose.yml, donc invisibles dans le conteneur : {oubliees}")

    def test_le_jeton_est_documente_sans_valeur(self):
        """`env.example` doit expliquer le jeton sans jamais porter de valeur : le dépôt
        est public, et un gabarit qui ressemble à un secret finit par en devenir un."""
        import pathlib
        import re

        chemin = pathlib.Path(__file__).resolve().parents[2] / "env.example"
        if not chemin.exists():
            pytest.skip("env.example hors de portée du conteneur — mord en CI.")
        texte = chemin.read_text(encoding="utf-8")
        assert "SUPERVISION_TOKEN" in texte
        assert not re.search(r"^\s*#?\s*SUPERVISION_TOKEN=\S", texte, re.M), (
            "env.example contient SUPERVISION_TOKEN=<valeur> : gabarit ou secret, "
            "les deux sont à proscrire dans un dépôt public")


class TestMessageDIndisponibilite:
    """Le message « rappelez dans quelques minutes » ne sert que GPU introuvable. S'il
    manque, personne ne le remarque — jusqu'au jour où l'appelant retrouve le silence."""

    def _wav(self, chemin):
        import wave

        chemin.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(chemin), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)

    def test_un_message_manquant_se_voit(self, etablissement, tmp_path, monkeypatch):
        from app.voice import greeting

        monkeypatch.setenv("MOSHI_TTS_URL", "wss://exemple.modal.run")
        monkeypatch.setenv("GREETING_CACHE_DIR", str(tmp_path / "accueils"))
        liste = tenants.list_all()
        for t in liste:
            self._wav(greeting._cache_path(t))
        controle = _controle("accueils")
        assert controle["niveau"] == supervision.ATTENTION
        assert controle["mesure"]["sans_message_d_indisponibilite"] == len(liste)

        for t in liste:
            self._wav(greeting._chemin_indisponible(t))
        controle = _controle("accueils")
        assert controle["niveau"] == supervision.OK
        assert controle["mesure"]["sans_message_d_indisponibilite"] == 0
