"""La porte qui met l'établissement d'essai sur répondeur, et l'en retire.

« Essayer le renvoi » vit dans la MÉMOIRE du processus de production (`renvoi.signaler_panne`) :
la recette d'appels, qui tourne dans un autre processus (`docker compose exec`), ne peut pas
l'écrire elle-même. D'où cette porte, appelée en local par `etage2.py` pour jouer le
répondeur (S6 à S8) sans numéro de secours, donc sans achat.

Quatre verrous : la porte ne s'ouvre que de l'intérieur du conteneur (`main.py`), la
recette doit être activée, le jeton est à usage unique et propre à
l'action demandée, et la porte n'agit QUE sur l'établissement d'essai — jamais sur un
identifiant fourni par l'appelant, qui n'a d'ailleurs aucun champ où le glisser.
"""
from .. import essai, renvoi
from . import gardes

ESSAYER, ARRETER = "essayer", "arreter"
ACTIONS = (ESSAYER, ARRETER)
# D'où la porte accepte d'être appelée : le conteneur lui-même (`etage2._PORTE`).
LOCALES = ("127.0.0.1", "::1")


def appliquer(action: str, nonce: str, jeton: str) -> int:
    """Le code HTTP à rendre : 204 fait, 404 porte fermée, 403 jeton refusé, 400 action inconnue."""
    tenant_id = essai.etablissement_id()
    # Le nom doit contenir « essai » : une faute de frappe dans RECETTE_ETABLISSEMENT ne
    # doit pas mettre un vrai restaurant sur répondeur.
    if not gardes.active() or tenant_id is None or essai.refus(tenant_id):
        return 404
    if not gardes.jeton_valide("secours-" + action, nonce, jeton):
        return 403
    if action == ESSAYER:
        renvoi.signaler_panne(renvoi.ESSAI, tenant_id)
    elif action == ARRETER:
        renvoi.arreter_essai(tenant_id)
    else:
        return 400
    return 204
