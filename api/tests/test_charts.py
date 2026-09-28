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
