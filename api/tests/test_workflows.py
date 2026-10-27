"""Les actions des workflows sont épinglées par l'empreinte de leur commit (ASSISTANTE-178).
Une étiquette (`@v4`) se déplace au gré de son auteur : le déploiement, qui tient une clé du
serveur, exécuterait du code que personne n'a relu."""
import pathlib
import re

WORKFLOWS = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
_EPINGLEE = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
_VERSION = re.compile(r"#\s*v\d+\.\d+\.\d+\s*$")


def _usages():
    for fichier in sorted(WORKFLOWS.glob("*.yml")):
        for n, ligne in enumerate(fichier.read_text().splitlines(), 1):
            m = _USES.match(ligne)
            if m:
                yield f"{fichier.name}:{n}", m.group(1), m.group(2)


class TestActionsEpinglees:
    def test_il_y_a_des_actions_a_verifier(self):
        assert len(list(_usages())) >= 5

    def test_chaque_action_est_epinglee_par_empreinte(self):
        # Les workflows locaux (`./.github/…`) sont du code du dépôt : déjà relus.
        libres = [ou for ou, cible, _ in _usages()
                  if not cible.startswith("./") and not _EPINGLEE.fullmatch(cible)]
        assert not libres, f"action non épinglée par empreinte : {libres}"

    def test_la_version_est_ecrite_a_cote(self):
        # Sans elle, personne ne sait ce que l'empreinte désigne, ni Dependabot quoi proposer.
        muettes = [ou for ou, cible, reste in _usages()
                   if not cible.startswith("./") and not _VERSION.search(reste)]
        assert not muettes, f"empreinte sans « # vX.Y.Z » : {muettes}"

    def test_aucun_workflow_ne_garde_les_droits_par_defaut(self):
        sans = [f.name for f in sorted(WORKFLOWS.glob("*.yml"))
                if not re.search(r"^permissions:", f.read_text(), re.M)]
        assert not sans, f"workflow sans « permissions: » de premier niveau : {sans}"
