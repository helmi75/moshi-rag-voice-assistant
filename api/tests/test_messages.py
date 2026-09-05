"""Messages pris au téléphone : la promesse doit être tenue (#32).

Ce module existe à cause d'un défaut trouvé en écoutant de vrais appels le 04/09/2026 :
dans **5 appels sur 10**, l'assistante annonçait « je transmets à l'équipe, ils vous
rappelleront » — et rien n'était enregistré nulle part. Le prompt le lui demandait, aucun
outil ne le faisait, et l'admin classait ces appels dans « info », au même titre qu'une
question d'horaires.

Les tests ci-dessous vérifient donc trois choses, dans cet ordre d'importance :
la promesse laisse une trace, le restaurateur la VOIT, et la trace tombe sous le RGPD
comme toute donnée personnelle.
"""
import json
from unittest.mock import patch

import pytest

from app import db, llm, messages, rgpd, tenants


@pytest.fixture()
def base(tmp_path):
    """Base neuve par test : la purge et l'effacement écrivent, ils ne doivent pas se
    marcher dessus d'un test à l'autre."""
    with patch.object(db, "DB_PATH", str(tmp_path / "messages.db")):
        db.init_db()
        yield


@pytest.fixture()
def etablissement(base):
    return tenants.create_tenant(
        "Le Cubano", "+33199000111",
        knowledge_base="## Horaires\nDu mardi au dimanche, le soir.",
    )


class TestLaPromesseLaisseUneTrace:
    def test_un_message_est_enregistre(self, etablissement):
        identifiant = messages.create_message(
            tenant_id=etablissement.id,
            subject="Candidature plongeur",
            details="Monsieur Kanakou cherche un poste en cuisine.",
            caller_number="+33612345678",
        )
        assert identifiant is not None
        message = messages.get_message(identifiant)
        assert message["subject"] == "Candidature plongeur"
        assert message["caller_number"] == "+33612345678"
        assert message["handled_at"] is None  # personne ne l'a encore traité

    def test_un_sujet_vide_n_ecrit_rien(self, etablissement):
        """Une ligne vide dans la liste coûte plus d'attention au restaurateur qu'elle
        ne lui en fait gagner."""
        assert messages.create_message(etablissement.id, subject="   ") is None
        assert messages.count_pending(etablissement.id) == 0

    def test_le_compteur_de_rappels_dus(self, etablissement):
        for sujet in ("Candidature", "Groupe de 20", "Réclamation"):
            messages.create_message(etablissement.id, subject=sujet)
        assert messages.count_pending(etablissement.id) == 3

    def test_marquer_traite_est_idempotent(self, etablissement):
        i = messages.create_message(etablissement.id, subject="Rappel")
        assert messages.marquer_traite(i, etablissement.id) is True
        assert messages.marquer_traite(i, etablissement.id) is False
        assert messages.count_pending(etablissement.id) == 0

    def test_un_autre_etablissement_ne_peut_pas_traiter_le_message(self, etablissement):
        """Le tenant est dans la clause WHERE, pas seulement vérifié avant : un
        identifiant deviné ne suffit pas."""
        autre = tenants.create_tenant("Autre", "+33199000222")
        i = messages.create_message(etablissement.id, subject="Rappel")
        assert messages.marquer_traite(i, autre.id) is False
        assert messages.get_message(i)["handled_at"] is None


class TestLOutilDuModele:
    """`take_message` est le seul outil qui n'exige pas de numéro : un appel masqué doit
    pouvoir laisser un message, c'est même le cas où il en a le plus besoin."""

    def _appeler(self, tenant, **entree):
        import asyncio
        return json.loads(asyncio.run(
            llm.run_tool(tenant, "take_message", entree,
                         caller_number=entree.pop("_numero", None),
                         call_id=entree.pop("_appel", None))))

    def test_le_modele_enregistre_un_message(self, etablissement):
        sortie = self._appeler(
            etablissement, subject="Demande à parler à Bertrand",
            details="Il travaille en cuisine.", _numero="+33612345678")
        assert sortie["status"] == "recorded"
        assert sortie["rappel_possible"] is True
        assert messages.count_pending(etablissement.id) == 1

    def test_un_appel_masque_laisse_quand_meme_un_message(self, etablissement):
        """Sans numéro, l'appelant ne peut ni réserver ni retrouver quoi que ce soit :
        lui refuser le message le laisserait sans aucun recours."""
        sortie = self._appeler(etablissement, subject="Réclamation", _numero=None)
        assert sortie["status"] == "recorded"
        # Et le restaurateur voit qu'il n'y a pas de quoi rappeler.
        assert sortie["rappel_possible"] is False

    def test_sans_objet_l_outil_refuse_sans_faire_echouer_l_appel(self, etablissement):
        sortie = self._appeler(etablissement, subject="")
        assert "error" in sortie  # le modèle lit le message et redemande
        assert messages.count_pending(etablissement.id) == 0

    def test_le_numero_vient_du_reseau_pas_du_modele(self, etablissement):
        """Même règle que pour les réservations : l'appelant ne décide pas de qui il
        est. Un numéro proposé dans les arguments est ignoré."""
        self._appeler(etablissement, subject="Rappel",
                      customer_phone="+33600000000",  # tentative du modèle
                      _numero="+33612345678")
        assert messages.list_messages(etablissement.id)[0]["caller_number"] == "+33612345678"

    def test_l_outil_est_bien_declare_au_modele(self):
        """Un outil absent du schéma n'existe pas pour le modèle, quoi qu'en dise le
        prompt — c'était exactement la panne d'origine."""
        assert any(t["name"] == "take_message" for t in llm.TOOLS)

    def test_le_prompt_exige_d_appeler_l_outil(self):
        """Le prompt disait « prends le message » sans nommer d'outil : le modèle
        annonçait le rappel et n'appelait rien."""
        from app.tenants import Tenant

        prompt = llm.build_system_prompt(Tenant(
            id=1, name="Chez Marcel", business_type="restaurant",
            phone_number="+33100000000", language="fr", greeting="Bonjour.",
            knowledge_base="x"))
        assert "take_message" in prompt


class TestRGPD:
    """Un nouveau stock de données personnelles qui échapperait à la purge rendrait le
    registre faux — c'est la faute que ce projet a déjà failli commettre avec l'audio."""

    def test_la_purge_efface_le_contenu_des_vieux_messages(self, etablissement, monkeypatch):
        from app import db

        i = messages.create_message(
            etablissement.id, subject="Candidature", details="Nom, parcours, souhaits",
            caller_number="+33612345678")
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE messages SET created_at = datetime('now', '-400 days') WHERE id = ?",
                (i,))
        resultat = rgpd.purger()
        assert resultat.messages >= 1
        message = messages.get_message(i)
        assert message["details"] is None
        assert message["caller_number"] is None
        assert "effacé" in message["subject"]

    def test_un_message_recent_survit_a_la_purge(self, etablissement):
        i = messages.create_message(etablissement.id, subject="Groupe", details="20 pers.")
        rgpd.purger()
        assert messages.get_message(i)["details"] == "20 pers."

    def test_le_droit_a_l_effacement_supprime_les_messages(self, etablissement):
        """Supprimés et non anonymisés : contrairement à un appel, un message ne porte
        aucune valeur comptable. Vidé de son objet, il ne resterait qu'une ligne que le
        restaurateur ne peut ni traiter ni comprendre."""
        messages.create_message(etablissement.id, subject="Rappel",
                                caller_number="+33612345678")
        resultat = rgpd.effacer_appelant("+33612345678")
        assert resultat.messages == 1
        assert messages.count_pending(etablissement.id) == 0

    def test_l_effacement_ne_touche_pas_les_autres_numeros(self, etablissement):
        messages.create_message(etablissement.id, subject="A", caller_number="+33611111111")
        messages.create_message(etablissement.id, subject="B", caller_number="+33622222222")
        rgpd.effacer_appelant("+33611111111")
        restants = [m["subject"] for m in messages.list_messages(etablissement.id)]
        assert restants == ["B"]
