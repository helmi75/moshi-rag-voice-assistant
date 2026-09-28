# Voxtral — la voix « Marie » de Mistral

> Epic **SCRUM-94**. Marie est une voix **en plus** des voix Moshi, choisie par
> établissement sur la page « Voix & accueil », avec son ton.

## Pourquoi

Test d'écoute à l'aveugle du 28/09/2026 : dix phrases tirées d'appels réels, en qualité
téléphone, dites par la voix Moshi « Développeuse », Marie neutre et Marie joyeuse. Helmi
a préféré Marie.

| | Moshi (notre GPU sur Modal) | Voxtral (Mistral) |
|---|---|---|
| Coût de la voix, sur les 201 appels en base | 17,02 $ réels (8,5 c/appel) | ≈ 1,70 $ (0,8 c/appel) |
| Tarif | au temps où le GPU est allumé | 0,016 $ pour 1 000 caractères prononcés |
| Premier son d'une réponse | 0,66 s (connexion pré-ouverte) | 0,43 s de médiane (une pointe à 3,2 s) |
| Premier appel, GPU éteint | 46 s de réveil, couverts par la musique d'attente | aucun réveil |
| Pannes de capacité | L4 `eu` introuvable les 23, 24 et 25/09 | pas de GPU à obtenir |

Même un GPU plein 24 h/24 (4 appels simultanés, ~120 appels par heure) reviendrait à
~0,75 c/appel : Voxtral n'est plus cher à aucun volume.

## Ce que fait l'application

- **Catalogue** (`api/app/voice/voices.py`) : Marie en quatre tons — neutre, joyeuse,
  curieuse, enthousiaste. Mistral propose aussi « triste » et « en colère », sans usage
  au standard d'un restaurant. Liste fermée, comme pour Moshi.
- **Voix en appel** (`api/app/voice/voxtral_tts.py`) : la liaison Twilio reste un
  websocket Media Streams, le pipeline reste Pipecat. Chaque phrase part en HTTPS à
  `POST https://api.mistral.ai/v1/audio/speech` (`stream: true`), l'audio revient en flux
  (événements `speech.audio.delta`, PCM float32 24 kHz), rééchantillonné en 8 kHz pour
  Twilio. Une connexion gardée ouverte pour tout l'appel.
- **Accueil pré-enregistré** et message « rappelez dans quelques minutes » : rendus par
  Voxtral en ~2 s, dans la voix choisie. Jamais deux voix dans un appel.
- **Décroché** : ni réveil ni musique d'attente, « Je vous écoute. » suit l'accueil.
- **Panne** : une phrase qui échoue est réessayée une fois (si rien n'est encore parti) ;
  délais bornés (connexion 5 s, 15 s entre deux morceaux). Chaque échec est compté pour
  la supervision (contrôle « Voix Voxtral »).
- **Sans clé** : Marie est grisée dans l'admin ; un établissement qui l'avait choisie
  retombe sur la voix Moshi par défaut, jamais sur le silence.

Mistral ne reçoit que le **texte** que dit l'assistante, jamais la voix de l'appelant.

## Mettre en service

1. Console Mistral → API Keys : créer une clé dédiée (« helmane-production »).
2. Sur le serveur, dans le `.env` : `MISTRAL_API_KEY=…`, puis redéployer.
3. Admin → établissement → « Voix & accueil » → Marie et son ton → « Changer la voix ».
   L'accueil est regénéré en quelques secondes : l'écouter sur la même page.
4. `/supervision` → « Voix Voxtral » au vert.

`VOXTRAL_MODEL` (défaut `voxtral-mini-tts-2603`) change de modèle sans toucher au code.

## Limites connues

| Limite | Ticket |
|---|---|
| Marie est une voix française : un appel en anglais garde son accent | SCRUM-100 |
| Le coût de la voix n'apparaît pas encore dans le suivi des coûts | SCRUM-99 |
| Si Mistral tombe en plein appel, les phrases concernées ne sont pas dites : pas de bascule vers Moshi (deux voix, et un GPU froid à réveiller) | SCRUM-97, à décider |
| Limites de débit du compte Mistral non mesurées | à la montée en charge |
