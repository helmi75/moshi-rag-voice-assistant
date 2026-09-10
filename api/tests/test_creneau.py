"""Un créneau déjà passé ne se réserve pas.

Relevé le 10/09/2026 sur un vrai appel (104) : l'assistante a proposé une table
« aujourd'hui à treize heures » alors qu'il était quinze heures. Le prompt lui donne
désormais l'heure, mais c'est le serveur qui tranche : ces tests fixent l'horloge du
restaurant à 15 h 00 et vérifient que rien de passé n'entre en base.
"""
import asyncio
import json
from datetime import date, datetime, time, timedelta

import pytest

from app import db, llm, reservations, tenants

APPELANT = "+33612345678"
AUJOURDHUI = date.today().isoformat()
DEMAIN = (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture()
def resto(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "creneau.db"))
    db.init_db()
    quinze_heures = datetime.combine(date.today(), time(15, 0), tzinfo=llm.FUSEAU)
    monkeypatch.setattr(llm, "maintenant", lambda: quinze_heures)
    return tenants.create_tenant("Chez Créneau", "+33199000444")


def _outil(tenant, nom, args):
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, APPELANT)))


def _resa(tenant, jour, heure="20:00"):
    return reservations.create_reservation(
        tenant_id=tenant.id, customer_name="Dupont", date=jour, time=heure,
        party_size=2, customer_phone=APPELANT)


class TestLePasseEstRefuse:
    def test_treize_heures_quand_il_est_quinze_heures(self, resto):
        reponse = _outil(resto, "check_availability",
                         {"date": AUJOURDHUI, "time": "13:00", "party_size": 2})
        assert "déjà passé" in reponse["error"]
        assert "15:00" in reponse["error"], "le modèle doit savoir quelle heure il est"

    def test_rien_n_est_enregistre(self, resto):
        reponse = _outil(resto, "create_reservation",
                         {"customer_name": "Bertrand", "date": AUJOURDHUI,
                          "time": "13:00", "party_size": 2})
        assert "error" in reponse
        assert reservations.list_reservations(resto.id) == []

    def test_modifier_vers_le_passe(self, resto):
        mienne = _resa(resto, DEMAIN)
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": mienne["id"], "date": AUJOURDHUI, "time": "13:00"})
        assert "déjà passé" in reponse["error"]
        assert reservations.get_reservation(mienne["id"])["date"] == DEMAIN

    def test_changer_seulement_l_heure_compte_la_date_existante(self, resto):
        """Sans la date de la réservation, « 13:00 » seul ne pourrait pas être jugé."""
        mienne = _resa(resto, AUJOURDHUI, "20:00")
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": mienne["id"], "time": "13:00"})
        assert "déjà passé" in reponse["error"]
        assert reservations.get_reservation(mienne["id"])["time"] == "20:00"

    def test_date_ou_heure_illisible(self, resto):
        reponse = _outil(resto, "check_availability",
                         {"date": "samedi", "time": "20h", "party_size": 2})
        assert "illisible" in reponse["error"]


class TestLeNomEstObligatoire:
    """Relevé au banc le 10/09/2026 : « …au nom de. C'est bien ça ? », « Très bien
    merci », et une table enregistrée sans nom — introuvable pour l'équipe en salle."""

    @pytest.mark.parametrize("nom", ["", "   ", None])
    def test_une_reservation_sans_nom_est_refusee(self, resto, nom):
        reponse = _outil(resto, "create_reservation",
                         {"customer_name": nom, "date": DEMAIN, "time": "20:00",
                          "party_size": 4})
        assert "Nom manquant" in reponse["error"]
        assert reservations.list_reservations(resto.id) == []


class TestLAvenirPasse:
    def test_plus_tard_le_meme_jour(self, resto):
        assert _outil(resto, "check_availability",
                      {"date": AUJOURDHUI, "time": "20:00", "party_size": 2})["available"]
        reponse = _outil(resto, "create_reservation",
                         {"customer_name": "Paul", "date": AUJOURDHUI,
                          "time": "20:00", "party_size": 4})
        assert reponse["status"] == "confirmed"

    def test_modifier_le_nombre_seulement(self, resto):
        mienne = _resa(resto, DEMAIN)
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": mienne["id"], "party_size": 4})
        assert reponse["status"] == "modified"
