"""scripts/notes_de_version.py (ASSISTANTE-175) : une version regroupe les sections « Non
publiée » du CHANGELOG, et ses notes en sont tirées telles quelles pour la release GitHub."""
import importlib.util
from pathlib import Path

import pytest

_CHEMIN = Path(__file__).resolve().parents[2] / "scripts" / "notes_de_version.py"
_spec = importlib.util.spec_from_file_location("notes_de_version", _CHEMIN)
nv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nv)

JOURNAL = """# Changelog

> Préambule.

## Non publiée — une préproduction (26/10/2026, ASSISTANTE-174)

### Ajouté
- La préproduction.

## Non publiée — la tournée dit son heure (26/10/2026, ASSISTANTE-173)

### Corrigé
- L'heure.

## v1.1.0 — déployée le 19/09/2026 — l'audit

Ancien texte.

## Non publiée — plus ancienne, après une version

- ne doit pas être reprise.
"""


class TestPreparer:
    def test_les_sections_non_publiees_du_haut_deviennent_la_version(self):
        texte = nv.preparer(JOURNAL, "v1.2.0", "26/10/2026")
        assert "## v1.2.0 — 26/10/2026\n" in texte
        assert "### Une préproduction (26/10/2026, ASSISTANTE-174)" in texte
        assert "### La tournée dit son heure (26/10/2026, ASSISTANTE-173)" in texte
        assert "#### Ajouté" in texte and "#### Corrigé" in texte
        assert texte.count("## Non publiée") == 1          # seule la plus ancienne reste
        assert texte.index("## v1.2.0") < texte.index("## v1.1.0")
        assert texte.startswith("# Changelog\n\n> Préambule.\n\n## v1.2.0")

    def test_un_commentaire_dans_un_bloc_de_code_n_est_pas_un_titre(self):
        journal = JOURNAL.replace("- La préproduction.\n",
                                  "```bash\n# relancer\n## pas un titre\n```\n")
        texte = nv.preparer(journal, "v1.2.0", "26/10/2026")
        assert "```bash\n# relancer\n## pas un titre\n```" in texte
        assert "### La tournée dit son heure" in texte

    def test_un_bloc_de_code_dans_une_liste_ne_coupe_pas_la_version(self):
        journal = JOURNAL.replace("- La préproduction.\n",
                                  "- étapes :\n  ```\n  ## pas un titre\n  ```\n#hashtag\n")
        texte = nv.preparer(journal, "v1.2.0", "26/10/2026")
        assert "  ## pas un titre" in texte and "\n#hashtag\n" in texte
        assert texte.count("## Non publiée") == 1

    def test_une_version_deja_publiee_est_refusee(self):
        with pytest.raises(nv.Refus, match="existe déjà"):
            nv.preparer(JOURNAL, "v1.1.0", "26/10/2026")

    def test_rien_a_publier_est_refuse(self):
        with pytest.raises(nv.Refus, match="Non publiée"):
            nv.preparer("# Changelog\n\n## v1.1.0 — x\n", "v1.2.0", "26/10/2026")

    @pytest.mark.parametrize("version", ["1.2.0", "v1.2", "v1.2.0-rc1", "v1.2.0\n## x"])
    def test_un_numero_mal_forme_est_refuse(self, version):
        with pytest.raises(nv.Refus, match="vX.Y.Z"):
            nv.preparer(JOURNAL, version, "26/10/2026")


class TestNotes:
    def test_les_notes_sont_la_section_de_la_version_seule(self):
        texte = nv.preparer(JOURNAL, "v1.2.0", "26/10/2026")
        notes = nv.notes(texte, "v1.2.0")
        assert notes.startswith("### Une préproduction")
        assert "- L'heure." in notes
        assert "v1.1.0" not in notes and "Ancien texte" not in notes

    def test_une_version_absente_est_refusee(self):
        with pytest.raises(nv.Refus, match="pas de section"):
            nv.notes(JOURNAL, "v9.9.9")

    def test_des_notes_trop_longues_pour_github_sont_coupees_et_le_disent(self):
        long = "# C\n\n## v1.2.0 — x\n\n" + "- ligne\n" * 30000 + "\n## v1.1.0 — y\n"
        notes = nv.notes(long, "v1.2.0")
        assert len(notes) <= nv.LIMITE_GITHUB
        assert notes.rstrip().endswith("CHANGELOG.md")
