#!/bin/bash
# Script de test de bout en bout pour l'assistant téléphonique
# Usage: ./test_e2e.sh [HOST] [NUMERO_TENANT]
# Exemple: ./test_e2e.sh localhost +33100000000

set -e

HOST="${1:-localhost}"
TENANT_NUMBER="${2:-+33100000000}"
API_PORT="${API_PORT:-8000}"
BASE_URL="http://${HOST}:${API_PORT}"

echo "========================================"
echo "  Tests de bout en bout - Assistant vocal"
echo "========================================"
echo "Host: $HOST"
echo "Tenant: $TENANT_NUMBER"
echo ""

# Couleurs
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

passed=0
failed=0

test_endpoint() {
    local name="$1"
    local method="$2"
    local url="$3"
    local data="$4"
    local expected="$5"

    echo -n "Test: $name... "

    if [ "$method" = "GET" ]; then
        response=$(curl -s -w "\n%{http_code}" "$url" 2>/dev/null || echo "ERROR")
    else
        response=$(curl -s -w "\n%{http_code}" -X POST --data-urlencode "${data}" "$url" 2>/dev/null || echo "ERROR")
    fi

    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if echo "$body" | grep -q "$expected"; then
        echo -e "${GREEN}PASS${NC}"
        passed=$((passed+1))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Expected: $expected"
        echo "  Got: $body"
        failed=$((failed+1))
    fi
}

twilio_post() {
    # POST form-encodé multi-champs
    local url="$1"; shift
    curl -s -X POST "$url" "$@"
}

echo ""
echo "--- Tests API ---"
echo ""

# Test 1: Health check
test_endpoint "Health Check" "GET" "${BASE_URL}/health" "" '"status":"ok"'

# Mode vocal du serveur (gather ou stream) — dicte le TwiML attendu
VOICE_MODE=$(curl -s "${BASE_URL}/health" | grep -o '"voice_mode":"[a-z]*"' | cut -d'"' -f4)
echo "Mode vocal serveur: ${VOICE_MODE:-inconnu}"

# Test 2: Appel entrant (Gather en mode gather, Connect/Stream en mode stream)
if [ "$VOICE_MODE" = "stream" ]; then
    EXPECTED_TWIML="<Connect>"
else
    EXPECTED_TWIML="<Gather"
fi
echo -n "Test: Voice Webhook (accueil, attendu ${EXPECTED_TWIML})... "
body=$(twilio_post "${BASE_URL}/twilio/voice" --data-urlencode "CallSid=CA123" --data-urlencode "To=${TENANT_NUMBER}")
if echo "$body" | grep -q "$EXPECTED_TWIML"; then
    echo -e "${GREEN}PASS${NC}"; passed=$((passed+1))
else
    echo -e "${RED}FAIL${NC}"; echo "  Got: $body"; failed=$((failed+1))
fi

# Test 3: Numéro inconnu → raccroche
echo -n "Test: Voice Webhook (numéro inconnu)... "
body=$(twilio_post "${BASE_URL}/twilio/voice" --data-urlencode "CallSid=CA124" --data-urlencode "To=+19999999999")
if echo "$body" | grep -q "<Hangup/>"; then
    echo -e "${GREEN}PASS${NC}"; passed=$((passed+1))
else
    echo -e "${RED}FAIL${NC}"; echo "  Got: $body"; failed=$((failed+1))
fi

# Test 4: Tour de conversation (mode gather uniquement — en stream la conversation
# passe par le WebSocket, pas par ce webhook). Nécessite ANTHROPIC_API_KEY côté
# serveur, sinon le message d'erreur poli est retourné — dans les deux cas un <Say>.
if [ "$VOICE_MODE" != "stream" ]; then
    echo -n "Test: Voice Webhook (tour de parole)... "
    body=$(twilio_post "${BASE_URL}/twilio/voice" \
        --data-urlencode "CallSid=CA123" \
        --data-urlencode "To=${TENANT_NUMBER}" \
        --data-urlencode "SpeechResult=Quels sont vos horaires ?")
    if echo "$body" | grep -q "<Say"; then
        echo -e "${GREEN}PASS${NC}"; passed=$((passed+1))
    else
        echo -e "${RED}FAIL${NC}"; echo "  Got: $body"; failed=$((failed+1))
    fi
else
    echo "Test: Voice Webhook (tour de parole)... SKIP (mode stream: conversation via WebSocket)"
fi

# Test 5: Webhook générique (vide)
test_endpoint "Generic Webhook (Empty)" "POST" "${BASE_URL}/twilio/webhook" "X=1" "<Response></Response>"

echo ""
echo "========================================"
echo "  Résultats"
echo "========================================"
echo -e "  ${GREEN}Passed: $passed${NC}"
echo -e "  ${RED}Failed: $failed${NC}"
echo ""

if [ $failed -gt 0 ]; then
    exit 1
fi

exit 0
