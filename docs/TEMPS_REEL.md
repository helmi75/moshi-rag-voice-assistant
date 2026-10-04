# Voix « temps réel » : huit technologies face à notre chaîne (étude du 04/10/2026)

Demande de Helmi : comparer notre architecture aux voix temps réel capables d'appeler des
outils, prix compris, quitte à rogner la marge si la conversation devient plus fluide.

**Étude sur documents.** Tous les prix ont été relevés sur les sites des fournisseurs le
**04/10/2026**. Rien n'a été écouté ni mesuré : aucune de ces technologies n'a pris un
appel chez nous. Les prix bougent — les relire avant toute décision.

## Verdict

1. **Le prix n'est pas l'obstacle.** La minute nous coûte 2,9 c€ et se vend de 23 à 45 c€.
   Même la plus chère des huit laisse une marge positive ; la plus intéressante,
   GPT-Live-1, double le coût et laisse 73 à 82 % de marge.
2. **« Temps réel » ne veut pas dire « plus fluide ».** Cinq des huit (ElevenLabs,
   Deepgram, Cartesia, Retell, et Hume en français) sont la même chaîne que la nôtre —
   transcription, modèle, voix — montée par quelqu'un d'autre. Elles coûtent 2,4 à
   3,8 fois plus cher et obligeraient à refaire journal, enregistrement et renvoi.
3. **Une seule change la nature de la conversation : GPT-Live-1**, qui écoute pendant
   qu'elle parle. C'est notre défaut mesuré — le découpage en tours — qu'elle attaque.
   Son point faible connu est le français (accent signalé par des développeurs).
4. **Recommandation : ne pas migrer, essayer.** Un établissement de test sur GPT-Live-1,
   le même banc et les mêmes vrais appels, puis l'oreille de Helmi. Gemini 3.8 Live en
   second candidat : il se branche sur notre Pipecat actuel et coûte à peu près comme
   aujourd'hui.

## Notre chaîne aujourd'hui

Twilio → Deepgram nova-3 (transcription) → Gemini Flash via OpenRouter (modèle, outils) →
Mistral Voxtral (voix), assemblés par Pipecat 1.5.

| Mesure | Valeur | Source |
|---|---|---|
| Coût | **0,031 $ la minute** (0,526 $ pour 17,05 min) | 7 appels relevés par Helmi le 01/10/2026 |
| Silence ressenti avant une réponse | 1,9 s de médiane (2,7 s au 9ᵉ décile) | 48 tours écoutés le 04/09/2026, avec l'ancienne voix |
| dont : fin de tour détectée | 0,57 s | idem |
| dont : modèle | 0,50 s | idem |
| dont : premier son de la voix | 0,61 s à l'époque ; 0,43 s avec Voxtral | `docs/VOXTRAL.md` |
| Tours coupés avant la fin de la phrase | 16 sur 48 avant réglage | 04/09/2026 ; corrigé par un délai de 2 s sur les tours hésitants |

Le silence total n'a pas été re-mesuré depuis le passage à la voix Mistral.

## Les prix, tels qu'affichés

| Technologie | Prix affiché | Ce qui se paie en plus |
|---|---|---|
| **OpenAI GPT-Live-1** | 0,05 $/min, à la seconde | le modèle d'arrière-plan qui raisonne et appelle les outils ; la téléphonie |
| **OpenAI Realtime 2.1 mini** | audio : 10 $ / 20 $ le million de jetons (entrée / sortie), 0,30 $ en cache ; texte : 0,60 $ / 2,40 $, 0,06 $ en cache | la téléphonie |
| *(OpenAI Realtime 2.1, pour repère)* | audio : 32 $ / 64 $, 0,40 $ en cache ; texte : 4 $ / 24 $ | la téléphonie |
| **Gemini 3.8 Live** | audio : 3 $ en entrée (0,005 $/min), 12 $ en sortie (0,018 $/min) ; texte : 0,75 $ / 4,50 $ | la téléphonie ; tout l'historique audio est refacturé à chaque tour |
| **ElevenLabs Agents** | 0,08 $/min (Pro 99 $ = 1 238 min ; Scale 299 $ = 3 738 min) | le modèle de langage ; ElevenLabs n'ajoute pas de frais de téléphonie |
| **Deepgram Voice Agent** | 0,075 $/min tout compris ; 0,059 $ avec notre modèle ; 0,050 $ avec notre modèle et notre voix | la téléphonie |
| **Cartesia Voice Agents** | 0,06 $/min | 0,014 $/min avec un numéro Cartesia ; le modèle (offert « pour un temps limité » aux agents créés dans leur interface) |
| **Hume EVI** | 0,07 $/min (Starter, Creator), 0,06 $ (Pro), 0,05 $ (Scale), 0,04 $ (Business) | la téléphonie ; EVI 4-mini exige un modèle de langage en supplément |
| **Retell AI** | 0,055 $/min d'infrastructure + voix 0,015 $ (0,040 $ avec ElevenLabs) + modèle de 0,008 $ (GPT 5 mini) à 0,064 $ (recommandé) + téléphonie 0,015 $ | options : base de connaissances 0,005 $/min… En voix-à-voix : Realtime 2.1 mini à 0,07 $/min |

Relevé au passage : Deepgram affiche nova-3 multilingue en flux à **0,0058 $/min**
(« prix actuel », 0,0092 $ en « prix normal »). Notre chiffrage (`COST_DEEPGRAM_PER_MIN`)
retient 0,0092 $ : à vérifier sur la facture.

## Ce que coûte une minute d'appel

Pour comparer, tout est ramené à une minute d'appel reçu sur un numéro français, Twilio
compris (0,010 $/min).

**Hypothèses**, pour les modèles facturés au jeton — à remplacer par une mesure dès qu'un
essai existe :

- appel de 3,5 minutes, 3,2 tours par minute (48 tours en 15 minutes, mesure du 04/09) ;
- l'assistante parle 21,5 s par minute (320 caractères par minute, mesurés) ; le client
  20 s (supposé) ;
- consignes, fiche de l'établissement et outils : 4 000 jetons de texte (ordre de grandeur) ;
- OpenAI : 600 jetons par minute de parole du client, 1 200 par minute de parole de
  l'assistante ; toute la conversation est renvoyée au modèle à chaque tour. La fourchette
  va de « le cache marche à chaque tour » à « le cache ne marche jamais » ;
- Gemini Live : 25 jetons par seconde ; la fourchette va de « seul le flux est facturé »
  à « l'historique audio et les consignes sont refacturés à chaque tour » (ce que
  confirme une réponse de Google sur son forum, 11/05/2026) ;
- plateformes : petit modèle de langage à 0,003 $/min quand il est facturé à part.

| Technologie | Coût par minute | Rapport | Appel de 3,5 min |
|---|---|---|---|
| **Notre chaîne** (mesuré) | **0,031 $** | 1 | 10,8 c$ |
| Gemini 3.8 Live (estimé) | 0,021 à 0,048 $ | 0,7 à 1,6 | ≈ 12 c$ |
| OpenAI Realtime 2.1 mini (estimé) | 0,023 à 0,064 $ | 0,7 à 2,1 | ≈ 15 c$ |
| **OpenAI GPT-Live-1** | ≈ 0,063 $ | 2,0 | 22 c$ |
| Hume EVI 4-mini | 0,070 à 0,083 $ | 2,3 à 2,7 | ≈ 27 c$ |
| Cartesia Voice Agents | 0,073 à 0,077 $ | 2,4 à 2,5 | ≈ 26 c$ |
| Deepgram Voice Agent | 0,085 $ | 2,7 | 30 c$ |
| ElevenLabs Agents | ≈ 0,093 $ | 3,0 | 33 c$ |
| Retell AI | 0,088 à 0,149 $ | 2,8 à 4,8 | ≈ 42 c$ |

L'estimation de Realtime 2.1 mini recoupe celle d'un tiers (Fora Soft, mis à jour le
07/09/2026 : 0,02 à 0,05 $/min hors téléphonie, cache en marche).

## Ce que deviennent les marges

Formule utilisée à plein, milieu de la fourchette, numéros compris (1,25 € chacun),
1 € = 1,08 $ (taux de `docs/TARIFS.md`, 28/09/2026 — à recalculer).

| Technologie | Essentiel 89 € · 250 min | Service 149 € · 600 min | Maison 349 € · 1 500 min |
|---|---|---|---|
| **Notre chaîne** | 8 € · **91 %** | 18 € · **88 %** | 49 € · **86 %** |
| Gemini 3.8 Live | 9 € · 90 % | 21 € · 86 % | 55 € · 84 % |
| OpenAI Realtime 2.1 mini | 11 € · 87 % | 25 € · 83 % | 66 € · 81 % |
| **OpenAI GPT-Live-1** | 16 € · **82 %** | 36 € · **76 %** | 93 € · **73 %** |
| Cartesia Voice Agents | 19 € · 79 % | 43 € · 71 % | 110 € · 68 % |
| Hume EVI 4-mini | 19 € · 79 % | 44 € · 71 % | 113 € · 68 % |
| Deepgram Voice Agent | 21 € · 76 % | 48 € · 67 % | 124 € · 64 % |
| ElevenLabs Agents | 23 € · 74 % | 53 € · 64 % | 135 € · 61 % |
| Retell AI | 29 € · 68 % | 67 € · 55 % | 171 € · 51 % |

Marge sur les coûts variables seulement, comme dans `docs/TARIFS.md`. La minute en plus
(0,20 à 0,45 €) reste rentable partout : la plus chère coûte 11 c€.

## Ce que chacune est vraiment

| Technologie | Nature | Outils | Français | Se branche sur notre Pipecat |
|---|---|---|---|---|
| **GPT-Live-1** | voix-à-voix, **écoute en parlant** ; un second modèle raisonne et appelle les outils | oui, par le modèle d'arrière-plan | pas de liste officielle ; accent signalé sur le forum d'OpenAI | oui, mais dans une version de Pipecat plus récente que notre 1.5 |
| **Realtime 2.1 mini** | voix-à-voix, tour par tour | oui (articles tiers) | non relu | le service OpenAI Realtime est dans notre 1.5 ; non essayé |
| **Gemini 3.8 Live** | voix-à-voix, tour par tour ; sorti le 15/09/2026 | oui | « 70 langues », le français n'est pas nommé sur la page | le service Gemini Live est dans notre 1.5 ; non essayé avec ce modèle |
| **ElevenLabs Agents** | plateforme : transcription + modèle + voix | oui (appels à notre API) | non relu | non : remplace le pipeline |
| **Deepgram Voice Agent** | plateforme : transcription + modèle + voix | oui | non trouvé sur les pages lues | non : remplace le pipeline |
| **Cartesia Voice Agents** | plateforme ; le code de l'agent (Python) tourne chez Cartesia | oui | non trouvé sur les pages lues | non : remplace le pipeline |
| **Hume EVI** | EVI 3 voix-à-voix mais **anglais seulement** ; EVI 4-mini parle français et exige un modèle en supplément | oui, selon le modèle en supplément | oui pour EVI 4-mini | non vérifié |
| **Retell AI** | plateforme, la plus complète côté téléphonie | oui (appels à notre API) | oui (voix et transcription) | non : remplace le pipeline |

Les huit savent appeler des outils : relu sur leurs pages le 04/10/2026, sauf Realtime 2.1
mini, où l'information vient d'articles tiers.

### Sur la fluidité

- Latences **annoncées** par OpenAI (relevé du 15/09/2026, non relu) : 0,8 s pour
  GPT-Live-1, 1,4 s pour Realtime 2.1. Notre médiane mesurée est de 1,9 s, dont 0,57 s à
  attendre la fin du tour.
- Un modèle voix-à-voix **tour par tour** (Realtime 2.1 mini, Gemini Live) supprime deux
  étages, pas l'attente de la fin du tour : le gain attendu est de quelques dixièmes de
  seconde, pas un changement de nature.
- Seul GPT-Live-1 décide lui-même quand répondre et quand se taire. Réserve publiée par
  OpenAI au lancement (relevé du 15/09, non relu) : il réagit plus lentement aux
  interruptions volontaires.
- Les plateformes ne publient pas de latence sur les pages lues.

### Ce qu'on garderait, ce qu'on perdrait

- **Par Pipecat** (GPT-Live-1, Realtime mini, Gemini Live) : on garde Twilio, les
  outils et leurs refus côté serveur (`llm.run_tool`), le journal des appels, le
  renvoi en cas de panne. À refaire ou à revérifier : l'accueil pré-rendu, la
  détection de langue, le rattrapage de parole, la vigie (elle compte les erreurs de
  la voix, du modèle et de la transcription, qui deviennent un seul service),
  l'enregistrement deux pistes, le chiffrage par poste.
- **Par une plateforme** : tout le pipeline est remplacé. Les outils deviennent des
  appels à notre API ; le journal, l'enregistrement, la supervision et le renvoi sont à
  reconstruire autour de ce que la plateforme veut bien exposer.
- **RGPD** : aujourd'hui la voix synthétisée est produite par une société française
  (Mistral) ; la transcription et le modèle sont déjà aux États-Unis. Avec un modèle
  voix-à-voix, **toute la voix du client** part chez OpenAI ou Google : une ligne du
  registre à réécrire (`docs/RGPD.md` §5), et la résidence des données à vérifier.

## L'essai proposé

1. Une clé OpenAI, un établissement de test, une branche : Pipecat monté de 1.5 à une
   version qui porte `OpenAILiveLLMService` (la page de Pipecat ne dit pas laquelle),
   avec nos outils en arrière-plan.
2. Le banc conversationnel (`scripts/banc_conversation.py`) sur les mêmes scénarios, en
   français et en anglais, puis dix vrais appels de Helmi.
3. Critères, chiffrés avant de commencer : silence ressenti (médiane, 9ᵉ décile), tours
   coupés, réservations abouties, coût mesuré par minute, et l'oreille de Helmi sur
   l'accent.
4. Second candidat si le français de GPT-Live-1 ne passe pas : Gemini 3.8 Live, sur
   notre Pipecat actuel.

Budget de l'essai : une heure d'appels sur GPT-Live-1 coûte environ 4 $.

## L'essai, tel qu'il est codé (04/10/2026)

Helmi a dit « lance l'essai » le 04/10/2026 et posé une clé OpenAI. Ce qui existe :

- **`api/app/voice/live.py`** : pour les établissements de `GPT_LIVE_ETABLISSEMENTS`,
  l'appel est relayé à GPT-Live. Sans Pipecat, finalement : GPT-Live accepte le format du
  téléphone (µ-law 8 kHz) et rend un flux continu, silences compris, au rythme de la
  parole — il n'y a rien à détecter ni à assembler, et monter Pipecat de version aurait
  touché tous les appels.
- **GPT-Live n'est que la voix. Le cerveau reste le nôtre** : `google/gemini-2.5-flash`
  par OpenRouter (décision de Helmi, 04/10/2026). GPT-Live confie le travail, notre
  modèle raisonne sur la conversation entendue et appelle les outils par
  `llm.run_tool` — les refus du serveur restent les mêmes —, et ce qu'il rend est donné
  à dire. `GPT_LIVE_MODELE=gpt-6-luna` confie ce rôle à OpenAI, pour comparer.
- Si la session ne s'ouvre pas en quatre secondes, l'appel suit le pipeline habituel.
- Au journal : transcription, blanc ressenti par tour, enregistrement, coût réel.

### Ce que l'API a montré

Vérifié le 04/10/2026 avec la clé du compte, depuis un conteneur jetable :

| Constat | Détail |
|---|---|
| Format du téléphone | `audio/pcmu` à 8 000 Hz accepté tel quel |
| Langue | aucun réglage de langue : elle se pilote par les consignes |
| Voix ouvertes | 22 : marin, cedar, alloy, ash, ballad, coral, echo, sage, shimmer, verse, quartz, ripple, vesper, willow, stone, gleam, meridian, bossa, tempo, beacon, delta, cinder |
| Voix fermées | « brise » et « sillage » (citées dans la référence d'OpenAI), « arbor » (ChatGPT) : refusées |
| Cerveau | le nôtre (travail confié au client) ou un modèle d'OpenAI (`gpt-6-luna`, `gpt-5.6-luna`) : les deux modes sont acceptés et ont abouti à une réservation |
| Ouverture de l'appel | la consigne « dis l'accueil » ne suffit pas toujours : six voix sur huit sont restées muettes au premier lot. L'accueil redonné comme propos à dire (`session.commentary.append`) a fait parler toutes les voix, mot pour mot |

### Les voix en français

Les 22 voix ont dit la même phrase d'accueil et de réservation (`scripts/essai_voix_gpt_live.py`,
extraits dans `local/essai-gpt-live/`, hors dépôt). Chaque extrait a été retranscrit par
Deepgram nova-3 en français : **toutes sont comprises** (0 à 3 mots mal transcrits sur 44,
souvent le nom propre), et toutes sont reconnues comme du français. Cette mesure écarte
une voix inintelligible ; **elle ne dit rien de l'accent**, qui se juge à l'oreille.

| Voix | Style annoncé par OpenAI | Débit | Mots mal transcrits |
|---|---|---|---|
| `marin` | aucun (voix générale) | 185 mots/min | 1 sur 44 |
| `cedar` | aucun (voix générale) | 206 mots/min | 2 sur 44 |
| `alloy` | aucun (voix générale) | 189 mots/min | 1 sur 44 |
| `ash` | aucun (voix générale) | 162 mots/min | 0 sur 44 |
| `ballad` | aucun (voix générale) | 178 mots/min | 1 sur 44 |
| `coral` | aucun (voix générale) | 178 mots/min | 0 sur 44 |
| `echo` | aucun (voix générale) | 188 mots/min | 0 sur 44 |
| `sage` | aucun (voix générale) | 159 mots/min | 2 sur 44 |
| `shimmer` | aucun (voix générale) | 172 mots/min | 0 sur 44 |
| `verse` | aucun (voix générale) | 189 mots/min | 2 sur 44 |
| `quartz` | anglais australien | 157 mots/min | 2 sur 44 |
| `ripple` | anglais australien | 196 mots/min | 0 sur 44 |
| `vesper` | anglais britannique | 207 mots/min | 0 sur 44 |
| `willow` | anglais irlandais | 151 mots/min | 0 sur 44 |
| `stone` | anglais irlandais | 197 mots/min | 0 sur 44 |
| `gleam` | anglais nord-américain | 210 mots/min | 0 sur 44 |
| `meridian` | anglais nord-américain | 207 mots/min | 0 sur 44 |
| `bossa` | portugais du Brésil | 197 mots/min | 1 sur 44 |
| `tempo` | portugais du Brésil | 187 mots/min | 1 sur 44 |
| `beacon` | anglais philippin | 204 mots/min | 0 sur 44 |
| `delta` | anglais du sud des États-Unis | 167 mots/min | 2 sur 44 |
| `cinder` | anglais du sud des États-Unis | 160 mots/min | 3 sur 44 |

Aucune voix n'est annoncée comme française. Les dix premières n'ont pas de style régional
déclaré ; les douze suivantes en ont un, étranger au français. Sur le forum d'OpenAI
(15 au 19/09/2026), `marin` est citée comme la meilleure en français. À écouter d'abord :
`marin`, `coral`, `sage`, `shimmer`, puis `cedar` et `alloy`.

Les voix sur mesure (clonage, avec enregistrement de consentement) existent mais sont
réservées aux « clients éligibles », et la page d'OpenAI ne les annonce pour GPT-Live
qu'avec des accents anglais.

### Ce que coûte un appel, mesuré

`scripts/essai_conversation_gpt_live.py` joue une réservation complète à travers
`voice/live.py`, contre le vrai GPT-Live : un faux Twilio envoie les répliques d'un client
au format du téléphone, `llm.run_tool` est le vrai, la base est jetable. Trois
conversations le 04/10/2026, voix `marin`, cerveau `google/gemini-2.5-flash` :

| | Conversation 1 | Conversation 2 | Conversation 3 |
|---|---|---|---|
| Durée de l'appel | 45,3 s | 41,8 s | 44,3 s |
| Voix GPT-Live (session facturée) | 3,33 c$ (40 s) | 3,08 c$ (37 s) | 3,25 c$ (39 s) |
| Cerveau Gemini (4 générations) | 0,44 c$ | 0,44 c$ | 0,35 c$ |
| Twilio (une minute entamée) | 1,00 c$ | 1,00 c$ | 1,00 c$ |
| **Total** | **4,77 c$** | **4,52 c$** | **4,60 c$** |
| **Par minute** | **0,063 $** | **0,065 $** | **0,062 $** |
| Réservation créée, raccroché par l'assistante | oui | oui | oui |
| Blanc avant ses réponses | 0,2 s · 0,2 s | elle enchaîne avant la fin du dernier mot | 0,4 s · 0,2 s |

Le coût du cerveau calculé par nos tarifs (0,30 $ le million de jetons d'entrée, 0,03 $ en
cache, 2,50 $ en sortie) est, au cent-millième près, celui qu'OpenRouter dit avoir
facturé. Les secondes de session sont celles qu'OpenAI annonce à la fermeture ; la clé
du compte n'a pas le droit de lire la facturation (`api.usage.read`), le montant débité
se vérifie sur platform.openai.com.

**Face à la chaîne actuelle** (0,031 $ la minute, mesuré sur 7 vrais appels le 01/10) :

| Poste, par minute d'un appel de 3,5 min | Chaîne actuelle | GPT-Live + Gemini |
|---|---|---|
| Twilio | 1,1 c$ | 1,1 c$ |
| Transcription (Deepgram) | 0,9 c$ | — |
| Voix | 0,5 c$ (Mistral) | 4,9 c$ (GPT-Live) |
| Cerveau (Gemini 2.5 Flash) | ≈ 0,5 c$ | ≈ 0,2 c$ |
| **Total** | **3,1 c$ · 2,9 c€** | **6,3 c$ · 5,8 c€** |

L'appel de 3,5 minutes est extrapolé des trois mesures (session = appel moins 5 s,
deux séries de travail confié) : 21,9 c$ contre 10,9 c$, **le double**.

| Formule à plein | Coût actuel | Coût GPT-Live | En plus par mois | Marge |
|---|---|---|---|---|
| Essentiel (89 €, 250 min) | 8,43 € | 15,73 € | + 7,30 € | 91 % → 82 % |
| Service (149 €, 600 min) | 18,47 € | 35,99 € | + 17,52 € | 88 % → 76 % |
| Maison (349 €, 1 500 min) | 49,31 € | 93,11 € | + 43,81 € | 86 % → 73 % |

Minute en plus : 83 % de marge à 0,35 €, 77 % à 0,25 €, 71 % à 0,20 € ; Liberté 87 %.
Pas d'abonnement chez OpenAI : tout est à l'usage. 1 € = 1,08 $.

**Ce que ces mesures ne disent pas** : ce sont trois réservations d'une quarantaine de
secondes avec un client de synthèse qui ne coupe pas la parole et parle sans bruit de
fond. Le blanc est lu sur les horodatages de la transcription d'OpenAI (pas de 200 ms),
pas mesuré sur le son. Un vrai appel de plusieurs minutes, une modification, un appel
en anglais, un nom épelé : rien de cela n'a été joué.

Défauts vus pendant ces essais et corrigés : le cerveau d'OpenAI réclamait un numéro de
rappel déjà connu (champ retiré) ; la phrase d'attente était dite deux fois (consigne
ajoutée au cerveau). Vu et laissé : l'accueil est parfois suivi d'une phrase de son cru
(« Que puis-je faire pour vous ? »).

### Pour allumer l'essai

1. Choisir la voix à l'oreille (`local/essai-gpt-live/`), la poser dans `GPT_LIVE_VOIX`.
2. Poser dans le `.env` du serveur `GPT_LIVE_ETABLISSEMENTS=` suivi de l'identifiant de
   l'établissement d'essai, puis déployer.
3. Recette T1 à T11 (`docs/RECETTE.md`).

Pour l'éteindre : vider `GPT_LIVE_ETABLISSEMENTS`.

**Non vérifié à ce jour** : aucun appel téléphonique n'est passé par GPT-Live. Le relais,
les outils et la clôture sont éprouvés contre une doublure qui joue le protocole ; la
conversation réelle, la tenue du français et le coût mesuré restent à constater.

## Sources (relevées le 04/10/2026)

- OpenAI : [tarifs](https://developers.openai.com/api/docs/pricing),
  [GPT-Live-1](https://developers.openai.com/api/docs/models/gpt-live-1),
  [coûts du temps réel](https://developers.openai.com/api/docs/guides/realtime-costs)
- Google : [tarifs Gemini](https://ai.google.dev/gemini-api/docs/pricing),
  [Live API](https://ai.google.dev/gemini-api/docs/live),
  [refacturation de l'historique](https://discuss.ai.google.dev/t/pricing-of-speech-to-speech-live-model/140340)
- ElevenLabs : [tarifs des agents](https://elevenlabs.io/pricing/agents),
  [outils](https://elevenlabs.io/docs/agents-platform/customization/tools)
- Deepgram : [tarifs](https://deepgram.com/pricing),
  [Voice Agent](https://developers.deepgram.com/docs/voice-agent)
- Cartesia : [tarifs](https://cartesia.ai/pricing),
  [Line](https://docs.cartesia.ai/line/introduction),
  [outils](https://docs.cartesia.ai/line/sdk/tools)
- Hume : [tarifs](https://www.hume.ai/pricing),
  [EVI](https://dev.hume.ai/docs/speech-to-speech-evi/overview),
  [outils](https://dev.hume.ai/docs/speech-to-speech-evi/features/tool-use)
- Retell : [tarifs](https://www.retellai.com/pricing),
  [langues](https://docs.retellai.com/build/language-support),
  [fonctions](https://docs.retellai.com/build/single-multi-prompt/custom-function)
- Pipecat : [service GPT-Live](https://docs.pipecat.ai/api-reference/server/services/s2s/openai-live)
- Recoupement : [Fora Soft, coût réel de Realtime](https://www.forasoft.com/blog/article/openai-realtime-api-pricing),
  [forum OpenAI, français de GPT-Live-1](https://community.openai.com/t/gpt-live-1-best-voice-and-prompting-for-natural-french/1397735)
