"""SQLite ne se lit ni ne s'écrit depuis la boucle d'événements sur le chemin d'appel.

L'admin, la purge et les appels tournent dans le même processus ; Twilio attend une trame
toutes les 20 ms. `busy_timeout` vaut 5 s : un accès qui attend un verrou DEPUIS la boucle
ferait bégayer la voix de tous les appels en cours. On vérifie donc que les accès du chemin
d'appel se font dans un autre fil que celui de la boucle — pas qu'une fonction est appelée."""
import asyncio
import json
import threading
import time
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import db, llm, tenants
from app.main import app

DEMO_NUMBER = "+33100000000"


def _attendre(condition, delai: float = 2.0) -> None:
    fin = time.monotonic() + delai
    while not condition() and time.monotonic() < fin:
        time.sleep(0.01)


def _espion_de_fil(monkeypatch):
    """Note le fil d'exécution de chaque ouverture de connexion SQLite."""
    fils: list[int] = []
    originale = db.get_conn

    def get_conn():
        fils.append(threading.get_ident())
        return originale()

    monkeypatch.setattr(db, "get_conn", get_conn)
    return fils


class TestLeCheminDAppelNeBloquePasLaBoucle:
    def test_un_outil_de_reservation_travaille_dans_un_autre_fil(self, monkeypatch):
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        fils = _espion_de_fil(monkeypatch)
        dans_un_mois = (date.today() + timedelta(days=30)).isoformat()

        async def scenario():
            fil_de_la_boucle = threading.get_ident()
            resultat = await llm.run_tool(tenant, "create_reservation", {
                "customer_name": "Durand", "date": dans_un_mois, "time": "20:00",
                "party_size": 2}, "+33600000001")
            return fil_de_la_boucle, resultat

        fil_de_la_boucle, resultat = asyncio.run(scenario())
        assert json.loads(resultat)["status"] == "confirmed"
        assert fils, "l'outil doit avoir ouvert la base"
        assert all(f != fil_de_la_boucle for f in fils), "SQLite touché depuis la boucle"

    def test_la_recherche_par_numero_aussi(self, monkeypatch):
        tenant = tenants.get_by_phone(DEMO_NUMBER)
        fils = _espion_de_fil(monkeypatch)

        async def scenario():
            fil = threading.get_ident()
            await llm.run_tool(tenant, "find_reservation", {}, "+33600000001")
            return fil

        fil = asyncio.run(scenario())
        assert fils and all(f != fil for f in fils)

    def test_le_decroche_passe_hors_boucle(self, monkeypatch):
        """Sur le WebSocket, la boucle tourne dans le fil du client de test : on vérifie
        ici que le routage et l'ouverture de l'appel passent bien par `db.hors_boucle`."""
        noms: list[str] = []
        originale = db.hors_boucle

        async def espion(fn, *args, **kwargs):
            noms.append(fn.__name__)
            return await originale(fn, *args, **kwargs)

        monkeypatch.setattr(db, "hors_boucle", espion)
        run_bot = AsyncMock()
        client = TestClient(app)
        with patch("app.main._get_bot_runner", return_value=run_bot):
            with client.websocket_connect("/ws/voice") as ws:
                ws.send_text(json.dumps({
                    "event": "start", "sequenceNumber": "1", "streamSid": "MZ-fil",
                    "start": {"accountSid": "AC000", "streamSid": "MZ-fil",
                              "callSid": "CA-fil", "tracks": ["inbound"],
                              "mediaFormat": {"encoding": "audio/x-mulaw",
                                              "sampleRate": 8000, "channels": 1},
                              "customParameters": {"To": DEMO_NUMBER, "CallSid": "CA-fil"}},
                }))
                # Le gestionnaire se suspend le temps des accès en thread : on attend
                # qu'il ait lancé le bot avant de fermer la session, sinon le client de
                # test l'annule en plein milieu.
                _attendre(lambda: run_bot.await_count == 1)
        assert {"get_by_phone", "start_call"} <= set(noms)
        assert run_bot.await_count == 1


class TestPragmas:
    def test_synchronous_suit_la_variable(self, monkeypatch):
        monkeypatch.setenv("DB_SYNCHRONOUS", "NORMAL")
        assert db.get_conn().execute("PRAGMA synchronous").fetchone()[0] == 1
        monkeypatch.setenv("DB_SYNCHRONOUS", "OFF")
        assert db.get_conn().execute("PRAGMA synchronous").fetchone()[0] == 0

    def test_une_valeur_inconnue_retombe_sur_normal(self, monkeypatch):
        """Une faute de frappe dans le .env ne doit ni casser la base ni la rendre moins
        sûre que le défaut."""
        monkeypatch.setenv("DB_SYNCHRONOUS", "TRES_VITE")
        assert db.get_conn().execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_wal_est_pose_par_init_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "wal.db"))
        db.init_db()
        assert db.get_conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
