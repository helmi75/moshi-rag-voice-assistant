#!/usr/bin/env python3
"""L'état de la CI d'un commit, l'ouverture d'une PR et sa fusion, sans quitter le terminal.

Pourquoi : le 24/10/2026, la PR 150 a été fusionnée alors qu'une exécution de la CI
tournait encore. On regardait la liste des vérifications du commit, et un job qui attend
un autre job (`image` attend `tests`) n'y figure qu'une fois lancé. Ce script lit aussi
les exécutions des workflows, et `fusionner` refuse tant qu'une seule n'est pas terminée
et verte, sur le commit de tête de la PR — celui que la CI a testé.

Sans dépendance. Le jeton est celui dont git se sert pour pousser (`~/.git-credentials`) :
il n'est jamais affiché, ni écrit ailleurs.

    python3 scripts/depot_github.py ci HEAD                   # état, ne modifie rien
    python3 scripts/depot_github.py ci 5719194 --attendre     # jusqu'à la fin de toutes les exécutions
    python3 scripts/depot_github.py pr --titre "…" --corps-fichier corps.md   # branche courante → main
    python3 scripts/depot_github.py fusionner 153 --attendre  # commit de fusion, sur le commit testé

Code de sortie : 0 vert (ou fait), 1 rouge (ou refusé), 2 encore en cours au bout du délai.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEPOT = "helmi75/moshi-rag-voice-assistant"
API = "https://api.github.com"

# Ce que GitHub rend pour un job terminé qui n'a pas échoué. Un job sauté est normal : `image`
# ne tourne que sur un push.
CONCLUSIONS_VERTES = {"success", "skipped", "neutral"}

# La CI du code ; les autres événements (la sonde planifiée) disent autre chose.
EVENEMENTS_CI = ("push", "pull_request")

VERT, ROUGE, EN_COURS, AUCUNE = "vert", "rouge", "en cours", "aucune exécution"


class RefusGitHub(Exception):
    """Ce que GitHub refuse, ou ce que ce script refuse de faire. Jamais le jeton dans le message."""


def lire_jeton(fichier: Path | None = None) -> str:
    fichier = fichier or Path.home() / ".git-credentials"
    try:
        lignes = fichier.read_text().splitlines()
    except OSError:
        raise RefusGitHub(f"pas de fichier {fichier} : aucun jeton GitHub") from None
    for ligne in lignes:
        m = re.match(r"https://[^:]+:([^@]+)@github\.com", ligne.strip())
        if m:
            return m.group(1)
    raise RefusGitHub(f"aucun jeton GitHub dans {fichier}")


class Depot:
    """Les appels à l'API GitHub. `ouvrir` est remplaçable : les tests n'ont pas de réseau."""

    def __init__(self, jeton: str, depot: str = DEPOT, ouvrir=urllib.request.urlopen):
        self._jeton = jeton
        self.depot = depot
        self._ouvrir = ouvrir

    def appeler(self, methode: str, chemin: str, corps: dict | None = None):
        """(statut, réponse décodée). Un refus HTTP est rendu, pas levé : l'appelant décide."""
        requete = urllib.request.Request(
            f"{API}/repos/{self.depot}{chemin}", method=methode,
            data=json.dumps(corps).encode() if corps is not None else None,
            headers={"Authorization": f"Bearer {self._jeton}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self._ouvrir(requete, timeout=30) as reponse:
                brut = reponse.read()
                return reponse.status, json.loads(brut) if brut else {}
        except urllib.error.HTTPError as e:
            brut = e.read()
            try:
                return e.code, json.loads(brut) if brut else {}
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RefusGitHub(f"GitHub injoignable ({type(e).__name__})") from None

    def lire(self, chemin: str) -> dict:
        statut, corps = self.appeler("GET", chemin)
        if statut != 200:
            raise RefusGitHub(f"GET {chemin} : {statut} {corps.get('message', '')}".rstrip())
        return corps

    def commit_complet(self, ref: str) -> str:
        """Un commit abrégé, une branche ou HEAD → l'empreinte complète (la seule que
        `actions/runs?head_sha=` comprenne)."""
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            try:
                # `--end-of-options` : « --show-toplevel » est un nom de commit, pas une option.
                ref = subprocess.run(["git", "rev-parse", "--verify", "--end-of-options",
                                      f"{ref}^{{commit}}"], check=True, capture_output=True,
                                     text=True).stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                ref = self.lire(f"/commits/{urllib.parse.quote(ref, safe='')}").get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise RefusGitHub("ce n'est pas un commit")
        return ref

    def etat_ci(self, sha: str, *, exiger_pull_request: bool = False) -> dict:
        """Les exécutions de la CI sur ce commit (workflows et jobs), et le verdict.

        La sonde de supervision est un workflow planifié : chacune de ses exécutions se
        rattache au dernier commit de `main`, et dit l'état de la production, pas celui du
        code (ASSISTANTE-168). Seuls comptent les workflows lancés par un push ou une PR, et
        les jobs de ceux-là. Le filtre par événement garde aussi la lecture courte : la
        sonde ajoute jusqu'à 96 exécutions par jour à la tête de `main`."""
        flux = []
        for evenement in EVENEMENTS_CI:
            flux += self.lire(f"/actions/runs?head_sha={sha}&event={evenement}&per_page=100"
                              ).get("workflow_runs", [])
        suites = {f.get("check_suite_id") for f in flux}
        jobs, page = [], 1
        while True:
            corps = self.lire(f"/commits/{sha}/check-runs?per_page=100&page={page}")
            lot = corps.get("check_runs", [])
            jobs += [r for r in lot if (r.get("app") or {}).get("slug") == "github-actions"
                     and (r.get("check_suite") or {}).get("id") in suites]
            if len(lot) < 100:
                break
            page += 1
        elements = ([{"genre": "workflow", "nom": f["name"], "evenement": f.get("event", ""),
                      "statut": f["status"], "conclusion": f.get("conclusion")} for f in flux]
                    + [{"genre": "job", "nom": j["name"], "evenement": "",
                        "statut": j["status"], "conclusion": j.get("conclusion")} for j in jobs])
        etat = verdict(elements)
        # Juste après l'ouverture d'une PR, GitHub n'a pas encore créé l'exécution qui teste
        # le résultat de la fusion : la CI du push, verte, ne suffit pas pour fusionner.
        if exiger_pull_request and etat == VERT and not any(
                f.get("event") == "pull_request" for f in flux):
            etat = EN_COURS
        return {"sha": sha, "verdict": etat, "elements": elements}

    def attendre_ci(self, sha: str, delai: float = 1200, pause: float = 20,
                    dormir=time.sleep, horloge=time.monotonic, **options) -> dict:
        """Jusqu'à ce que tout soit terminé. Un rouge précoce n'arrête pas l'attente : on rend
        compte de toutes les exécutions, pas de la première qui échoue."""
        fin = horloge() + delai
        while True:
            etat = self.etat_ci(sha, **options)
            if etat["verdict"] in (VERT, ROUGE) or horloge() >= fin:
                return etat
            dormir(pause)

    def ouvrir_pr(self, branche: str, titre: str, corps: str, base: str = "main") -> dict:
        statut, rep = self.appeler("POST", "/pulls", {"title": titre, "head": branche,
                                                      "base": base, "body": corps})
        if statut == 201:
            return {"numero": rep["number"], "url": rep["html_url"], "nouvelle": True}
        if statut == 422:
            proprio = self.depot.split("/")[0]
            ouvertes = self.lire(f"/pulls?state=open&head={proprio}:{urllib.parse.quote(branche)}")
            if ouvertes:
                return {"numero": ouvertes[0]["number"], "url": ouvertes[0]["html_url"],
                        "nouvelle": False}
        raise RefusGitHub(f"PR refusée : {statut} {rep.get('message', '')}".rstrip())

    def fusionner(self, numero: int, *, attendre: bool = False, **attente) -> dict:
        """Commit de fusion, sur le commit de tête que la CI a testé. Refus sinon."""
        pr = self.lire(f"/pulls/{numero}")
        if pr.get("state") != "open" or pr.get("merged"):
            raise RefusGitHub(f"PR {numero} : pas ouverte ({pr.get('state')})")
        sha = pr["head"]["sha"]
        etat = (self.attendre_ci(sha, exiger_pull_request=True, **attente) if attendre
                else self.etat_ci(sha, exiger_pull_request=True))
        if etat["verdict"] != VERT:
            raise RefusGitHub(f"PR {numero} : CI {etat['verdict']} sur {sha[:8]}, pas de fusion\n"
                              + resume(etat))
        # `sha` : GitHub refuse (409) si la branche a bougé depuis la lecture de la CI.
        statut, rep = self.appeler("PUT", f"/pulls/{numero}/merge",
                                   {"merge_method": "merge", "sha": sha})
        if statut != 200 or not rep.get("merged"):
            raise RefusGitHub(f"PR {numero} : fusion refusée, {statut} {rep.get('message', '')}")
        return {"numero": numero, "teste": sha, "fusion": rep["sha"]}


def verdict(elements: list[dict]) -> str:
    """Vert seulement si tout est terminé sans échec, et qu'il y a au moins une exécution :
    juste après un push, GitHub n'a encore rien créé, et « rien » n'est pas un feu vert."""
    if not elements:
        return AUCUNE
    if any(e["statut"] != "completed" for e in elements):
        return EN_COURS
    if all(e["conclusion"] in CONCLUSIONS_VERTES for e in elements):
        return VERT
    return ROUGE


def resume(etat: dict) -> str:
    lignes = [f"CI de {etat['sha'][:8]} : {etat['verdict']}"]
    for e in etat["elements"]:
        quoi = e["conclusion"] if e["statut"] == "completed" else e["statut"]
        evenement = f" ({e['evenement']})" if e["evenement"] else ""
        lignes.append(f"  {e['genre']:8} {e['nom']}{evenement} : {quoi}")
    return "\n".join(lignes)


def _branche_courante() -> str:
    return subprocess.run(["git", "branch", "--show-current"], check=True, capture_output=True,
                          text=True).stdout.strip()


def main(argv=None, depot: Depot | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sous = p.add_subparsers(dest="commande", required=True)
    ci = sous.add_parser("ci", help="état de la CI d'un commit")
    ci.add_argument("commit")
    ci.add_argument("--attendre", action="store_true")
    ci.add_argument("--delai", type=float, default=1200, help="secondes (20 min par défaut)")
    pr = sous.add_parser("pr", help="ouvrir la PR de la branche courante vers main")
    pr.add_argument("--titre", required=True)
    pr.add_argument("--corps-fichier", required=True, type=Path)
    pr.add_argument("--branche")
    fu = sous.add_parser("fusionner", help="fusionner une PR sur le commit que la CI a testé")
    fu.add_argument("numero", type=int)
    fu.add_argument("--attendre", action="store_true")
    fu.add_argument("--delai", type=float, default=1200)
    args = p.parse_args(argv)

    try:
        depot = depot or Depot(lire_jeton())
        if args.commande == "ci":
            sha = depot.commit_complet(args.commit)
            etat = depot.attendre_ci(sha, delai=args.delai) if args.attendre else depot.etat_ci(sha)
            print(resume(etat))
            return {VERT: 0, ROUGE: 1}.get(etat["verdict"], 2)
        if args.commande == "pr":
            branche = args.branche or _branche_courante()
            if branche in ("", "main"):
                raise RefusGitHub("pas de PR depuis main")
            rep = depot.ouvrir_pr(branche, args.titre, args.corps_fichier.read_text())
            print(f"PR {rep['numero']} {'ouverte' if rep['nouvelle'] else 'déjà ouverte'} : {rep['url']}")
            return 0
        rep = depot.fusionner(args.numero, attendre=args.attendre, delai=args.delai)
        print(f"PR {rep['numero']} fusionnée : commit testé {rep['teste'][:8]}, "
              f"fusion {rep['fusion'][:8]}")
        return 0
    except RefusGitHub as e:
        print(f"REFUS : {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
