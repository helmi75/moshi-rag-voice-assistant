# Brancher un numéro Twilio

Un numéro Twilio = un établissement. Twilio appelle l'application à chaque appel entrant ;
l'application reconnaît l'établissement au numéro appelé (`To`) et ouvre le flux audio.

## Prérequis

- un compte Twilio **actif** et un numéro ;
- l'application joignable en **HTTPS** : en production derrière Caddy
  ([DEPLOY.md](DEPLOY.md)), en essai via un tunnel (plus bas).

## 1. Identifiants

Console Twilio → *Account info* : **Account SID** (public) et **Auth Token** (le secret),
dans le `.env` : `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`. Le jeton sert à vérifier la
signature de chaque requête Twilio : **un jeton renouvelé dans la console doit être
reporté dans le `.env`**, sinon toutes les requêtes sont refusées.

## 2. Pointer le numéro sur l'application

Console → *Phone Numbers → Active numbers* → le numéro :

| Réglage | Valeur |
|---|---|
| Voice → *A call comes in* → Webhook, `HTTP POST` | `https://DOMAINE/twilio/voice` |
| Messaging → *A message comes in* (facultatif) | `https://DOMAINE/twilio/sms` |

Ou en une commande (même effet, `/twilio/webhook` aiguille voix et SMS) :
`python3 scripts/twilio_setup_number.py --webhook https://DOMAINE/twilio/webhook`.

## 3. Déclarer le numéro sur l'établissement

Admin → *Enseignes* → la fiche → **Numéro Twilio**, au format international sans espace
(`+33612345678`) : c'est la forme exacte que Twilio transmet. Un numéro qui ne correspond
à aucun établissement entend « Ce numéro n'est pas encore configuré. Au revoir. »

## 4. URL publiques et signature

Dans le `.env` :

```
PUBLIC_URL=https://DOMAINE                 # l'origine EXACTE saisie dans la console
PUBLIC_WS_URL=wss://DOMAINE/ws/voice       # où Twilio branche le flux audio
TWILIO_SIGNATURE=log                       # 48 h d'observation, puis retirer la ligne
```

Twilio signe l'URL qu'il appelle ; derrière Caddy l'application voit `http://api:8000/…`,
d'où `PUBLIC_URL`. Mise en service pas à pas : [DEPLOY.md](DEPLOY.md), section
« Signature des requêtes Twilio ».

## 5. Vérifier

1. Appeler le numéro : l'accueil de l'établissement, puis une conversation.
2. Sonde `/supervision` (ou admin → *Santé & coûts*) : « Signature des requêtes Twilio »
   avec des requêtes acceptées et **aucune refusée**, « Configuration du chemin
   d'appel » au vert.
3. Console Twilio → *Monitor → Alerts* : aucune erreur.

## Essai sans domaine (tunnel)

```bash
ngrok http 8000
# puis dans le .env, avec l'URL donnée par ngrok :
#   PUBLIC_URL=https://xxxx.ngrok-free.app
#   PUBLIC_WS_URL=wss://xxxx.ngrok-free.app/ws/voice
docker compose up -d api
```

et le webhook du numéro sur `https://xxxx.ngrok-free.app/twilio/voice`. L'URL change à
chaque lancement de ngrok : console et `.env` avec.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| Erreur 11200 / 11205 dans *Monitor* | l'application n'est pas joignable à cette URL (DNS, Caddy, tunnel arrêté) |
| Réponse 403, sonde « toutes les requêtes refusées » | `PUBLIC_URL` ne correspond pas à l'URL de la console, ou jeton Twilio périmé : remettre `TWILIO_SIGNATURE=log` le temps de corriger |
| « Ce numéro n'est pas encore configuré » | le numéro n'est déclaré sur aucun établissement, ou pas au format `+33…` |
| L'appel décroche puis raccroche sans un mot | `PUBLIC_WS_URL` absente ou malformée (la sonde passe en panne « Configuration ») |
| La sonde « Alertes Twilio » dit « HTTP 401 » | jeton du `.env` refusé par Twilio : il a été renouvelé dans la console |

Journaux en direct : `docker compose logs -f --tail=50 api`.
