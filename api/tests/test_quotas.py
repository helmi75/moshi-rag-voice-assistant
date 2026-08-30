"""Plafond et consommation mensuelle (#31).

Le plafond est ce qui protège la marge. Deux propriétés comptent plus que les autres,
et chacune a son test :

1. **il ne coupe jamais la ligne** — décision produit, pas oubli d'implémentation ;
2. **il compte le mois calendaire** — la maille de facturation, sinon le restaurateur
   ne peut pas relire sa facture.
"""
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


def _appels(tenant_id: int, combien: int, *, mois_decale: int = 0, prefixe: str = "Q") -> None:
    """Écrit `combien` appels. `mois_decale=1` les place le mois précédent."""
    date = datetime.now(timezone.utc).replace(day=15, hour=12)
    if mois_decale:
        date = (date.replace(day=1) - timedelta(days=1)).replace(day=15, hour=12)
    horodatage = date.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_conn() as conn:
        for i in range(combien):
            conn.execute(
                "INSERT INTO calls (call_sid, tenant_id, started_at) VALUES (?, ?, ?)",
                (f"{prefixe}{mois_decale}-{i}", tenant_id, horodatage),
            )


class TestComptage:
    def test_aucun_appel(self, resto):
        c = quotas.etat(resto)
        assert c.appels == 0 and c.hors_forfait == 0
        assert c.niveau == quotas.OK

    def test_seul_le_mois_en_cours_compte(self, resto):
        """La facture porte sur un mois. Compter en fenêtre glissante donnerait un
        plafond que le restaurateur ne peut pas relire sur sa facture."""
        _appels(resto.id, 5)
        _appels(resto.id, 40, mois_decale=1)
        assert quotas.etat(resto).appels == 5

    def test_les_appels_des_autres_ne_comptent_pas(self, resto):
        autre = tenants.create_tenant("Voisin", "+33199000778")
        _appels(autre.id, 30, prefixe="V")
        _appels(resto.id, 3)
        assert quotas.etat(resto).appels == 3
        assert quotas.etat(autre).appels == 30

    def test_un_appel_sans_reservation_compte_quand_meme(self, resto):
        """Il consomme exactement le même GPU. Ne facturer que les appels aboutis
        reviendrait à offrir les autres — qui coûtent le même prix."""
        _appels(resto.id, 4)
        with db.get_conn() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM calls WHERE reservation_id IS NOT NULL"
            ).fetchone()[0] == 0
        assert quotas.etat(resto).appels == 4


class TestSeuils:
    def _sous_formule(self, resto, appels, formule="essentiel"):
        tenants.update_tenant(resto.id, plan=formule)
        _appels(resto.id, appels)
        return quotas.etat(tenants.get_by_id(resto.id))

    def test_sous_le_seuil_rien_ne_bouge(self, resto):
        assert self._sous_formule(resto, 50).niveau == quotas.OK

    def test_a_80_pour_cent_on_previent(self, resto):
        """Prévenir APRÈS le dépassement ne prévient de rien."""
        inclus = plans.get("essentiel").appels_inclus
        c = self._sous_formule(resto, int(inclus * plans.SEUIL_ALERTE))
        assert c.niveau == quotas.ALERTE
        assert c.hors_forfait == 0 and c.montant_eur == 0.0

    def test_au_dela_on_facture(self, resto):
        inclus = plans.get("essentiel").appels_inclus
        c = self._sous_formule(resto, inclus + 7)
        assert c.niveau == quotas.DEPASSEMENT
        assert c.hors_forfait == 7
        assert c.montant_eur == round(7 * plans.DEPASSEMENT_EUR, 2)

    def test_pile_au_plafond_n_est_pas_un_depassement(self, resto):
        """L'appel n°150 est le dernier inclus, pas le premier facturé."""
        inclus = plans.get("essentiel").appels_inclus
        c = self._sous_formule(resto, inclus)
        assert c.hors_forfait == 0 and c.montant_eur == 0.0

    def test_la_barre_ne_deborde_pas_mais_le_chiffre_reste_exact(self, resto):
        inclus = plans.get("essentiel").appels_inclus
        c = self._sous_formule(resto, inclus * 3)
        assert c.part_affichable == 100      # la barre ne dépasse pas sa largeur
        assert c.part == pytest.approx(3.0)  # …mais l'ampleur reste lisible


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
        _appels(resto.id, 1000)
        c = quotas.etat(tenants.get_by_id(resto.id))
        assert c.montant_eur > 0
        assert c.niveau == quotas.DEPASSEMENT  # signalé…
        assert c.appels == 1000                # …et tous les appels ont bien été servis


class TestAlertes:
    def test_rien_a_signaler_sous_le_seuil(self, resto):
        _appels(resto.id, 10)
        assert quotas.alertes([resto]) == []

    def test_le_depassement_chiffre_ce_qui_sera_facture(self, resto):
        tenants.update_tenant(resto.id, plan="essentiel")
        _appels(resto.id, plans.get("essentiel").appels_inclus + 20)
        alerte = quotas.alertes([tenants.get_by_id(resto.id)])[0]
        assert "20 appel" in alerte["title"]
        assert f"{round(20 * plans.DEPASSEMENT_EUR, 2):.2f}" in alerte["detail"]
        assert "pas été coupée" in alerte["detail"]


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
