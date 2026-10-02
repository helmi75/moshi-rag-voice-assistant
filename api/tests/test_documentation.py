"""Les consignes du dépôt ne mentent pas : CLAUDE.md et le skill `admin-ui`.

Ces deux fichiers sont lus AVANT tout travail ici, par une session qui ne connaît pas
encore le code. Une consigne qui cite une fonction renommée, un fichier supprimé ou une
commande qui ne marche plus coûte plus cher qu'une consigne absente : elle est suivie.
Relevé le 01/10/2026 en les relisant : le skill demandait `asyncio.create_task` (interdit
ici), annonçait des graphiques « sans aucun script », et donnait une commande de test
qui ne pouvait pas tourner.

Ce qui est vérifié : les fichiers cités existent, les noms cités (fonctions, classes,
constantes) sont définis dans le code, les tests donnés en exemple existent, et le nombre
de contrôles de supervision annoncé dans la documentation est le vrai.
"""
import pathlib
import re

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[2]
CONSIGNES = [RACINE / "CLAUDE.md", RACINE / ".claude" / "skills" / "admin-ui" / "SKILL.md"]

if not CONSIGNES[0].exists():
    pytest.skip("CLAUDE.md hors de portée (tests montés seuls dans le conteneur) — mord en CI.",
                allow_module_level=True)

# Où un nom de fichier cité sans son chemin peut se trouver.
DOSSIERS = ["", "api/app", "api/app/admin", "api/app/admin/static", "api/app/admin/templates",
            "api/app/voice", "api/app/connecteurs", "api/tests", "scripts", "docs",
            ".github/workflows"]
EXTENSIONS = ("py", "md", "yml", "sh", "lock", "txt", "toml", "css", "js", "html", "json")
# Un jeton qui contient un de ces caractères est un exemple ou un motif, pas un nom réel.
MOTIF = re.compile(r"[*<>…{}\[\]$ ]|\.\.\.")


def _jetons(fichier: pathlib.Path) -> list[str]:
    """Ce qui est écrit entre accents graves, hors des blocs de code."""
    texte = re.sub(r"```.*?```", "", fichier.read_text(encoding="utf-8"), flags=re.S)
    return sorted(set(re.findall(r"`([^`\n]+)`", texte)))


def _est_un_fichier(jeton: str) -> bool:
    chemin = re.split(r"::|:", jeton)[0]
    return bool(re.fullmatch(rf"[\w./-]+\.({'|'.join(EXTENSIONS)})", chemin)) or (
        "/" in chemin and chemin.endswith("/") and not chemin.startswith("/"))


def _trouver(chemin: str) -> bool:
    chemin = chemin.rstrip("/")
    return any((RACINE / dossier / chemin).exists() for dossier in DOSSIERS)


@pytest.fixture(scope="module")
def code() -> dict[str, str]:
    """Le source Python du dépôt, par nom de module (`db`, `bot`, `test_renvoi`…)."""
    sources: dict[str, str] = {}
    for dossier in ("api", "scripts"):
        for fichier in (RACINE / dossier).rglob("*.py"):
            if "__pycache__" in fichier.parts:
                continue
            nom = fichier.parent.name if fichier.name == "__init__.py" else fichier.stem
            sources[nom] = sources.get(nom, "") + "\n" + fichier.read_text(encoding="utf-8")
    return sources


def _defini(nom: str, source: str) -> bool:
    """`nom` est-il une fonction, une classe, une variable, un attribut de classe ou un
    filtre de gabarit défini dans ce source ?"""
    n = re.escape(nom)
    return bool(re.search(
        rf"^\s*(?:async\s+)?def\s+{n}\b|^\s*class\s+{n}\b"          # fonction, classe
        rf"|^\s*(?:\w+,\s*)*{n}(?:,\s*\w+)*\s*(?::[^=\n]+)?=[^=]"   # variable (seule ou en n-uplet)
        rf"|\[\"{n}\"\]\s*=",                                    # filtre ou global de gabarit
        source, flags=re.M))


@pytest.mark.parametrize("consignes", CONSIGNES, ids=lambda f: f.name)
class TestLesConsignesNeMententPas:
    def test_les_fichiers_cites_existent(self, consignes):
        absents = [j for j in _jetons(consignes)
                   if not MOTIF.search(j) and _est_un_fichier(j)
                   and not _trouver(re.split(r"::|:", j)[0])]
        assert absents == [], (
            f"{consignes.name} cite des fichiers qui n'existent pas (renommés, supprimés ?)")

    def test_les_noms_cites_existent(self, consignes, code):
        """`module.nom`, `fichier.py:nom` et `fichier.py::Classe` : `nom` doit être
        défini dans ce module. Un renommage sans mise à jour des consignes échoue ici."""
        absents = []
        for jeton in _jetons(consignes):
            if MOTIF.search(jeton):
                continue
            m = (re.fullmatch(r"(?:[\w/]+/)?(\w+)\.py::?(\w+)(?:::\w+)*", jeton)
                 or re.fullmatch(r"(\w+)\.(\w+)(?:\(\))?", jeton))
            if not m or m.group(1) not in code or _est_un_fichier(jeton) and ":" not in jeton:
                continue
            if not _defini(m.group(2), code[m.group(1)]):
                absents.append(jeton)
        assert absents == [], f"{consignes.name} cite des noms qui n'existent plus dans le code"

    def test_les_constantes_citees_existent(self, consignes, code):
        """Un nom tout en capitales est une constante du code ou une variable
        d'environnement : il doit apparaître dans le code, `env.example` ou le compose."""
        ailleurs = "".join((RACINE / f).read_text(encoding="utf-8")
                           for f in ("env.example", "docker-compose.yml"))
        partout = "\n".join(code.values()) + ailleurs
        absents = [j for j in _jetons(consignes)
                   if re.fullmatch(r"_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+", j)
                   and not re.search(rf"\b{re.escape(j)}\b", partout)]
        assert absents == [], f"{consignes.name} cite des constantes introuvables"


def test_ce_garde_fou_mord(code):
    """Un contrôle qui dirait oui à tout ne protégerait rien : il doit refuser un nom
    renommé et un fichier disparu."""
    assert _defini("hors_boucle", code["db"]) and not _defini("hors_boucles", code["db"])
    assert _defini("_MIGRATIONS", code["db"]) and not _defini("_MIGRATION", code["db"])
    assert _defini("TestCablage", code["test_supervision"])
    assert not _defini("TestCablages", code["test_supervision"])
    assert _defini("SECOURS_RENVOYE", code["calls"])            # défini dans un n-uplet
    assert not _defini("run_tool", code["bot"])                 # utilisé là, défini ailleurs
    assert _trouver("api/app/db.py") and _trouver("reservations/_row.html")
    assert not _trouver("api/app/n_existe_pas.py") and not _trouver("_row.html")
    assert _est_un_fichier("db.py:_MIGRATIONS") and not _est_un_fichier("db.hors_boucle")


def test_les_tests_donnes_en_exemple_existent():
    """La commande « un fichier, un test » de CLAUDE.md doit désigner un vrai test."""
    texte = CONSIGNES[0].read_text(encoding="utf-8")
    exemples = re.findall(r"tests/(test_\w+\.py)\s+-q\s+-k\s+(\w+)", texte)
    assert exemples, "CLAUDE.md ne montre plus comment lancer un seul test"
    for fichier, filtre in exemples:
        source = (RACINE / "api" / "tests" / fichier).read_text(encoding="utf-8")
        assert re.search(rf"def test_\w*{re.escape(filtre)}", source), (fichier, filtre)


def test_les_commandes_citent_l_image_et_les_scripts_reels():
    texte = CONSIGNES[0].read_text(encoding="utf-8")
    for script in set(re.findall(r"scripts/[\w.-]+", texte)):
        assert (RACINE / script).exists(), script
    compose = (RACINE / "docker-compose.yml").read_text(encoding="utf-8")
    # L'image s'appelle <dossier du projet>-<service> : le service doit exister.
    assert re.search(r"^  api:\s*$", compose, flags=re.M)
    assert "moshi-rag-voice-assistant-api" in texte


def test_le_nombre_de_controles_annonce_est_le_vrai():
    """Ajouter un contrôle de supervision sans mettre la documentation à jour laissait
    « 14 contrôles » dans ARCHITECTURE.md alors qu'il y en avait 16."""
    from test_supervision import TestEnumeration

    attendus = len(TestEnumeration.ATTENDUS)
    for fichier in ("README.md", "ARCHITECTURE.md"):
        annonces = re.findall(r"(\d+)\s+contrôles",
                              (RACINE / fichier).read_text(encoding="utf-8"))
        assert annonces and {int(n) for n in annonces} == {attendus}, (fichier, annonces)
    tableau = (RACINE / "docs" / "SUPERVISION.md").read_text(encoding="utf-8")
    section = tableau.split("| Contrôle | Ce qu'il attrape | Panne |")[1].split("\n\n")[0]
    lignes = [ligne for ligne in section.splitlines() if ligne.startswith("| ") and "---" not in ligne]
    assert len(lignes) == attendus, "docs/SUPERVISION.md : une ligne par contrôle"


def test_les_documents_juges_a_jour_ne_parlent_pas_d_une_voix_perimee():
    """README et ARCHITECTURE présentaient encore Moshi comme LA voix, trois jours après
    la bascule sur Mistral : un lecteur partait sur le mauvais module."""
    for fichier in ("README.md", "ARCHITECTURE.md"):
        texte = (RACINE / fichier).read_text(encoding="utf-8")
        assert "Voxtral" in texte and "secours" in texte, fichier


def test_le_skill_ne_redonne_pas_les_consignes_perimees():
    skill = CONSIGNES[1].read_text(encoding="utf-8")
    assert "zéro JS" not in skill and "docker compose exec api" not in skill
    # `asyncio.create_task` n'y figure que pour être interdit.
    for ligne in skill.splitlines():
        if "asyncio.create_task" in ligne:
            assert "jamais" in ligne
