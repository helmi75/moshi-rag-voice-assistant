"""Modification et annulation par téléphone (#33).

Le code métier est simple ; **l'autorisation ne l'est pas**. Un modèle de langage propose
un identifiant de réservation. Il ne l'invente pas aujourd'hui — mais rien ne l'en
empêche, et une réservation annulée par erreur chez quelqu'un d'autre est un incident
qu'aucun log ne rattrape.

Ces tests visent donc d'abord ce qui doit être IMPOSSIBLE.
"""
from datetime import date, timedelta

import json
import pytest
from unittest.mock import patch

from app import db, llm, reservations, tenants


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "modif.db")):
        db.init_db()
        yield


@pytest.fixture()
def resto(base):
    return tenants.create_tenant("Chez Modif", "+33199000333")


APPELANT = "+33612345678"
AUTRE = "+33699999999"


def _demain() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def _resa(tenant_id, *, tel=APPELANT, jour=None, heure="20:00", couverts=2, nom="Dupont"):
    return reservations.create_reservation(
        tenant_id=tenant_id, customer_name=nom, date=jour or _demain(),
        time=heure, party_size=couverts, customer_phone=tel)


async def _outil(tenant, nom, args, numero=APPELANT):
    return json.loads(await llm.run_tool(tenant, nom, args, numero))


class TestCeQuiDoitEtreImpossible:
    @pytest.mark.parametrize("outil", ["modify_reservation", "cancel_reservation"])
    def test_on_ne_touche_pas_a_la_reservation_d_un_autre(self, resto, outil):
        """Le scénario qui compte : l'appelant demande l'annulation en donnant — par
        erreur ou non — l'identifiant de quelqu'un d'autre."""
        import asyncio

        victime = _resa(resto.id, tel=AUTRE)
        reponse = asyncio.run(_outil(resto, outil,
                                     {"reservation_id": victime["id"], "party_size": 8},
                                     numero=APPELANT))
        assert "error" in reponse
        apres = reservations.get_reservation(victime["id"])
        assert apres["cancelled_at"] is None
        assert apres["party_size"] == 2

    def test_on_ne_touche_pas_a_la_reservation_d_un_autre_etablissement(self, resto):
        """Même numéro d'appelant, autre restaurant : le cloisonnement doit tenir."""
        import asyncio

        voisin = tenants.create_tenant("Le Voisin", "+33199000334")
        chez_le_voisin = _resa(voisin.id, tel=APPELANT)
        reponse = asyncio.run(_outil(resto, "cancel_reservation",
                                     {"reservation_id": chez_le_voisin["id"]}))
        assert "error" in reponse
        assert reservations.get_reservation(chez_le_voisin["id"])["cancelled_at"] is None

    @pytest.mark.parametrize("outil", ["find_reservation", "modify_reservation",
                                       "cancel_reservation"])
    def test_un_appel_masque_ne_donne_acces_a_rien(self, resto, outil):
        """Sans numéro, on ne peut pas savoir à qui appartient une réservation. Refuser
        est la seule réponse honnête — et le message dit à l'assistante quoi faire."""
        import asyncio

        mienne = _resa(resto.id)
        reponse = asyncio.run(_outil(resto, outil,
                                     {"reservation_id": mienne["id"]}, numero=None))
        assert "error" in reponse
        assert "masqué" in reponse["error"]
        assert reservations.get_reservation(mienne["id"])["cancelled_at"] is None

    def test_un_identifiant_inexistant_ne_dit_pas_qu_il_est_inexistant(self, resto):
        """Distinguer « n'existe pas » de « pas à vous » confirmerait à un appelant
        qu'une réservation existe à un identifiant donné."""
        import asyncio

        autre = _resa(resto.id, tel=AUTRE)
        inconnu = asyncio.run(_outil(resto, "cancel_reservation", {"reservation_id": 999999}))
        pas_a_moi = asyncio.run(_outil(resto, "cancel_reservation",
                                       {"reservation_id": autre["id"]}))
        assert inconnu["error"] == pas_a_moi["error"]

    def test_find_ne_montre_que_les_siennes(self, resto):
        import asyncio

        _resa(resto.id, tel=APPELANT, nom="Moi")
        _resa(resto.id, tel=AUTRE, nom="Quelqu'un d'autre")
        reponse = asyncio.run(_outil(resto, "find_reservation", {}))
        noms = [r["customer_name"] for r in reponse["reservations"]]
        assert noms == ["Moi"]


class TestCeQuiDoitMarcher:
    def test_annuler_sa_reservation(self, resto):
        import asyncio

        mienne = _resa(resto.id)
        reponse = asyncio.run(_outil(resto, "cancel_reservation",
                                     {"reservation_id": mienne["id"]}))
        assert reponse["status"] == "cancelled"
        assert reservations.get_reservation(mienne["id"])["cancelled_at"] is not None

    def test_modifier_sa_reservation(self, resto):
        import asyncio

        mienne = _resa(resto.id, heure="20:00", couverts=2)
        reponse = asyncio.run(_outil(resto, "modify_reservation",
                                     {"reservation_id": mienne["id"],
                                      "time": "21:30", "party_size": 4}))
        assert reponse["status"] == "modified"
        apres = reservations.get_reservation(mienne["id"])
        assert apres["time"] == "21:30" and apres["party_size"] == 4
        assert apres["date"] == mienne["date"], "un champ non fourni ne doit pas bouger"

    def test_modifier_sans_rien_changer_est_refuse(self, resto):
        """Sinon l'assistante annoncerait « c'est modifié » sans que rien ne l'ait été."""
        import asyncio

        mienne = _resa(resto.id)
        reponse = asyncio.run(_outil(resto, "modify_reservation",
                                     {"reservation_id": mienne["id"]}))
        assert "error" in reponse

    def test_les_passees_ne_sont_pas_proposees(self, resto):
        """On ne modifie pas le dîner d'hier, et le proposer ferait parler l'assistante
        d'une réservation périmée."""
        import asyncio

        hier = (date.today() - timedelta(days=1)).isoformat()
        _resa(resto.id, jour=hier)
        reponse = asyncio.run(_outil(resto, "find_reservation", {}))
        assert reponse["reservations"] == []


class TestUneAnnulationLibereLaTable:
    """Le piège silencieux : une annulée qui compte encore ferait refuser une table
    pourtant libre, sans qu'aucune erreur n'apparaisse nulle part."""

    def test_les_couverts_annules_ne_comptent_plus(self, resto):
        mienne = _resa(resto.id, couverts=6)
        avant = reservations.count_for_slot(resto.id, mienne["date"], "20:00")
        reservations.cancel_reservation(mienne["id"])
        apres = reservations.count_for_slot(resto.id, mienne["date"], "20:00")
        assert avant == 6 and apres == 0

    def test_le_graphique_des_creneaux_les_ignore(self, resto):
        mienne = _resa(resto.id, couverts=6)
        reservations.cancel_reservation(mienne["id"])
        assert reservations.covers_by_slot(resto.id, mienne["date"]) == []

    def test_la_liste_a_venir_les_masque_par_defaut(self, resto):
        """Laisser une annulée dans la liste ferait préparer des couverts pour des
        clients qui ne viendront pas."""
        mienne = _resa(resto.id)
        reservations.cancel_reservation(mienne["id"])
        assert reservations.list_filtered(resto.id) == []
        visibles = reservations.list_filtered(resto.id, inclure_annulees=True)
        assert len(visibles) == 1 and visibles[0]["cancelled_at"] is not None

    def test_annuler_deux_fois_ne_reecrit_pas_l_heure(self, resto):
        """L'heure réelle de l'annulation est ce qui compte en cas de litige."""
        mienne = _resa(resto.id)
        premier = reservations.cancel_reservation(mienne["id"])["cancelled_at"]
        second = reservations.cancel_reservation(mienne["id"])["cancelled_at"]
        assert premier == second


class TestOutillageDuModele:
    def test_les_trois_outils_sont_exposes(self):
        """Un outil implémenté mais non déclaré est invisible pour le modèle : la
        fonctionnalité existerait sans jamais servir."""
        noms = {t["name"] for t in llm.TOOLS}
        assert {"find_reservation", "modify_reservation", "cancel_reservation"} <= noms

    def test_aucun_outil_ne_demande_le_numero_au_modele(self):
        """Le numéro vient du réseau téléphonique. Le laisser passer par le modèle
        reviendrait à accepter que l'appelant se déclare qui il veut.

        Ce test a mordu en étant écrit : `create_reservation` exposait `customer_phone`,
        et seul le pipeline streaming l'écrasait — le mode `gather` laissait passer la
        valeur du modèle."""
        for outil in llm.TOOLS:
            champs = outil["input_schema"]["properties"]
            assert not any("phone" in c or "numero" in c for c in champs), outil["name"]

    def test_la_garde_couvre_tous_les_outils_sensibles(self):
        """Le filet : tout outil qui agit sur une réservation existante doit figurer
        dans OUTILS_APPELANT, sinon il échappe à la vérification du numéro."""
        assert set(llm.OUTILS_APPELANT) == {
            "find_reservation", "modify_reservation", "cancel_reservation"}

    def test_le_prompt_n_interdit_plus_d_annuler(self):
        """Le prompt disait « ne prétends jamais avoir annulé ». Le laisser aurait rendu
        les trois outils inutilisables : le modèle aurait obéi à l'interdiction."""
        tenant = tenants.Tenant(id=1, name="T", business_type="restaurant",
                                phone_number="+33100000002", language="fr-FR",
                                greeting="Bonjour.", knowledge_base="")
        prompt = llm.build_system_prompt(tenant)
        assert "tu ne sais pas encore le faire" not in prompt
        assert "find_reservation" in prompt


class TestLeNumeroVientDuReseau:
    """Le téléphone d'une réservation n'est pas un détail de contact : c'est la clé qui
    autorisera sa modification. Le laisser proposer par le modèle reviendrait à accepter
    que l'appelant décide de qui il est.

    Trouvé en écrivant ces tests : `create_reservation` exposait `customer_phone` au
    modèle, et seul le pipeline streaming l'écrasait — le mode `gather` non.
    """

    def test_le_modele_ne_peut_pas_choisir_le_telephone(self, resto):
        import asyncio

        asyncio.run(_outil(resto, "create_reservation",
                           {"customer_name": "Moi", "date": _demain(),
                            "time": "20:00", "party_size": 2,
                            "customer_phone": AUTRE},   # tentative
                           numero=APPELANT))
        creee = reservations.list_reservations(resto.id)[0]
        assert creee["customer_phone"] == APPELANT

    def test_un_appel_masque_cree_sans_telephone(self, resto):
        """La réservation existe — on ne refuse pas une table à un numéro masqué — mais
        elle ne sera pas modifiable au téléphone, faute de pouvoir identifier qui appelle."""
        import asyncio

        asyncio.run(_outil(resto, "create_reservation",
                           {"customer_name": "Anonyme", "date": _demain(),
                            "time": "20:00", "party_size": 2},
                           numero=None))
        creee = reservations.list_reservations(resto.id)[0]
        assert creee["customer_phone"] is None

    def test_le_telephone_n_est_plus_declare_au_modele(self):
        champs = next(t for t in llm.TOOLS if t["name"] == "create_reservation")
        assert "customer_phone" not in champs["input_schema"]["properties"]
