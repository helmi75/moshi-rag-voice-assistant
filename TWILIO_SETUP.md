# Guide de configuration Twilio

Ce guide vous explique comment connecter votre numéro Twilio à l'application.

## 📋 Prérequis

1. Un compte Twilio actif
2. Un numéro de téléphone Twilio (acheté ou d'essai)
3. L'application déployée et accessible publiquement (via Vast.ai ou un tunnel)

## 🔧 Configuration

### Étape 1 : Obtenir vos identifiants Twilio

1. Connectez-vous à votre [Console Twilio](https://console.twilio.com/)
2. Sur le tableau de bord, vous trouverez :
   - **Account SID** : Votre identifiant de compte
   - **Auth Token** : Votre token d'authentification (cliquez sur "view" pour le voir)
3. Copiez ces valeurs dans votre fichier `.env`

### Étape 2 : Configurer votre numéro Twilio

1. Allez dans [Phone Numbers > Manage > Active numbers](https://console.twilio.com/us1/develop/phone-numbers/manage/incoming)
2. Cliquez sur votre numéro de téléphone
3. Dans la section **Messaging**, configurez :
   - **A MESSAGE COMES IN** : 
     - Méthode : `HTTP POST`
     - URL : `https://VOTRE_DOMAINE_OU_IP/twilio/webhook`
     - Exemple : `https://votre-domaine.com/twilio/webhook` ou `http://VOTRE_IP:8000/twilio/webhook`
   
4. Dans la section **Voice & Fax**, configurez :
   - **A CALL COMES IN** :
     - Méthode : `HTTP POST`
     - URL : `https://VOTRE_DOMAINE_OU_IP/twilio/webhook`
     - Exemple : `https://votre-domaine.com/twilio/webhook` ou `http://VOTRE_IP:8000/twilio/webhook`

5. Cliquez sur **Save** pour enregistrer les modifications

### Étape 3 : Rendre votre application accessible

#### Option A : Avec un domaine (recommandé pour production)

1. Configurez votre domaine pour pointer vers l'IP de votre instance Vast.ai
2. Configurez Caddy pour HTTPS (voir `caddy/Caddyfile`)
3. Utilisez `https://votre-domaine.com/twilio/webhook` comme URL webhook

#### Option B : Avec ngrok (pour les tests)

1. Installez ngrok : `https://ngrok.com/download`
2. Démarrez un tunnel :
   ```bash
   ngrok http 8000
   ```
3. Copiez l'URL HTTPS fournie (ex: `https://abc123.ngrok.io`)
4. Utilisez `https://abc123.ngrok.io/twilio/webhook` comme URL webhook dans Twilio

#### Option C : IP publique directe (si accessible)

Si votre instance Vast.ai a une IP publique accessible :
- Utilisez `http://VOTRE_IP:8000/twilio/webhook`
- **Note** : Twilio préfère HTTPS, donc cette option n'est recommandée que pour les tests

### Étape 4 : Tester la configuration

#### Test SMS

1. Envoyez un SMS à votre numéro Twilio depuis votre téléphone
2. Vous devriez recevoir une réponse automatique
3. Vérifiez les logs :
   ```bash
   docker compose logs -f api
   ```

#### Test Appel vocal

1. Appelez votre numéro Twilio
2. Parlez à l'assistant
3. Vérifiez les logs pour voir les transcriptions

## 🔍 Vérification

### Vérifier que le webhook fonctionne

1. Testez l'endpoint directement :
   ```bash
   curl -X POST http://localhost:8000/twilio/webhook \
     -d "MessageSid=test123" \
     -d "From=+1234567890" \
     -d "Body=Bonjour"
   ```

2. Vous devriez recevoir du XML TwiML en réponse

### Vérifier les logs

```bash
# Logs de l'API
docker compose logs -f api

# Logs de Moshi
docker compose logs -f moshi

# Tous les logs
docker compose logs -f
```

## 🐛 Dépannage

### Le webhook ne reçoit pas les messages

1. **Vérifiez l'URL** : Assurez-vous que l'URL est accessible publiquement
2. **Vérifiez HTTPS** : Twilio préfère HTTPS, utilisez ngrok ou un domaine avec certificat
3. **Vérifiez les logs Twilio** : Allez dans [Monitor > Logs](https://console.twilio.com/monitor/logs) pour voir les erreurs
4. **Vérifiez les logs de l'API** : `docker compose logs api`

### Erreur 11200 (Connection Timeout)

- Votre serveur n'est pas accessible depuis Internet
- Vérifiez que le port 8000 (ou 80/443) est ouvert
- Utilisez ngrok pour tester

### Erreur 11205 (HTTP Retrieval Failure)

- L'URL du webhook est incorrecte
- Vérifiez que l'URL est accessible
- Vérifiez que l'endpoint retourne du TwiML valide

### Les messages ne sont pas traités

1. Vérifiez que Moshi est démarré : `docker compose ps`
2. Vérifiez les logs de Moshi : `docker compose logs moshi`
3. Testez l'endpoint de santé : `curl http://localhost:8000/health/moshi`

## 📝 Notes importantes

- **SMS** : Le webhook reçoit le texte directement dans le champ `Body`
- **Appels vocaux** : Twilio transcrit la voix en texte et l'envoie dans `SpeechResult`
- **TwiML** : Toutes les réponses doivent être en format TwiML (XML)
- **HTTPS** : Twilio recommande fortement HTTPS pour les webhooks en production

## 🔗 Ressources

- [Documentation Twilio Webhooks](https://www.twilio.com/docs/usage/webhooks)
- [TwiML Reference](https://www.twilio.com/docs/voice/twiml)
- [Twilio Console](https://console.twilio.com/)

