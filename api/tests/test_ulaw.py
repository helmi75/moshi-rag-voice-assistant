"""Codec µ-law : prouvé contre la référence, tant que la référence existe (#88).

Ce module réimplémente ce que fait `audioop`, parce qu'`audioop` disparaît en Python
3.13. Une réimplémentation non vérifiée serait pire que le problème qu'elle évite : des
enregistrements silencieusement corrompus, découverts le jour où on cherche à réécouter
l'appel qui s'est mal passé.

D'où le test croisé ci-dessous, sous `importorskip` : il compare octet par octet avec
`audioop` aujourd'hui, et se **désactivera de lui-même** en 3.13 au lieu de casser la
suite. Les autres tests, eux, ne dépendent d'aucune référence externe.
"""
import struct

import numpy as np
import pytest

from app.voice import ulaw


def _signal(n: int = 4000) -> bytes:
    """Un signal qui balaie toute la dynamique : sinusoïde + rampe + extrêmes.

    Un silence ou un bruit blanc ne testeraient qu'une poignée des 256 valeurs µ-law ;
    la compression est logarithmique, ses erreurs vivent aux extrémités."""
    t = np.linspace(0, 40 * np.pi, n)
    sinus = (np.sin(t) * 30000).astype(np.int32)
    rampe = np.linspace(-32768, 32767, n).astype(np.int32)
    melange = ((sinus + rampe) // 2).astype("<i2")
    melange[:4] = [-32768, 32767, 0, -1]
    return melange.tobytes()


class TestAllerRetour:
    def test_la_taille_est_divisee_par_deux_puis_retablie(self):
        pcm = _signal(1000)
        code = ulaw.encoder(pcm)
        assert len(code) == 1000
        assert len(ulaw.decoder(code)) == 2000

    def test_l_erreur_reste_bornee(self):
        """Le µ-law est destructif par construction. Ce qui compte n'est pas l'égalité,
        mais que l'erreur reste dans l'épaisseur du format — sinon la voix serait
        dégradée et le diagnostic porterait sur autre chose que ce qui s'est dit.

        La mesure doit changer de nature selon l'amplitude, et c'est le sujet même du
        µ-law : il quantifie finement près de zéro et grossièrement dans les aigus.
        Mesurer l'erreur RELATIVE sur un échantillon à -1 donne 700 % pour un écart de
        7 unités, parfaitement inaudible. On borne donc l'absolu en bas, le relatif en
        haut. Bornes issues d'une mesure, pas d'une intuition (max constaté : 8 et 4,3 %)."""
        pcm = _signal()
        avant = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        apres = np.frombuffer(ulaw.decoder(ulaw.encoder(pcm)), dtype="<i2").astype(np.int32)
        erreur = np.abs(avant - apres)

        petits = np.abs(avant) < 256
        assert erreur[petits].max() <= 8, "quantification trop grossière près du silence"

        grands = ~petits
        relative = erreur[grands] / np.abs(avant[grands])
        assert np.median(relative) < 0.02
        assert relative.max() < 0.08, "au-delà, ce n'est plus du G.711"

    def test_le_signe_est_conserve(self):
        """Une inversion de signe passerait inaperçue à l'oreille sur une voix seule,
        et rendrait le mixage stéréo faux."""
        pcm = np.array([-20000, -5000, -100, 100, 5000, 20000], dtype="<i2").tobytes()
        apres = np.frombuffer(ulaw.decoder(ulaw.encoder(pcm)), dtype="<i2")
        assert list(np.sign(apres)) == [-1, -1, -1, 1, 1, 1]

    @pytest.mark.parametrize("entree", [b"", b"\x00"])
    def test_les_entrees_degenerees_ne_levent_pas(self, entree):
        """Appelé sur le chemin d'un appel en cours : un octet orphelin (échantillon
        coupé en deux par un découpage de tampon) ne doit jamais faire tomber l'appel."""
        assert ulaw.encoder(entree) == b""
        assert ulaw.decoder(b"") == b""


class TestContreLaReference:
    def test_encodage_identique_a_audioop(self):
        audioop = pytest.importorskip(
            "audioop", reason="audioop supprimé en Python 3.13 — la référence n'existe "
                              "plus, les autres tests restent la garantie")
        pcm = _signal()
        assert ulaw.encoder(pcm) == audioop.lin2ulaw(pcm, 2)

    def test_decodage_identique_a_audioop(self):
        audioop = pytest.importorskip("audioop")
        code = bytes(range(256))
        assert ulaw.decoder(code) == audioop.ulaw2lin(code, 2)


class TestEnteteWav:
    def test_quarante_quatre_octets_et_les_bons_champs(self):
        """L'en-tête est fabriqué à la lecture, à partir de la taille réelle du fichier.
        S'il était faux, le navigateur refuserait de lire — ou lirait du bruit."""
        entete = ulaw.entete_wav(16000, canaux=1, rate=8000)
        assert len(entete) == 44
        assert entete[0:4] == b"RIFF" and entete[8:12] == b"WAVE"
        assert struct.unpack("<I", entete[4:8])[0] == 36 + 16000
        assert entete[12:16] == b"fmt "
        taille_fmt, format_audio, canaux, rate, debit, bloc, bits = struct.unpack(
            "<IHHIIHH", entete[16:36])
        assert (taille_fmt, format_audio, canaux, rate, bits) == (16, 1, 1, 8000, 16)
        assert bloc == 2 and debit == 8000 * 2
        assert entete[36:40] == b"data"
        assert struct.unpack("<I", entete[40:44])[0] == 16000

    def test_le_stereo_double_le_debit(self):
        _, _, canaux, _, debit, bloc, _ = struct.unpack(
            "<IHHIIHH", ulaw.entete_wav(32000, canaux=2)[16:36])
        assert (canaux, bloc, debit) == (2, 4, 32000)

    def test_un_fichier_tronque_reste_decrivable(self):
        """Le cas qui justifie tout ce choix : le processus est mort en plein appel.
        On a moins d'octets que prévu, et on doit quand même pouvoir écouter."""
        tronque = ulaw.encoder(_signal())[:1234]
        pcm = ulaw.decoder(tronque)
        assert struct.unpack("<I", ulaw.entete_wav(len(pcm))[40:44])[0] == len(pcm) == 2468


class TestEntrelacement:
    def test_les_deux_voix_vont_dans_deux_oreilles(self):
        gauche = np.array([100, 200, 300], dtype="<i2").tobytes()
        droite = np.array([-100, -200, -300], dtype="<i2").tobytes()
        stereo = np.frombuffer(ulaw.entrelacer(gauche, droite), dtype="<i2")
        assert list(stereo) == [100, -100, 200, -200, 300, -300]

    def test_la_piste_la_plus_courte_est_completee_par_du_silence(self):
        """Les deux pistes n'ont jamais exactement la même longueur : l'assistante
        commence à parler après l'appelant. Sans complément, l'alignement serait faux et
        on entendrait les coupures au mauvais endroit."""
        gauche = np.array([1, 2, 3, 4], dtype="<i2").tobytes()
        droite = np.array([9], dtype="<i2").tobytes()
        stereo = np.frombuffer(ulaw.entrelacer(gauche, droite), dtype="<i2")
        assert list(stereo) == [1, 9, 2, 0, 3, 0, 4, 0]

    def test_deux_pistes_vides(self):
        assert ulaw.entrelacer(b"", b"") == b""
