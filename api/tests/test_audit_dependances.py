"""pip-audit bloque la CI (ASSISTANTE-177) ; ses exceptions sont écrites, datées et liées à
une version. Une exception qui survit à la montée de son paquet couvrirait un avis que
personne n'a relu."""
import pathlib
import re

RACINE = pathlib.Path(__file__).resolve().parents[2]
EXCEPTIONS = RACINE / ".github" / "pip-audit-exceptions.txt"
_ENTETE = re.compile(r"# ([A-Za-z0-9_.-]+)==(\S+) : \S")
_AVIS = re.compile(r"(CVE-\d{4}-\d+|GHSA(-[a-z0-9]{4}){3}|PYSEC-\d{4}-\d+)")


def _verrou() -> dict[str, str]:
    paires = (ligne.split("==") for ligne in
              (RACINE / "api" / "app" / "requirements.lock").read_text().splitlines()
              if "==" in ligne and not ligne.startswith("#"))
    return {nom.lower(): version.strip() for nom, version in paires}


def _blocs() -> list[tuple[str, str, list[str]]]:
    blocs = []
    for bloc in EXCEPTIONS.read_text().split("\n\n"):
        lignes = bloc.strip().splitlines()
        avis = [ligne for ligne in lignes if not ligne.startswith("#")]
        if avis:
            entete = _ENTETE.match(lignes[0])
            assert entete, f"bloc sans « # paquet==version : raison » : {lignes[0]!r}"
            blocs.append((entete.group(1).lower(), entete.group(2), avis))
    return blocs


class TestLesExceptions:
    def test_chaque_bloc_vise_la_version_verrouillee(self):
        verrou = _verrou()
        for paquet, version, _ in _blocs():
            assert verrou.get(paquet) == version, (
                f"{paquet}=={version} n'est plus la version verrouillée "
                f"({verrou.get(paquet)}) : relancer pip-audit et revoir ce bloc.")

    def test_une_ligne_un_identifiant_d_avis(self):
        avis = [a for _, _, liste in _blocs() for a in liste]
        assert avis
        for a in avis:
            assert _AVIS.fullmatch(a), a
        assert len(avis) == len(set(avis))


class TestLaCI:
    def test_l_audit_bloque_et_lit_les_exceptions(self):
        ci = (RACINE / ".github" / "workflows" / "ci.yml").read_text()
        etape = re.search(r"- name: Audit des dépendances \(pip-audit\)\n(.*?)\n\n", ci, re.S).group(1)
        assert "continue-on-error" not in etape
        assert "pip-audit-exceptions.txt" in etape
        assert "requirements.lock" in etape
