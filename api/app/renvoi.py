"""En cas de panne, l'appel est renvoyé vers le restaurant (ASSISTANTE-118).

Jusqu'ici, une assistante en panne laissait le client sans personne : au mieux une voix
de secours, au pire un silence puis la tonalité. Décision de Helmi (01/10/2026) :

- l'assistante marche → rien ne change, elle répond à toute heure ;
- elle est en panne, pendant les horaires d'ouverture → l'appel sonne au numéro de
  secours de l'établissement (le fixe, sinon le portable du gérant) ;
- elle est en panne hors horaires, sans numéro de secours, ou personne ne décroche →
  le client laisse un message, que le restaurant reçoit par e-mail.

Comment : le TwiML de l'appel place une étape APRÈS le flux média (`/twilio/suite`).
Twilio n'y vient que si le flux s'arrête sans que l'appel soit raccroché — c'est le cas
quand le pipeline s'effondre, ou quand il rend la ligne après avoir demandé le renvoi
(`demander`). Aucune commande n'est envoyée à Twilio pendant la panne : c'est lui qui
vient chercher la suite, donc le mécanisme tient même si notre accès à son API est
lui-même la panne.

Ce module ne fait ni réseau ni base : des décisions, du TwiML, et deux mémoires en
RAM (un seul worker uvicorn, comme tout l'état vivant des appels).
"""
import time as _temps
from datetime import datetime, time, timedelta
from typing import Optional
from xml.sax.saxutils import escape

from . import calls, connecteurs, disponibilite, horloge

# Pourquoi l'assistante passe la main. Rangé dans `calls.secours_motif`.
VOIX, MODELE, TRANSCRIPTION = "voix", "modele", "transcription"
PIPELINE, FLUX, PANNE_RECENTE = "pipeline", "flux", "panne_recente"
# Un essai lancé depuis l'admin : le prochain appel de l'établissement est renvoyé, pour
# vérifier que le téléphone du restaurant sonne — sans attendre une vraie panne.
ESSAI = "essai"
LIBELLES = {
    VOIX: "la voix ne répondait plus",
    MODELE: "le modèle ne répondait plus",
    TRANSCRIPTION: "la transcription ne répondait plus",
    PIPELINE: "le pipeline vocal s'est arrêté sur une erreur",
    FLUX: "le flux audio n'a pas pu s'ouvrir",
    PANNE_RECENTE: "une panne venait d'être constatée sur un autre appel",
    ESSAI: "essai de renvoi lancé depuis l'admin",
}

RENVOI, REPONDEUR = "renvoi", "repondeur"

# Le téléphone du restaurant sonne 15 s, pas plus : si son fixe est lui-même renvoyé
# vers nous quand personne ne décroche (délai habituel des opérateurs : 20 à 30 s),
# l'appel doit basculer sur le répondeur AVANT de revenir ici en boucle.
SONNERIE_SECONDES = 15
MESSAGE_MAX_SECONDES = 120
# Après une panne, les appels suivants sont renvoyés d'emblée pendant trois minutes au
# lieu de revivre le même silence. Passé ce délai, un appel retente l'assistante.
MEMOIRE_PANNE_SECONDES = 180.0
# Sans horaires renseignés, on ne fait pas sonner un portable à 3 h du matin.
JOURNEE_PAR_DEFAUT = (time(8, 0), time(23, 0))
# On décroche au restaurant avant le premier service : la mise en place.
AVANCE_OUVERTURE = timedelta(minutes=60)
_OUBLI_APPEL_SECONDES = 6 * 3600.0

ANNONCE_RENVOI = "Un instant, je vous mets en relation avec le restaurant."
ANNONCE_REPONDEUR = ("Notre assistante ne peut pas vous répondre pour le moment. Après le "
                     "bip, laissez votre nom, votre numéro et votre demande : le restaurant "
                     "vous rappellera.")
SANS_MESSAGE = "Nous n'avons pas reçu de message. Au revoir."
MERCI = "Merci, votre message est transmis au restaurant. Au revoir."

# Pannes récentes : établissement (ou None pour tout le parc) → (instant, motif).
_pannes: dict[Optional[int], tuple[float, str]] = {}
# Appels dont le flux média s'est ouvert : CallSid → {"ouvert": instant, "motif": …}.
_appels: dict[str, dict] = {}


def reinitialiser() -> None:
    """Pour les tests."""
    _pannes.clear()
    _appels.clear()


# ---- La mémoire des pannes ------------------------------------------------------------

def signaler_panne(motif: str, tenant_id: Optional[int] = None) -> None:
    """Une panne vient d'être constatée. La voix, le modèle, la transcription et le flux
    sont communs à tout le parc ; une erreur du pipeline peut ne tenir qu'aux données
    d'un établissement, et n'engage que lui."""
    cle = tenant_id if motif in (PIPELINE, ESSAI) else None
    _pannes[cle] = (_temps.monotonic(), motif)


def panne_recente(tenant_id: int) -> Optional[str]:
    """Le motif de la panne encore fraîche qui concerne cet établissement, ou None."""
    maintenant = _temps.monotonic()
    for cle in (None, tenant_id):
        panne = _pannes.get(cle)
        if panne and maintenant - panne[0] <= MEMOIRE_PANNE_SECONDES:
            return panne[1]
    return None


def essai_en_cours(tenant_id: int) -> bool:
    return panne_recente(tenant_id) == ESSAI


def arreter_essai(tenant_id: int) -> None:
    if (_pannes.get(tenant_id) or (0, ""))[1] == ESSAI:
        del _pannes[tenant_id]


# ---- Ce que chaque appel a demandé -----------------------------------------------------

def flux_ouvert(call_sid: Optional[str]) -> None:
    """Le flux média de cet appel est arrivé jusqu'à nous."""
    if not call_sid:
        return
    maintenant = _temps.monotonic()
    for ancien in [c for c, a in _appels.items()
                   if maintenant - a["ouvert"] > _OUBLI_APPEL_SECONDES]:
        del _appels[ancien]
    _appels.setdefault(call_sid, {"ouvert": maintenant, "motif": None})


def demander(call_sid: Optional[str], motif: str) -> None:
    """L'assistante ne peut pas servir cet appel : à la fin du flux, il sera renvoyé."""
    if not call_sid:
        return
    flux_ouvert(call_sid)
    if _appels[call_sid]["motif"] is None:
        _appels[call_sid]["motif"] = motif


def est_demande(call_sid: Optional[str]) -> bool:
    return bool(call_sid) and bool((_appels.get(call_sid) or {}).get("motif"))


def motif_a_la_fin_du_flux(call_sid: Optional[str]) -> Optional[str]:
    """Ce que `/twilio/suite` doit faire de cet appel : None = il s'est terminé
    normalement, on raccroche.

    Un appel dont le flux ne s'est JAMAIS ouvert chez nous (adresse injoignable, serveur
    redémarré au milieu de l'appel) n'a été servi par personne : c'est une panne."""
    if not call_sid:
        return None
    appel = _appels.get(call_sid)
    return FLUX if appel is None else appel["motif"]


# ---- La décision -----------------------------------------------------------------------

def on_decroche(tenant, instant: Optional[datetime] = None) -> bool:
    """Y a-t-il quelqu'un au restaurant pour décrocher, à cette heure ?"""
    instant = instant or horloge.maintenant()
    horaires = disponibilite.charger(connecteurs.avec_horaires_du_carnet(tenant).opening_hours)
    if not disponibilite.est_configure(horaires):
        debut, fin = JOURNEE_PAR_DEFAUT
        return debut <= instant.time() < fin
    return (disponibilite.ouvert_a(horaires, instant)
            or disponibilite.ouvert_a(horaires, instant + AVANCE_OUVERTURE))


def decision(tenant, *, appelant: Optional[str] = None, transfere_depuis: Optional[str] = None,
             instant: Optional[datetime] = None) -> tuple[str, Optional[str]]:
    """(RENVOI, numéro à composer) ou (REPONDEUR, None)."""
    secours = (tenant.numero_secours or "").strip()
    if not secours:
        return REPONDEUR, None
    # L'appel nous arrive DU numéro de secours, ou a été renvoyé par lui : le rappeler
    # ferait tourner l'appel en rond entre sa ligne et la nôtre.
    if secours in ((appelant or "").strip(), (transfere_depuis or "").strip()):
        return REPONDEUR, None
    if not on_decroche(tenant, instant):
        return REPONDEUR, None
    return RENVOI, secours


# ---- Le TwiML --------------------------------------------------------------------------

def _dire(texte: str) -> str:
    return f'    <Say language="fr-FR">{escape(texte)}</Say>'


def twiml_renvoi(numero: str, *, appelant: Optional[str], ligne: Optional[str]) -> str:
    """L'appel sonne au restaurant. Le restaurant voit le numéro du client ; pour un
    appel masqué, notre ligne — il n'y a rien d'autre à montrer, et Twilio facture quatre
    fois plus cher un renvoi vers un portable sans numéro européen à présenter."""
    presente = calls.numero_appelant(appelant) or (ligne or "").strip()
    identite = f' callerId="{escape(presente)}"' if presente else ""
    return "\n".join([
        _dire(ANNONCE_RENVOI),
        f'    <Dial timeout="{SONNERIE_SECONDES}" answerOnBridge="true"{identite} '
        'action="/twilio/secours/fin" method="POST">',
        f"        <Number>{escape(numero)}</Number>",
        "    </Dial>",
    ])


def twiml_repondeur() -> str:
    return "\n".join([
        _dire(ANNONCE_REPONDEUR),
        f'    <Record maxLength="{MESSAGE_MAX_SECONDES}" timeout="5" playBeep="true" '
        'trim="trim-silence" action="/twilio/repondeur" method="POST" '
        'recordingStatusCallback="/twilio/repondeur/pret" '
        'recordingStatusCallbackMethod="POST" recordingStatusCallbackEvent="completed"/>',
        # Twilio ne vient ici que si rien n'a été enregistré.
        _dire(SANS_MESSAGE),
        "    <Hangup/>",
    ])


def twiml_merci() -> str:
    return _dire(MERCI) + "\n    <Hangup/>"


def twiml_sans_message() -> str:
    return _dire(SANS_MESSAGE) + "\n    <Hangup/>"
