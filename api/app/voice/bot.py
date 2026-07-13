"""Pipeline vocal streaming (phase 2) : Twilio Media Streams + Pipecat.

Chaque étage (STT/LLM/TTS) est un service Pipecat interchangeable : Deepgram et
Cartesia (APIs, phase A) pourront être remplacés par Kyutai STT/TTS auto-hébergés
(phase B) sans toucher au cerveau métier (llm.py / tenants.py).

Les imports Pipecat sont faits à l'intérieur des fonctions : l'application reste
utilisable en mode `gather` même si les extras audio ne sont pas installés.
"""
import os

from loguru import logger

from .. import llm
from ..tenants import Tenant


def make_tool_handler(tenant: Tenant):
    """Handler Pipecat commun aux outils métier : délègue à llm.run_tool."""

    async def handle(params):  # params: pipecat FunctionCallParams
        try:
            result = await llm.run_tool(tenant, params.function_name, params.arguments or {})
        except Exception as exc:
            result = f"Erreur outil {params.function_name}: {exc}"
        await params.result_callback(result)

    return handle


def build_tts():
    """Construit le service TTS selon TTS_PROVIDER (défaut : pocket = voix Kyutai,
    CPU, sans clé). `cartesia` en alternative (API, nécessite CARTESIA_API_KEY)."""
    provider = os.getenv("TTS_PROVIDER", "pocket").strip().lower()
    if provider == "pocket":
        from .pocket_tts import PocketTTSService

        return PocketTTSService()
    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        return CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY", ""),
            voice_id=os.getenv("CARTESIA_VOICE_ID", ""),
            model=os.getenv("CARTESIA_MODEL") or None,
        )
    raise ValueError(
        f"TTS_PROVIDER inconnu : {provider!r} (valeurs acceptées : pocket, cartesia)"
    )


def build_function_schemas():
    """Convertit llm.TOOLS (schéma neutre) en FunctionSchema Pipecat."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    return [
        FunctionSchema(
            name=tool["name"],
            description=tool["description"],
            properties=tool["input_schema"]["properties"],
            required=tool["input_schema"]["required"],
        )
        for tool in llm.TOOLS
    ]


async def run_bot(websocket, stream_sid: str, call_sid: str | None, tenant: Tenant) -> None:
    """Construit et exécute le pipeline Pipecat pour un appel Twilio Media Streams."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    # "fr-FR" (Twilio/tenant) -> "fr" (Deepgram)
    language = (tenant.language or "fr-FR").split("-")[0]

    # Le raccrochage automatique en fin de session nécessite les identifiants
    # Twilio ; sans eux (dev/tests), on le désactive au lieu de planter.
    account_sid = os.getenv("TWILIO_ACCOUNT_SID") or None
    auth_token = os.getenv("TWILIO_AUTH_TOKEN") or None
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=account_sid,
        auth_token=auth_token,
        params=TwilioFrameSerializer.InputParams(
            auto_hang_up=bool(account_sid and auth_token)
        ),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        live_options=LiveOptions(
            # nova-2 a un support français robuste ; surchargeable via DEEPGRAM_MODEL
            model=os.getenv("DEEPGRAM_MODEL", "nova-2"),
            language=language,
        ),
    )

    tts = build_tts()

    headers = {}
    if os.getenv("OPENROUTER_SITE_URL"):
        headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL")
    if os.getenv("OPENROUTER_APP_NAME"):
        headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME")
    llm_service = OpenAILLMService(
        api_key=os.getenv("OPENROUTER_API_KEY", ""),
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        model=llm.MODEL,
        default_headers=headers or None,
    )
    tool_handler = make_tool_handler(tenant)
    for tool in llm.TOOLS:
        llm_service.register_function(tool["name"], tool_handler)

    context = LLMContext(
        messages=[{"role": "system", "content": llm.build_system_prompt(tenant)}],
        tools=build_function_schemas(),
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        # VAD Silero pour le début de tour ; fin de tour via smart-turn v3 (défaut
        # Pipecat 1.5, modèle ONNX embarqué) -> barge-in et coupures naturelles.
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm_service,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            # Twilio Media Streams est en 8 kHz : caler tout le pipeline dessus
            # évite des rééchantillonnages inutiles.
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Appel terminé (tenant {tenant.id}, {call_sid}), arrêt du pipeline.")
        await task.cancel()

    # Message d'accueil : mis en file AVANT le démarrage du pipeline (déterministe,
    # ne dépend pas de l'événement on_client_connected qui peut être manqué puisque
    # les messages Twilio "connected"/"start" ont déjà été consommés par le webhook).
    logger.info(f"Démarrage du pipeline vocal (tenant {tenant.id}, {call_sid}).")
    await task.queue_frames([TTSSpeakFrame(tenant.greeting)])

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
