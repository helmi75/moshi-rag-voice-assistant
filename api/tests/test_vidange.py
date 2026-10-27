"""Un déploiement ne coupe plus les appels en cours (ASSISTANTE-159).

Le conteneur redémarre à chaque déploiement : un appel en cours était coupé net, d'où la
règle « déployer hors service », qui ne tiendra plus quand il n'y aura plus d'heure creuse.
`scripts/deploy.sh` pose un drapeau dans le volume de données ; tant qu'il est là, chaque
NOUVEL appel est servi comme pendant une panne (renvoi au restaurant, ou répondeur), et le
script attend que la jauge des appels en cours tombe à zéro avant de lancer la nouvelle
version. Celle-ci efface le drapeau en démarrant.
"""
import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import db, main, renvoi, tenants

DEMO = "+33100000000"


@pytest.fixture()
def drapeau(tmp_path):
    chemin = tmp_path / "vidange"
    with patch.object(renvoi, "chemin_du_drapeau", lambda: str(chemin)):
        yield chemin


class TestLeDrapeau:
    def test_il_vit_a_cote_de_la_base(self):
        # Dans le volume de données : deploy.sh l'écrit depuis l'hôte, le conteneur le lit.
        assert os.path.dirname(renvoi.chemin_du_drapeau()) == os.path.dirname(db.DB_PATH)

    def test_sans_drapeau_pas_de_vidange(self, drapeau):
        assert renvoi.vidange_en_cours() is False

    def test_le_drapeau_pose_par_deploy_sh_est_vu(self, drapeau):
        drapeau.touch()
        assert renvoi.vidange_en_cours() is True

    def test_un_drapeau_oublie_ne_bloque_pas_la_ligne_indefiniment(self, drapeau):
        # Un déploiement interrompu sans nettoyage : passé le délai, l'assistante reprend.
        drapeau.touch()
        vieux = time.time() - renvoi.VIDANGE_MAX_SECONDES - 60
        os.utime(drapeau, (vieux, vieux))
        assert renvoi.vidange_en_cours() is False

    def test_la_nouvelle_version_efface_le_drapeau_en_demarrant(self, drapeau):
        drapeau.touch()
        renvoi.fin_de_vidange()
        assert not drapeau.exists()
        renvoi.fin_de_vidange()      # sans drapeau : rien, pas d'erreur

    def test_un_drapeau_illisible_ne_fait_pas_echouer_un_appel(self, drapeau):
        with patch.object(renvoi.os, "stat", side_effect=PermissionError("refusé")):
            assert renvoi.vidange_en_cours() is False


class TestLesNouveauxAppels:
    def _appel(self, client):
        return client.post("/twilio/voice", data={"CallSid": "CA-vidange", "To": DEMO,
                                                  "From": "+33612345678"})

    def test_pendant_la_vidange_un_nouvel_appel_part_au_secours(self, drapeau):
        drapeau.touch()
        client = TestClient(main.app)
        reponse = self._appel(client)
        assert "<Connect>" not in reponse.text
        assert ("<Dial" in reponse.text) or ("<Record" in reponse.text)
        with db.get_conn() as conn:
            ligne = conn.execute("SELECT secours_motif FROM calls WHERE call_sid = ?",
                                 ("CA-vidange",)).fetchone()
        assert ligne["secours_motif"] == renvoi.VIDANGE

    def test_sans_vidange_l_appel_va_a_l_assistante(self, drapeau):
        client = TestClient(main.app)
        assert "<Connect>" in self._appel(client).text

    def test_le_motif_a_son_libelle(self):
        assert renvoi.VIDANGE in renvoi.LIBELLES


def test_le_tenant_de_demo_existe():
    # Garde-fou du test lui-même : DEMO est le numéro semé par conftest.
    assert tenants.get_by_phone(DEMO) is not None


class TestLaSupervision:
    def test_un_renvoi_de_vidange_n_est_pas_une_panne(self):
        """Sinon le contrôle d'après lancement de deploy.sh défaisait le déploiement."""
        from app import supervision
        recents = [{"id": 1, "tenant_id": 1, "started_at": supervision._maintenant().strftime(
            "%Y-%m-%dT%H:%M:%SZ"), "status": "secours_renvoye", "secours_motif": renvoi.VIDANGE}]
        with patch.object(supervision.calls, "secours_recents", return_value=recents):
            c = supervision._controle_secours()
        assert c.niveau == supervision.ATTENTION
        assert c.mesure["secours_24h"] == 1

    def test_une_vraie_panne_recente_le_reste(self):
        from app import supervision
        maintenant = supervision._maintenant().strftime("%Y-%m-%dT%H:%M:%SZ")
        recents = [{"id": 2, "tenant_id": 1, "started_at": maintenant,
                    "status": "secours_renvoye", "secours_motif": renvoi.VIDANGE},
                   {"id": 1, "tenant_id": 1, "started_at": maintenant,
                    "status": "secours_renvoye", "secours_motif": renvoi.VOIX}]
        with patch.object(supervision.calls, "secours_recents", return_value=recents):
            assert supervision._controle_secours().niveau == supervision.PANNE
