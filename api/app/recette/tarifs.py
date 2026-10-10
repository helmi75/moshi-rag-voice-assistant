"""Ce qu'un appel d'essai peut coûter AU PLUS, pour refuser avant de dépenser.

Une borne haute, pas une estimation : la durée maximale d'un appel d'essai, aux tarifs que
l'application applique déjà à chaque appel (`calls.py`, relevés chez les fournisseurs). Le
coût réel se lit après l'appel ; c'est lui qui est rendu et noté au carnet.
"""
import math

from .. import calls

DUREE_MAX_SECONDES = 180          # la limite donnée à Twilio (`TimeLimit`) et au client d'essai
# Le cerveau et, sur la chaîne classique, la voix se paient au jeton et au caractère : un
# plafond large par appel, cinq fois ce que les bancs du 04/10 et du 09/10/2026 ont mesuré.
_MODELE_AU_PLUS = 0.025
_VOIX_CLASSIQUE_AU_PLUS = 0.05


def borne(moteur: str, etage: int) -> float:
    minutes = math.ceil(DUREE_MAX_SECONDES / 60)
    if moteur == "gpt_live":
        total = minutes * calls._COST_GPT_LIVE_PER_MIN + _MODELE_AU_PLUS
    else:
        total = minutes * calls._COST_DEEPGRAM_PER_MIN + _MODELE_AU_PLUS + _VOIX_CLASSIQUE_AU_PLUS
    if etage == 2:
        # Deux jambes : celle qui appelle (sortant vers un fixe) et celle qui reçoit.
        total += minutes * (calls._COST_RENVOI_FIXE + calls._COST_TWILIO_PER_MIN)
    return round(total, 4)
