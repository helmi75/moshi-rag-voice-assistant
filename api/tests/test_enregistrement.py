"""Enregistrement des appels : ce qui doit être IMPOSSIBLE (#88).

Ce module a une règle qui prime sur toutes les autres — **il ne doit jamais faire échouer
un appel**. La majorité des tests ci-dessous ne vérifient donc pas qu'il enregistre bien,
mais qu'il **renonce proprement** : disque plein, dossier non inscriptible, file saturée,
exception inattendue. Dans chaque cas l'appel doit continuer et la raison être notée,
parce que « pas d'enregistrement » sans motif ne se diagnostique pas.
"""
import asyncio
import os

import numpy as np
import pytest

from app.voice import enregistrement, ulaw


@pytest.fixture(autouse=True)
def enregistrement_actif(tmp_path, monkeypatch):
    monkeypatch.setenv("ENREGISTREMENT_APPELS", "1")
    monkeypatch.setenv("RGPD_MENTION", "1")
    monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path / "enregistrements"))
    yield


def _pcm(n: int = 800, valeur: int = 5000) -> bytes:
    return np.full(n, valeur, dtype="<i2").tobytes()


def _lancer(coro):
    return asyncio.run(coro)


class TestInterrupteur:
    def test_actif_par_defaut_non(self, monkeypatch):
        """Le défaut est de NE PAS enregistrer : la fonctionnalité s'allume
        explicitement, elle ne s'impose pas à un déploiement qui ne l'a pas demandée."""
        monkeypatch.delenv("ENREGISTREMENT_APPELS", raising=False)
        assert enregistrement.actif() is False

    def test_couper_la_mention_coupe_l_enregistrement(self, monkeypatch):
        """LE test de ce module. On n'enregistre jamais quelqu'un qui n'a pas été
        informé — et ce n'est pas une politesse d'implémentation : c'est le même
        interrupteur qui pilote ce que l'appelant entend et ce qui touche le disque."""
        monkeypatch.setenv("RGPD_MENTION", "0")
        assert enregistrement.actif() is False

    def test_les_deux_conditions_sont_necessaires(self, monkeypatch):
        monkeypatch.setenv("ENREGISTREMENT_APPELS", "0")
        monkeypatch.setenv("RGPD_MENTION", "1")
        assert enregistrement.actif() is False


class TestChemins:
    def test_deterministe_et_scope_par_etablissement(self):
        p = enregistrement.chemin(7, 42, "appelant")
        assert p.name == "appel42-appelant.ulaw"
        assert p.parent.name == "tenant7"

    @pytest.mark.parametrize("piste", ["", "autre", "../../etc/passwd", "APPELANT"])
    def test_une_piste_inconnue_est_refusee(self, piste):
        """La liste des pistes sert aussi de liste blanche à la route qui sert l'audio :
        si elle laissait passer une chaîne arbitraire, l'admin servirait un chemin choisi
        par le client."""
        with pytest.raises(ValueError):
            enregistrement.chemin(1, 1, piste)

    def test_les_identifiants_sont_forces_en_entiers(self):
        """Un identifiant textuel glissé ici composerait un nom de fichier arbitraire."""
        assert enregistrement.chemin("7", "42", "appelant").name == "appel42-appelant.ulaw"


class TestEnregistrementNominal:
    def test_deux_fichiers_distincts_sont_ecrits(self):
        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=1, call_id=10)
            assert await e.demarrer() is True
            for _ in range(5):
                e.ecrire("appelant", _pcm(valeur=8000))
                e.ecrire("assistante", _pcm(valeur=-8000))
            await e.fermer()
            return e

        e = _lancer(scenario())
        appelant = enregistrement.chemin(1, 10, "appelant")
        assistante = enregistrement.chemin(1, 10, "assistante")
        assert appelant.exists() and assistante.exists()
        # 5 tranches de 800 échantillons → 4000 octets µ-law par piste.
        assert appelant.stat().st_size == 4000
        assert e.etat()["octets"] == 8000
        # Les deux pistes portent bien des signaux différents.
        assert appelant.read_bytes() != assistante.read_bytes()

    def test_le_contenu_est_relisible(self):
        """Ce qui est écrit doit pouvoir être réécouté — c'est tout l'objet du chantier."""
        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=2, call_id=20)
            await e.demarrer()
            e.ecrire("appelant", _pcm(n=1600, valeur=12000))
            await e.fermer()

        _lancer(scenario())
        brut = enregistrement.chemin(2, 20, "appelant").read_bytes()
        pcm = np.frombuffer(ulaw.decoder(brut), dtype="<i2")
        assert pcm.size == 1600
        assert abs(int(np.median(pcm)) - 12000) < 500  # quantification µ-law


class TestIlNeFaitJamaisEchouerUnAppel:
    """Chaque test simule une panne d'infrastructure et exige que l'appel survive."""

    def test_disque_insuffisant_on_renonce_sans_lever(self, monkeypatch):
        monkeypatch.setattr(enregistrement, "espace_libre_mo", lambda *a, **k: 10)
        monkeypatch.setenv("ENREGISTREMENT_DISQUE_MINIMUM_MO", "2000")

        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=1, call_id=11)
            demarre = await e.demarrer()
            e.ecrire("appelant", _pcm())  # ne doit rien faire, ni lever
            await e.fermer()
            return demarre, e.etat()

        demarre, etat = _lancer(scenario())
        assert demarre is False
        assert etat["raison"] == enregistrement.DISQUE
        assert etat["octets"] == 0

    def test_ouverture_impossible_on_renonce_sans_lever(self, monkeypatch):
        def refuse(*a, **k):
            raise PermissionError("dossier en lecture seule")

        monkeypatch.setattr(enregistrement.Enregistreur, "_ouvrir", refuse)

        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=1, call_id=12)
            demarre = await e.demarrer()
            await e.fermer()
            return demarre, e.etat()

        demarre, etat = _lancer(scenario())
        assert demarre is False
        assert etat["raison"] == enregistrement.ECRITURE

    def test_une_ecriture_qui_echoue_arrete_l_enregistrement_pas_l_appel(self, monkeypatch):
        def casse(self, piste, pcm16):
            raise OSError("No space left on device")

        monkeypatch.setattr(enregistrement.Enregistreur, "_ecrire_bloc", casse)

        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=1, call_id=13)
            await e.demarrer()
            e.ecrire("appelant", _pcm())
            await asyncio.sleep(0.05)
            e.ecrire("appelant", _pcm())  # après la panne : toujours pas d'exception
            await e.fermer()
            return e.etat()

        etat = _lancer(scenario())
        assert etat["raison"] == enregistrement.ECRITURE

    def test_sans_identifiant_en_base_on_n_invente_pas_de_nom(self):
        """`start_call` est best-effort : il peut ne pas avoir rendu d'id. On préfère ne
        pas enregistrer plutôt que composer une clé de fichier au hasard."""
        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=1, call_id=None)
            demarre = await e.demarrer()
            e.ecrire("appelant", _pcm())
            await e.fermer()
            return demarre, e.etat()

        demarre, etat = _lancer(scenario())
        assert demarre is False and etat["raison"] == enregistrement.ECRITURE

    def test_file_saturee_les_tranches_perdues_sont_comptees(self):
        """Sous un à-coup disque, on perd des tranches PLUTÔT que de gonfler la mémoire.
        Perdues, mais comptées : un enregistrement troué se reconnaît."""
        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=1, call_id=14)
            await e.demarrer()
            for _ in range(enregistrement.Enregistreur._FILE_MAX + 30):
                e.ecrire("appelant", _pcm())  # sans await : la file ne se vide pas
            await e.fermer()
            return e.etat()

        etat = _lancer(scenario())
        assert etat["chunks_perdus"] > 0

    def test_ecrire_avant_demarrage_ne_leve_pas(self):
        e = enregistrement.Enregistreur(tenant_id=1, call_id=15)
        e.ecrire("appelant", _pcm())  # aucun démarrage : silencieux
        assert e.etat()["octets"] == 0

    def test_fermer_sans_avoir_demarre_ne_leve_pas(self):
        _lancer(enregistrement.Enregistreur(tenant_id=1, call_id=16).fermer())


class TestPlafondDeDuree:
    def test_au_dela_du_plafond_on_cesse_d_ecrire(self, monkeypatch):
        """Une ligne restée ouverte ne doit pas remplir le disque à elle seule."""
        monkeypatch.setenv("ENREGISTREMENT_MAX_SECONDES", "1")  # 8 000 octets par piste

        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=3, call_id=30)
            await e.demarrer()
            for _ in range(60):  # bien au-delà du plafond
                e.ecrire("appelant", _pcm(n=800))
                await asyncio.sleep(0)
            await e.fermer()
            return e.etat()

        etat = _lancer(scenario())
        assert etat["raison"] == enregistrement.DUREE
        assert etat["octets"] <= 1 * 8000 * 2 + 800


class TestSuppression:
    def test_supprime_les_deux_pistes_et_les_compte(self):
        async def scenario():
            e = enregistrement.Enregistreur(tenant_id=4, call_id=40)
            await e.demarrer()
            e.ecrire("appelant", _pcm())
            e.ecrire("assistante", _pcm())
            await e.fermer()

        _lancer(scenario())
        assert enregistrement.supprimer(4, 40) == 2
        assert not enregistrement.chemin(4, 40, "appelant").exists()

    def test_supprimer_ce_qui_n_existe_pas_ne_leve_pas(self):
        """La purge appelle cette fonction sur des appels dont les fichiers ont pu
        disparaître autrement — restauration de sauvegarde, nettoyage manuel."""
        assert enregistrement.supprimer(99, 99) == 0
