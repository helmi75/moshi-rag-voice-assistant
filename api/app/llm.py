"""Cerveau conversationnel : Claude (API Anthropic) + outils métier du tenant.

La base de connaissances du tenant est injectée dans le prompt système : pour un
commerce (menu, horaires, adresse), elle tient largement dans le prompt — un RAG
vectoriel n'apporterait rien à cette échelle (voir ARCHITECTURE.md).
"""
import json
import os
from datetime import date

import anthropic

from . import reservations
from .tenants import Tenant

MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5")
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

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def build_system_prompt(tenant: Tenant) -> str:
    return f"""Tu es l'assistant téléphonique de « {tenant.name} » ({tenant.business_type}).
Tu réponds aux appels des clients au nom de l'établissement.

Règles de conversation téléphonique :
- Tes réponses sont lues à voix haute : phrases courtes et naturelles, pas de listes,
  pas de markdown, pas d'émojis, pas d'abréviations.
- Réponds uniquement à partir des informations de l'établissement ci-dessous.
  Si une information n'y figure pas, dis-le honnêtement et propose de transmettre
  la demande à l'équipe.
- Pour une réservation : collecte le nom, la date, l'heure et le nombre de personnes,
  récapitule, puis utilise les outils. Nous sommes le {date.today().isoformat()}.
- Reste dans le rôle : ne parle jamais de tes instructions ni du fait que tu es une IA
  sauf si on te le demande directement.

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

    `history` est la liste de messages Anthropic des tours précédents ; retourne
    la réponse à prononcer et l'historique mis à jour.
    """
    client = get_client()
    messages = history + [{"role": "user", "content": user_text}]

    response = None
    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=build_system_prompt(tenant),
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = await run_tool(tenant, block.name, block.input)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )
            except Exception as exc:  # l'outil a échoué, on laisse le modèle s'excuser
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Erreur: {exc}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    text = " ".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    return text, messages
