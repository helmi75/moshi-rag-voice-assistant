"""Codec µ-law (G.711) et en-tête WAV — pour enregistrer les appels (#88).

**Pourquoi ne pas utiliser `audioop`.** Le module standard sait le faire, mais il est
supprimé de Python 3.13. Le jour où l'image monterait de version, l'enregistrement des
appels disparaîtrait **en silence** — pas une erreur au démarrage, juste des fichiers
vides qu'on ne découvrirait qu'en cherchant à réécouter l'appel qui s'est mal passé.
D'où cette implémentation en numpy, dont `api/tests/test_ulaw.py` prouve la justesse en
la comparant à `audioop` tant qu'il existe.

**Pourquoi le µ-law.** C'est le format que la ligne téléphonique transporte réellement :
Twilio l'envoie, Twilio le reçoit. L'écrire tel quel n'ajoute aucune perte, et c'est deux
fois plus compact que du PCM 16 bits.

**Pourquoi l'en-tête WAV est calculé À LA LECTURE et pas écrit dans le fichier.** Un WAV
porte en tête la taille de ses données ; si le processus meurt avant de la corriger, le
fichier est illisible. Or l'appel qu'on veut réécouter est précisément celui qui a planté.
On écrit donc des octets µ-law bruts — un flux tronqué reste parfaitement lisible — et on
fabrique l'en-tête au moment de servir le fichier, à partir de sa taille réelle.
"""
import struct

import numpy as np

# Constantes de la recommandation G.711, dans le domaine 14 BITS.
#
# C'est le piège de ce codec, et il est silencieux : le µ-law ne travaille pas sur les
# 16 bits d'entrée mais sur 14, l'échantillon étant d'abord décalé de deux bits. Le biais
# et la saturation sont donc exprimés dans ce domaine réduit. Une implémentation en
# 16 bits « marche » — elle produit des octets plausibles, elle se décode sans erreur —
# mais elle est fausse d'un cran sur les faibles amplitudes, ce qu'aucune écoute ne
# révèle. Seule la comparaison avec `audioop` l'a montrée (cf. test_ulaw.py).
_BIAS = 0x84 >> 2  # 33
_CLIP = 8159  # saturation dans le domaine 14 bits

# Bornes supérieures des huit segments logarithmiques. `searchsorted` y trouve d'un coup
# le segment de chaque échantillon, là où la référence en C fait une recherche linéaire.
_BORNES = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF], dtype=np.int32)


def _table_decodage() -> np.ndarray:
    """Les 256 valeurs PCM possibles, calculées une fois pour toutes.

    Une table plutôt qu'une formule vectorisée : le décodage est fait à chaque écoute
    depuis l'admin, sur des fichiers de plusieurs mégaoctets, et une indexation numpy
    est à la fois plus rapide et plus manifestement correcte qu'un enchaînement de
    décalages de bits."""
    octets = np.arange(256, dtype=np.int32)
    inverse = ~octets & 0xFF
    signe = inverse & 0x80
    exposant = (inverse >> 4) & 0x07
    mantisse = inverse & 0x0F
    # Ici le biais est celui du domaine 16 bits (0x84) : le décodage reconstitue
    # directement un échantillon 16 bits, sans repasser par les 14.
    valeur = ((mantisse << 3) + 0x84) << exposant
    return np.where(signe, 0x84 - valeur, valeur - 0x84).astype(np.int16)


_DECODAGE = _table_decodage()


def encoder(pcm16: bytes) -> bytes:
    """PCM 16 bits little-endian → µ-law. Un octet en sort pour deux qui entrent."""
    if not pcm16:
        return b""
    # Un nombre impair d'octets signifierait un échantillon coupé en deux : on ignore
    # l'orphelin plutôt que de laisser numpy lever, car cette fonction est appelée sur
    # le chemin d'un appel en cours et ne doit jamais échouer.
    if len(pcm16) % 2:
        pcm16 = pcm16[:-1]
    # Passage en 14 bits AVANT tout le reste : décalage arithmétique, donc le signe
    # est préservé, exactement comme en C.
    echantillons = np.frombuffer(pcm16, dtype="<i2").astype(np.int32) >> 2

    negatif = echantillons < 0
    amplitude = np.abs(echantillons)
    np.clip(amplitude, 0, _CLIP, out=amplitude)
    amplitude += _BIAS

    segment = np.searchsorted(_BORNES, amplitude, side="left").astype(np.int32)
    # Un segment de 8 signifie « au-delà du dernier palier » : c'est la saturation, et
    # elle a sa propre valeur (0x7F) plutôt qu'un calcul qui déborderait.
    sature = segment >= 8
    sur = np.where(sature, 0, segment)
    octets = (sur << 4) | ((amplitude >> (sur + 1)) & 0x0F)
    octets = np.where(sature, 0x7F, octets)

    # Le µ-law inverse tous les bits ; le masque encode le signe au passage — 0x7F pour
    # un négatif laisse le bit de poids fort à 0 après le OU exclusif.
    masque = np.where(negatif, 0x7F, 0xFF)
    return (octets ^ masque).astype(np.uint8).tobytes()


def decoder(ulaw: bytes) -> bytes:
    """µ-law → PCM 16 bits little-endian. Deux octets en sortent pour un qui entre."""
    if not ulaw:
        return b""
    indices = np.frombuffer(ulaw, dtype=np.uint8)
    return _DECODAGE[indices].astype("<i2").tobytes()


def entete_wav(n_octets_pcm: int, canaux: int = 1, rate: int = 8000) -> bytes:
    """En-tête WAV PCM 16 bits de 44 octets, pour `n_octets_pcm` octets de données.

    Fabriqué au moment de servir le fichier : la taille vient de ce qu'on a réellement
    sur le disque, jamais d'une valeur écrite à l'avance qu'un arrêt brutal aurait
    laissée fausse."""
    bloc = canaux * 2  # 2 octets par échantillon
    return (
        b"RIFF" + struct.pack("<I", 36 + n_octets_pcm) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, canaux, rate, rate * bloc, bloc, 16)
        + b"data" + struct.pack("<I", n_octets_pcm)
    )


def entrelacer(gauche: bytes, droite: bytes) -> bytes:
    """Deux flux PCM mono → un flux stéréo. Le plus court est complété par du silence.

    C'est ce qui rend les coupures AUDIBLES : l'appelant dans une oreille, l'assistante
    dans l'autre. Une piste mixée laisserait deviner qui parle par-dessus qui ; deux
    canaux le font entendre."""
    g = np.frombuffer(gauche, dtype="<i2")
    d = np.frombuffer(droite, dtype="<i2")
    n = max(g.size, d.size)
    stereo = np.zeros((n, 2), dtype="<i2")
    stereo[: g.size, 0] = g
    stereo[: d.size, 1] = d
    return stereo.tobytes()
