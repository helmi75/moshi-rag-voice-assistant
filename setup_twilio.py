#!/usr/bin/env python3
"""
Script pour configurer automatiquement Twilio avec l'application
Nécessite: pip install twilio
"""

import os
from twilio.rest import Client
from dotenv import load_dotenv

# Charger les variables d'environnement
load_dotenv()

def setup_twilio_webhook():
    """Configure le webhook Twilio pour le numéro de téléphone"""
    
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    phone_number = os.getenv('TWILIO_NUMBER')
    
    if not all([account_sid, auth_token, phone_number]):
        print("❌ Erreur: Variables d'environnement manquantes")
        print("Assurez-vous que TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN et TWILIO_NUMBER sont définis dans .env")
        return
    
    # Demander l'URL du webhook
    webhook_url = input("Entrez l'URL de votre webhook (ex: https://votre-domaine.com/twilio/webhook): ").strip()
    
    if not webhook_url:
        print("❌ URL webhook requise")
        return
    
    try:
        client = Client(account_sid, auth_token)
        
        # Récupérer le numéro de téléphone
        phone_numbers = client.incoming_phone_numbers.list(phone_number=phone_number)
        
        if not phone_numbers:
            print(f"❌ Numéro {phone_number} non trouvé dans votre compte Twilio")
            return
        
        phone = phone_numbers[0]
        
        # Mettre à jour la configuration
        print(f"\n📞 Configuration du numéro: {phone_number}")
        print(f"🔗 URL webhook: {webhook_url}\n")
        
        # Configurer pour les SMS
        phone.update(
            sms_url=webhook_url,
            sms_method='POST'
        )
        print("✅ Configuration SMS mise à jour")
        
        # Configurer pour les appels vocaux
        phone.update(
            voice_url=webhook_url,
            voice_method='POST'
        )
        print("✅ Configuration Voice mise à jour")
        
        print(f"\n✅ Configuration terminée!")
        print(f"Votre numéro {phone_number} est maintenant connecté à {webhook_url}")
        
    except Exception as e:
        print(f"❌ Erreur lors de la configuration: {str(e)}")
        print("\nVérifiez:")
        print("1. Vos identifiants Twilio sont corrects")
        print("2. Le numéro de téléphone appartient à votre compte")
        print("3. Vous avez les permissions nécessaires")

if __name__ == "__main__":
    print("=" * 60)
    print("Configuration automatique du webhook Twilio")
    print("=" * 60)
    print()
    setup_twilio_webhook()

