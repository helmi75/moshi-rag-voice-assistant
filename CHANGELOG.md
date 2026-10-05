# Changelog

> **Sur la numérotation.** Les tags `v0.1.0` à `v0.6.0` ont été posés pendant la phase
> d'expérimentation, par deux personnes suivant deux logiques différentes : `v0.1.0`
> (19/07) est ainsi postérieur à `v0.5.0` (17/07), et la release GitHub `v0.2.0` décrit
> un contenu qui n'est pas celui de son tag. Ces tags sont publics, donc **ils ne sont
> pas réécrits** : les déplacer casserait toute référence existante pour un gain
> cosmétique. `v1.0.0` marque la reprise sur une numérotation cohérente.

## Non publiée — le travail en agents (05/10/2026)

### Modifié
- **Trois agents, un orchestrateur** : `.claude/agents/developpeur.md` code un ticket dans
  son propre arbre de travail, `.claude/agents/relecteur.md` relit un diff en lecture seule,
  `qa-recette` garde son rôle avec un modèle moins cher. Les règles d'économie et la
  répartition des décisions sont dans `CLAUDE.md` (« Qui décide quoi », « Travail en
  agents »).
- **Ouvrir une PR ne demande plus l'accord de Helmi** ; fusionner, déployer, écrire en
  production, dépenser, supprimer des données et clore un ticket le demandent toujours.

## Non publiée — une table, une réservation (ASSISTANTE-126, 05/10/2026)

Appel 240 : l'assistante entend « Kikato », enregistre la table ; le client épelle
« Kikao » ; faute de pouvoir corriger un nom, une seconde réservation est créée au même
créneau. Deux tables pour un client, deux e-mails « nouvelle réservation » à 46 secondes
d'écart.

### Corrigé
- **Même numéro, même jour, même heure : le serveur refuse une seconde réservation**, et
  son refus dit au modèle de corriger la première. Un autre client, une autre heure, un
  autre jour, un appel masqué ou une table annulée ne sont pas concernés.
- **`modify_reservation` corrige un nom.** Carnet interne : le nom change. resOS, où le
  nom ne se change pas par l'API utilisée : une note « Nom corrigé par le client » part
  au restaurant.
- Une « modification » qui ne change rien n'écrit rien et ne prévient personne (relevé le
  04/10 : trois e-mails « réservation modifiée » identiques pour un même appel).

### Non corrigé
- Sur le chemin GPT-Live, la première réservation a été créée au récapitulatif, avant la
  confirmation du client. Signalé à la session qui tient `voice/live.py`.
- Les deux réservations de l'appel 240 sont toujours en base : à annuler dans l'admin.

## Non publiée — « Rappelez-moi » jusqu'à minuit, et plus de prospect perdu la nuit (ASSISTANTE-125, 05/10/2026)

### Modifié
- **Marie rappelle de 8 h à minuit** (22 h auparavant), décision de Helmi : un
  restaurateur regarde le site après son service. `RAPPEL_HEURES` vaut `8-24` par défaut.
- **Hors plage, la demande n'est plus refusée et perdue.** Personne n'est appelé, la page
  répond « C'est noté : nous vous rappelons à partir de 8 h », et le numéro part par
  e-mail à `RAPPEL_NOTIFIER` (« À rappeler — demande reçue sur le site hors plage ») pour
  que Helmi rappelle lui-même. Les plafonds par numéro, par connexion et par jour valent
  comme pour un appel. Sans messagerie, ou si l'e-mail ne part pas, la page dit « revenez
  entre 8 h et minuit » : elle ne promet rien que personne ne lira.

### Relevé le 05/10/2026 sur le journal de Twilio
- Le premier rappel créé en Irlande (`ie1`) est passé : 137 s, terminé normalement.
- **Un rappel coûte 0,1603 $ la minute entamée, pas 0,0404 $** : la ligne présentée est le
  numéro américain (`TWILIO_NUMBER`), et Twilio facture alors l'appel vers un portable
  français au tarif d'une origine hors d'Europe (111 s → 0,3206 $ le 04/10 en `us1` ;
  137 s → 0,4809 $ le 05/10 en `ie1`). Le coût affiché dans l'admin pour ces appels est
  donc sous-estimé. Non corrigé : présenter le numéro français diviserait ce prix par quatre.

## Non publiée — le logo : la toque (ASSISTANTE-124, 04/10/2026)

### Modifié
- **Le logo d'Helmane est une toque de chef, avec la voix dessinée dedans** (trois barres).
  Il remplace le combiné de téléphone, un pictogramme libre que l'on retrouve sur beaucoup
  de sites et qui ne pouvait pas servir de marque. Choisi par Helmi le 04/10/2026 parmi
  onze pistes. Il est dans l'onglet du navigateur, dans l'en-tête de la page d'accueil et
  dans la barre latérale de l'admin ; un test tient les trois dessins identiques.
- L'admin affiche lui aussi l'icône dans l'onglet du navigateur (il n'en avait aucune).

Non fait : la recherche d'antériorité à l'INPI, et les fichiers pour l'impression.

## Non publiée — les appels peuvent être traités en Irlande (ASSISTANTE-123, 04/10/2026)

Le numéro français est traité par Twilio aux États-Unis (`voice_region: us1`, relevé le
04/10/2026) : la voix traverse l'Atlantique à chaque réplique. Depuis le serveur de
Paris, 83 ms d'aller-retour vers la Virginie contre 19 ms vers l'Irlande. Ce lot rend la
bascule possible ; **il ne bascule rien** — elle se décide et se fait chez Twilio.

### Ajouté
- **`twilio_region.py`** : chaque région de Twilio a son jeton et son API.
  `TWILIO_AUTH_TOKEN_IE1` porte le jeton irlandais, `TWILIO_REGION` dit à quelle région
  l'application s'adresse (défaut `us1`). Une région demandée sans son jeton se replie
  sur `us1`, et la supervision le signale.
- La signature accepte le jeton de chaque région posée : un appel traité en Irlande est
  signé par le jeton irlandais, un SMS par l'américain. La supervision compte par région.
- Raccrochage, rappel du site et relève des alertes s'adressent à la bonne région ; un
  message vocal est cherché dans la région choisie, puis dans l'autre.
- **`scripts/twilio_region.py`** : lit la région d'un numéro, recopie ses webhooks dans
  l'autre région, bascule et revient. Il refuse de basculer tant que l'application
  n'accepte pas une requête signée par le jeton de la région visée.
- `docs/TWILIO_SETUP.md` § 8 (marche à suivre et retour arrière), recette X.
- La relève des alertes Twilio se fait région par région : une région qui refuse son
  jeton est nommée (« relève impossible en ie1 (HTTP 401) ») sans faire taire l'autre.
  Vécu le jour même : le secret d'une clé d'API posé à la place du jeton irlandais.

### Vérifié, et pas vérifié
- Essayé en lecture seule sur le vrai numéro ; le jeton américain est bien refusé par
  l'API irlandaise (401).
- **Pas essayé** : tout ce qui demande le jeton irlandais (préparation, sonde, bascule),
  un appel réellement traité en Irlande, le tarif et les permissions de sortie en `ie1`.

## Non publiée — essai de GPT-Live, la voix-à-voix d'OpenAI (04/10/2026)

### Ajouté
- **`voice/live.py`** : pour les établissements nommés dans `GPT_LIVE_ETABLISSEMENTS`, et
  eux seuls, l'appel est relayé à GPT-Live au format du téléphone (µ-law 8 kHz), sans
  Pipecat. Elle écoute pendant qu'elle parle. GPT-Live n'est que la voix : le cerveau
  reste `google/gemini-2.5-flash` (décision de Helmi), qui raisonne sur la conversation
  entendue et appelle nos outils, toujours par `llm.run_tool` avec tous leurs refus.
- Si la session ne s'ouvre pas (clé, crédit, réseau, plus de quatre secondes), l'appel est
  servi par le pipeline habituel. Si elle tombe en cours d'appel, le renvoi vers le
  restaurant joue comme pour toute panne.
- L'appel est au journal : transcription des deux côtés, blanc ressenti tour par tour
  (comparable à celui du pipeline), enregistrement deux pistes, coût réel : 0,05 $ la
  minute de voix (tarif d'OpenAI du 04/10/2026) et les jetons du cerveau à leur tarif.
- `scripts/essai_conversation_gpt_live.py` : une réservation complète jouée contre le vrai
  GPT-Live, sans téléphone. Trois essais : réservation créée à chaque fois, 0,062 à
  0,065 $ la minute, réponses 0,2 à 0,4 s après la fin de la phrase du client.
- `scripts/essai_voix_gpt_live.py` : un extrait en français par voix, pour choisir à
  l'oreille.
- `docs/TEMPS_REEL.md` : l'étude des huit technologies et le compte rendu de l'essai.

### Ce que l'essai a déjà appris
- 22 voix sont ouvertes au compte ; « brise » et « sillage », citées dans la référence
  d'OpenAI, sont refusées. Aucune voix n'est annoncée comme française.
- La consigne d'accueil ne fait pas toujours parler le modèle : l'accueil lui est redonné
  au bout de 2,5 s de silence.

## Non publiée — l'admin aux couleurs du site, menu du téléphone (04/10/2026)

### Modifié
- **L'admin porte le thème du site** : fond brun nuit et orange en sombre, crème en clair,
  polices Inter et Inter Tight. Le choix clair / sombre / comme l'appareil reste. Les
  graphiques passent à l'orange (appels) et au bleu canard (réservations), validés sur les
  deux surfaces.
- **Sur téléphone, la navigation de l'admin se range derrière un bouton aux trois barres** :
  elle occupait jusque-là trois ou quatre lignes en haut de chaque page.
- **La page d'accueil n'a plus qu'un thème, le sombre** (décision de Helmi).
- Sur téléphone, l'en-tête du site garde un bouton « Espace client » à côté d'« Être
  rappelé » : les liens s'y effaçaient tous, et un client n'avait plus de chemin vers sa
  connexion.
- La page ne dit plus « sans engagement » ni « tarif fondateur » sous les formules.
- **Les mentions légales ne sont pas publiées** tant que l'éditeur n'est pas renseigné
  (`site.EDITEUR`) : ni lien, ni page (404). Le texte est prêt.

### Corrigé
- En thème clair, les boutons-liens (« Nouvel établissement ») restaient au bleu de Pico :
  ses variables l'emportaient sur les nôtres. On ne le voyait pas tant que l'accent était
  bleu lui aussi.

### Ajouté
- **Agent `qa-recette`** (`.claude/agents/qa-recette.md`) : la part de la recette qui ne
  demande aucun appel — suite de tests, production en lecture seule, rendu des pages.

## Non publiée — mentions légales, démonstration fictive, voix par défaut (03/10/2026)

### Ajouté
- **Page « Mentions légales »** (`/mentions-legales`, lien en pied de page) : hébergeur
  (Hostinger, relevé le 03/10/2026), données personnelles du rappel et de l'appel de
  démonstration, durées lues dans `rgpd.py`, prestataires, droits, cookies. L'identité de
  l'éditeur (`site.EDITEUR`) reste à fournir : la page n'affiche que les champs remplis.

### Modifié
- **Le restaurant de démonstration est fictif : « Le Bouchon Doré »**, bistrot du 11e.
  C'était le Fouquet's, avec sa vraie adresse et son vrai standard, que l'assistante
  donnait à qui l'essayait. Renommé en production le 03/10/2026 ; le semis d'une base
  neuve suit.
- **Voix par défaut : Marie enthousiaste** (c'était Marie neutre), pour tout établissement
  qui n'a pas choisi de voix — dont celui du rappel du site.
- La page d'accueil ne promet plus 14 jours d'essai.

## Non publiée — la grille à la minute (ASSISTANTE-120, 02/10/2026)

### Modifié
- **Les formules se vendent en minutes, plus en appels** (grille validée par Helmi le
  02/10/2026, prix hors taxes) : Essentiel 89 € pour 250 minutes, Service 149 € pour 600,
  Maison 349 € pour 1 500. Les concurrents comptent tous en minutes, et le forfait en
  appels nous faisait porter la durée de chaque appel. Raisonnement, marges et relevé des
  concurrents dans `docs/TARIFS.md`.
- **La minute en plus est propre à chaque formule** : 0,35 €, 0,25 € et 0,20 €, à la place
  de 0,30 € par appel pour tout le monde.
- **Le forfait se décompte à la seconde**, sur la durée des appels clos du mois.
- La salle de contrôle, la vue du parc, la fiche établissement et la page d'accueil
  affichent des minutes. La page annonce « Prix hors taxes ».
- La page d'accueil traduit chaque forfait en appels (« soit environ 70 appels »), sur la
  base de 3 min 30 par appel — la durée moyenne de nos 24 appels d'essai, ce que la page
  dit. `plans.DUREE_APPEL_MIN` est à remplacer par la durée mesurée chez le premier client.

### Corrigé
- **Vue du parc sur un écran étroit** : les quatre chiffres d'un établissement s'écrasaient
  sur son nom et ses étiquettes (un mot par ligne sur un téléphone, la dernière colonne
  hors de l'écran). Ils forment maintenant un bloc qui passe sous le nom.

### Ajouté
- **Formule Liberté**, sans abonnement : 0,45 € la minute, 10 € de mise en service par
  numéro. Elle n'a ni plafond ni alerte : l'admin écrit les minutes et le montant du mois.
- **Un appel rendu pendant une panne n'est pas décompté** : sa durée est celle que le
  restaurant a passée à son propre téléphone (garde-fou de mutation).
- **Un appel du banc d'essai n'est pas décompté** non plus : sa durée en base est celle de
  la connexion, parfois sept heures pour 80 secondes de conversation (garde-fou de mutation).

### Limites connues
- Rien n'est facturé par le code : le forfait compte et prévient, la facture reste à faire
  à la main.
- La mise en service de Liberté et la suspension d'une ligne inactive ne sont pas codées.
- La formule Maison reste inapplicable tant que rien ne regroupe plusieurs établissements.
- Un établissement déjà sur Essentiel, Service ou Maison garde sa formule ; son forfait se
  lit désormais en minutes (250, 600, 1 500 au lieu de 150, 400, 750 appels).

## Non publiée — le site, et Marie qui rappelle (ASSISTANTE-119, 02/10/2026)

### Ajouté
- **La page d'accueil du site**, servie à la racine (`/`) : `helmane.fr` y redirige déjà.
  Les prix viennent de `app/plans.py` — l'ancienne page annonçait encore la formule Maison
  à 249 € pour 1 200 appels. Polices, styles et script sont servis d'ici : la politique de
  sécurité n'autorise aucune origine tierce.
- **« Rappelez-moi »** : le visiteur laisse son numéro, Marie l'appelle dans la minute et
  répond pour l'établissement de démonstration (`app/rappel.py`, décision de Helmi le
  02/10). C'est la seule route qui fait composer un numéro à un inconnu, donc surtout des
  refus : numéros de métropole seulement (ni 08, ni outre-mer, ni étranger), deux rappels
  par numéro et par jour, trois demandes par adresse et par heure, **quinze appels par
  jour pour tout le site**, rien entre 22 h et 8 h, quatre minutes au plus par appel.
- Si l'assistante vient de tomber en panne, aucun rappel ne part : on ne fait pas
  entendre un silence à un restaurateur qui l'essaie.
- `/twilio/rappel`, signée : ce que Twilio lit quand la personne décroche. L'appel n'est
  pas renvoyé vers le restaurant s'il s'interrompt.
- Un appel passé par Marie est chiffré au **tarif sortant** (0,0404 $/min vers un portable,
  0,0187 vers un fixe, grille Twilio du 01/10/2026) et étiqueté « Rappel du site » au
  journal. Au plafond, la dépense reste sous 4 $ par jour (quinze appels de quatre
  minutes vers un portable, autres postes à environ 0,02 $/min).
- Un e-mail « Demande de rappel depuis le site » à `RAPPEL_NOTIFIER` (sinon `ADMIN_EMAIL`).
- Migration v18 : table `rappels`, colonne `calls.sortant`. Les demandes sont effacées au
  bout de 30 jours (`RETENTION_RAPPEL_JOURS`) et par l'effacement à la demande.

### Corrigé à la revue (03/10/2026)
- Le compteur de demandes par adresse a son propre verrou : il partageait celui de
  l'inscription en base, et une base occupée aurait figé la boucle qui porte l'audio.
- « +33 (0)6 12 34 56 78 » est accepté : le zéro entre parenthèses n'est plus compté.
- Une demande de rappel trop grosse est refusée sur sa taille annoncée, sans être lue.

### Limites connues
- Aucun vrai rappel n'a encore été passé : le chemin Twilio est testé avec un bouchon.
- La page n'a pas de mentions légales (éditeur, hébergeur) : elles sont à fournir.
- Répondeur : si la personne ne décroche pas et que sa messagerie prend l'appel, Marie
  parle à la messagerie jusqu'à ce que le silence la fasse raccrocher.

## Non publiée — les consignes du dépôt, tenues par un test (02/10/2026)

### Ajouté
- **`CLAUDE.md`** : les commandes, le cycle de travail (branche, tests, revue, fusion,
  déploiement, recette), l'architecture, les règles que les tests imposent, les pièges du
  moteur d'appel, et quels documents croire.
- **`test_documentation.py`** : `CLAUDE.md` et le guide de l'admin ne peuvent plus citer un
  fichier, une fonction ou un test qui n'existe pas ; le nombre de contrôles de
  supervision annoncé dans la documentation doit être le vrai.

### Corrigé
- Le guide de l'admin (`.claude/skills/admin-ui`) demandait `asyncio.create_task`,
  annonçait des graphiques sans script et donnait une commande de test qui ne pouvait pas
  tourner. Il gagne l'heure de Paris, le thème sombre, les pièges rencontrés et la façon
  de vérifier un rendu.
- `README.md` et `ARCHITECTURE.md` présentaient encore Moshi comme la voix : c'est
  Mistral depuis le 28/09. Ajout du renvoi en cas de panne et du carnet resOS ;
  « 14 contrôles » devient 17. Les documents historiques sont signalés comme tels.

## Non publiée — en cas de panne, l'appel est renvoyé vers le restaurant (ASSISTANTE-118, 01/10/2026)

Demande de Helmi : un client ne doit plus rester sans personne quand l'assistante tombe.

### Ajouté
- **Numéro de secours** par établissement (« Fiche établissement ») : le fixe du
  restaurant, sinon le portable du gérant. Réglé par le restaurateur lui-même.
- **Renvoi en cas de panne.** Quand la voix, le modèle ou la transcription échoue deux
  fois de suite pendant un appel, ou que le pipeline s'arrête sur une erreur, l'assistante
  rend la ligne sans raccrocher : le restaurant sonne 15 s pendant ses horaires
  d'ouverture (une heure d'avance comprise). Pendant trois minutes, les appels suivants
  sont renvoyés dès le décroché, puis un appel retente l'assistante.
- **Répondeur** hors horaires, sans numéro de secours, ou si personne ne décroche : le
  client laisse un message. Le restaurant reçoit un e-mail tout de suite ; le message
  s'écoute dans la fiche de l'appel. Enregistré par Twilio, il est rapatrié chez nous puis
  effacé de chez Twilio.
- **Journal des appels** : « Renvoyé au restaurant », « Message vocal », « Panne · client
  non servi », avec la raison ; filtre « Pannes ». Le coût compte la communication
  renvoyée (0,0187 $/min vers un fixe, 0,0404 $/min vers un portable).
- **Supervision** : contrôle « Appels passés en secours » (panne si un appel y est passé
  dans les 20 dernières minutes) ; alerte quand un établissement n'a pas de numéro.
- **Essayer le renvoi** (super-admin) : pendant trois minutes, les appels d'un
  établissement sont traités comme une panne.

### À faire à la main
- Le cas « notre serveur ne répond plus du tout » se règle dans la console Twilio
  (adresse de secours du numéro) : procédure dans docs/TWILIO_SETUP.md §6.

## Non publiée — thème sombre au choix, graphiques au survol, fiche d'une réservation (ASSISTANTE-114 à 116, 29/09/2026)

Retours de Helmi après la recette du sprint 2.

### Ajouté
- **Thème clair ou sombre au choix** (ASSISTANTE-114) : trois pastilles dans la barre
  latérale — comme l'appareil (par défaut), clair, sombre. La page bascule aussitôt ; le
  choix est retenu sur l'appareil et posé par le serveur, sans flash du mauvais thème.
- **Graphiques interactifs** (ASSISTANTE-115) : au survol, au toucher ou au clavier (les
  flèches parcourent les jours), une bulle donne la date en toutes lettres et le nombre ;
  la barre visée reste pleine, les autres s'effacent. Un jour à zéro se survole aussi.
- **Fiche d'une réservation** (ASSISTANTE-116) : dans la vue du jour, un clic sur une
  réservation ouvre la réservation, son client (numéro cliquable, ses autres réservations,
  ses appels) et la conversation qui l'a prise (enregistrement, transcription). Marche
  aussi pour resOS ; sans appel rattaché, la fiche le dit sans supposer d'où elle vient.

### Changé
- **Tout l'admin est à l'heure de Paris** (01/10). Les appels (journal, salle de contrôle,
  fiche d'un appel, diagnostic), la date d'annulation d'une réservation, la date de
  création d'un compte et l'heure du relevé de « Santé & coûts » s'affichaient en UTC :
  deux heures trop tôt l'été, une l'hiver, et la veille pour ce qui se passe après
  minuit. La base reste en UTC ; seul l'affichage change.

### Corrigé
- Le bouton « Quitter » reprend son apparence discrète (il prenait le bleu des boutons
  principaux).
- **Après la revue de code du 01/10** (fiche d'une réservation, graphiques, thème) :
  - une adresse de fiche mal formée (« ² », vingt-cinq chiffres) donnait une erreur 500 :
    c'est une réservation introuvable ;
  - la fiche ne dit plus « saisie à la main » ni « première réservation » sans le savoir,
    et date l'appel à l'heure du restaurant (un appel de 00 h 30 passait à la veille) ;
  - un identifiant refusé par resOS est « introuvable », pas une panne à réessayer ;
  - le super-admin revient au jour de l'établissement d'où il venait ; « Marquer
    traité » depuis la fiche y revient ;
  - au doigt, la bulle des graphiques ne se referme plus à la levée du doigt ; sans
    script, chaque jour garde l'infobulle du navigateur ;
  - un choix de thème qui n'a pas pu être enregistré ne reste pas affiché ;
  - le numéro et les notes d'une carte du jour se sélectionnent de nouveau.

## Non publiée — Sprint 2 « Admin pro » : une interface restaurateur plus simple (SCRUM-106 à 113, 28/09/2026)

Retours de Helmi sur l'admin, traités dans le sprint 2.

### Ajouté
- **Réservations en calendrier** (SCRUM-112), dans les deux interfaces : vue **mois**
  (réservations et couverts par jour, un clic ouvre le jour), **semaine** (chaque
  réservation à son heure) et **jour** (cartes à modifier ou annuler sur place). Jours
  fermés grisés d'après les horaires ; statut écrit sur chaque réservation (confirmée,
  à valider, annulée…). Sur téléphone, la vue du jour s'ouvre d'elle-même. Filtre par
  établissement pour le super-admin. Pour un établissement sur resOS, le calendrier lit
  **le carnet resOS** (paginé) ; un resOS injoignable est signalé, jamais montré comme
  une journée vide. La liste d'avant reste disponible (vue « Liste »).
- **« Ce que l'IA sait » se modifie sur place** (SCRUM-111) : chaque fiche s'édite dans
  sa carte, s'ajoute, se supprime ; confirmation visible ; jauge de 12 000 caractères
  conservée. Même texte `## Titre` dans le prompt qu'avant. Un enregistrement fait sur
  une base modifiée entre-temps est refusé au lieu d'écraser.

### Changé
- **Les horaires d'ouverture vivent dans « Ce que l'IA sait »** (SCRUM-110) : plus de
  menu à part ; l'ancienne adresse y redirige.
- **La fiche « Établissement » ne garde que l'identité** (SCRUM-111), en rubriques ; elle
  n'écrit plus ni la base ni l'accueil.
- **Accueil de ~5 s au lieu de ~9** (SCRUM-107) : « Bonjour, <restaurant>. Je suis
  l'assistante vocale, cet appel est enregistré. » — sans « un instant s'il vous plaît »
  quand la voix n'a pas de GPU à réveiller. Formulation à valider (docs/RGPD.md §7).
- **L'aperçu de l'accueil suit la voix choisie** et affiche le texte prononcé (SCRUM-106) :
  le navigateur ne rejoue plus une ancienne copie.
- **Fiche d'un appel côté restaurateur** (SCRUM-108) : l'enregistrement (les deux voix
  mêlées) et la transcription ; le diagnostic technique est réservé au super-admin.
- **Graphiques « par jour »** (SCRUM-113) : la valeur au-dessus de chaque barre.

## Non publiée — bascule sur Mistral, toutes ses voix, et le vrai coût des appels (SCRUM-94, SCRUM-99, 28/09/2026)

Décision de Helmi après trois appels en voix Marie (« c'est hyper fluide ») : on bascule
sur Mistral et on abandonne Moshi.

### Changé
- **Toutes les voix viennent de Mistral** : les 30 voix du catalogue (Marie, Jane,
  Oliver, Paul, chacune en plusieurs tons), relues chez Mistral toutes les heures. Un
  lecteur fait entendre la voix sélectionnée avant de l'enregistrer. Voix par défaut du
  parc : Marie neutre. Les voix Moshi ne se choisissent plus ; Moshi reste le secours
  si la clé Mistral manque. La clé Mistral devient une variable obligatoire.
- **Le coût des appels est chiffré sur ce qu'ils consomment**, poste par poste :
  Twilio à la minute entamée (0,01 $, facture du numéro français, au lieu de
  0,0085 $/min), Deepgram, Gemini aux jetons (au lieu d'un forfait), la voix aux
  caractères (au lieu de 2 c/min de GPU). La répartition de « Santé & coûts » est la
  somme exacte des postes. Appel 204 : 9 c au lieu de 13 c.
- **Le banc d'essai ne gonfle plus les coûts** : huit de ses appels (10/09), restés
  connectés sept heures pour ~80 s de conversation, comptaient 80 $ fictifs sur les
  97 $ affichés. Ils sont chiffrés sur leur durée active, sans téléphone : l'historique
  passe à 17,65 $.

## Non publiée — la voix « Marie » de Mistral, au choix, avec son ton (SCRUM-94, 28/09/2026)

### Ajouté
- **Marie (Voxtral, Mistral)** rejoint le catalogue des voix, en quatre tons : neutre,
  joyeuse, curieuse, enthousiaste. Chaque ton s'écoute sur la page « Voix & accueil ».
  Retenue par Helmi au test d'écoute à l'aveugle du 28/09 (dix phrases d'appels réels).
- **Pas de GPU derrière** : ni réveil de 46 s ni musique d'attente au décroché, premier
  son d'une réponse en ~0,5 s. La voix coûte ~0,8 centime par appel, contre 8,5 pour le
  GPU sur les 201 appels enregistrés.
- Contrôle de supervision **« Voix Voxtral »**, le seizième : clé absente (panne),
  synthèses échouées dans l'heure (attention).
- La liaison Twilio ne change pas : seule la voix passe par l'API de Mistral, en flux.
  Mise en service : `docs/VOXTRAL.md` (une clé `MISTRAL_API_KEY` dans le `.env`).

## Non publiée — elle écoute mieux, redemande moins, rappelle les numéros masqués (27/09/2026)

Tiré de 19 appels de test (183 à 201) : 27 « Vous êtes toujours là ? », des questions déjà
répondues posées deux fois, et un appel masqué que personne ne pouvait rappeler.

### Corrigé
- **La voix entendue sans mots est rattrapée.** 17 relances sur 27 tombaient sur quelqu'un
  qui venait de répondre : le VAD l'entendait, la transcription en direct ne rendait rien
  (« 20 heures, 20 heures », « Ouais ouais »). Le segment est désormais retranscrit à part ;
  si rien ne revient, l'assistante dit tout de suite « Pardon, je n'ai pas bien entendu »,
  et plus jamais « Vous êtes toujours là ? » à quelqu'un qui vient de parler.
  `RATTRAPAGE_PAROLE=off` le retire.
- **« Je vérifie tout de suite. » est suivi d'une vérification.** 8 relances venaient d'une
  annonce restée sans outil. Le modèle est relancé 1,5 s après, au lieu de laisser
  l'appelant huit secondes dans le silence.
- **Ce qui a été dit une fois est acquis** : changer de jour ne fait plus redemander l'heure
  ni le nombre de personnes. Au banc, sur le modèle de production : de 4/10 à 8/10 sur le
  pire cas, 10/10 sur les trois autres.
- **Appel masqué** : l'assistante demande un numéro de rappel avant de transmettre un
  message, le relit, et il figure dans l'e-mail et sur la fiche de l'appel. Pour une
  réservation, il est ajouté aux notes.
- **Les correctifs de l'admin atteignent le navigateur** : les feuilles de style portent
  l'empreinte de leur contenu. La correction des cases à cocher était en ligne, mais le
  navigateur gardait l'ancienne page.

## Non publiée — la page Carnet resOS disait faux (27/09/2026)

Deux jours de tests en échec (appels 179 et 180 compris) sans que rien ne le montre : la case
« resOS lent » et quatre jours fermés étaient cochés, mais **affichés vides**.

### Corrigé
- **Les cases à cocher de tout l'admin** perdaient leur coche : une règle CSS imposait un fond
  à tous les champs, cases comprises. Touchait aussi la case « Fermé » de la page Horaires.
- **La panne simulée ne se mêle plus aux réglages** : trois boutons toujours visibles
  (« resOS fonctionne », « en panne », « lent »), l'actif en bleu, l'état écrit en toutes
  lettres, un bandeau rouge tant qu'elle dure, et **arrêt automatique au bout de 30 min**.
- **Chaque enregistrement est confirmé** en tête de page, relu dans l'état.
- Pendant une panne simulée, la page lit le carnet fictif directement au lieu d'attendre 4 s
  à chaque rafraîchissement. Dates courtes (« ven. 2 oct. »), sources en français.

## Non publiée — resOS : bac à sable, horaires, panne (SCRUM-86, 87, 93, 25/09/2026)

### Ajouté
- **resOS en bac à sable** : un troisième carnet, « resOS — bac à sable ». On appelle le vrai
  numéro et la réservation arrive dans un faux resOS propre à l'établissement, conservé d'un
  déploiement à l'autre, sans clé ni abonnement. Tout le client resOS s'exécute.
- **Page « Carnet resOS »** dans l'admin : état de resOS, réservations à venir rafraîchies
  toutes les 5 s, journal de ce que l'assistante a demandé. En bac à sable, le super-admin
  joue le restaurant : valider ou refuser, capacité, services, jours fermés, panne ou
  lenteur simulées. Recette pas à pas : section R de `docs/RECETTE.md`.
- **Les horaires viennent de resOS** (SCRUM-86) : lus en tâche de fond, gardés 10 minutes,
  servis au prompt et au refus des créneaux fermés comme ceux saisis chez nous. Aucune
  attente pour l'appelant. La page Horaires passe en lecture seule pour ces établissements.
- **Panne de resOS** (SCRUM-87) : coupe-circuit de 60 s (pas de second blanc de 4 s dans le
  même appel), e-mail immédiat au restaurateur (un par demi-heure), et un contrôle de
  supervision « Carnet resOS », le quinzième.

## Non publiée — le carnet resOS (SCRUM-82 à 85, 25/09/2026)

Un établissement peut écrire ses réservations dans **resOS**, le logiciel de réservation du
restaurant, au lieu de notre base. resOS n'ayant ni bac à sable ni compte de test, tout est
développé contre un faux resOS calqué sur sa documentation publique (`docs/RESOS.md`).

### Ajouté
- **Un carnet par établissement** (`app/connecteurs/`) : `run_tool` parle à une interface
  commune, et le choix se fait dans la fiche de l'établissement (super-admin). Par défaut,
  et pour tout le parc existant : notre carnet, sans aucun changement.
- **resOS** : vérifier un créneau, créer, retrouver par numéro, modifier, annuler. Les
  réservations arrivent en **demandes à valider par le restaurant** (`request`, source
  `phone`), et l'assistante ne dit jamais « confirmé ».
- Tout ce qui n'est pas une réponse sûre de resOS (plus de 4 s, panne, clé absente ou
  refusée) devient un refus que le modèle relit : il **n'annonce rien** et prend un
  message. Le créneau est revérifié juste avant d'écrire, et le numéro de l'appelant est
  revérifié sur chaque réservation rendue par resOS.
- Un appel qui a réservé dans resOS garde la référence resOS (`calls.reservation_externe`)
  et compte parmi les appels avec réservation.

### À poser au déploiement
Rien pour le parc existant (migration v14 automatique). Pour un restaurant sous resOS :
`RESOS_API_KEYS=<id>=<clé>` dans le `.env`, puis « resOS » dans sa fiche.

## Non publiée — quand le GPU ne vient pas (25/09/2026)

Trois pannes de capacité chez Modal en deux jours (plus de GPU L4 en Europe : 23/09, 24/09 à
23 h 15, 25/09 à 8 h 48). Après 90 s de musique d'attente, la reprise partait vers un serveur
absent : l'appelant n'entendait plus **rien** et finissait par raccrocher (appels 158 et 159).

### Ajouté
- **Message « rappelez dans quelques minutes »** : si le GPU n'a pas répondu à la fin de
  l'attente (`MOSHI_HOLD_MAX_SECONDS`, 90 s), l'assistante s'excuse, demande de rappeler et
  raccroche. Le message est pré-rendu dans la voix de l'établissement, au même moment que
  l'accueil — c'est le seul moment où le serveur de voix est sûr de répondre. Il figure dans
  la transcription, et l'appel reste compté parmi les « Appels muets » : le filet amortit la
  panne, il ne la cache pas. `MOSHI_INDISPONIBLE_TEXT` remplace le texte.
- Le contrôle « Voix d'accueil » signale un message d'indisponibilité manquant.

### À poser au déploiement
Rien. Le message est rendu au démarrage, dès que le GPU répond (quelques secondes).

## Non publiée — ce que quatre vrais appels ont montré (20/09/2026)

Quatre appels passés le 20/09 au soir, réécoutés et relus en base (appels 138 à 141).
Rien ici ne vient d'une revue de code : ce sont quatre défauts qu'on entend.

### Corrigé
- **L'appelant qui changeait de langue n'était plus entendu du tout.** Une conversation
  commencée en français puis basculée en anglais rendait l'assistante sourde : la langue
  était tranchée une fois pour toutes et le STT verrouillé sur `fr`, où l'anglais ne
  produit rien. La langue se re-décide désormais en continu, et deux tours de parole sans
  la moindre transcription font revenir au bilingue — c'est le seul symptôme qui survive
  au verrou, puisque Deepgram verrouillé n'étiquette plus les langues.
- **L'accueil figurait deux fois** dans le contexte du modèle et dans la transcription
  rendue au restaurateur : la phrase de reprise était à la fois pré-inscrite et inscrite
  par l'agrégateur. Règle posée : on ne pré-inscrit que ce qui ne traverse pas le pipeline.
- **Un nom connu était épelé** au lieu d'être prononcé (« Is it H E L M I, like last
  time? ») : les noms tout en capitales sont remis en casse de titre, à l'écriture comme
  à la lecture, et le prompt le dit.
- **Résumé d'appel** : la colonne `calls.summary` n'avait jamais été écrite. Une phrase
  est produite après le raccroché, en tâche de fond, et remplace l'extrait brut dans la
  liste des appels et le tableau de bord. `RESUME_APPELS=0` coupe la fonction.

### À poser au déploiement
Rien d'obligatoire. `RESUME_APPELS` et `RESUME_MODEL` sont facultatifs (défauts : actif,
modèle de la conversation).

## v1.1.0 — déployée le 19/09/2026 — l'audit du 18/09/2026

Fusionnée dans `main` par la PR #96 et déployée sur le VPS le 19/09 (`scripts/deploy.sh`,
base migrée en v13).

### Pourquoi cette version
L'audit du 18/09 a relevé cinq défauts critiques : webhooks Twilio non authentifiés,
une route publique qui listait les réservations, un GPU ouvert avec la clé de
démonstration, des sauvegardes restées sur la machine, et une disponibilité toujours
« oui ». Cette version les ferme, et fait du produit un seul chemin d'appel.

### Sécurité
- **Signature Twilio** (`X-Twilio-Signature`) vérifiée sur les webhooks et la poignée de
  main du flux ; `PUBLIC_URL` ; mode `log` pour observer avant `enforce`.
- **Route publique `/tenants/{id}/reservations` supprimée.**
- **Clé privée du serveur de voix** posée au démarrage du conteneur Modal ; `modal
  deploy` refuse `public_token` ; contrôle de supervision.
- **Appels masqués** : les numéros de substitution de Twilio (`+266696687`…) ne sont plus
  pris pour un vrai numéro — tous les appels masqués partageaient leurs réservations.
- Jeton de supervision en en-tête seulement ; numéro de l'appelant tronqué dans les
  journaux ; saisies de l'admin vérifiées côté serveur (numéro E.164, dates, couverts).

### Ajouté
- **Horaires d'ouverture** par établissement, appliqués par le serveur : un créneau fermé
  est refusé avec les plages ouvertes du jour (écran « Horaires d'ouverture »).
- **E-mail au restaurateur** à chaque réservation, modification, annulation et message
  pris (SMTP générique, `SMTP_*`).
- **Nom du dernier passage** proposé au lieu d'être redemandé.
- **Copie des sauvegardes hors du serveur** (rclone), surveillée.
- Sonde de vie Docker (`HEALTHCHECK`), index SQLite v13, `ruff` et `pip-audit` en CI.
- **GPU chaud aux heures de service seulement** (`MOSHI_KEEPWARM_HEURES`), désactivé par défaut.

### Modifié
- **Un seul chemin d'appel** : Media Streams + Pipecat + Deepgram + moshi-server. Retirés :
  la boucle Gather/Say, Pocket TTS, Kyutai en PyTorch, Cartesia, l'ancienne app Modal
  (lisibles au tag `archive/moteurs-locaux`). Image 5,67 Go → 845 Mo.
- **Dépendances verrouillées** (`requirements.lock`), installées telles quelles par l'image
  et la CI.
- **Les fenêtres du tableau de bord commencent à minuit, heure de Paris** (et non plus
  UTC) : les totaux « aujourd'hui », « ce mois-ci » et les stats par jour peuvent bouger
  légèrement par rapport à v1.0.
- Coût Deepgram estimé au tarif `multi` (0,0092 $/min, borne haute) ; `docs/TARIFS.md`
  recalculé.
- « Annuler » dans l'admin annule la réservation (ligne barrée) au lieu de l'effacer.
- Supprimer un établissement supprime aussi ses messages et ses enregistrements.
- Le restaurant de démonstration n'est plus réaligné à chaque démarrage : l'admin fait
  foi (`SEED_DEMO`).
- **23 réglages atteignent enfin le conteneur** (GPU chaud, coûts, fuseau, délais…) :
  absents de `docker-compose.yml`, ils étaient sans effet même posés dans le `.env`.
- Accès SQLite hors de la boucle d'événements (appel et admin) ; tâches de fond
  retenues ; `lifespan` ; journaux loguru partout.

### À poser au déploiement
- `.env` du VPS : `PUBLIC_URL=https://app.helmane.fr`, `TWILIO_SIGNATURE=log` (48 h, puis
  retirer), `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` ; le jeton
  Twilio **courant**.
- Clé du serveur de voix : `MOSHI_TTS_API_KEY` dans le `.env` local et celui du VPS,
  puis `modal deploy` (ordre dans `docs/MODAL.md`).
- `/opt/backups/backup.env` avec `RCLONE_REMOTE`, et le script de sauvegarde réinstallé.
- Dans l'admin : les horaires de chaque établissement.

## v1.0.x — août et septembre 2026 — déployé depuis `main`, sans tag

- Supervision extérieure : sonde `/supervision`, alerte GitHub Actions, relève des
  alertes Twilio (#24).
- Sauvegarde quotidienne vérifiée ; API publiée sur `127.0.0.1` seulement ; audit de
  sécurité de l'admin (#21).
- Grille tarifaire (#29), plafond mensuel qui ne coupe jamais la ligne (#31).
- RGPD : durées de conservation appliquées, droit à l'effacement (#22).
- Modification et annulation de réservation par téléphone (#33).
- Enregistrement des appels et journal de bord de l'interaction (#88).
- Messages pris pour l'équipe : la promesse de rappel laisse une trace (#32).
- Banc d'essai d'appels simultanés ; modèle de fin de tour partagé (#40).

## v1.0.0 — 2026-07-31 — Première version en production

### Pourquoi cette version
Premier jalon où le service tourne **en production, sur son propre domaine, validé par
des appels réels** : `https://app.helmane.fr`, sur un VPS parisien qui survit au reboot,
avec la marque Helmane. Les versions précédentes étaient des étapes techniques.

### Ajouté
- **Plateforme admin v3** : coquille à barre latérale, vue du parc et écran « Santé &
  coûts » pour le super-admin, salle de contrôle par établissement. Tout ce qui est
  affiché est mesuré — aucune métrique inventée.
- **Voix par établissement** (migration v5) : catalogue fermé de 7 voix françaises
  choisies à l'écoute, servies par le serveur Modal. La liste est fermée parce qu'une
  voix inconnue n'est PAS refusée par moshi-server : elle est remplacée en silence.
- **Mesure du blanc ressenti** tour par tour (migration v4), stockée en base — les
  journaux de conteneur, eux, repartent de zéro à chaque déploiement.
- **Numéro de l'appelant** enregistré (migration v3).
- **Domaine et HTTPS** : Caddy accepte plusieurs noms d'hôte, ce qui permet de basculer
  de domaine sans fenêtre de coupure sur le webhook Twilio.

### Corrigé
- **Panne de production** : un `reasoning` passé en mot-clé nu levait un `TypeError`
  dans le SDK OpenAI et rendait TOUS les appels muets. Le bon passage est `extra_body`.
- **Blanc de 6 s** avant chaque appel d'outil : le raisonnement de Gemini 2.5, actif par
  défaut. Coupé (`LLM_REASONING=off`) — 6,16 s → 0,59 s mesurés.
- **Coupures sur bruit** : Pipecat ouvrait un tour sur VAD *ou* transcription ; un souffle
  tranchait la phrase et rien ne repartait, d'où les « allô » du client
  (`INTERRUPTION=mots`).
- **Fuite entre établissements** : le nom des autres enseignes apparaissait dans les
  lignes de réservation d'un restaurateur après une édition.
- Relance incongrue après un « au revoir » : l'assistante raccroche désormais.

### Réglages figés après validation à l'oreille
`DEEPGRAM_MODEL=nova-3` (noms propres), `VAD_STOP_SECS=0.5`, `INTERRUPTION=mots`,
`LLM_REASONING=off`. Blanc médian mesuré : **1,16 s** (p90 1,58 s).

### Mesuré
Coût par appel : **11,4 ¢** en conditions réelles. 209 tests, aucun appel réseau.

---

## Archive — branche `claude/moshi-gpu-980ti-0w8lsv` (abandonnée) — Pocket TTS sur GPU

### Pourquoi
Sur CPU, `french_24l` met 5-10 s à produire son premier morceau → voix saccadée. Un
GPU (même une GTX 980 Ti / Maxwell) rend la génération temps réel (< 1 s).

### Ajouté
- `_select_device()` dans `pocket_tts.py` : `POCKET_TTS_DEVICE=auto` (défaut : GPU si
  dispo, sinon CPU) / `cuda` / `cpu`. Le modèle (`nn.Module`) est déplacé sur le GPU
  via `.to(device)` avant la préparation de l'état de voix.
- `api/Dockerfile.gpu` : PyTorch **2.5.1 + CUDA 12.1** épinglé — dernière lignée
  compatible Maxwell (sm_52) satisfaisant pocket-tts (>=2.5).
- `docker-compose.gpu.yml` : surcouche réservant un GPU NVIDIA + `POCKET_TTS_DEVICE=cuda`.
  Lancement : `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build`.
- `POCKET_TTS_DEVICE` dans le compose de base et `env.example` ; `docs/GPU.md`
  (prérequis nvidia-container-toolkit, lancement, vérification, note sur le 1.6B).
- 4 tests de sélection de device. 53 tests au total, toujours zéro appel réseau.

---

## v0.2.0 — 2026-07-14 — Pipeline vocal temps réel + voix française fiable

Point stable pour un premier client. Ce qui marche de bout en bout :

- **Routage multi-tenant** par numéro Twilio appelé, avec réalignement automatique du
  tenant de démo sur `TWILIO_NUMBER` au démarrage (fin du « ce numéro n'est pas encore
  configuré » quand un mauvais numéro s'était figé dans le volume Docker).
- **Mode `gather` (défaut, recommandé sur CPU)** : voix **neuronale** Amazon Polly
  française (Léa) via `<Say voice="Polly.Lea-Neural">` — naturelle, incluse dans Twilio,
  zéro latence, zéro clé. Surchargeable par `TWILIO_VOICE`.
- **Mode `stream`** : pipeline Pipecat temps réel (Deepgram STT + LLM OpenRouter +
  Pocket TTS / Cartesia). Voix Kyutai « Pocket TTS » branchée.
- **LLM** OpenRouter avec function calling (`check_availability`, `create_reservation`).

**Limite connue, cause de la voix saccadée en `stream` :** Pocket TTS français
(`french_24l`) sur **CPU** met 5–10 s à produire son premier morceau, au-delà du seuil
de Pipecat → l'audio est haché ou perdu. Sur CPU, utiliser `VOICE_MODE=gather` (Polly)
ou `TTS_PROVIDER=cartesia`. Le support **GPU** (qui rend Pocket TTS temps réel) est la
suite, sur une branche dédiée.

---

## 2026-07 — Voix Kyutai (Pocket TTS) dans le pipeline streaming

### Pourquoi
La voix du mode `gather` est le TTS robotique de Twilio. L'utilisateur veut la voix
de la famille Unmute (Kyutai). Kyutai TTS 1.6B (la voix exacte d'unmute.sh) exige un
GPU ; **Pocket TTS** (kyutai-labs, MIT, 100 M params) offre la même famille de voix
en tournant sur **CPU** — dont la voix `estelle`, littéralement un échantillon du
site Unmute.

### Ajouté
- `api/app/voice/pocket_tts.py` : `PocketTTSService(TTSService)` — service TTS Pipecat
  basé sur Pocket TTS. Chargement du modèle + voix en singleton (une fois par process),
  génération streaming (`generate_audio_stream`) offloadée en thread, resample vers le
  8 kHz Twilio, sérialisée par un lock (modèle non thread-safe). Voix préréglée
  (`estelle`, défaut) ou clonage depuis un extrait audio (`POCKET_TTS_VOICE=chemin/url`).
- `bot.build_tts()` : sélecteur `TTS_PROVIDER` — **`pocket` par défaut** (voix Kyutai,
  CPU, sans clé), `cartesia` en alternative API.
- `pocket-tts[audio]` dans requirements ; `libsndfile1` dans le Dockerfile (soundfile) ;
  `TTS_PROVIDER`/`POCKET_TTS_VOICE`/`POCKET_TTS_LANGUAGE`/`HF_HOME` dans compose et env.
- 9 nouveaux tests (`api/tests/test_pocket_tts.py`, modèle mocké) : résolution de voix,
  frames audio produites, gestion d'erreur, singleton, sélecteur `build_tts`. 45 tests
  au total, toujours zéro appel réseau.

### Notes
- Le français impose la variante `french_24l` (bug attrapé en test réel : `french`
  seul lève une ValueError côté pocket-tts).
- Limite assumée phase A : génération TTS sérialisée (faible simultanéité). Le 1.6B GPU
  reste la cible phase B pour la qualité maximale et la concurrence.

---

## 2026-07 — Bascule du LLM vers OpenRouter (choix libre du modèle)

### Pourquoi
Blocage de facturation côté compte Anthropic (compte classé « Team », questionnaire
Trust & Safety requis avant tout achat de crédits). OpenRouter donne accès à
n'importe quel modèle (Claude, GPT, Gemini, Llama, Mistral, DeepSeek...) avec une
seule clé, un onboarding paiement plus simple, et surtout des **modèles gratuits
avec function calling** (`openrouter/free`) — débloque les tests immédiatement sans
dépenser, tout en donnant la liberté de choix de modèle.

### Modifié
- `api/app/llm.py` : `anthropic` → `openai` (`AsyncOpenAI` pointé sur
  `https://openrouter.ai/api/v1`), boucle d'outils réécrite au format Chat
  Completions (function calling OpenAI : `tool_calls`, arguments JSON en chaîne).
  `MODEL` par défaut : `openrouter/free`. Invariant conservé : l'historique retourné
  ne contient jamais le message système (ré-injecté à chaque appel) — **aucun
  changement dans `main.py`**
- `api/app/voice/bot.py` : `AnthropicLLMService` → `OpenAILLMService` (pipecat,
  pointé sur OpenRouter) — seul le bloc de construction du service change, le reste
  du pipeline (contexte, VAD, function calling) était déjà écrit de façon neutre
- `api/app/requirements.txt` : `anthropic` → `openai`, extra pipecat
  `[anthropic,...]` → `[openai,...]`
- `env.example`, `docker-compose.yml`, `api/tests/conftest.py` : `ANTHROPIC_API_KEY`
  → `OPENROUTER_API_KEY` (+ `OPENROUTER_BASE_URL`, `OPENROUTER_SITE_URL`,
  `OPENROUTER_APP_NAME` optionnels)
- Tests : `TestLLMToolLoop` réécrite au format OpenAI (2 nouveaux tests : schéma
  d'outils, robustesse aux arguments JSON malformés du modèle) — 36 tests au total,
  toujours zéro appel réseau. `test_voice_stream.py` inchangé (déjà neutre vis-à-vis
  du fournisseur LLM)

---

## 2026-07 — Phase 2A : voix temps réel (Twilio Media Streams + Pipecat)

### Ajouté
- **Mode `VOICE_MODE=stream`** : pipeline audio streaming, latence ~1 s, barge-in
  - `api/app/voice/bot.py` : pipeline Pipecat 1.5 (Deepgram STT fr → Claude + outils
    → Cartesia TTS fr), VAD Silero + smart-turn v3, raccrochage auto si identifiants
    Twilio présents
  - `POST /twilio/voice` renvoie `<Connect><Stream>` en mode stream (tenant transmis
    via `<Parameter To>`)
  - `WS /ws/voice` : poignée de main Media Streams, résolution du tenant, garde-fous
    (tenant inconnu → 1008, protocole invalide → 1002, crash pipeline → 1011)
- Cerveau partagé entre les deux modes : `llm.run_tool` (ex-`_run_tool`), mêmes
  `TOOLS` et prompt système
- 17 nouveaux tests (`api/tests/test_voice_stream.py`) : TwiML stream, WebSocket,
  outils partagés, pont Pipecat — LLM et bot mockés, zéro réseau
- `/health` expose `voice_mode` ; `tests/test_e2e.sh` s'adapte au mode du serveur
- Docker : `pipecat-ai[anthropic,cartesia,deepgram,silero]~=1.5.0`, `libgomp1`,
  variables `VOICE_MODE`/`PUBLIC_WS_URL`/`DEEPGRAM_*`/`CARTESIA_*` dans compose et env

### Inchangé
- Mode `gather` par défaut : fonctionne sans clés Deepgram/Cartesia, aucun test cassé

---

## 2026-07 — Pivot SaaS : cerveau multi-tenant, abandon de Moshi (phase 1)

Refonte complète orientée produit SaaS (voir ROADMAP.md et ARCHITECTURE.md).

### Ajouté
- **Multi-tenant** : `api/app/tenants.py` — routage par numéro Twilio appelé (`To`),
  base de connaissances, langue et message d'accueil par commerce (SQLite)
- **LLM Claude** : `api/app/llm.py` — API Anthropic avec function calling
  (`check_availability`, `create_reservation`), KB du tenant en prompt système,
  mémoire de conversation par appel (`CallSid`)
- **Réservations SQLite** : `api/app/reservations.py` réécrit (fichier JSON → SQLite,
  rattachement au tenant), endpoint `GET /tenants/{id}/reservations`
- `ROADMAP.md` (phases 1 → 4) et `ARCHITECTURE.md`
- Suite de tests réécrite : 17 tests, LLM mocké (`api/tests/`)

### Supprimé
- Service **Moshi** (`moshi/`, `MOSHI_INTEGRATION.md`) et la réservation GPU du
  docker-compose : modèle speech-to-speech incontrôlable pour un assistant métier et
  coût GPU fixe — remplacé par des APIs cloud (justification dans ARCHITECTURE.md)
- `api/app/rag.py` (stub) : la KB tenant tient dans le prompt système ; un vrai RAG
  vectoriel est planifié en phase 4 quand les KB grossiront

### Modifié
- `api/app/main.py` : webhooks Twilio branchés sur le LLM (l'appel placeholder Moshi
  qui ne fonctionnait pas est supprimé), gestion d'erreur polie, échappement XML
- `docker-compose.yml` : 2 services (api + caddy), plus de GPU, volume de données
- `env.example` : `ANTHROPIC_API_KEY` + `LLM_MODEL` remplacent les variables Moshi
- `README.md` : pitch SaaS, quickstart sans GPU
- `tests/test_e2e.sh` : adapté au routage multi-tenant

---

## Historique — Nettoyage et optimisation (prototype Moshi)

- Suppression de `README.txt` obsolète, imports inutilisés nettoyés
- Refactorisation de `main.py` (fonctions `_call_moshi_api`, `_process_user_message`)
- Voir `git log` pour le détail du prototype initial basé sur Moshi/Vast.ai
