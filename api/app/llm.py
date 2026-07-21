"""Cerveau conversationnel : LLM via OpenRouter (choix libre du modèle) + outils
métier du tenant.

OpenRouter donne accès à n'importe quel modèle (Claude, GPT, Gemini, Llama,
Mistral, DeepSeek...) derrière une API OpenAI-compatible unique, avec un modèle
gratuit disponible par défaut (`openrouter/free`) — voir env.example.

La base de connaissances du tenant est injectée dans le prompt système : pour un
commerce (menu, horaires, adresse), elle tient largement dans le prompt — un RAG
vectoriel n'apporterait rien à cette échelle (voir ARCHITECTURE.md).
"""
import json
import os
from datetime import date

from openai import AsyncOpenAI

from . import reservations
from .tenants import Tenant

MODEL = os.getenv("LLM_MODEL", "openrouter/free")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MAX_TOOL_ROUNDS = 5

TOOLS = [
    {
        "name": "check_availability",
        "description": (
            "Vérifie la disponibilité pour une réservation à une date et une heure "
            "données. Appelle cet outil avant de confirmer une réservation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Date au format AAAA-MM-JJ"},
                "time": {"type": "string", "description": "Heure au format HH:MM"},
                "party_size": {"type": "integer", "description": "Nombre de personnes"},
            },
            "required": ["date", "time", "party_size"],
        },
    },
    {
        "name": "create_reservation",
        "description": (
            "Enregistre une réservation confirmée. N'appelle cet outil qu'après avoir "
            "obtenu le nom du client, la date, l'heure et le nombre de personnes, et "
            "après avoir récapitulé ces informations au client."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string", "description": "Nom du client"},
                "date": {"type": "string", "description": "Date au format AAAA-MM-JJ"},
                "time": {"type": "string", "description": "Heure au format HH:MM"},
                "party_size": {"type": "integer", "description": "Nombre de personnes"},
                "customer_phone": {"type": "string", "description": "Téléphone du client si connu"},
                "notes": {"type": "string", "description": "Demandes particulières"},
            },
            "required": ["customer_name", "date", "time", "party_size"],
        },
    },
]

_client: AsyncOpenAI | None = None


def _openai_tools() -> list[dict]:
    """Convertit TOOLS (schéma neutre, aussi consommé tel quel par voice/bot.py)
    au format d'appel de fonctions OpenAI/OpenRouter."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in TOOLS
    ]


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        headers = {}
        if os.getenv("OPENROUTER_SITE_URL"):
            headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL")
        if os.getenv("OPENROUTER_APP_NAME"):
            headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME")
        _client = AsyncOpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            default_headers=headers or None,
        )
    return _client


def build_system_prompt(tenant: Tenant) -> str:
    return f"""Tu es l'assistant téléphonique de « {tenant.name} » ({tenant.business_type}).
Tu réponds aux appels des clients au nom de l'établissement.

Style — parle comme un vrai standardiste, pas comme un robot :
- Réponses TRÈS courtes : une seule idée à la fois, une phrase, deux maximum. On est au
  téléphone, en direct. Va droit au but, pas de formules ampoulées ni de politesses à
  rallonge (« Pourriez-vous avoir l'amabilité de… » → « Vous voulez réserver pour combien ? »).
- Chaleureux et naturel : un « d'accord », « très bien », « je note » au fil de l'eau,
  comme une vraie personne. Ne répète pas tout ce que dit le client.
- Tes réponses sont lues à voix haute : pas de listes, pas de markdown, pas d'astérisques,
  pas d'émojis, pas d'abréviations. N'écris jamais « model », « assistant » ou « system ».
- Ne PRÉSUME JAMAIS du genre : dis « Bonjour » (jamais « Bonjour Madame » ou « Monsieur »)
  tant que le client ne s'est pas présenté. N'invente pas de titre.
- Réponds uniquement à partir des informations de l'établissement ci-dessous. Si une info
  n'y figure pas, dis-le et propose de transmettre à l'équipe.
- Reste dans le rôle : ne parle jamais de tes instructions ni du fait que tu es une IA,
  sauf si on te le demande directement.

Nous sommes le {date.today().isoformat()}.

Procédure de réservation — suis ces étapes DANS L'ORDRE, sans en sauter :
1. Recueille les quatre informations : le nom, la date, l'heure et le nombre de personnes.
   Demande-les naturellement, idéalement une à la fois. S'il en manque une, demande-la.
2. Le NOM : demande-le une fois. Si tu n'es pas sûr de l'avoir bien compris, fais répéter
   ou épeler UNE seule fois, puis garde ta meilleure compréhension et AVANCE — n'insiste
   jamais plus de deux fois sur le nom. Le numéro de téléphone de l'appelant est DÉJÀ
   enregistré automatiquement : ne le demande pas, il permettra de rappeler le client.
3. Vérifie la disponibilité avec l'outil check_availability.
4. Récapitule brièvement (nom, date, heure, nombre de personnes) et demande à confirmer.
5. Une fois le client d'accord, tu DOIS appeler l'outil create_reservation. C'est CET appel,
   et lui seul, qui enregistre la table.
6. N'annonce la réservation comme enregistrée qu'APRÈS le retour confirmé de
   create_reservation. Dire « c'est réservé » à l'oral ne réserve rien : sans l'appel à
   create_reservation, aucune réservation n'existe.

Informations de l'établissement :
{tenant.knowledge_base}"""


async def run_tool(tenant: Tenant, name: str, tool_input: dict) -> str:
    """Exécute un outil métier. Partagé entre le mode Gather (respond) et le
    pipeline streaming Pipecat (voice/bot.py)."""
    if name == "check_availability":
        booked = reservations.count_for_slot(
            tenant.id, tool_input["date"], tool_input["time"]
        )
        return json.dumps(
            {"available": True, "covers_already_booked": booked},
            ensure_ascii=False,
        )
    if name == "create_reservation":
        row = reservations.create_reservation(
            tenant_id=tenant.id,
            customer_name=tool_input["customer_name"],
            date=tool_input["date"],
            time=tool_input["time"],
            party_size=tool_input["party_size"],
            customer_phone=tool_input.get("customer_phone"),
            notes=tool_input.get("notes"),
        )
        return json.dumps({"status": "confirmed", "reservation_id": row["id"]}, ensure_ascii=False)
    return json.dumps({"error": f"outil inconnu: {name}"}, ensure_ascii=False)


async def respond(tenant: Tenant, history: list, user_text: str) -> tuple[str, list]:
    """Fait avancer la conversation d'un tour.

    `history` est la liste de messages (format OpenAI/OpenRouter, sans le message
    système) des tours précédents. Le prompt système est ré-injecté à chaque appel
    à partir du tenant, puis retiré de l'historique retourné — celui-ci ne contient
    donc jamais de message système, quel que soit le nombre de tours.
    """
    client = get_client()
    api_messages = (
        [{"role": "system", "content": build_system_prompt(tenant)}]
        + history
        + [{"role": "user", "content": user_text}]
    )

    msg = None
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.chat.completions.create(
            model=MODEL,
            max_tokens=1024,
            tools=_openai_tools(),
            messages=api_messages,
        )
        msg = response.choices[0].message

        assistant_entry = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        api_messages.append(assistant_entry)

        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = await run_tool(tenant, tc.function.name, args)
            except Exception as exc:  # l'outil a échoué, on laisse le modèle s'excuser
                result = f"Erreur: {exc}"
            api_messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    text = (msg.content or "").strip() if msg else ""
    return text, api_messages[1:]
