---
name: admin-ui
description: Conventions de la plateforme admin (FastAPI + Jinja2 + htmx) — à charger AVANT d'ajouter ou modifier une page, route, gabarit, style ou script de l'admin. Sécurité (CSRF, cloisonnement), motifs d'interface, heures, thème, pièges connus, et comment vérifier un rendu.
---

# Plateforme admin — conventions

L'admin vit dans `api/app/admin/` : routes (`routes_*.py`), templates Jinja2
(`templates/`), assets vendorés (`static/` — Pico.css v2 + htmx 1.9, PAS de CDN).
Server-rendered, même app/conteneur que l'API vocale. Un seul worker uvicorn.

## Règles de sécurité NON NÉGOCIABLES (toute nouvelle route)

1. **Jamais de middleware d'auth global** : l'auth passe par les dépendances.
   Les webhooks Twilio, `/ws/voice`, `/health` ne doivent JAMAIS exiger session/CSRF.
2. Route protégée → elle est DANS `admin_router` (qui porte `Depends(current_user)`).
   Route super-admin → ajouter `Depends(deps.require_superadmin)`.
3. **Tout POST** porte `dependencies=[Depends(deps.verify_csrf)]`. Les forms classiques
   embarquent `<input type="hidden" name="csrf_token" value="{{ request.session.get('csrf', '') }}">` ;
   les appels htmx sont couverts par le `hx-headers` posé sur `<body>` (base.html).
4. **Scoping tenant** : jamais de `tenant_id` pris tel quel — passer par
   `deps.resolve_tenant(tenant_id, user)` (force/vérifie le tenant du restaurateur).
   Pour un OBJET (réservation, appel) : charger l'objet puis
   `deps.check_tenant_access(user, obj["tenant_id"])`.
5. `FileResponse` : uniquement des chemins déterministes construits côté serveur
   (cache greeting, `hold_music_dir()/tenant{id}.wav`) — jamais un nom de fichier client.
6. bcrypt (verify/hash/create_user) → TOUJOURS `await asyncio.to_thread(...)` :
   l'event loop sert des appels vocaux en parallèle.

## Patterns UI

- **Page** : template qui `{% extends "base.html" %}` + `{% block title %}` +
  `{% block content %}`. La nav (base.html) est conditionnelle au rôle via
  `request.state.user`.
- **Fragment htmx** : fichier préfixé `_` (ex. `reservations/_row.html`), rendu par une
  route GET dédiée, PAS d'extends.
- **Édition inline** (voir `reservations/`) : `reservations/_row.html` a un bouton
  `hx-get=".../{id}/edit" hx-target="closest tr" hx-swap="outerHTML"` ;
  `reservations/_row_edit.html` est un `<tr>` formulaire `hx-post` qui re-rend la ligne ;
  « Annuler » = `hx-get=".../{id}/row"`. Suppression :
  `hx-post=".../delete" hx-confirm="..." hx-target="closest tr" hx-swap="delete swap:0.2s"`
  et la route renvoie `HTMLResponse("")`.
- **Polling** : fragment avec `hx-trigger="every 3s"` qui s'auto-remplace quand l'état
  change (voir `voice/_greeting_status.html`).
- **Redirects post-POST** : `RedirectResponse(url, status_code=303)` (jamais 302).
- Longues opérations (rendu de l'accueil) : `taches.lancer(coro, nom=…)` + polling UI,
  jamais d'await dans la route, jamais `asyncio.create_task` nu.
- **Aucun script en ligne** (la CSP de Caddy le bloque, attributs `onclick` compris) :
  tout comportement va dans `static/admin.js`, branché par délégation sur `document`.
- htmx n'échange que les réponses 2xx ; `admin.js` laisse passer **422** (saisie refusée)
  et **409** (conflit) pour que le fragment d'erreur s'affiche.
- Un état n'est jamais porté par la seule couleur : le mot est écrit (`chip` + libellé).
- Carte entièrement cliquable (voir `reservations/_carte.html`) : le lien du titre
  s'étend par `::after` ; boutons et texte à copier passent au-dessus (`z-index: 1`).
- Fichiers statiques : `{{ statique('admin.css') }}` (empreinte du contenu, sinon le
  navigateur garde l'ancienne version après un déploiement).

## Graphiques

`admin/charts.py` : SVG rendu par le serveur, complet sans script (chaque barre garde son
`<title>`). `admin.js` y ajoute la bulle du survol, à partir des attributs `data-quand` /
`data-valeur` de chaque groupe `.viz-barre` — texte inséré par `textContent`. Charger le
skill `dataviz` avant de créer un nouveau type de graphique. Couleurs par variables CSS `--viz-series-1` (orange,
appels) / `--viz-series-2` (bleu canard, réservations) définies dans `static/admin.css`
(clair + sombre). Le texte porte l'encre texte (`--viz-text*`), jamais la couleur de
série. Une série par graphique → pas de légende, le titre nomme la série. Jamais de
double axe. Labels de valeur visibles. Changer une couleur de série : repasser le
validateur du skill `dataviz` sur la surface claire ET la surface sombre.

## Données

- Accès SQLite : TOUJOURS via les modules `api/app/tenants.py`, `api/app/reservations.py`,
  `api/app/users.py`, `api/app/calls.py` (jamais de SQL dans les routes).
- Nouveau champ/table → migration dans `db.py` `_MIGRATIONS` (append un script
  idempotent, `PRAGMA user_version` s'incrémente tout seul). Ne JAMAIS éditer un
  script de migration déjà livré.
- `get_conn()` est en WAL + busy_timeout : ne pas changer.
- Route `async` : tout accès à la base passe par `await db.hors_boucle(fn, …)`, y compris
  `deps.resolve_tenant`. Une route `def` simple tourne déjà dans le pool de fils.
  `test_db_hors_boucle.py` liste les pages : y ajouter toute nouvelle page.
- **Heures** : la base est en UTC. Dans un gabarit, un horodatage passe par
  `| date_paris` (AAAA-MM-JJ), `| jour_paris` (JJ/MM/AAAA) ou `| heure_paris` ; jamais
  `x_at[:10]`. `presenters.call_view` rend déjà `date_label` / `time_label` à Paris.
- Aucun chiffre inventé : ce qui s'affiche se lit en base ou se mesure.

## Thème clair / sombre

Un seul jeu de variables `--app-*` dans `static/admin.css`. Le sombre existe en DEUX
copies qui doivent rester identiques — `@media (prefers-color-scheme: dark)` et
`:root[data-theme="dark"]` (thème choisi) — de même pour `.viz-root`. `test_theme.py`
compare les deux. Pas de couleur en dur dans un gabarit : `var(--app-…)`.

L'admin porte la palette du site (`api/app/site/static/site.css`) : brun nuit et orange vif
en sombre, crème et orange brûlé en clair (l'orange vif ne tient pas 4,5:1 en texte sur
blanc). Ce qui est posé sur l'accent prend `var(--app-accent-ink)`, jamais `#fff`. Les
variables de Pico se redéfinissent dans le bloc `:root:root:root` : sous un simple `:root`,
Pico reprend la main en clair. Polices Inter et Inter Tight, servies depuis `/site/static/`.

## Téléphone

Sous 900 px la barre latérale devient une barre du haut : la marque, et un bouton
`[data-menu]` qui déroule `.menu` (navigation, thème, compte). `admin.js` pose `data-ouvert`
sur `.sidebar` et `aria-expanded` sur le bouton ; Échap et un toucher hors de la barre
referment. Tout nouvel élément de la barre latérale va DANS `.menu`.

## Pièges déjà rencontrés

- **Pico** stylise `role="group"` comme un groupe de boutons, et `[type="submit"]` a une
  spécificité plus forte qu'une classe : préfixer le sélecteur (`button.logout`) ou
  donner la classe `outline` / `secondary`.
- **FastAPI** rend `None` pour un champ de formulaire VIDE comme pour un champ absent :
  pour distinguer « vidé » de « non envoyé », lire `await request.form()`.
- Une variable de contexte qui porte le nom d'une fonction globale de gabarit
  (`carnet_resos`, `statique`, `theme_choisi`…) la masque : `'bool' object is not callable`.
- `str.isdigit()` accepte « ² » : pour un identifiant d'URL, `isascii() and isdigit()`,
  et borner la longueur (SQLite déborde au-delà de 18 chiffres).
- Ne jamais affirmer dans une page ce que la base ne dit pas (« première réservation »,
  « saisie à la main ») : écrire ce qu'on sait.

## Vérifier un rendu

Une page ne se juge pas à ses tests : la regarder, en clair et en sombre, ordinateur et
téléphone. Copier `api/app` et `api/tests` dans un dossier temporaire, y ajouter un test
qui écrit les pages dans `/out` (adresses `/admin/static/x?v=…` réécrites en `x`, fichiers
de `static/` copiés à côté), puis :

```bash
docker run --rm --user root -v "$PWD:/out" --entrypoint chromium-browser zenika/alpine-chrome \
  --headless --no-sandbox --disable-gpu --hide-scrollbars --window-size=1280,1100 \
  --virtual-time-budget=3000 --screenshot=/out/page.png file:///out/page.html
```

`--window-size=390,1300` pour un téléphone. Un fragment chargé par htmx (les graphiques)
s'insère à la main dans la page. Pour éprouver un comportement de `admin.js` (survol,
bascule), ajouter un `<script>` qui envoie les événements et écrit le résultat dans la
page, puis lire `--dump-dom`.

## Tests

Pattern : `TestClient(app)` avec fixture `client()` FRAÎCHE par test (isolation des
cookies). Login via POST `/admin/login` (compte de test : `admin@test.local` /
`test-admin-pass`, semé par `conftest.py`). CSRF en test : décoder le cookie session
(base64 du payload avant le premier point) pour lire `csrf`, l'envoyer en header
`X-CSRF-Token`. Toujours inclure un test 403 cross-tenant pour toute nouvelle
ressource scopée. Jinja échappe l'apostrophe (`&#39;`) : la remplacer avant de chercher
un texte dans la page. Lancer les tests : commande de `CLAUDE.md` (l'image ne contient pas
les tests, on monte le dépôt).
