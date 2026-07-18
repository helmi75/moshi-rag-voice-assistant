---
name: admin-ui
description: Conventions de la plateforme admin (FastAPI + Jinja2 + htmx) — à charger AVANT d'ajouter ou modifier une page, route ou template admin. Garantit cohérence UI et sécurité (CSRF, scoping tenant).
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
- **Fragment htmx** : fichier préfixé `_` (ex. `_row.html`), rendu par une route GET
  dédiée, PAS d'extends.
- **Édition inline** (voir `reservations/`) : `_row.html` a un bouton
  `hx-get=".../{id}/edit" hx-target="closest tr" hx-swap="outerHTML"` ;
  `_row_edit.html` est un `<tr>` formulaire `hx-post` qui re-rend `_row.html` ;
  « Annuler » = `hx-get=".../{id}/row"`. Suppression :
  `hx-post=".../delete" hx-confirm="..." hx-target="closest tr" hx-swap="delete swap:0.2s"`
  et la route renvoie `HTMLResponse("")`.
- **Polling** : fragment avec `hx-trigger="every 3s"` qui s'auto-remplace quand l'état
  change (voir `voice/_greeting_status.html`).
- **Redirects post-POST** : `RedirectResponse(url, status_code=303)` (jamais 302).
- Longues opérations (rendu TTS 60-90 s) : `asyncio.create_task(...)` + polling UI,
  jamais d'await dans la route.

## Graphiques

`admin/charts.py` : SVG server-rendered, zéro JS. Charger le skill `dataviz` avant de
créer un nouveau type de graphique. Couleurs par variables CSS `--viz-series-1` (bleu,
appels) / `--viz-series-2` (aqua, réservations) définies dans `static/admin.css`
(clair + sombre). Le texte porte l'encre texte (`--viz-text*`), jamais la couleur de
série. Une série par graphique → pas de légende, le titre nomme la série. Jamais de
double axe. Labels de valeur visibles (règle de relief pour l'aqua).

## Données

- Accès SQLite : TOUJOURS via les modules `app/tenants.py`, `app/reservations.py`,
  `app/users.py`, `app/calls.py` (jamais de SQL dans les routes).
- Nouveau champ/table → migration dans `db.py` `_MIGRATIONS` (append un script
  idempotent, `PRAGMA user_version` s'incrémente tout seul). Ne JAMAIS éditer un
  script de migration déjà livré.
- `get_conn()` est en WAL + busy_timeout : ne pas changer.

## Tests

Pattern : `TestClient(app)` avec fixture `client()` FRAÎCHE par test (isolation des
cookies). Login via POST `/admin/login` (compte de test : `admin@test.local` /
`test-admin-pass`, semé par `conftest.py`). CSRF en test : décoder le cookie session
(base64 du payload avant le premier point) pour lire `csrf`, l'envoyer en header
`X-CSRF-Token`. Toujours inclure un test 403 cross-tenant pour toute nouvelle
ressource scopée. Lancer : `cd api && python -m pytest tests/ -q` (ou dans le
conteneur : `docker compose exec api sh -c "cd /app && python -m pytest tests -q"`).
