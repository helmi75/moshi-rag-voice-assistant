"""Versions numérotées (ASSISTANTE-175) : le CHANGELOG fait foi, la release GitHub le recopie.

    python3 scripts/notes_de_version.py preparer v1.2.0   regroupe les sections « Non publiée »
    python3 scripts/notes_de_version.py notes v1.2.0      écrit les notes de la version

`preparer` réécrit CHANGELOG.md : les sections « Non publiée » du haut, livrées depuis la
dernière version, passent sous un seul titre `## v1.2.0 — date`. Releases groupées, décision
de Helmi du 24/10/2026 : une version rassemble plusieurs déploiements. Le commit qui en sort
passe par une PR ; le tag, posé ensuite sur `main`, vaut accord de Helmi, et le workflow
`release.yml` appelle `notes`.
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
NON_PUBLIEE = "## Non publiée — "
# GitHub refuse le corps d'une release au-delà de 125 000 caractères.
LIMITE_GITHUB = 120_000
_VERSION = re.compile(r"v\d+\.\d+\.\d+")


class Refus(Exception):
    pass


def _verifier(version: str) -> None:
    if not _VERSION.fullmatch(version):
        raise Refus(f"« {version} » n'est pas un numéro vX.Y.Z.")


def _titres(lignes: list[str]) -> list[int]:
    titres, dans_un_bloc = [], False
    for i, ligne in enumerate(lignes):
        if ligne.startswith("```"):
            dans_un_bloc = not dans_un_bloc
        elif not dans_un_bloc and ligne.startswith("## "):
            titres.append(i)
    return titres


def preparer(texte: str, version: str, jour: str) -> str:
    _verifier(version)
    lignes = texte.split("\n")
    titres = _titres(lignes)
    if any(lignes[i].startswith(f"## {version} ") or lignes[i] == f"## {version}" for i in titres):
        raise Refus(f"{version} existe déjà dans le CHANGELOG.")
    if not titres or not lignes[titres[0]].startswith(NON_PUBLIEE):
        raise Refus("aucune section « Non publiée » en tête du CHANGELOG : rien à publier.")
    # Les sections « Non publiée » contiguës du haut, jusqu'à la première version : une
    # section plus bas appartient à l'histoire d'avant, on n'y touche pas.
    fin = next((i for i in titres if not lignes[i].startswith(NON_PUBLIEE)), len(lignes))
    corps, dans_un_bloc = [], False
    for ligne in lignes[titres[0]:fin]:
        if ligne.startswith("```"):
            dans_un_bloc = not dans_un_bloc
        # Dans un bloc de code, `# …` est un commentaire de shell, pas un titre.
        if dans_un_bloc:
            pass
        elif ligne.startswith(NON_PUBLIEE):
            titre = ligne[len(NON_PUBLIEE):]
            ligne = "### " + titre[:1].upper() + titre[1:]
        elif ligne.startswith("#"):
            ligne = "#" + ligne
        corps.append(ligne)
    while corps and corps[-1] == "":
        corps.pop()
    bloc = [f"## {version} — {jour}", ""] + corps + [""]
    return "\n".join(lignes[:titres[0]] + bloc + lignes[fin:])


def notes(texte: str, version: str) -> str:
    _verifier(version)
    lignes = texte.split("\n")
    titres = _titres(lignes)
    debut = next((i for i in titres if lignes[i].startswith(f"## {version} ")
                  or lignes[i] == f"## {version}"), None)
    if debut is None:
        raise Refus(f"pas de section {version} dans le CHANGELOG : lancer d'abord `preparer`.")
    fin = next((i for i in titres if i > debut), len(lignes))
    resultat = "\n".join(lignes[debut + 1:fin]).strip() + "\n"
    if len(resultat) > LIMITE_GITHUB:
        suite = "\n\n…\n\nLa suite : CHANGELOG.md\n"
        coupe = resultat[:LIMITE_GITHUB - len(suite)]
        resultat = coupe[:coupe.rfind("\n")] + suite
    return resultat


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("action", choices=["preparer", "notes"])
    p.add_argument("version")
    p.add_argument("--changelog", type=Path, default=CHANGELOG)
    p.add_argument("--date", help="JJ/MM/AAAA (défaut : aujourd'hui)")
    args = p.parse_args(argv)
    try:
        texte = args.changelog.read_text(encoding="utf-8")
        if args.action == "notes":
            sys.stdout.write(notes(texte, args.version))
        else:
            jour = args.date or date.today().strftime("%d/%m/%Y")
            args.changelog.write_text(preparer(texte, args.version, jour), encoding="utf-8")
            print(f"{args.version} préparée dans {args.changelog.name}.")
    except Refus as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
