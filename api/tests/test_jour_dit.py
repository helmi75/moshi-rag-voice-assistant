"""Une table ne se note pas un autre jour que celui que le client a dit.

Vrai appel d'essai du 11/10/2026, un dimanche à 20 h 56 : « lundi à dix-neuf heures » a
donné une table le mardi 13. Le modèle calcule mal les dates quand son raisonnement est
coupé ; le prompt lui donne désormais un calendrier, mais c'est le serveur qui tranche.
Ces tests fixent l'horloge du restaurant à ce dimanche-là.
"""
import asyncio
import json
from datetime import datetime

import pytest

from app import db, llm, reservations, tenants

APPELANT = "+33612345678"
LUNDI, MARDI, LUNDI_SUIVANT = "2026-10-12", "2026-10-13", "2026-10-19"


@pytest.fixture()
def resto(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "jour.db"))
    db.init_db()
    dimanche_soir = datetime(2026, 10, 11, 20, 56, tzinfo=llm.FUSEAU)
    monkeypatch.setattr(llm, "maintenant", lambda: dimanche_soir)
    return tenants.create_tenant("Chez Lundi", "+33199000555")


def _outil(tenant, nom, args):
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, APPELANT)))


def _tables(tenant):
    return reservations.list_reservations(tenant.id)


class TestRienNeSEcritUnAutreJour:
    def test_lundi_demande_mardi_calcule_rien_n_est_enregistre(self, resto):
        reponse = _outil(resto, "create_reservation", {
            "customer_name": "Garnier", "date": MARDI, "weekday": "lundi",
            "time": "19:00", "party_size": 2})
        assert "est un mardi, et `weekday` dit lundi" in reponse["error"]
        assert f"c'est le {LUNDI} :" in reponse["error"]
        assert LUNDI_SUIVANT not in reponse["error"], "une seule date : deux font hésiter"
        assert _tables(resto) == []

    def test_modifier_vers_un_autre_jour_que_celui_dit(self, resto):
        table = reservations.create_reservation(
            tenant_id=resto.id, customer_name="Garnier", date=LUNDI, time="19:00",
            party_size=2, customer_phone=APPELANT)
        reponse = _outil(resto, "modify_reservation", {
            "reservation_id": table["id"], "date": "2026-10-16", "weekday": "jeudi"})
        assert "est un vendredi" in reponse["error"] and "2026-10-15" in reponse["error"]
        assert _tables(resto)[0]["date"] == LUNDI

    @pytest.mark.parametrize("jour_dit", ["lundi prochain", "Lundi soir.", "ce lundi-là"])
    def test_le_jour_est_reconnu_dans_ce_que_le_modele_recopie(self, resto, jour_dit):
        reponse = _outil(resto, "create_reservation", {
            "customer_name": "Garnier", "date": MARDI, "weekday": jour_dit,
            "time": "19:00", "party_size": 2})
        assert "Date refusée" in reponse["error"] and _tables(resto) == []

    @pytest.mark.parametrize("jour", ["9999-12-31", "0001-01-01"])
    def test_une_date_aux_bornes_ne_fait_pas_echouer_l_outil(self, resto, jour):
        reponse = _outil(resto, "check_availability", {
            "date": jour, "weekday": "lundi", "time": "19:00", "party_size": 2})
        assert isinstance(reponse, dict)

    def test_la_meme_date_renvoyee_avec_une_autre_heure_n_est_pas_un_changement_de_jour(self, resto):
        table = reservations.create_reservation(
            tenant_id=resto.id, customer_name="Garnier", date=MARDI, time="19:00",
            party_size=2, customer_phone=APPELANT)
        reponse = _outil(resto, "modify_reservation", {
            "reservation_id": table["id"], "date": "2026-10-13", "time": "20:00",
            "weekday": "lundi"})
        assert reponse["status"] == "modified"

    def test_le_jour_dit_en_anglais(self, resto):
        reponse = _outil(resto, "create_reservation", {
            "customer_name": "Garnier", "date": MARDI, "weekday": "Monday",
            "time": "19:00", "party_size": 2})
        assert "Date refusée" in reponse["error"] and _tables(resto) == []


class TestLaDisponibiliteRegardeLeJourDit:
    def test_elle_repond_pour_le_lundi_et_le_dit(self, resto):
        reponse = _outil(resto, "check_availability", {
            "date": MARDI, "weekday": "lundi", "time": "19:00", "party_size": 2})
        assert reponse["available"] is True
        assert (reponse["date"], reponse["jour"]) == (LUNDI, "lundi 12 octobre 2026")
        assert LUNDI in reponse["consigne_date"] and MARDI in reponse["consigne_date"]

    def test_la_date_retenue_est_la_plus_proche_de_celle_calculee(self, resto):
        """« Lundi en quinze » calculé un jour trop tard reste en quinze."""
        reponse = _outil(resto, "check_availability", {
            "date": "2026-10-27", "weekday": "lundi", "time": "19:00", "party_size": 2})
        assert reponse["date"] == "2026-10-26"

    def test_la_date_retenue_n_est_jamais_passee(self, resto):
        """Dimanche 11 : le samedi le plus proche du lundi 12 est la veille, on prend le suivant."""
        reponse = _outil(resto, "check_availability", {
            "date": LUNDI, "weekday": "samedi", "time": "19:00", "party_size": 2})
        assert reponse["date"] == "2026-10-17"

    def test_le_creneau_juge_est_celui_du_jour_dit(self, resto):
        """Dimanche 20 h 56 : « dimanche dix-neuf heures » calculé au lundi est déjà passé."""
        reponse = _outil(resto, "check_availability", {
            "date": LUNDI, "weekday": "dimanche", "time": "19:00", "party_size": 2})
        assert "déjà passé" in reponse["error"]

    def test_sans_erreur_aucune_consigne(self, resto):
        reponse = _outil(resto, "check_availability", {
            "date": LUNDI, "weekday": "lundi", "time": "19:00", "party_size": 2})
        assert "consigne_date" not in reponse and reponse["jour"] == "lundi 12 octobre 2026"


class TestLeBonJourPasse:
    def test_lundi_demande_lundi_calcule(self, resto):
        reponse = _outil(resto, "create_reservation", {
            "customer_name": "Garnier", "date": LUNDI, "weekday": "Lundi ",
            "time": "19:00", "party_size": 2})
        assert reponse["status"] == "confirmed"
        assert reponse["jour"] == "lundi 12 octobre 2026"
        assert [t["date"] for t in _tables(resto)] == [LUNDI]

    @pytest.mark.parametrize("jour_dit", [None, "", "demain", "le quatorze"])
    def test_sans_jour_de_la_semaine_rien_n_est_refuse(self, resto, jour_dit):
        reponse = _outil(resto, "check_availability", {
            "date": MARDI, "weekday": jour_dit, "time": "19:00", "party_size": 2})
        assert "error" not in reponse

    def test_la_disponibilite_redit_le_jour(self, resto):
        reponse = _outil(resto, "check_availability",
                         {"date": MARDI, "time": "19:00", "party_size": 2})
        assert reponse["jour"] == "mardi 13 octobre 2026"

    def test_changer_l_heure_d_une_table_ne_regarde_pas_le_jour_dit(self, resto):
        table = reservations.create_reservation(
            tenant_id=resto.id, customer_name="Garnier", date=MARDI, time="19:00",
            party_size=2, customer_phone=APPELANT)
        reponse = _outil(resto, "modify_reservation", {
            "reservation_id": table["id"], "time": "20:00", "weekday": "lundi"})
        assert reponse["status"] == "modified" and reponse["jour"] == "mardi 13 octobre 2026"


class TestLePromptDonneLeCalendrier:
    def test_chaque_jour_de_la_semaine_a_sa_date(self, resto):
        prompt = llm.build_system_prompt(resto)
        assert "demain lundi 12 = 2026-10-12, mardi 13 = 2026-10-13," in prompt
        assert "dimanche 18 = 2026-10-18." in prompt
        assert "2026-10-19" not in prompt
        assert "Calcule toi-même" not in prompt

    def test_les_trois_outils_a_date_recoivent_le_jour_dit(self):
        avec = {o["name"] for o in llm.TOOLS if "weekday" in o["input_schema"]["properties"]}
        assert avec == {"check_availability", "create_reservation", "modify_reservation"}
