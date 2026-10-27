"""Alertes Telegram (ASSISTANTE-161) : une par épisode, jamais sur le chemin de l'appel,
et le jeton (qui est dans l'URL de l'API) ne sort jamais dans un journal. Aucun réseau."""
import asyncio
import threading
import uuid

import httpx
import pytest
from loguru import logger

from app import db, notifications, renvoi, supervision, telegram, tenants

JETON = "123456:SECRET-DU-BOT"


@pytest.fixture(autouse=True)
def _propre(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    telegram.reinitialiser()
    yield
    telegram.reinitialiser()
    supervision.vider_cache()


@pytest.fixture()
def actif(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", JETON)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


class _FilImmediat:
    """Un « fil » qui joue sa tâche tout de suite : les tests lisent les messages sans attendre."""
    def __init__(self, target, args=(), **_):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture()
def envoyes(monkeypatch):
    liste: list[str] = []
    monkeypatch.setattr(telegram, "_envoyer", liste.append)
    monkeypatch.setattr(telegram, "Thread", _FilImmediat)
    return liste


def test_inactif_sans_variables(envoyes):
    assert telegram.actif() is False
    assert telegram.expedier("x") is False
    assert telegram.alerter("a", "x") is False
    assert envoyes == []


def test_inactif_avec_le_jeton_seul(monkeypatch, envoyes):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", JETON)
    assert telegram.actif() is False


def test_une_seule_alerte_par_episode_puis_le_retour_a_la_normale(actif, envoyes, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(telegram.time, "monotonic", lambda: t[0])
    assert telegram.alerter("ep", "panne") is True
    assert telegram.alerter("ep", "panne encore") is False
    assert telegram.retablir("ep", "rétabli") is True
    assert telegram.retablir("ep", "rétabli encore") is False
    t[0] += telegram.REPRISE_SECONDES
    assert telegram.alerter("ep", "nouvelle panne") is True
    assert envoyes == ["panne", "rétabli", "nouvelle panne"]


def test_retablir_sans_episode_ouvert_ne_dit_rien(actif, envoyes):
    assert telegram.retablir("jamais", "rétabli") is False
    assert envoyes == []


def test_l_episode_expire(actif, envoyes, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(telegram.time, "monotonic", lambda: t[0])
    assert telegram.alerter("ep", "un", valable_secondes=60) is True
    t[0] += 59
    assert telegram.alerter("ep", "deux", valable_secondes=60) is False
    t[0] += 2
    # Épisode refermé, mais pas de nouvelle alerte avant REPRISE_SECONDES : pas de rafale.
    assert telegram.alerter("ep", "trois", valable_secondes=60) is False
    t[0] += telegram.REPRISE_SECONDES
    assert telegram.alerter("ep", "quatre", valable_secondes=60) is True
    assert envoyes == ["un", "quatre"]


def test_pas_de_retablissement_pour_un_episode_expire(actif, envoyes, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(telegram.time, "monotonic", lambda: t[0])
    telegram.alerter("ep", "un", valable_secondes=60)
    t[0] += 61
    assert telegram.retablir("ep", "rétabli") is False


def test_l_envoi_ne_bloque_pas_l_appelant(actif, monkeypatch):
    sortie, fini = threading.Event(), threading.Event()

    def lent(texte):
        sortie.wait(5)
        fini.set()

    monkeypatch.setattr(telegram, "_envoyer", lent)
    assert telegram.expedier("lent") is True
    # Rendu la main AVANT la fin de l'envoi : appelé en direct, `fini` serait déjà posé.
    assert not fini.is_set()
    sortie.set()
    assert fini.wait(5)


def _journaux():
    lignes: list[str] = []
    return lignes, logger.add(lambda m: lignes.append(str(m)), level="DEBUG")


def test_un_echec_d_envoi_est_sans_effet_et_le_jeton_ne_fuit_pas(actif, monkeypatch):
    def explose(url, **kw):
        raise httpx.ConnectError(f"échec sur {url}")   # httpx recopie l'URL, donc le jeton
    monkeypatch.setattr(telegram.httpx, "post", explose)
    lignes, num = _journaux()
    try:
        telegram._envoyer("bonjour")   # ne lève pas
    finally:
        logger.remove(num)
    texte = "".join(lignes)
    assert "ConnectError" in texte
    assert JETON not in texte and "SECRET" not in texte


def test_un_code_http_d_erreur_se_journalise_sans_l_url(actif, monkeypatch):
    def refuse(url, **kw):
        return httpx.Response(401, request=httpx.Request("POST", url))
    monkeypatch.setattr(telegram.httpx, "post", refuse)
    lignes, num = _journaux()
    try:
        telegram._envoyer("bonjour")
    finally:
        logger.remove(num)
    texte = "".join(lignes)
    assert "401" in texte
    assert JETON not in texte


def test_l_envoi_poste_sur_l_api_telegram(actif, monkeypatch):
    vus = {}

    def poste(url, json=None, timeout=None):
        vus.update(url=url, json=json, timeout=timeout)
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr(telegram.httpx, "post", poste)
    telegram._envoyer("salut")
    assert vus["url"] == f"https://api.telegram.org/bot{JETON}/sendMessage"
    assert vus["json"] == {"chat_id": "42", "text": "salut"}
    assert vus["timeout"] == 10


# ---- Les branchements ----------------------------------------------------------------

def test_une_panne_signalee_alerte_une_fois(actif, envoyes):
    renvoi.signaler_panne(renvoi.PIPELINE, 7)
    renvoi.signaler_panne(renvoi.PIPELINE, 7)
    assert len(envoyes) == 1 and renvoi.PIPELINE in envoyes[0]


def test_un_appel_passe_en_secours_alerte_avec_le_nom_de_l_etablissement(actif, envoyes):
    db.init_db()
    tenant = tenants.create_tenant("Chez Telegram", f"+3364{uuid.uuid4().int % 10_000_000:07d}")
    try:
        from app import main
        sid = f"CA{uuid.uuid4().hex}"
        for _ in range(2):
            asyncio.run(main._secours(tenant, sid, "+33611223344", tenant.phone_number,
                                      None, renvoi.PANNE_RECENTE))
    finally:
        tenants.delete_tenant(tenant.id)
    assert len(envoyes) == 1
    assert "Chez Telegram" in envoyes[0] and renvoi.PANNE_RECENTE in envoyes[0]
    assert "+33611223344" not in envoyes[0]


def _controle(niveau):
    return supervision.Controle("test_tg", "Contrôle d'essai", niveau, "ça ne va pas")


def test_la_supervision_alerte_en_panne_puis_annonce_le_retour(actif, envoyes, monkeypatch):
    niveau = [supervision.PANNE]
    monkeypatch.setattr(supervision, "controles", lambda: [_controle(niveau[0])])
    supervision.etat(force=True)
    supervision.etat(force=True)          # même épisode : rien de plus
    niveau[0] = supervision.OK
    supervision.etat(force=True)
    supervision.etat(force=True)
    assert len(envoyes) == 2
    assert "Contrôle d'essai" in envoyes[0] and "rétabli" in envoyes[1]


def test_la_supervision_ne_pousse_rien_depuis_le_cache(actif, envoyes, monkeypatch):
    monkeypatch.setattr(supervision, "controles", lambda: [_controle(supervision.OK)])
    supervision.etat(force=True)
    monkeypatch.setattr(supervision, "controles", lambda: [_controle(supervision.PANNE)])
    supervision.etat()                    # cache encore frais : pas de recalcul
    assert envoyes == []


def test_le_credit_openai_et_le_solde_twilio_ecrivent_aussi_sur_telegram(actif, envoyes):
    asyncio.run(notifications.prevenir_credit_openai("2026-10-27T10:00:00Z"))
    asyncio.run(notifications.prevenir_solde_twilio(1.5, "USD", 5.0))
    asyncio.run(notifications.prevenir_solde_twilio(None, None, 5.0))
    assert len(envoyes) == 3
    assert "OpenAI" in envoyes[0] and "1.50" in envoyes[1] and "suspendu" in envoyes[2]


def test_un_controle_qui_oscille_ne_fait_pas_de_rafale(actif, envoyes, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(telegram.time, "monotonic", lambda: t[0])
    for _ in range(5):
        telegram.alerter("supervision:x", "panne")
        telegram.retablir("supervision:x", "rétabli")
        t[0] += 20
    assert envoyes == ["panne", "rétabli"]


def test_un_essai_de_renvoi_ne_fait_pas_sonner_l_alerte(actif, envoyes):
    from app import renvoi
    renvoi.signaler_panne(renvoi.ESSAI, 7)
    assert envoyes == []


def test_retabli_seulement_au_vert(actif, envoyes):
    from app import supervision
    panne = supervision.Controle("x", "Contrôle X", supervision.PANNE, "en panne")
    attention = supervision.Controle("x", "Contrôle X", supervision.ATTENTION, "INCONNU")
    ok = supervision.Controle("x", "Contrôle X", supervision.OK, "ok")
    supervision._pousser_telegram([panne])
    supervision._pousser_telegram([attention])
    assert envoyes == ["Helmane : Contrôle X en panne. en panne"]
    supervision._pousser_telegram([ok])
    assert envoyes[-1] == "Helmane : Contrôle X est rétabli."
