"""Graphiques en barres de l'admin (admin/charts.py) : la valeur au-dessus des barres.

SCRUM-113 (28/09/2026) : sur les graphiques de 30 jours (« Appels par jour »,
« Réservations par jour »), seuls le maximum et le dernier jour étaient écrits ; Helmi
lisait le reste à l'œil.
"""
import re

from app.admin import charts


def _valeurs(svg: str) -> list[tuple[float, str]]:
    return [(float(x), v) for x, v in re.findall(
        r'class="viz-valeur" x="([\d.]+)"[^>]*>([^<]+)</text>', str(svg))]


def test_trente_jours_une_valeur_par_barre_non_nulle():
    points = [(f"{j:02d}/09", (j * 7) % 13) for j in range(1, 31)]
    valeurs = _valeurs(charts.bar_chart(points, title="Appels par jour"))
    non_nuls = [v for _, v in points if v]
    assert [v for _, v in valeurs] == [f"{v:g}" for v in non_nuls]


def test_rien_sur_une_barre_a_zero():
    valeurs = _valeurs(charts.bar_chart([("a", 0), ("b", 3), ("c", 0)], title="t"))
    assert [v for _, v in valeurs] == ["3"]


def test_les_valeurs_ne_se_chevauchent_pas_sur_trente_jours():
    points = [(str(j), 120) for j in range(30)]
    xs = [x for x, _ in _valeurs(charts.bar_chart(points, title="t"))]
    # « 120 » en taille 10 fait ~18 unités : deux centres doivent en être plus loin.
    assert min(b - a for a, b in zip(xs, xs[1:])) >= 18


def test_au_dela_le_maximum_et_le_dernier_seulement():
    points = [(str(j), j % 5 + 1) for j in range(90)]
    assert len(_valeurs(charts.bar_chart(points, title="t"))) <= 2


# ---- Survol (ASSISTANTE-115) : chaque jour porte sa date et sa valeur en toutes lettres.

def _barres(svg) -> list[dict]:
    return [dict(re.findall(r'([\w-]+)="([^"]*)"', g)) for g in re.findall(
        r'<g class="viz-barre"([^>]*)>', str(svg))]


def test_chaque_jour_porte_sa_date_et_sa_valeur():
    points = [("09-22", 0, "Lundi 22 septembre 2026"), ("09-23", 1, "Mardi 23 septembre 2026"),
              ("09-24", 12, "Mercredi 24 septembre 2026")]
    barres = _barres(charts.bar_chart(points, title="Appels par jour",
                                      unite=("appel", "appels")))
    assert [(b["data-quand"], b["data-valeur"]) for b in barres] == [
        ("Lundi 22 septembre 2026", "0 appel"), ("Mardi 23 septembre 2026", "1 appel"),
        ("Mercredi 24 septembre 2026", "12 appels")]
    assert barres[2]["aria-label"] == "Mercredi 24 septembre 2026 : 12 appels"


def test_un_jour_a_zero_se_survole_aussi():
    """Pas de barre à dessiner, mais une colonne à viser : « 0 appel » est une réponse."""
    svg = str(charts.bar_chart([("a", 0), ("b", 3)], title="t"))
    groupes = re.findall(r'<g class="viz-barre".*?</g>', svg)
    assert len(groupes) == 2
    assert 'class="viz-cible"' in groupes[0] and 'class="viz-marque"' not in groupes[0]
    assert 'class="viz-marque"' in groupes[1]


def test_la_cible_couvre_la_colonne_entiere():
    svg = str(charts.bar_chart([("a", 1), ("b", 30)], title="t", height=180))
    hauteurs = {float(h) for h in re.findall(r'class="viz-cible"[^>]*height="([\d.]+)"', svg)}
    assert hauteurs == {180 - 22 - 26}  # toute la zone de tracé, quelle que soit la valeur


def test_un_seul_arret_de_tabulation_le_dernier_jour():
    barres = _barres(charts.bar_chart([(str(j), j) for j in range(30)], title="t"))
    assert [b["tabindex"] for b in barres].count("0") == 1 and barres[-1]["tabindex"] == "0"


def test_le_libelle_est_echappe():
    svg = str(charts.bar_chart([("a", 2, '<b onclick="x">')], title="t"))
    assert "<b onclick" not in svg and "&lt;b onclick=&quot;x&quot;&gt;" in svg


def test_la_bulle_est_branchee():
    from pathlib import Path

    import app.admin as admin

    script = (Path(admin.__file__).parent / "static" / "admin.js").read_text()
    assert '".viz-barre"' in script and "dataset.valeur" in script
    assert "textContent" in script  # jamais innerHTML : le libellé vient de la base


def test_les_graphiques_du_tableau_de_bord_ont_des_dates_en_lettres():
    from app.admin.routes_dashboard import _fill_days

    points = _fill_days([], 3, "n_calls")
    assert len(points) == 3 and all(len(p) == 3 for p in points)
    assert re.fullmatch(r"[A-Z][a-zé]+ \d{1,2} [a-zéû]+ \d{4}", points[-1][2])
