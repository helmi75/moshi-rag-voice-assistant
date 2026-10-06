"""Graphiques SVG server-rendered (zéro dépendance).

Le rendu est complet sans script : chaque jour garde son <title>, l'infobulle du
navigateur. admin.js la remplace par sa bulle (ASSISTANTE-115) : chaque jour est un groupe
`.viz-barre` qui porte sa date et sa valeur en toutes lettres (`data-quand`,
`data-valeur`), avec une cible de la hauteur du graphique — un jour à zéro se survole
aussi, et viser une barre de 19 unités au doigt ne suffirait pas.

Conventions (skill dataviz) :
- une série par graphique → pas de légende, le titre nomme la série ;
- le titre est une <figcaption>, hors du dessin : il garde sa taille quand le graphique
  rétrécit sur un téléphone, et la valeur de la plus haute barre ne peut pas s'écrire
  dessus (recette du 05/10/2026 : un « 6 » sur « Couverts réservés par créneau ») ;
- couleurs par variables CSS (--viz-series-*) définies dans admin.css pour les deux
  thèmes ; le texte porte l'encre texte (muted), jamais la couleur de série ;
- barres fines, sommet arrondi 4px ancré à la baseline, écart 2px minimum ;
- labels de valeur directs + la bulle au survol (depuis le 04/10/2026 les deux séries,
  orange et bleu canard, tiennent 3:1 sur leur surface ; les valeurs restent). Une valeur
  au-dessus de CHAQUE barre non nulle tant qu'elle y tient (SCRUM-113 : Helmi lisait
  30 jours à l'œil, seuls le maximum et le dernier jour étaient écrits) ;
- axe/grille en retrait (une baseline discrète), pas de double axe.
"""
from html import escape

from markupsafe import Markup

_W, _H = 640, 180
_MARGIN_L, _MARGIN_B, _MARGIN_T = 8, 22, 26
# Au-delà, le graphique est « dense » : sur un téléphone il garde une largeur lisible et
# défile dans sa carte (admin.css, `.viz-dense`). En deçà, les colonnes sont assez larges
# pour que ses textes grossissent sans se toucher.
_DENSE = 16


def _top_rounded_bar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Rect à coins supérieurs arrondis, ancré à la baseline (jamais le bas)."""
    r = min(r, w / 2, h)  # un bar minuscule ne doit pas s'inverser
    if h <= 0:
        return ""
    return (
        f'M{x:.1f},{y + h:.1f} v{-(h - r):.1f} q0,{-r} {r},{-r} '
        f'h{w - 2 * r:.1f} q{r},0 {r},{r} v{h - r:.1f} z'
    )


def _compte(valeur: float, unite: tuple[str, str]) -> str:
    """« 12 appels », « 1 réservation », « 0 appel » : singulier jusqu'à 1, comme en
    français."""
    singulier, pluriel = unite
    return f"{valeur:g} {singulier if valeur <= 1 else pluriel}".strip()


def bar_chart(points: list[tuple], *, title: str, series: int = 1,
              width: int = _W, height: int = _H, tone: str = "light",
              unite: tuple[str, str] = ("", ""),
              empty_label: str = "Aucune donnée sur la période.") -> Markup:
    """Bar chart une série. points = [(label, valeur)] ou [(label, valeur, libellé
    long)] : le label court va sur l'axe, le long (« Mardi 23 septembre 2026 ») dans la
    bulle du survol. `unite` = (singulier, pluriel) de la valeur dans la bulle.
    series = slot catégoriel (1|2).

    `tone="dark"` = le graphique est posé sur une surface sombre volontaire (la carte
    « appels du parc »), qui ne suit pas le thème : il bascule alors sur les variables
    `--viz-*-on-dark`, des pas choisis pour CE fond — jamais une inversion automatique.
    """
    suffix = "-on-dark" if tone == "dark" else ""
    if not points:
        return Markup(
            f'<figure class="viz-root viz-{escape(tone)}">'
            f'<figcaption>{escape(title)}</figcaption>'
            f'<p class="muted">{escape(empty_label)}</p></figure>'
        )
    n = len(points)
    vmax = max(p[1] for p in points) or 1
    plot_w = width - _MARGIN_L * 2
    plot_h = height - _MARGIN_B - _MARGIN_T
    gap = 2 if n <= 40 else 1
    bar_w = max(3.0, (plot_w - gap * (n - 1)) / n)
    baseline_y = _MARGIN_T + plot_h

    # Labels d'axe X : ~6 ticks maxi pour éviter les collisions.
    step = max(1, round(n / 6))
    # Une valeur par barre tant que la barre est assez large pour « 999 » en taille 10
    # (30 jours tiennent : ~19 unités par barre) ; au-delà, maximum + dernier seulement.
    show_value = bar_w + gap >= 14
    max_i = max(range(n), key=lambda i: points[i][1])

    # role="group" et non "img" : un « img » rend ses enfants muets, or chaque jour est
    # un élément que le clavier atteint.
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="group" '
        f'aria-label="{escape(title)} : {n} jours, maximum {vmax:g}" '
        f'style="width:100%;height:auto;font-family:inherit">',
        # Baseline discrète (grille en retrait).
        f'<line x1="{_MARGIN_L}" y1="{baseline_y}" x2="{width - _MARGIN_L}" '
        f'y2="{baseline_y}" stroke="var(--viz-grid{suffix})" stroke-width="1"/>',
    ]
    for i, point in enumerate(points):
        label, value = point[0], point[1]
        quand = escape(point[2] if len(point) > 2 else label)
        texte = escape(_compte(value, unite))
        x = _MARGIN_L + i * (bar_w + gap)
        h = plot_h * (value / vmax)
        y = baseline_y - h
        path = _top_rounded_bar(x, y, bar_w, h)
        # Un seul arrêt de tabulation par graphique, le dernier jour : les flèches font
        # le reste (admin.js).
        parts.append(
            f'<g class="viz-barre" role="img" tabindex="{0 if i == n - 1 else -1}" '
            f'aria-label="{quand} : {texte}" data-quand="{quand}" data-valeur="{texte}">'
            # Repli sans script : sur 90 jours, seuls le maximum et le dernier jour sont
            # écrits — sans ce <title>, les autres valeurs seraient hors d'atteinte.
            f'<title>{quand} : {texte}</title>'
            f'<rect class="viz-cible" x="{x - gap / 2:.1f}" y="{_MARGIN_T}" '
            f'width="{bar_w + gap:.1f}" height="{plot_h}" fill="transparent"/>'
            + (f'<path class="viz-marque" d="{path}" '
               f'fill="var(--viz-series-{series}{suffix})"/>' if path else "")
            + '</g>'
        )
        if value and (show_value or i == max_i or i == n - 1):
            parts.append(
                f'<text class="viz-valeur" x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" '
                f'text-anchor="middle" fill="var(--viz-text-muted{suffix})" font-size="10">'
                f'{value:g}</text>'
            )
        if i % step == 0 or i == n - 1:
            parts.append(
                f'<text class="viz-axe" x="{x + bar_w / 2:.1f}" y="{baseline_y + 14}" text-anchor="middle" '
                f'fill="var(--viz-text-muted{suffix})" font-size="10">{escape(label)}</text>'
            )
    parts.append("</svg>")
    dense = " viz-dense" if n > _DENSE else ""
    return Markup(
        f'<figure class="viz-root viz-{escape(tone)}{dense}">'
        f'<figcaption>{escape(title)}</figcaption>'
        f'<div class="chart-block">{"".join(parts)}</div></figure>'
    )
