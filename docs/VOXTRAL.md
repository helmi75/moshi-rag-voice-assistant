# Voxtral — les voix de Mistral

> Epic **SCRUM-94**. **Depuis le 28/09/2026, toutes les voix viennent de Mistral**
> (décision de Helmi après le test d'écoute et trois appels réels). Chaque établissement
> choisit la sienne parmi les 30 voix du catalogue de Mistral, sur la page « Voix &
> accueil ». Moshi ne se choisit plus : il reste la voix de SECOURS si la clé manque.

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

- **Catalogue** (`api/app/voice/voices.py`) : celui de l'API de Mistral
  (`GET /v1/audio/voices`), relu au démarrage puis toutes les heures ; une copie
  (`voice/voxtral_voix.json`, 30 voix au 28/09/2026) sert tant que Mistral n'a pas
  répondu. Marie (français, 6 tons), Paul (anglais américain, 8), Oliver et Jane
  (anglais britannique, 8 chacun). Les tons sont traduits et accordés (« Paul ·
  joyeux », « Jane · curieuse »). Liste fermée : on n'envoie que ce que Mistral a listé.
  Voix par défaut du parc : Marie neutre (`VOIX_PAR_DEFAUT` pour en changer).
- **Écoute dans l'admin** : un extrait par voix, rendu à la première écoute puis gardé
  (`VOIX_EXTRAITS_DIR`), en qualité téléphone. ~0,15 c par voix, une seule fois.
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
- **Sans clé** : la liste est grisée dans l'admin, et tous les établissements parlent
  avec la voix de secours Moshi (GPU, réveil de 46 s) — jamais le silence. La
  supervision est en panne (« Configuration » et « Voix Mistral »).

Mistral ne reçoit que le **texte** que dit l'assistante, jamais la voix de l'appelant.

## Ce que coûte un appel (SCRUM-99)

Chaque appel est chiffré à sa clôture, poste par poste, sur ce qu'il a consommé
(`api/app/calls.py`) : Twilio à la minute ENTAMÉE (0,01 $, facture du numéro
français), Deepgram à la minute (0,0092 $), Gemini aux jetons (0,30 $ le million en
entrée, 0,03 $ en cache, 2,50 $ en sortie), la voix aux caractères (0,016 $ pour
1 000). La page « Santé & coûts » additionne ces postes : la répartition tombe
exactement sur le total. Les appels du banc d'essai ne paient pas de téléphone et sont
chiffrés sur leur durée active.

## Mettre en service

1. Console Mistral → API Keys : créer une clé dédiée (« helmane-production »).
2. Sur le serveur, dans le `.env` : `MISTRAL_API_KEY=…`, puis redéployer.
3. Admin → établissement → « Voix & accueil » → choisir la voix (le lecteur fait
   entendre la voix sélectionnée) → « Changer la voix ». L'accueil est regénéré en
   quelques secondes : l'écouter sur la même page.
4. `/supervision` → « Voix Voxtral » au vert.

`VOXTRAL_MODEL` (défaut `voxtral-mini-tts-2603`) change de modèle sans toucher au code.

## Limites connues

| Limite | Ticket |
|---|---|
| La voix ne change pas quand l'appel passe en anglais (Marie garde son accent) | SCRUM-100 |
| Le code Moshi (secours) et le serveur GPU sur Modal restent en place | abandon, deuxième étape |
| Si Mistral tombe en plein appel, les phrases concernées ne sont pas dites : pas de bascule vers Moshi (deux voix, et un GPU froid à réveiller) | SCRUM-97, à décider |
| Limites de débit du compte Mistral non mesurées | à la montée en charge |
