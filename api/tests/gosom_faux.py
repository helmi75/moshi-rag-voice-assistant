"""Un faux service gosom pour les tests (ASSISTANTE-189) : aucun réseau, un transport httpx
simulé qui rejoue le CSV relevé sur un vrai service le 15/11/2026 (données de personnes
remplacées par des chaînes fictives, que les tests cherchent partout)."""
import pathlib
from types import SimpleNamespace

import httpx

CSV = pathlib.Path(__file__).parent / "fixtures" / "gosom_cuba.csv"

# Ce que le CSV porte et que Helmane ne doit JAMAIS garder, ni afficher, ni journaliser.
INTERDITS = ("AVIS-FICTIF-NE-DOIT-JAMAIS-ETRE-GARDE", "PROPRIETAIRE-FICTIF",
             "contact-fictif@exemple.invalid", "Jeanne Fictive", "exemple.invalid")


def faux_gosom(csv_texte: str | None = None, *, etats=("ok",), telechargement=200,
               reponse_creation=None):
    """(transport, appels) : `appels` liste les « METHODE chemin » reçus, dans l'ordre.
    `etats` : les états rendus successivement par le relevé (le dernier se répète)."""
    appels: list[str] = []
    suite = list(etats)
    contenu = CSV.read_text(encoding="utf-8") if csv_texte is None else csv_texte

    def handler(requete: httpx.Request) -> httpx.Response:
        chemin = requete.url.path
        appels.append(f"{requete.method} {chemin}")
        if requete.method == "POST" and chemin == "/api/v1/jobs":
            return reponse_creation or httpx.Response(200, json={"id": "job-1"})
        if requete.method == "DELETE":
            return httpx.Response(200, json={})
        if chemin.endswith("/download"):
            return httpx.Response(telechargement, content=contenu.encode())
        etat = suite.pop(0) if len(suite) > 1 else suite[0]
        return httpx.Response(200, json={"ID": "job-1", "Status": etat})

    return httpx.MockTransport(handler), SimpleNamespace(liste=appels)
