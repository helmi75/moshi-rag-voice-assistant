"""Graphiques SVG server-rendered (zéro JS, zéro dépendance).

Conventions (skill dataviz) :
- une série par graphique → pas de légende, le titre nomme la série ;
- couleurs par variables CSS (--viz-series-*) définies dans admin.css pour les deux
  thèmes ; le texte porte l'encre texte (muted), jamais la couleur de série ;
- barres fines, sommet arrondi 4px ancré à la baseline, écart 2px minimum ;
- labels de valeur directs (règle de « relief » : la série aqua est sous 3:1 sur
  fond clair, donc les valeurs sont affichées) + <title> natif au survol ;
- axe/grille en retrait (une baseline discrète), pas de double axe.
"""
from html import escape

from markupsafe import Markup

_W, _H = 640, 180
_MARGIN_L, _MARGIN_B, _MARGIN_T = 8, 22, 26


def _top_rounded_bar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Rect à coins supérieurs arrondis, ancré à la baseline (jamais le bas)."""
    r = min(r, w / 2, h)  # un bar minuscule ne doit pas s'inverser
    if h <= 0:
        return ""
    return (
        f'M{x:.1f},{y + h:.1f} v{-(h - r):.1f} q0,{-r} {r},{-r} '
        f'h{w - 2 * r:.1f} q{r},0 {r},{r} v{h - r:.1f} z'
    )


def bar_chart(points: list[tuple[str, float]], *, title: str, series: int = 1,
              width: int = _W, height: int = _H) -> Markup:
    """Bar chart une série. points = [(label, valeur)]. series = slot catégoriel (1|2)."""
    if not points:
        return Markup(
            f'<figure class="viz-root"><figcaption>{escape(title)}</figcaption>'
            f'<p class="muted">Aucune donnée sur la période.</p></figure>'
        )
    n = len(points)
    vmax = max(v for _, v in points) or 1
    plot_w = width - _MARGIN_L * 2
    plot_h = height - _MARGIN_B - _MARGIN_T
    gap = 2 if n <= 40 else 1
    bar_w = max(3.0, (plot_w - gap * (n - 1)) / n)
    baseline_y = _MARGIN_T + plot_h

    # Labels d'axe X : ~6 ticks maxi pour éviter les collisions.
    step = max(1, round(n / 6))
    show_value = n <= 20  # au-delà : seulement max + dernier (labels sélectifs)
    max_i = max(range(n), key=lambda i: points[i][1])

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(title)} : {n} jours, maximum {vmax:g}" '
        f'style="width:100%;height:auto;font-family:inherit">',
        # Baseline discrète (grille en retrait).
        f'<line x1="{_MARGIN_L}" y1="{baseline_y}" x2="{width - _MARGIN_L}" '
        f'y2="{baseline_y}" stroke="var(--viz-grid)" stroke-width="1"/>',
        f'<text x="{_MARGIN_L}" y="16" fill="var(--viz-text)" font-size="13" '
        f'font-weight="600">{escape(title)}</text>',
    ]
    for i, (label, value) in enumerate(points):
        x = _MARGIN_L + i * (bar_w + gap)
        h = plot_h * (value / vmax)
        y = baseline_y - h
        path = _top_rounded_bar(x, y, bar_w, h)
        if path:
            parts.append(
                f'<path d="{path}" fill="var(--viz-series-{series})">'
                f'<title>{escape(label)} : {value:g}</title></path>'
            )
        if value and (show_value or i == max_i or i == n - 1):
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle" '
                f'fill="var(--viz-text-muted)" font-size="10">{value:g}</text>'
            )
        if i % step == 0 or i == n - 1:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{baseline_y + 14}" text-anchor="middle" '
                f'fill="var(--viz-text-muted)" font-size="10">{escape(label)}</text>'
            )
    parts.append("</svg>")
    return Markup(
        f'<figure class="viz-root chart-block">{"".join(parts)}</figure>'
    )
