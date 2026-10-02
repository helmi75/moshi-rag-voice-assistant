"""Le message vocal laissé pendant une panne est rapatrié chez nous (ASSISTANTE-118).

Ce qui compte : le message s'écoute dans la fiche de l'appel, à la suite de ce qui
avait déjà été enregistré ; il n'est effacé de chez Twilio qu'une fois écrit chez nous ;
et rien de tout cela ne peut lever — le restaurant a déjà été prévenu par e-mail."""
import asyncio
import io
import uuid
import wave

import numpy as np
import pytest

from app import calls, db, repondeur, tenants
from app.voice import enregistrement

SID = "RE" + "a1" * 16


def _wav(secondes=1.0, taux=8000, canaux=1) -> bytes:
    n = int(secondes * taux)
    signal = (np.sin(np.arange(n) * 0.2) * 8000).astype("<i2")
    if canaux == 2:
        signal = np.repeat(signal, 2)
    sortie = io.BytesIO()
    with wave.open(sortie, "wb") as fichier:
        fichier.setnchannels(canaux)
        fichier.setsampwidth(2)
        fichier.setframerate(taux)
        fichier.writeframes(signal.tobytes())
    return sortie.getvalue()


class _Reponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code, self.content = status_code, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Twilio:
    """Un Twilio de poche : ce qu'on lui demande, et ce qu'il répond."""
    appels: list = []
    telechargement = _Reponse(200, _wav(2.0))
    effacement = _Reponse(204)

    def __init__(self, **options):
        _Twilio.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, adresse):
        _Twilio.appels.append(("GET", adresse))
        return _Twilio.telechargement

    async def delete(self, adresse):
        _Twilio.appels.append(("DELETE", adresse))
        return _Twilio.effacement


@pytest.fixture()
def appel(monkeypatch, tmp_path):
    monkeypatch.setenv("ENREGISTREMENT_DIR", str(tmp_path))
    monkeypatch.setenv("ENREGISTREMENT_DISQUE_MINIMUM_MO", "0")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "jeton")
    monkeypatch.setattr(repondeur.httpx, "AsyncClient", _Twilio)
    _Twilio.appels, _Twilio.telechargement, _Twilio.effacement = [], _Reponse(200, _wav(2.0)), _Reponse(204)
    tenant = tenants.create_tenant("Chez Message", f"+3365{id(object()) % 10_000_000:07d}")
    sid = f"CA{uuid.uuid4().hex}"
    call_id = calls.ouvrir_secours(sid, tenant.id, "+33611223344", "voix")
    yield tenant, sid, call_id
    tenants.delete_tenant(tenant.id)


def _octets(call_id: int):
    with db.get_conn() as conn:
        return conn.execute("SELECT recording_bytes FROM calls WHERE id = ?", (call_id,)).fetchone()[0]


class TestLaConversion:
    def test_un_wav_de_twilio_devient_du_ulaw_8k(self):
        assert len(repondeur.en_ulaw(_wav(1.0))) == 8000

    def test_stereo_et_16k_aussi(self):
        assert len(repondeur.en_ulaw(_wav(1.0, taux=16000, canaux=2))) == pytest.approx(8000, abs=40)

    def test_un_format_inattendu_est_refuse(self):
        sortie = io.BytesIO()
        with wave.open(sortie, "wb") as fichier:
            fichier.setnchannels(1)
            fichier.setsampwidth(1)
            fichier.setframerate(8000)
            fichier.writeframes(b"\x80" * 100)
        with pytest.raises(ValueError):
            repondeur.en_ulaw(sortie.getvalue())


class TestLeRapatriement:
    def test_le_message_arrive_chez_nous_puis_quitte_twilio(self, appel):
        tenant, sid, call_id = appel
        assert asyncio.run(repondeur.rapatrier(sid, SID)) is True
        piste = enregistrement.chemin(tenant.id, call_id, "appelant")
        assert piste.stat().st_size == 16000 and _octets(call_id) == 16000
        base = f"https://api.twilio.com/2010-04-01/Accounts/AC{'0' * 32}/Recordings/{SID}"
        assert _Twilio.appels == [("GET", base + ".wav"), ("DELETE", base + ".json")]
        assert _Twilio.options["auth"] == ("AC" + "0" * 32, "jeton")

    def test_il_se_range_apres_la_conversation_deja_enregistree(self, appel):
        """Un appel renvoyé en cours de route a déjà ses deux pistes : le message ne doit
        ni les écraser, ni se superposer à la fin de ce que l'assistante disait."""
        tenant, sid, call_id = appel
        client_, assistante = (enregistrement.chemin(tenant.id, call_id, p)
                               for p in ("appelant", "assistante"))
        client_.parent.mkdir(parents=True, exist_ok=True)
        client_.write_bytes(b"\x10" * 3000)
        assistante.write_bytes(b"\x20" * 5000)
        assert asyncio.run(repondeur.rapatrier(sid, SID)) is True
        contenu = client_.read_bytes()
        assert contenu[:3000] == b"\x10" * 3000            # la conversation est intacte
        assert contenu[3000:5000] == b"\xff" * 2000        # silence jusqu'à la fin de l'autre piste
        assert len(contenu) == 5000 + 16000
        assert _octets(call_id) == len(contenu) + 5000

    def test_il_n_est_efface_de_twilio_que_s_il_est_ecrit(self, appel):
        _, sid, call_id = appel
        _Twilio.telechargement = _Reponse(503)
        assert asyncio.run(repondeur.rapatrier(sid, SID)) is False
        assert [m for m, _ in _Twilio.appels] == ["GET"] and _octets(call_id) is None

    def test_twilio_garde_sa_copie_on_le_sait(self, appel):
        tenant, sid, call_id = appel
        _Twilio.effacement = _Reponse(500)
        assert asyncio.run(repondeur.rapatrier(sid, SID)) is False
        assert _octets(call_id) == 16000   # le message est bien chez nous

    @pytest.mark.parametrize("identifiant", ["", None, "RE123", "../Calls/CA1", "RE" + "g" * 32,
                                             "RE" + "a" * 32 + "/../x"])
    def test_seul_un_identifiant_d_enregistrement_entre_dans_l_adresse(self, appel, identifiant):
        _, sid, _ = appel
        assert asyncio.run(repondeur.rapatrier(sid, identifiant)) is False
        assert _Twilio.appels == []

    def test_sans_identifiants_twilio_rien_n_est_tente(self, appel, monkeypatch):
        _, sid, _ = appel
        monkeypatch.delenv("TWILIO_ACCOUNT_SID")
        assert asyncio.run(repondeur.rapatrier(sid, SID)) is False and _Twilio.appels == []

    def test_un_appel_inconnu_ne_leve_pas(self, appel):
        assert asyncio.run(repondeur.rapatrier("CAinconnu", SID)) is False

    def test_disque_presque_plein_le_message_reste_chez_twilio(self, appel, monkeypatch):
        _, sid, _ = appel
        monkeypatch.setenv("ENREGISTREMENT_DISQUE_MINIMUM_MO", "999999999")
        assert asyncio.run(repondeur.rapatrier(sid, SID)) is False and _Twilio.appels == []


def test_le_message_s_ecoute_dans_la_fiche_de_l_appel(appel):
    from fastapi.testclient import TestClient

    from app.main import app

    tenant, sid, call_id = appel
    asyncio.run(repondeur.rapatrier(sid, SID))
    session = TestClient(app)
    session.post("/admin/login", data={"email": "admin@test.local", "password": "test-admin-pass"})
    page = session.get(f"/admin/calls/{call_id}").text
    assert f"/admin/calls/{call_id}/audio.wav?piste=mixte" in page
    audio = session.get(f"/admin/calls/{call_id}/audio.wav?piste=mixte")
    assert audio.status_code == 200 and len(audio.content) > 2 * 16000   # 2 s en PCM 16 bits
