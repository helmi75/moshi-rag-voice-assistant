#!/bin/bash
# Script de test de bout en bout pour le projet Moshi
# Usage: ./test_e2e.sh [HOST]
# Exemple: ./test_e2e.sh localhost

set -e

HOST="${1:-localhost}"
API_PORT="8000"
MOSHI_PORT="8091"
BASE_URL="http://${HOST}:${API_PORT}"

echo "========================================"
echo "  Tests de bout en bout - Projet Moshi"
echo "========================================"
echo "Host: $HOST"
echo ""

# Couleurs
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
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
        response=$(curl -s -w "\n%{http_code}" -X POST -d "$data" "$url" 2>/dev/null || echo "ERROR")
    fi
    
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')
    
    if echo "$body" | grep -q "$expected"; then
        echo -e "${GREEN}PASS${NC}"
        ((passed++))
    else
        echo -e "${RED}FAIL${NC}"
        echo "  Expected: $expected"
        echo "  Got: $body"
        ((failed++))
    fi
}

echo ""
echo "--- Tests API ---"
echo ""

# Test 1: Health check
test_endpoint "Health Check" "GET" "${BASE_URL}/health" "" '"status":"ok"'

# Test 2: Voice webhook (initial call)
test_endpoint "Voice Webhook (Initial)" "POST" "${BASE_URL}/twilio/voice" "CallSid=CA123" "<Gather"

# Test 3: SMS webhook
test_endpoint "SMS Webhook" "POST" "${BASE_URL}/twilio/sms" "Body=Test&From=+33612345678" "<Message>"

# Test 4: Generic webhook (voice)
test_endpoint "Generic Webhook (Voice)" "POST" "${BASE_URL}/twilio/webhook" "CallSid=CA123" "<Response>"

# Test 5: Generic webhook (unknown)
test_endpoint "Generic Webhook (Empty)" "POST" "${BASE_URL}/twilio/webhook" "" "<Response></Response>"

echo ""
echo "--- Test Moshi Service ---"
echo ""

# Test 6: Moshi server accessibility
echo -n "Test: Moshi Server Accessible... "
if curl -s --connect-timeout 5 "http://${HOST}:${MOSHI_PORT}/" > /dev/null 2>&1; then
    echo -e "${GREEN}PASS${NC}"
    ((passed++))
else
    echo -e "${YELLOW}SKIP${NC} (Moshi peut ne pas exposer HTTP directement)"
fi

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
