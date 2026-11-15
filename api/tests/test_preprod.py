"""La préproduction (ASSISTANTE-174) partage le serveur, le réseau Docker et le Caddy de la
production : ce qui les sépare tient à quelques lignes de configuration, gardées ici."""
import pathlib
import re

import yaml

RACINE = pathlib.Path(__file__).resolve().parents[2]


class _Chargeur(yaml.SafeLoader):
    """Les étiquettes de Compose (`!override`, `!reset`) que yaml ne connaît pas."""


_Chargeur.add_constructor("!override", lambda c, n: c.construct_sequence(n)
                          if isinstance(n, yaml.SequenceNode) else c.construct_mapping(n))
_Chargeur.add_constructor("!reset", lambda c, n: None)


def _compose(nom):
    return yaml.load((RACINE / nom).read_text(), Loader=_Chargeur)  # noqa: S506 — chargeur sûr, deux étiquettes en plus


class TestLaPreproduction:
    def test_son_conteneur_ne_s_appelle_jamais_api(self):
        # Sur le réseau partagé, un second `api` se partagerait le nom avec la production :
        # Caddy enverrait des appels réels en préproduction.
        services = _compose("docker-compose.preprod.yml")["services"]
        assert list(services) == ["api_preprod", "gosom"]

    def test_elle_a_sa_base_et_son_image(self):
        c = _compose("docker-compose.preprod.yml")
        s = c["services"]["api_preprod"]
        assert c["name"] == "helmane-preprod"          # volume helmane-preprod_api_data
        assert "api_data" in c["volumes"]
        assert s["image"].endswith(":preprod")
        assert s["image"] != _compose("docker-compose.yml")["services"]["api"]["image"]

    def test_son_port_n_ecoute_que_la_boucle_locale(self):
        ports = _compose("docker-compose.preprod.yml")["services"]["api_preprod"]["ports"]
        assert ports == ["127.0.0.1:8001:8000"]
        assert "!override" in (RACINE / "docker-compose.preprod.yml").read_text()

    def test_elle_rejoint_le_reseau_de_la_production(self):
        c = _compose("docker-compose.preprod.yml")
        assert c["networks"]["production"] == {
            "external": True, "name": "moshi-rag-voice-assistant_default"}

    def test_caddy_la_sert_sous_son_nom_et_hors_des_moteurs(self):
        caddy = (RACINE / "caddy" / "Caddyfile").read_text()
        bloc = re.search(r"\{\$PREPROD_ADDRESS::82\} \{(.*?)\n\}", caddy, re.S).group(1)
        assert "reverse_proxy api_preprod:8000" in bloc
        assert "noindex" in bloc and "import securite" in bloc
        env = _compose("docker-compose.yml")["services"]["caddy"]["environment"]
        assert env["PREPROD_ADDRESS"] == "${PREPROD_ADDRESS:-:82}"


class TestGosomResteEnPreproduction:
    """ASSISTANTE-189 : le lecteur de fiches Google Maps est contraire aux conditions de Google
    (Helmi l'a accepté le 15/11/2026 pour la préproduction SEULEMENT) : il ne doit jamais
    entrer dans la production, ni être joignable d'ailleurs que de l'API de préproduction."""

    def test_gosom_est_absent_de_la_production(self):
        compose = (RACINE / "docker-compose.yml").read_text()
        assert "gosom" not in _compose("docker-compose.yml")["services"]
        assert "google-maps-scraper" not in compose
        # seule la variable (vide) est déclarée, pour le câblage ; jamais d'adresse
        assert 'GOSOM_URL: "${GOSOM_URL:-}"' in compose and "gosom:8080" not in compose

    def test_gosom_ne_publie_aucun_port(self):
        gosom = _compose("docker-compose.preprod.yml")["services"]["gosom"]
        assert "ports" not in gosom

    def test_son_image_est_epinglee_par_empreinte(self):
        image = _compose("docker-compose.preprod.yml")["services"]["gosom"]["image"]
        assert re.fullmatch(r"gosom/google-maps-scraper@sha256:[0-9a-f]{64}", image)

    def test_il_n_ecrit_rien_sur_disque(self):
        gosom = _compose("docker-compose.preprod.yml")["services"]["gosom"]
        assert gosom["tmpfs"] == ["/gmapsdata"] and "volumes" not in gosom
        assert gosom["command"] == ["-web", "-data-folder", "/gmapsdata"]

    def test_il_a_son_reseau_que_la_production_ne_partage_pas(self):
        c = _compose("docker-compose.preprod.yml")
        assert c["services"]["gosom"]["networks"] == ["gosom"]
        assert "gosom" in c["services"]["api_preprod"]["networks"]
        assert c["networks"]["gosom"] in ({}, None)   # pas `external` : propre à la préproduction
        assert c["services"]["api_preprod"]["environment"]["GOSOM_URL"] == "http://gosom:8080"
