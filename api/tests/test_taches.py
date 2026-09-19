"""Le registre des tâches de fond (app/taches.py) : retenues, journalisées, arrêtables.

Le motif d'origine — `asyncio.create_task(...)` sans garder la tâche — laissait deux trous
que rien ne signalait : une tâche ramassable avant sa fin, et une exception perdue. Ces
tests vérifient les deux réparations, pas la bibliothèque asyncio."""
import asyncio

import pytest
from loguru import logger

from app import taches


def _journal_capture():
    lignes: list[str] = []
    identifiant = logger.add(lambda message: lignes.append(str(message)), level="WARNING")
    return lignes, identifiant


class TestRegistre:
    def test_une_exception_est_journalisee_avec_le_nom(self):
        lignes, identifiant = _journal_capture()

        async def explose():
            raise RuntimeError("le disque a disparu")

        async def scenario():
            tache = taches.lancer(explose(), nom="rendu de l'accueil")
            await asyncio.sleep(0.01)  # laisse la tâche mourir et le rappel de fin passer
            return tache

        try:
            tache = asyncio.run(scenario())
        finally:
            logger.remove(identifiant)
        assert tache.done()
        assert any("rendu de l'accueil" in l and "RuntimeError" in l and "disparu" in l
                   for l in lignes), lignes

    def test_la_tache_est_retenue_puis_liberee(self):
        async def scenario():
            depart = asyncio.Event()

            async def attend():
                await depart.wait()

            avant = taches.en_cours()
            tache = taches.lancer(attend(), nom="attente")
            assert taches.en_cours() == avant + 1  # retenue : pas ramassable
            depart.set()
            await tache
            await asyncio.sleep(0)  # le rappel de fin passe juste après le réveil
            assert taches.en_cours() == avant  # libérée : pas de fuite

        asyncio.run(scenario())

    def test_une_annulation_n_est_pas_une_erreur(self):
        lignes, identifiant = _journal_capture()

        async def scenario():
            tache = taches.lancer(asyncio.sleep(60), nom="longue")
            await asyncio.sleep(0)
            tache.cancel()
            await asyncio.sleep(0.01)

        try:
            asyncio.run(scenario())
        finally:
            logger.remove(identifiant)
        assert not lignes

    def test_arreter_tout_annule_et_attend(self):
        async def scenario():
            tache = taches.lancer(asyncio.sleep(60), nom="boucle infinie")
            await asyncio.sleep(0)
            await taches.arreter_tout()
            return tache

        tache = asyncio.run(scenario())
        assert tache.cancelled()
        assert taches.en_cours() == 0

    def test_hors_boucle_leve_clairement(self):
        """Hors boucle, rien ne s'exécuterait jamais : autant le dire tout de suite — et
        refermer la coroutine, sinon Python avertit qu'elle n'a jamais été attendue."""
        with pytest.raises(RuntimeError, match="hors d'une boucle"):
            taches.lancer(asyncio.sleep(0), nom="orpheline")


class TestCycleDeVie:
    """Le démarrage et l'arrêt passent par le `lifespan` de l'application : quitter le
    TestClient doit arrêter TOUTES les boucles de fond, même celles qui tournent sans fin."""

    def test_quitter_l_application_arrete_les_taches_de_fond(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app

        monkeypatch.setenv("SUPERVISION_TWILIO_SECONDES", "900")
        monkeypatch.setenv("RETENTION_INTERVALLE_SECONDES", "3600")
        with TestClient(app):
            noms = {t.get_name() for t in taches.retenues()}
            assert {"relève des alertes Twilio", "purge des données personnelles"} <= noms
        assert taches.en_cours() == 0

    def test_plus_aucun_on_event(self):
        """Les `on_event` sont dépréciés, et leur ordre dépendait de l'ordre d'écriture."""
        from app.main import app

        assert not app.router.on_startup and not app.router.on_shutdown
