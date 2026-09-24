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
        # Le jour du RESTAURANT, pas celui du serveur : entre minuit et deux heures,
        # heure de Paris, `date.today()` (UTC) désigne encore la veille.
        aujourdhui = llm.maintenant().date()
        assert aujourdhui.isoformat() in prompt
        assert llm._date_en_toutes_lettres(aujourdhui) in prompt

    def test_l_heure_du_restaurant(self, monkeypatch):
        """Appel 104 : sans l'heure, elle a proposé treize heures à quinze heures."""
        from datetime import datetime

        monkeypatch.setattr(llm, "maintenant",
                            lambda: datetime(2026, 9, 10, 15, 7, tzinfo=llm.FUSEAU))
        prompt = llm.build_system_prompt(_tenant())
        assert "2026-09-10" in prompt
        assert "15 h 07 au restaurant" in prompt

    def test_l_anglais_est_prevu(self):
        prompt = llm.build_system_prompt(_tenant())
        assert "anglais" in prompt
        # Banc anglais du 10/09/2026 : « Mister Helmi », un SMS de confirmation promis,
        # et « c'est bien ça ? » recopié en français au milieu d'un appel en anglais.
        assert "Mister" in prompt
        assert "promets jamais" in prompt
        assert "TRADUIS" in prompt
        assert "goodbye" in prompt  # et le pipeline sait le reconnaître :
        from app.voice.bot import _FORMULES_DE_CONGE

        assert "goodbye" in _FORMULES_DE_CONGE

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

    def test_l_anglais_des_la_premiere_phrase_mais_pas_sur_deux_mots(self):
        """SCRUM-89, mesuré au banc le 24/09/2026 sur de vrais appels : un appelant qui
        commence en anglais recevait une réponse EN FRANÇAIS 15 à 19 fois sur 20 (appel
        153). L'ancienne règle disait « continue en anglais » : le modèle attendait qu'on
        ait commencé. Mais « réponds dans la langue de la dernière phrase », essayé aussi,
        faisait basculer 17 Français sur 20 au moment de confirmer, parce qu'en bilingue
        « C'est ça » est transcrit « Yes, sir. » (mesuré le 10/09). Les deux moitiés de la
        règle se tiennent : les retirer l'une sans l'autre refait l'une des deux pannes."""
        prompt = " ".join(llm.build_system_prompt(_tenant()).split())
        assert "Réponds en anglais DÈS sa première phrase" in prompt
        assert "même si ton accueil était en français" in prompt
        assert "Deux ou trois mots (« Yes, sir. », « Okay. ») ne changent pas la langue" in prompt

    def test_elle_ne_pretend_jamais_ne_parler_que_francais(self):
        """SCRUM-89, appel 152 : elle ENTENDAIT l'anglais et a répondu « je ne peux
        communiquer qu'en français ». La consigne le dit désormais en toutes lettres, dans le
        prompt ET dans l'outil où elle s'est réfugiée."""
        prompt = " ".join(llm.build_system_prompt(_tenant()).split())
        assert "Ne dis jamais que tu ne parles que français" in prompt
        [outil] = [o for o in llm.TOOLS if o["name"] == "take_message"]
        assert "JAMAIS parce que le client parle anglais" in outil["description"]

    def test_reste_compact(self):
        """Le prompt est renvoyé à chaque tour : au-delà, on paie en latence.

        Porté de 6 000 à 7 000 le 10/09/2026 pour les règles tirées des appels 100 à
        106 — heure du restaurant, épellation, récapitulatif en question, anglais —,
        chacune corrigeant une faute entendue. Environ 200 jetons de plus. Le plafond
        reste là pour que la prochaine règle se paie par une autre qu'on retire."""
        assert len(llm.build_system_prompt(_tenant())) < 7000


class TestAppelantConnu:
    """Le nom du dernier passage est PROPOSÉ, jamais présumé."""

    def test_sans_nom_pas_de_section(self):
        assert "# Appelant" not in llm.build_system_prompt(_tenant())

    def test_le_nom_est_propose_avant_la_prise_de_reservation(self):
        prompt = llm.build_system_prompt(_tenant(), appelant="Durand")
        assert "« Durand »" in prompt
        assert "PROPOSE" in prompt and "Ne le présume" in prompt
        assert prompt.index("# Appelant") < prompt.index("# Réservation")

    def test_un_nom_connu_se_prononce_il_ne_s_epelle_pas(self):
        """La règle d'épellation vise le nom qu'on vient d'entendre épeler, pas celui
        qu'on relit du dernier passage : « Is it H E L M I, like last time? » (appel 140
        du 20/09/2026) n'est pas une question, c'est un bug."""
        prompt = llm.build_system_prompt(_tenant(), appelant="Durand")
        assert "PRONONCE-le" in prompt and "ne l'épelle jamais" in prompt
