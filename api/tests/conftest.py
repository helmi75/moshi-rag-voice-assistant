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
# Plateforme admin : super-admin semé au démarrage + secret de session déterministes.
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["SESSION_SECRET"] = "test-session-secret"
# La suite ne doit JAMAIS toucher au réseau : c'est ce qui la rend exécutable partout et
# reproductible. La relève des alertes Twilio est le seul point du code qui appelle un
# tiers en tâche de fond — on la coupe ici. Les identifiants Twilio arrivent du `.env`
# via docker-compose quand les tests tournent en conteneur, donc l'appel PARTIRAIT
# vraiment sans cette ligne. `rafraichir_twilio` reste testée, avec httpx bouchonné.
os.environ["SUPERVISION_TWILIO_SECONDES"] = "0"
# Même raison, autre risque : la purge de rétention (#22) ÉCRIT en base. Lancée à chaque
# démarrage d'un TestClient, elle effacerait des données que d'autres tests viennent de
# créer, et l'ordre d'exécution déciderait du résultat. `purger()` est testée en direct.
os.environ["RETENTION_INTERVALLE_SECONDES"] = "0"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
