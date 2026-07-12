import os
import sys
import tempfile

# Configuration de test AVANT l'import de l'application :
# base SQLite jetable, clé API factice (le client OpenRouter est mocké),
# numéro Twilio de démo déterministe.
_tmpdir = tempfile.mkdtemp(prefix="voice-assistant-tests-")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "app.db")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-used")
os.environ["TWILIO_NUMBER"] = "+33100000000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
