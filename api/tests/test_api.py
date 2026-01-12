"""
Tests unitaires et d'intégration pour l'API Moshi
Exécuter avec: pytest tests/ -v
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
import sys
import os

# Ajouter le chemin de l'application
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

from app.main import app

client = TestClient(app)


class TestHealthEndpoint:
    """Tests pour l'endpoint /health"""
    
    def test_health_returns_ok(self):
        """Vérifier que /health retourne un statut ok"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "moshi_api" in data


class TestTwilioVoiceWebhook:
    """Tests pour l'endpoint /twilio/voice"""
    
    def test_voice_initial_call_returns_gather(self):
        """Premier appel sans SpeechResult doit retourner un Gather"""
        response = client.post("/twilio/voice", data={
            "CallSid": "CA123456789"
        })
        assert response.status_code == 200
        assert "text/xml" in response.headers["content-type"]
        content = response.text
        assert "<Response>" in content
        assert "<Gather" in content
        assert "language=\"fr-FR\"" in content
    
    @patch("app.main.httpx.AsyncClient")
    def test_voice_with_speech_result(self, mock_client):
        """Appel avec SpeechResult doit appeler Moshi et répondre"""
        # Mock de la réponse Moshi
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Bonjour!"}}]
        }
        
        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_client.return_value = mock_instance
        
        response = client.post("/twilio/voice", data={
            "SpeechResult": "Je voudrais réserver une table",
            "CallSid": "CA123456789"
        })
        
        assert response.status_code == 200
        content = response.text
        assert "<Response>" in content
        assert "<Say" in content
    
    def test_voice_error_handling(self):
        """Tester la gestion d'erreur quand Moshi est down"""
        with patch("app.main.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post.side_effect = Exception("Connection refused")
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_client.return_value = mock_instance
            
            response = client.post("/twilio/voice", data={
                "SpeechResult": "Test",
                "CallSid": "CA123456789"
            })
            
            assert response.status_code == 200
            assert "erreur" in response.text.lower()


class TestTwilioSMSWebhook:
    """Tests pour l'endpoint /twilio/sms"""
    
    @patch("app.main.httpx.AsyncClient")
    def test_sms_webhook(self, mock_client):
        """Tester le webhook SMS"""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Réponse SMS"}}]
        }
        
        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_client.return_value = mock_instance
        
        response = client.post("/twilio/sms", data={
            "Body": "Bonjour",
            "From": "+33612345678"
        })
        
        assert response.status_code == 200
        content = response.text
        assert "<Message>" in content


class TestGenericWebhook:
    """Tests pour l'endpoint /twilio/webhook"""
    
    def test_webhook_with_callsid_routes_to_voice(self):
        """CallSid présent doit router vers voice"""
        response = client.post("/twilio/webhook", data={
            "CallSid": "CA123456789"
        })
        assert response.status_code == 200
        assert "<Gather" in response.text
    
    @patch("app.main.httpx.AsyncClient")
    def test_webhook_with_body_routes_to_sms(self, mock_client):
        """Body présent doit router vers SMS"""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}]
        }
        
        mock_instance = AsyncMock()
        mock_instance.post.return_value = mock_response
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_client.return_value = mock_instance
        
        response = client.post("/twilio/webhook", data={
            "Body": "Test SMS"
        })
        assert response.status_code == 200
        assert "<Message>" in response.text
    
    def test_webhook_unknown_returns_empty_response(self):
        """Requête inconnue retourne une réponse vide"""
        response = client.post("/twilio/webhook", data={})
        assert response.status_code == 200
        assert "<Response></Response>" in response.text
