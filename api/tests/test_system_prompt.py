"""Tests du prompt système de l'assistante téléphonique.

Le prompt est ré-envoyé à chaque tour d'appel : ce qu'on verrouille ici, ce sont les
éléments dont l'absence casse un comportement observé au téléphone (date résolue,
base de connaissances injectée, formats d'outils stricts). Aucun appel réseau.
"""
from datetime import date

from app import llm
from app.tenants import Tenant


def _tenant(**kwargs) -> Tenant:
    defaults = dict(
        id=1,
        name="Chez Marcel",
        business_type="restaurant",
        phone_number="+33100000000",
        language="fr",
        greeting="Bonjour, Chez Marcel.",
        knowledge_base="## Horaires\nDu mardi au samedi, midi et soir.",
    )
    defaults.update(kwargs)
    return Tenant(**defaults)


class TestDateEnToutesLettres:
    def test_jour_de_semaine_et_mois_en_francais(self):
        # 10 août 2026 est un lundi.
        assert llm._date_en_toutes_lettres(date(2026, 8, 10)) == "lundi 10 août 2026"

    def test_dimanche_dernier_index_de_la_table(self):
        # Garde-fou sur le décalage weekday() (0 = lundi, 6 = dimanche).
        assert llm._date_en_toutes_lettres(date(2026, 8, 16)).startswith("dimanche")

    def test_decembre_dernier_index_des_mois(self):
        assert "décembre" in llm._date_en_toutes_lettres(date(2026, 12, 1))


class TestBuildSystemPrompt:
    def test_identite_du_tenant(self):
        prompt = llm.build_system_prompt(_tenant())
        assert "Chez Marcel" in prompt
        assert "restaurant" in prompt

    def test_base_de_connaissances_injectee(self):
        prompt = llm.build_system_prompt(_tenant())
        assert "Du mardi au samedi, midi et soir." in prompt

    def test_base_vide_ne_casse_pas_le_prompt(self):
        prompt = llm.build_system_prompt(_tenant(knowledge_base=""))
        assert "Chez Marcel" in prompt

    def test_date_du_jour_sous_les_deux_formes(self):
        """Le modèle a besoin du jour de la semaine pour résoudre « vendredi
        prochain », et de l'ISO pour remplir les appels d'outils."""
        prompt = llm.build_system_prompt(_tenant())
        aujourdhui = date.today()
        assert aujourdhui.isoformat() in prompt
        assert llm._date_en_toutes_lettres(aujourdhui) in prompt

    def test_formats_stricts_des_outils_rappeles(self):
        prompt = llm.build_system_prompt(_tenant())
        assert "AAAA-MM-JJ" in prompt
        assert "HH:MM" in prompt

    def test_outil_de_reservation_nomme(self):
        """Sans appel à create_reservation, aucune table n'est enregistrée : le
        prompt doit nommer l'outil explicitement."""
        prompt = llm.build_system_prompt(_tenant())
        assert "create_reservation" in prompt
        assert "check_availability" in prompt

    def test_formule_de_conge_reconnue_par_le_pipeline(self):
        """Les formules de congé du prompt doivent faire partie de celles que
        voice/bot.py sait reconnaître pour raccrocher."""
        from app.voice.bot import _FORMULES_DE_CONGE

        prompt = llm.build_system_prompt(_tenant()).lower()
        assert any(formule in prompt for formule in _FORMULES_DE_CONGE)

    def test_reste_compact(self):
        """Le prompt est renvoyé à chaque tour : au-delà, on paie en latence."""
        assert len(llm.build_system_prompt(_tenant())) < 6000
