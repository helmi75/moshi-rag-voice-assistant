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

## 6. En cas de panne : le renvoi vers le restaurant (ASSISTANTE-118)

Rien à régler côté Twilio pour les deux premiers cas : c'est l'application qui répond.

| Panne | Ce qui se passe | Où ça se règle |
|---|---|---|
| La voix, le modèle ou la transcription lâche **pendant** l'appel (deux échecs de suite) | L'assistante rend la ligne ; Twilio lit la suite du TwiML (`/twilio/suite`) : le restaurant sonne 15 s, sinon répondeur | « Fiche établissement » → **Numéro de secours** |
| Une panne vient d'être constatée (moins de 3 minutes) | Les appels suivants sont renvoyés dès le décroché, sans revivre le silence | idem |
| **Notre serveur ne répond plus du tout** (VPS éteint, déploiement en cours) | Twilio n'obtient aucun TwiML : l'appel échoue, **sauf** si le numéro a une adresse de secours | console Twilio, ci-dessous — **pas encore fait** |

Pour le troisième cas, l'adresse de secours doit vivre hors de notre serveur. Dans la
console Twilio : *TwiML Bins* → créer un bin par numéro, avec le numéro de secours de
l'établissement :

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="fr-FR">Un instant, je vous mets en relation avec le restaurant.</Say>
  <Dial timeout="15" answerOnBridge="true">+33142000000</Dial>
  <Say language="fr-FR">Le restaurant ne peut pas vous répondre. Merci de rappeler dans quelques minutes.</Say>
</Response>
```

puis *Phone Numbers* → le numéro → *A call comes in* → **Primary handler fails** → ce bin.
Ce bin ne connaît ni les horaires ni le répondeur : il sonne, et c'est tout. À refaire si
le numéro de secours change.

**Coût d'un appel renvoyé** (grille Twilio du compte, France, relevée le 01/10/2026) :
l'appel reçu continue de courir (0,010 $/min) et le renvoi s'y ajoute — 0,0187 $/min vers
un fixe, 0,0404 $/min vers un portable. Le restaurant voit le numéro du client ; pour un
appel masqué, notre ligne.

**Boucle** : si le fixe du restaurant est lui-même renvoyé vers notre numéro quand
personne ne décroche, l'appel reviendrait ici. D'où les 15 s de sonnerie (plus court que
le renvoi d'un opérateur), et la règle : un appel qui arrive du numéro de secours, ou
renvoyé par lui, n'y repart jamais.

**Essayer** : « Fiche établissement » → *Essayer le renvoi* (super-admin). Pendant
3 minutes, les appels de cet établissement sont traités comme une panne.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| Erreur 11200 / 11205 dans *Monitor* | l'application n'est pas joignable à cette URL (DNS, Caddy, tunnel arrêté) |
| Réponse 403, sonde « toutes les requêtes refusées » | `PUBLIC_URL` ne correspond pas à l'URL de la console, ou jeton Twilio périmé : remettre `TWILIO_SIGNATURE=log` le temps de corriger |
| « Ce numéro n'est pas encore configuré » | le numéro n'est déclaré sur aucun établissement, ou pas au format `+33…` |
| L'appel décroche puis raccroche sans un mot | `PUBLIC_WS_URL` absente ou malformée (la sonde passe en panne « Configuration ») |
| La sonde « Alertes Twilio » dit « HTTP 401 » | jeton du `.env` refusé par Twilio : il a été renouvelé dans la console |

Journaux en direct : `docker compose logs -f --tail=50 api`.
