"""Forfait et consommation mensuelle (#31, ASSISTANTE-120).

Le forfait est ce qui protège la marge. Trois propriétés comptent plus que les autres,
et chacune a son test :

1. **il ne coupe jamais la ligne** — décision produit, pas oubli d'implémentation ;
2. **il compte le mois calendaire** — la maille de facturation, sinon le restaurateur
   ne peut pas relire sa facture ;
3. **il compte des minutes, à la seconde** — ce que la grille vend depuis le 02/10/2026.
"""
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import db, plans, quotas, tenants


@pytest.fixture()
def base(tmp_path):
    with patch.object(db, "DB_PATH", str(tmp_path / "quotas.db")):
        db.init_db()
        yield


@pytest.fixture()
def resto(base):
    return tenants.create_tenant("Chez Quota", "+33199000777")


def _appels(tenant_id: int, combien: int, *, secondes=60.0, mois_decale: int = 0,
            prefixe: str = "Q", panne: bool = False) -> None:
    """Écrit `combien` appels clos de `secondes` chacun (une minute par défaut, pour que
    les comptes se lisent). `mois_decale=1` les place le mois précédent ; `secondes=None`
    laisse l'appel en cours ; `panne` en fait un appel rendu par le secours."""
    date = datetime.now(timezone.utc).replace(day=15, hour=12)
    if mois_decale:
        date = (date.replace(day=1) - timedelta(days=1)).replace(day=15, hour=12)
    horodatage = date.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_conn() as conn:
        for i in range(combien):
            conn.execute(
                "INSERT INTO calls (call_sid, tenant_id, started_at, duration_seconds, "
                "secours_motif) VALUES (?, ?, ?, ?, ?)",
                (f"{prefixe}{mois_decale}-{secondes}-{i}", tenant_id, horodatage, secondes,
                 "voix" if panne else None),
            )


class TestComptage:
    def test_aucun_appel(self, resto):
        c = quotas.etat(resto)
        assert c.appels == 0 and c.minutes == 0 and c.hors_forfait == 0
        assert c.niveau == quotas.OK

    def test_le_decompte_est_a_la_seconde(self, resto):
        """Trois appels de quarante secondes font deux minutes, pas trois minutes
        entamées : c'est ce que la page d'accueil promet."""
        _appels(resto.id, 3, secondes=40.0)
        c = quotas.etat(resto)
        assert c.appels == 3
        assert c.minutes == pytest.approx(2.0)

    def test_seul_le_mois_en_cours_compte(self, resto):
        """La facture porte sur un mois. Compter en fenêtre glissante donnerait un
        plafond que le restaurateur ne peut pas relire sur sa facture."""
        _appels(resto.id, 5)
        _appels(resto.id, 40, mois_decale=1)
        assert quotas.etat(resto).minutes == pytest.approx(5.0)

    def test_les_appels_des_autres_ne_comptent_pas(self, resto):
        autre = tenants.create_tenant("Voisin", "+33199000778")
        _appels(autre.id, 30, prefixe="V")
        _appels(resto.id, 3)
        assert quotas.etat(resto).minutes == pytest.approx(3.0)
        assert quotas.etat(autre).minutes == pytest.approx(30.0)

    def test_un_appel_sans_reservation_compte_quand_meme(self, resto):
        """Il consomme la même voix et la même transcription. Ne facturer que les appels
        aboutis reviendrait à offrir les autres — qui coûtent le même prix."""
        _appels(resto.id, 4)
        with db.get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM calls WHERE reservation_id IS NOT NULL"
            ).fetchone()[0] == 0
        assert quotas.etat(resto).minutes == pytest.approx(4.0)

    def test_un_appel_en_cours_n_a_pas_encore_de_minutes(self, resto):
        _appels(resto.id, 1, secondes=None)
        c = quotas.etat(resto)
        assert c.appels == 1 and c.minutes == 0

    def test_un_appel_du_banc_d_essai_n_est_pas_decompte(self, resto):
        """Le banc est le nôtre, et sa connexion peut rester ouverte des heures : compté
        à la minute, un seul essai dépasserait le forfait d'un client."""
        from app import calls

        _appels(resto.id, 2)
        _appels(resto.id, 1, secondes=7 * 3600.0, prefixe=calls.PREFIXE_BANC)
        c = quotas.etat(resto)
        assert c.appels == 2 and c.minutes == pytest.approx(2.0)
        du_parc = quotas.etat_par_tenant([resto])[resto.id]
        assert du_parc.appels == 2 and du_parc.minutes == pytest.approx(2.0)

    def test_un_appel_sans_identifiant_compte(self, resto):
        """Un filtre sur l'identifiant ne doit pas faire disparaître les appels qui n'en
        ont pas : en SQL, NULL ne ressemble à rien, et ne « diffère » de rien non plus."""
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO calls (call_sid, tenant_id, started_at, duration_seconds) "
                "VALUES (NULL, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 120)", (resto.id,))
        assert quotas.etat(resto).minutes == pytest.approx(2.0)

    def test_un_appel_rendu_pendant_une_panne_n_est_pas_decompte(self, resto):
        """L'assistante n'a pas servi l'appel : sa durée est celle que le restaurant a
        passée à son propre téléphone. La compter ferait payer notre panne au client."""
        _appels(resto.id, 2)
        _appels(resto.id, 1, secondes=600.0, panne=True, prefixe="P")
        c = quotas.etat(resto)
        assert c.appels == 2 and c.minutes == pytest.approx(2.0)
        du_parc = quotas.etat_par_tenant([resto])[resto.id]
        assert du_parc.appels == 2 and du_parc.minutes == pytest.approx(2.0)


class TestSeuils:
    def _sous_formule(self, resto, minutes, formule="essentiel"):
        tenants.update_tenant(resto.id, plan=formule)
        _appels(resto.id, 1, secondes=minutes * 60.0)
        return quotas.etat(tenants.get_by_id(resto.id))

    def test_sous_le_seuil_rien_ne_bouge(self, resto):
        assert self._sous_formule(resto, 50).niveau == quotas.OK

    def test_a_80_pour_cent_on_previent(self, resto):
        """Prévenir APRÈS le dépassement ne prévient de rien."""
        inclus = plans.get("essentiel").minutes_incluses
        c = self._sous_formule(resto, int(inclus * plans.SEUIL_ALERTE))
        assert c.niveau == quotas.ALERTE
        assert c.hors_forfait == 0 and c.montant_eur == 0.0

    def test_au_dela_on_facture(self, resto):
        essentiel = plans.get("essentiel")
        c = self._sous_formule(resto, essentiel.minutes_incluses + 7)
        assert c.niveau == quotas.DEPASSEMENT
        assert c.hors_forfait == pytest.approx(7.0)
        assert c.montant_eur == round(7 * essentiel.minute_supp_eur, 2)

    def test_la_minute_en_plus_est_celle_de_la_formule(self, resto):
        """Chaque formule a son tarif : facturer celui d'Essentiel à un client Service
        lui ferait payer la minute 40 % trop cher."""
        service = plans.get("service")
        assert service.minute_supp_eur != plans.get("essentiel").minute_supp_eur
        c = self._sous_formule(resto, service.minutes_incluses + 100, formule="service")
        assert c.montant_eur == round(100 * service.minute_supp_eur, 2)

    def test_pile_au_plafond_n_est_pas_un_depassement(self, resto):
        """La dernière minute du forfait est incluse, pas la première facturée."""
        inclus = plans.get("essentiel").minutes_incluses
        c = self._sous_formule(resto, inclus)
        assert c.hors_forfait == 0 and c.montant_eur == 0.0

    def test_vingt_secondes_au_dela_se_facturent_vingt_secondes(self, resto):
        essentiel = plans.get("essentiel")
        c = self._sous_formule(resto, essentiel.minutes_incluses + 1 / 3)
        assert c.niveau == quotas.DEPASSEMENT
        assert c.montant_eur == round(essentiel.minute_supp_eur / 3, 2)
        assert c.hors_forfait_affichable == 1  # jamais « 0 minute » à côté d'un montant

    def test_la_barre_ne_deborde_pas_mais_le_chiffre_reste_exact(self, resto):
        inclus = plans.get("essentiel").minutes_incluses
        c = self._sous_formule(resto, inclus * 3)
        assert c.part_affichable == 100      # la barre ne dépasse pas sa largeur
        assert c.part == pytest.approx(3.0)  # …mais l'ampleur reste lisible


class TestSansForfait:
    """La formule Liberté n'inclut rien : chaque minute est facturée, et il n'y a pas de
    plafond à dépasser."""

    def _en_liberte(self, resto, minutes):
        tenants.update_tenant(resto.id, plan="liberte")
        _appels(resto.id, 1, secondes=minutes * 60.0)
        return tenants.get_by_id(resto.id)

    def test_chaque_minute_est_facturee(self, resto):
        liberte = plans.get("liberte")
        c = quotas.etat(self._en_liberte(resto, 42))
        assert c.hors_forfait == pytest.approx(42.0)
        assert c.montant_eur == round(42 * liberte.minute_supp_eur, 2)

    def test_ni_plafond_ni_alerte(self, resto):
        """« Plafond dépassé » ferait peur à un client qui paie ce qu'il a choisi."""
        tenant = self._en_liberte(resto, 900)
        c = quotas.etat(tenant)
        assert c.niveau == quotas.OK and c.part == 0.0 and c.part_affichable == 0
        assert quotas.alertes([tenant]) == []


class TestLaLigneNEstJamaisCoupee:
    """Décision produit du 30/08/2026, confirmée par Helmi. Un restaurant qui perd ses
    réservations un samedi soir résilie le lundi ; le dépassement facturé rapporte plus
    que l'appel refusé n'économise."""

    def test_aucune_fonction_ne_sait_refuser_un_appel(self):
        """La tentation ne doit pas exister dans l'API du module : pas de `bloque`,
        pas d'`autorise`. Si quelqu'un ajoute un jour un tel verrou, ce test le voit."""
        interdits = [n for n in dir(quotas)
                     if any(mot in n.lower()
                            for mot in ("bloque", "refus", "autorise", "interdit", "limite"))]
        assert not interdits, f"le module expose de quoi couper la ligne : {interdits}"

    def test_le_depassement_reste_un_montant_pas_un_verdict(self, resto):
        tenants.update_tenant(resto.id, plan="essentiel")
        _appels(resto.id, 100, secondes=600.0)
        c = quotas.etat(tenants.get_by_id(resto.id))
        assert c.montant_eur > 0
        assert c.niveau == quotas.DEPASSEMENT  # signalé…
        assert c.appels == 100                 # …et tous les appels ont bien été servis


class TestAlertes:
    def test_rien_a_signaler_sous_le_seuil(self, resto):
        _appels(resto.id, 10)
        assert quotas.alertes([resto]) == []

    def test_le_depassement_chiffre_ce_qui_sera_facture(self, resto):
        essentiel = plans.get("essentiel")
        tenants.update_tenant(resto.id, plan="essentiel")
        _appels(resto.id, 1, secondes=(essentiel.minutes_incluses + 20) * 60.0)
        alerte = quotas.alertes([tenants.get_by_id(resto.id)])[0]
        assert "20 minute" in alerte["title"]
        assert f"{round(20 * essentiel.minute_supp_eur, 2):.2f}".replace(".", ",") in alerte["detail"]
        assert "la minute" in alerte["detail"]
        assert "pas été coupée" in alerte["detail"]

    def test_l_alerte_annonce_le_tarif_de_la_minute_en_plus(self, resto):
        essentiel = plans.get("essentiel")
        tenants.update_tenant(resto.id, plan="essentiel")
        _appels(resto.id, 1, secondes=essentiel.minutes_incluses * 0.9 * 60.0)
        alerte = quotas.alertes([tenants.get_by_id(resto.id)])[0]
        assert "90 % du forfait" in alerte["title"]
        assert f"{essentiel.minute_supp_eur:.2f} €".replace(".", ",") in alerte["detail"]


class TestFormuleAppliquee:
    def test_sans_formule_le_defaut_s_applique(self, resto):
        """Le parc existant n'a pas de formule attribuée : refuser de servir faute de
        formule serait une panne créée par la facturation."""
        assert quotas.etat(resto).formule.id == plans.DEFAUT

    def test_une_formule_inventee_ne_donne_pas_son_plafond(self, resto):
        """Base éditée à la main, formulaire forgé : un plafond doit toujours venir
        d'une formule réellement vendue."""
        tenants.update_tenant(resto.id, plan="illimite-gratuit")
        assert quotas.etat(tenants.get_by_id(resto.id)).formule.id == plans.DEFAUT

    def test_les_formules_multi_etablissements_sont_signalees(self, resto):
        """Rien ne regroupe cinq établissements sous un même client : appliquer Maison
        par établissement accorderait cinq fois le forfait vendu. Le fait est porté,
        pas dissimulé."""
        tenants.update_tenant(resto.id, plan="maison")
        c = quotas.etat(tenants.get_by_id(resto.id))
        assert c.groupement_manquant is True
        for f in plans.catalogue():
            if f.etablissements_inclus == 1:
                tenants.update_tenant(resto.id, plan=f.id)
                assert quotas.etat(tenants.get_by_id(resto.id)).groupement_manquant is False


class TestParcEntier:
    def test_une_seule_requete_pour_tout_le_parc(self, base):
        """N établissements ne doivent pas faire N requêtes : la vue du parc les affiche
        tous à chaque chargement."""
        liste = [tenants.create_tenant(f"R{i}", f"+3319900{i:04d}") for i in range(4)]
        for i, t in enumerate(liste):
            _appels(t.id, i * 3, prefixe=f"P{i}")
        with patch.object(db, "get_conn", wraps=db.get_conn) as espion:
            etats = quotas.etat_par_tenant(liste)
        assert espion.call_count == 1
        assert [etats[t.id].appels for t in liste] == [0, 3, 6, 9]
        assert [etats[t.id].minutes for t in liste] == pytest.approx([0, 3, 6, 9])


class TestLaFormuleNeSeChoisitPasSoiMeme:
    """La formule décide du plafond ET de la facture. Un restaurateur qui pourrait se
    l'attribuer choisirait son propre tarif — c'est une élévation de privilège qui ne
    ressemble pas à une faille de sécurité, et qui coûte de l'argent.

    Le champ voyage dans le MÊME formulaire que ceux qu'un restaurateur a le droit de
    modifier (nom, accueil, base de connaissances) : il ne suffit donc pas de cacher le
    `<select>` dans le gabarit, il faut que la route refuse le champ.
    """

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app)

    @staticmethod
    def _csrf(client):
        import base64
        import json as _json

        cookie = client.cookies.get("session")
        charge = cookie.split(".")[0]
        charge += "=" * (-len(charge) % 4)
        return _json.loads(base64.urlsafe_b64decode(charge))["csrf"]

    def test_un_restaurateur_ne_peut_pas_changer_sa_formule(self, client):
        from app import users

        tenant = tenants.create_tenant("Chez Malin", "+33199000999")
        tenants.update_tenant(tenant.id, plan="essentiel")
        user = users.create_user(f"malin-{tenant.id}@test.fr", "resto-pass-quota",
                                 users.ROLE_RESTAURATEUR, tenant.id)
        try:
            assert client.post("/admin/login", data={"email": user.email,
                                                     "password": "resto-pass-quota"},
                               follow_redirects=False).status_code == 303
            reponse = client.post(
                f"/admin/tenants/{tenant.id}",
                data={"name": "Chez Malin", "business_type": "restaurant",
                      "language": "fr-FR", "greeting": "", "knowledge_base": "",
                      "plan": "maison"},
                headers={"X-CSRF-Token": self._csrf(client)},
                follow_redirects=False)
            assert reponse.status_code == 303  # la requête aboutit…
            # …mais le champ réservé est ignoré : cinq fois le forfait n'a pas été volé.
            assert tenants.get_by_id(tenant.id).plan == "essentiel"
        finally:
            tenants.delete_tenant(tenant.id)

    def test_le_super_admin_le_peut(self, client):
        """Contre-épreuve : sans elle, le test précédent passerait même si la route
        ignorait le champ pour TOUT LE MONDE — donc si la fonctionnalité n'existait pas."""
        tenant = tenants.create_tenant("Chez Patron", "+33199000998")
        try:
            assert client.post("/admin/login", data={"email": "admin@test.local",
                                                     "password": "test-admin-pass"},
                               follow_redirects=False).status_code == 303
            client.post(
                f"/admin/tenants/{tenant.id}",
                data={"name": "Chez Patron", "phone_number": "+33199000998",
                      "business_type": "restaurant", "language": "fr-FR",
                      "greeting": "", "knowledge_base": "", "plan": "maison"},
                headers={"X-CSRF-Token": self._csrf(client)},
                follow_redirects=False)
            assert tenants.get_by_id(tenant.id).plan == "maison"
        finally:
            tenants.delete_tenant(tenant.id)


class TestAffichage:
    """Ce que l'admin écrit doit être ce que la grille vend : des minutes, le tarif de la
    formule, et rien qui ressemble à un plafond quand il n'y a pas de forfait."""

    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        assert client.post("/admin/login", data={"email": "admin@test.local",
                                                 "password": "test-admin-pass"},
                           follow_redirects=False).status_code == 303
        return client

    @staticmethod
    def _texte(reponse) -> str:
        assert reponse.status_code == 200
        return " ".join(reponse.text.replace("&#39;", "'").split())

    @pytest.fixture()
    def etablissement(self):
        tenant = tenants.create_tenant("Chez Minute", "+33199000444")
        yield tenant
        tenants.delete_tenant(tenant.id)

    def test_le_tableau_de_bord_compte_en_minutes(self, client, etablissement):
        tenants.update_tenant(etablissement.id, plan="essentiel")
        _appels(etablissement.id, 2, secondes=90.0, prefixe="AFF-E")
        essentiel = plans.get("essentiel")
        page = self._texte(client.get(f"/admin/?tenant_id={etablissement.id}"))
        assert "Forfait Essentiel" in page and "Dans le forfait" in page
        assert (f"3 minutes d'appel ce mois-ci sur {essentiel.minutes_incluses} "
                "minutes incluses, en 2 appel(s)") in page
        assert f"chaque minute est facturée {essentiel.minute_supp_eur:.2f} €".replace(".", ",") in page
        assert f'aria-label="3 minutes sur {essentiel.minutes_incluses} minutes incluses"' in page

    def test_un_appel_court_s_affiche_en_secondes(self, client, etablissement):
        """Appel 219 du 03/10/2026 : 23 secondes. « 0 minute » aurait fait croire qu'il
        n'avait pas compté."""
        tenants.update_tenant(etablissement.id, plan="essentiel")
        _appels(etablissement.id, 1, secondes=23.0, prefixe="AFF-C")
        page = self._texte(client.get(f"/admin/?tenant_id={etablissement.id}"))
        assert "23 secondes d'appel ce mois-ci" in page
        assert not re.search(r"(?<![0-9])0 minute", page)  # « 250 minutes » n'en est pas

    @pytest.mark.parametrize("secondes,attendu", [(1, "1 seconde"), (59.4, "59 secondes"),
                                                  (60, "1 minute"), (95, "2 minutes"),
                                                  (12_725, "212 minutes")])
    def test_la_consommation_se_lit_a_l_unite_pres(self, secondes, attendu):
        c = quotas._consommation(plans.get("essentiel"), 1, secondes)
        assert c.consomme_lisible == attendu

    def test_le_depassement_s_ecrit_en_minutes_et_en_euros(self, client, etablissement):
        service = plans.get("service")
        tenants.update_tenant(etablissement.id, plan="service")
        _appels(etablissement.id, 1, secondes=(service.minutes_incluses + 40) * 60.0,
                prefixe="AFF-S")
        page = self._texte(client.get(f"/admin/?tenant_id={etablissement.id}"))
        assert "Forfait dépassé" in page
        assert "40 minute(s) hors forfait" in page
        assert f"{40 * service.minute_supp_eur:.2f} € à facturer".replace(".", ",") in page
        assert "ne le sera pas" in page  # la ligne n'est jamais coupée, et on le dit

    def test_sans_forfait_ni_barre_ni_plafond(self, client, etablissement):
        liberte = plans.get("liberte")
        tenants.update_tenant(etablissement.id, plan="liberte")
        _appels(etablissement.id, 1, secondes=20 * 60.0, prefixe="AFF-L")
        page = self._texte(client.get(f"/admin/?tenant_id={etablissement.id}"))
        assert "Formule Liberté" in page and "Sans abonnement" in page
        assert f"soit {20 * liberte.minute_supp_eur:.2f} € à facturer".replace(".", ",") in page
        assert "incluses" not in page and 'class="meter' not in page
        assert "dépassé" not in page

    def test_le_parc_affiche_les_minutes_du_mois(self, client, etablissement):
        essentiel = plans.get("essentiel")
        tenants.update_tenant(etablissement.id, plan="essentiel")
        _appels(etablissement.id, 1, secondes=12 * 60.0, prefixe="AFF-P")
        page = self._texte(client.get("/admin/"))
        assert f"<b>12/{essentiel.minutes_incluses}</b> <span>min ce mois\u2011ci</span>" in page

    def test_le_parc_n_ecrit_pas_zero_minute_apres_un_vrai_appel(self, client, etablissement):
        """Recette du 05/10/2026 : un appel de 23 secondes s'affichait « 0/250 » dans la
        vue du parc, alors que la salle de contrôle écrit « 23 secondes »."""
        essentiel = plans.get("essentiel")
        tenants.update_tenant(etablissement.id, plan="essentiel")
        _appels(etablissement.id, 1, secondes=23.0, prefixe="AFF-23")
        ligne = self._ligne_du_parc(client, etablissement)
        assert f"<b>&lt;1/{essentiel.minutes_incluses}</b>" in ligne
        assert f"<b>0/{essentiel.minutes_incluses}</b>" not in ligne

    def test_sans_appel_le_parc_ecrit_bien_zero(self, client, etablissement):
        essentiel = plans.get("essentiel")
        tenants.update_tenant(etablissement.id, plan="essentiel")
        assert f"<b>0/{essentiel.minutes_incluses}</b>" in self._ligne_du_parc(client, etablissement)

    def _ligne_du_parc(self, client, etablissement) -> str:
        page = self._texte(client.get("/admin/"))
        debut = page.index(f'href="/admin/?tenant_id={etablissement.id}"')
        return page[debut:page.index("</a>", debut)]

    def test_les_chiffres_du_parc_forment_un_bloc_qui_passe_sous_le_nom(self, client,
                                                                         etablissement):
        """Sur un téléphone, les quatre chiffres s'écrasaient sur le nom et ses
        étiquettes : ils sont groupés, et la feuille de style les descend d'une ligne."""
        import pathlib
        page = client.get("/admin/").text
        assert 'class="card-list parc-liste"' in page
        assert page.count('class="row-metrics"') == page.count('class="card-row" href="/admin/?tenant_id=')
        feuille = (pathlib.Path(__file__).resolve().parents[1] / "app" / "admin" / "static"
                   / "admin.css").read_text(encoding="utf-8")
        assert ".parc-liste { container-type: inline-size; }" in feuille
        assert ".parc-liste .card-row { flex-wrap: wrap; }" in feuille

    def test_le_parc_n_invente_pas_de_plafond_sans_forfait(self, client, etablissement):
        tenants.update_tenant(etablissement.id, plan="liberte")
        _appels(etablissement.id, 1, secondes=12 * 60.0, prefixe="AFF-PL")
        page = self._texte(client.get("/admin/"))
        assert "<b>12</b> <span>min ce mois\u2011ci</span>" in page

    def test_la_fiche_propose_les_formules_en_minutes(self, client, etablissement):
        page = self._texte(client.get(f"/admin/tenants/{etablissement.id}/edit"))
        for f in plans.catalogue():
            if f.sans_forfait:
                assert (f"{f.label} · sans abonnement · {f.minute_supp_eur:.2f}".replace(".", ",")
                        + " €/min") in page
            else:
                assert f"{f.label} · {f.prix_mensuel_eur} €/mois · {f.minutes_incluses} min" in page
        assert "appels" not in page[page.index('name="plan"'):page.index("</select>", page.index('name="plan"'))]
