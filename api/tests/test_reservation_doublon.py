"""Une table, une réservation : pas de seconde création pour corriger la première.

Appel 240, le 05/10/2026. L'assistante entend « Kikato », enregistre la table ; le client
doute, épelle « Kikao » ; l'outil de modification ne savait pas changer un nom, et une
SECONDE réservation a été créée au même créneau. Deux tables pour un client, deux e-mails
« nouvelle réservation » au restaurant, et à vingt heures la salle refusait du monde pour
une table fantôme.

Ce que le serveur garantit maintenant, quel que soit le modèle qui tient la conversation :
même numéro, même jour, même heure = la même table. On la corrige, on ne la double pas.
"""
import asyncio
import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app import db, llm, reservations, tenants

APPELANT = "+33612345678"
AUTRE = "+33699999999"


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "doublon.db")):
        db.init_db()
        yield


@pytest.fixture()
def resto(base):
    return tenants.create_tenant("Chez Doublon", "+33199000444")


@pytest.fixture()
def emails():
    """Ce qui serait parti au restaurant : (évènement, nom du client)."""
    partis: list = []
    with patch.object(llm.notifications, "planifier",
                      side_effect=lambda tenant, evenement, donnees: partis.append(
                          (evenement, donnees["reservation"]["customer_name"]))):
        yield partis


def _demain() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def _outil(tenant, nom, args, numero=APPELANT) -> dict:
    return json.loads(asyncio.run(llm.run_tool(tenant, nom, args, numero)))


def _creer(tenant, nom="Kikato", heure="20:00", couverts=2, numero=APPELANT, jour=None) -> dict:
    return _outil(tenant, "create_reservation",
                  {"customer_name": nom, "date": jour or _demain(), "time": heure,
                   "party_size": couverts}, numero)


def _tables(tenant) -> list[tuple]:
    return [(r["customer_name"], r["time"], r["party_size"])
            for r in reservations.list_reservations(tenant.id) if not r.get("cancelled_at")]


class TestPasDeSecondeReservation:
    def test_une_seconde_creation_au_meme_creneau_est_refusee(self, resto, emails):
        """L'appel 240, rejoué : rien de plus en base, et le refus dit quoi faire."""
        premiere = _creer(resto, "Kikato")
        assert premiere["status"] == "confirmed"
        seconde = _creer(resto, "Kikao")
        assert "error" in seconde
        assert "modify_reservation" in seconde["error"]
        assert str(premiere["reservation_id"]) in seconde["error"] and "Kikato" in seconde["error"]
        assert _tables(resto) == [("Kikato", "20:00", 2)]
        assert emails == [("reservation_creee", "Kikato")]

    @pytest.mark.parametrize("heure", ["9:00", "09:00", "09:00:00"])
    def test_une_heure_ecrite_autrement_reste_la_meme_table(self, resto, heure):
        """Le contrôle du créneau accepte « 9:00 » : comparé tel quel à « 09:00 », le
        doublon passait (revue du 05/10/2026)."""
        apres = (date.today() + timedelta(days=2)).isoformat()
        assert _creer(resto, "Kikato", heure="09:00", jour=apres)["status"] == "confirmed"
        assert "error" in _creer(resto, "Kikao", heure=heure, jour=apres)
        assert len(_tables(resto)) == 1

    def test_le_creneau_est_range_sous_sa_forme_canonique(self, resto):
        apres = date.today() + timedelta(days=2)
        ecrit = f"{apres.year}-{apres.month}-{apres.day}"          # sans zéros
        assert _creer(resto, "Kikato", heure="9:05", jour=ecrit)["status"] == "confirmed"
        (table,) = reservations.list_reservations(resto.id)
        assert (table["date"], table["time"]) == (apres.isoformat(), "09:05")

    def test_meme_avec_un_autre_nombre_de_personnes(self, resto, emails):
        _creer(resto, "Kikato", couverts=2)
        assert "error" in _creer(resto, "Kikato", couverts=4)
        assert _tables(resto) == [("Kikato", "20:00", 2)]

    def test_une_autre_heure_est_une_autre_table(self, resto):
        _creer(resto, "Kikato", heure="20:00")
        assert _creer(resto, "Kikato", heure="12:30")["status"] == "confirmed"
        assert len(_tables(resto)) == 2

    def test_un_autre_jour_est_une_autre_table(self, resto):
        _creer(resto, "Kikato")
        apres = (date.today() + timedelta(days=2)).isoformat()
        assert _creer(resto, "Kikato", jour=apres)["status"] == "confirmed"

    def test_un_autre_client_au_meme_creneau_reserve_normalement(self, resto):
        _creer(resto, "Kikato")
        assert _creer(resto, "Durand", numero=AUTRE)["status"] == "confirmed"
        assert len(_tables(resto)) == 2

    def test_une_table_annulee_ne_bloque_pas_la_suivante(self, resto):
        premiere = _creer(resto, "Kikato")
        _outil(resto, "cancel_reservation", {"reservation_id": premiere["reservation_id"]})
        assert _creer(resto, "Kikao")["status"] == "confirmed"

    def test_un_appel_masque_n_est_pas_concerne(self, resto):
        """Sans numéro, rien ne dit que c'est la même personne : on ne refuse pas."""
        args = {"customer_name": "Kikato", "date": _demain(), "time": "20:00", "party_size": 2,
                "numero_rappel": "06 12 34 56 78"}
        assert _outil(resto, "create_reservation", args, numero=None)["status"] == "confirmed"
        assert _outil(resto, "create_reservation", args, numero=None)["status"] == "confirmed"

    def test_un_autre_etablissement_n_est_pas_concerne(self, resto):
        ailleurs = tenants.create_tenant("Chez Ailleurs", "+33199000445")
        _creer(resto, "Kikato")
        assert _creer(ailleurs, "Kikato")["status"] == "confirmed"


class TestOnCorrigeLaPremiere:
    def test_le_nom_se_corrige_et_le_restaurant_recoit_un_seul_avis(self, resto, emails):
        premiere = _creer(resto, "Kikato")
        corrigee = _outil(resto, "modify_reservation",
                          {"reservation_id": premiere["reservation_id"], "customer_name": "Kikao"})
        assert corrigee["status"] == "modified" and corrigee["customer_name"] == "Kikao"
        assert _tables(resto) == [("Kikao", "20:00", 2)]
        assert emails == [("reservation_creee", "Kikato"), ("reservation_modifiee", "Kikao")]

    def test_on_ne_corrige_pas_le_nom_de_la_table_d_un_autre(self, resto):
        victime = _creer(resto, "Durand", numero=AUTRE)
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": victime["reservation_id"], "customer_name": "Pirate"})
        assert "error" in reponse
        assert _tables(resto) == [("Durand", "20:00", 2)]

    def test_modifier_avec_les_valeurs_deja_enregistrees_ne_previent_personne(self, resto, emails):
        """Relevé le 04/10/2026 : trois e-mails « réservation modifiée » identiques pour un
        même appel, l'outil rappelé sans que rien ne change."""
        premiere = _creer(resto, "Kikato")
        for _ in range(3):
            reponse = _outil(resto, "modify_reservation",
                             {"reservation_id": premiere["reservation_id"], "customer_name": "kikato",
                              "date": _demain(), "time": "20:00", "party_size": 2})
            assert reponse["status"] == "unchanged" and "error" not in reponse
        assert emails == [("reservation_creee", "Kikato")]

    @pytest.mark.parametrize("vide", [" ", "   ", "\t"])
    def test_un_nom_fait_d_espaces_n_efface_pas_le_nom(self, resto, emails, vide):
        """Une table sans nom est introuvable en salle (relevé au banc le 10/09/2026)."""
        premiere = _creer(resto, "Kikato")
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": premiere["reservation_id"], "customer_name": vide})
        assert "error" in reponse
        assert _tables(resto) == [("Kikato", "20:00", 2)]
        assert emails == [("reservation_creee", "Kikato")]

    def test_un_vrai_changement_previent_toujours(self, resto, emails):
        premiere = _creer(resto, "Kikato")
        reponse = _outil(resto, "modify_reservation",
                         {"reservation_id": premiere["reservation_id"], "party_size": 5})
        assert reponse["status"] == "modified" and reponse["party_size"] == 5
        assert emails[-1] == ("reservation_modifiee", "Kikato")


class TestCeQueLeModeleSait:
    def test_l_outil_de_modification_accepte_un_nom(self):
        outil = next(o for o in llm.TOOLS if o["name"] == "modify_reservation")
        assert "customer_name" in outil["input_schema"]["properties"]
        assert outil["input_schema"]["required"] == ["reservation_id"]

    def test_l_outil_dit_qu_il_corrige_un_nom(self):
        """La règle n'est pas dans le prompt, qui a un plafond de taille : elle est dans
        la description de l'outil, et dans le refus du serveur au moment où elle sert."""
        outil = next(o for o in llm.TOOLS if o["name"] == "modify_reservation")
        assert "NOM mal noté" in outil["description"]
        assert "create_reservation" in outil["description"]
