"""Pipeline vocal streaming (phase 2) : Twilio Media Streams + Pipecat.

Chaque étage (STT/LLM/TTS) est un service Pipecat interchangeable : Deepgram et
Cartesia (APIs, phase A) pourront être remplacés par Kyutai STT/TTS auto-hébergés
(phase B) sans toucher au cerveau métier (llm.py / tenants.py).

Les imports Pipecat sont faits à l'intérieur des fonctions : l'application reste
utilisable en mode `gather` même si les extras audio ne sont pas installés.
"""
import asyncio
import os

from loguru import logger

from .. import llm
from ..tenants import Tenant


def make_tool_handler(
    tenant: Tenant,
    created_reservations: list[int] | None = None,
    caller_number: str | None = None,
):
    """Handler Pipecat commun aux outils métier : délègue à llm.run_tool.

    `created_reservations` (journal des appels) : collecte les ids des réservations
    créées pendant l'appel — le handler voit passer tous les résultats d'outils.
    `caller_number` : numéro Twilio de l'appelant, injecté d'office comme customer_phone
    de la réservation (identifiant fiable, quelle que soit la qualité de transcription du
    nom). Le client n'a jamais à le dicter."""

    async def handle(params):  # params: pipecat FunctionCallParams
        args = dict(params.arguments or {})
        # Le numéro de l'appelant est la source de vérité : on l'impose comme téléphone
        # de la réservation (le nom, lui, peut être écorché par la STT sur le 8 kHz).
        if params.function_name == "create_reservation" and caller_number:
            args["customer_phone"] = caller_number
        try:
            result = await llm.run_tool(tenant, params.function_name, args)
        except Exception as exc:
            result = f"Erreur outil {params.function_name}: {exc}"
        if created_reservations is not None and params.function_name == "create_reservation":
            try:
                import json

                rid = json.loads(result).get("reservation_id")
                if rid:
                    created_reservations.append(int(rid))
            except (ValueError, TypeError, AttributeError):
                pass  # résultat d'erreur ou format inattendu : pas de lien de résa
        await params.result_callback(result)

    return handle


def build_tts():
    """Construit le service TTS selon TTS_PROVIDER (défaut : pocket = voix Kyutai,
    CPU, sans clé). `cartesia` en alternative (API, nécessite CARTESIA_API_KEY)."""
    provider = os.getenv("TTS_PROVIDER", "pocket").strip().lower()
    logger.info(f"TTS provider sélectionné : {provider}")
    if provider == "pocket":
        from .pocket_tts import PocketTTSService

        return PocketTTSService()
    if provider == "moshi_server":
        # Voix Moshi 1.6B via le serveur Rust moshi-server (production, fluide).
        # L'app est simple cliente websocket (aucun modèle en local) ; le serveur
        # tourne sur Modal GPU (voir deploy/modal_moshi_server.py).
        from .moshi_server_tts import MoshiServerTTSService

        return MoshiServerTTSService()
    if provider == "kyutai":
        # Kyutai TTS 1.6B en PyTorch DANS l'app (GPU requis). Reste sous le temps réel
        # (sacade) sur L4/T4 — préférer moshi_server. Conservé pour référence/repli.
        from .kyutai_tts import KyutaiTTSService

        return KyutaiTTSService()
    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService
        from pipecat.transcriptions.language import Language

        # Français par défaut + modèle multilingue : sans ça Cartesia lit le
        # français avec un modèle/accent anglais.
        lang_code = os.getenv("CARTESIA_LANGUAGE", "fr").strip().lower()
        try:
            language = Language(lang_code)
        except ValueError:
            language = Language.FR
        return CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY", ""),
            voice_id=os.getenv("CARTESIA_VOICE_ID", ""),
            model=os.getenv("CARTESIA_MODEL", "sonic-2"),
            params=CartesiaTTSService.InputParams(language=language),
        )
    raise ValueError(
        f"TTS_PROVIDER inconnu : {provider!r} "
        "(valeurs acceptées : moshi_server, pocket, kyutai, cartesia)"
    )


def build_stt(tenant: Tenant, language: str):
    """Construit le service STT selon STT_PROVIDER (défaut : deepgram).

    `kyutai` = module ASR de moshi-server (Kyutai stt-1b-en_fr) : français natif, VAD
    sémantique, servi par le même serveur Modal que le TTS -> un fournisseur externe de
    moins et -1,5 ¢/appel. `deepgram` (nova-2) reste le défaut/repli (bascule instantanée
    par STT_PROVIDER, sans redéploiement)."""
    provider = os.getenv("STT_PROVIDER", "deepgram").strip().lower()
    logger.info(f"STT provider sélectionné : {provider}")

    if provider == "kyutai":
        from pipecat.transcriptions.language import Language

        from .kyutai_stt import KyutaiSTTService

        try:
            lang = Language(language)
        except ValueError:
            lang = Language.FR
        return KyutaiSTTService(language=lang)

    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions

        # Boost de vocabulaire Deepgram : le nom de l'établissement + le lexique de la
        # réservation. Réduit les transcriptions farfelues sur l'audio téléphone 8 kHz
        # (mots inventés à la place de « réservation », noms propres écorchés...).
        keywords = [f"{w}:5" for w in (tenant.name or "").replace("'", " ").split() if len(w) > 2]
        keywords += [
            "réservation:3", "réserver:3", "couverts:2", "personnes:2", "table:2",
            "midi:1", "soir:1", "demain:1", "allergie:2", "terrasse:2", "annuler:2",
        ]
        extra_kw = os.getenv("DEEPGRAM_KEYWORDS", "")  # "mot:5,autre:2" pour compléter
        keywords += [k.strip() for k in extra_kw.split(",") if k.strip()]

        return DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            live_options=LiveOptions(
                # nova-2 a un support français robuste ; surchargeable via DEEPGRAM_MODEL
                model=os.getenv("DEEPGRAM_MODEL", "nova-2"),
                language=language,
                # smart_format : dates/nombres proprement formatés (« 20 h » plutôt que
                # « vingt heures ») -> le LLM extrait mieux date/heure/couverts.
                smart_format=True,
                keywords=keywords,
            ),
        )

    raise ValueError(
        f"STT_PROVIDER inconnu : {provider!r} (valeurs acceptées : deepgram, kyutai)"
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


async def run_bot(
    websocket,
    stream_sid: str,
    call_sid: str | None,
    tenant: Tenant,
    caller_number: str | None = None,
) -> None:
    """Construit et exécute le pipeline Pipecat pour un appel Twilio Media Streams.

    `caller_number` : numéro de l'appelant (Twilio From), rattaché d'office à toute
    réservation créée pendant l'appel."""
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

    # STT interchangeable (deepgram par défaut, kyutai = module ASR de moshi-server).
    stt = build_stt(tenant, language)

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
    # Journal des appels : collecte les réservations créées pendant CET appel.
    created_reservations: list[int] = []
    tool_handler = make_tool_handler(tenant, created_reservations, caller_number)
    for tool in llm.TOOLS:
        llm_service.register_function(tool["name"], tool_handler)

    # Warmup LLM (audit latence : 1er tour mesuré à 4,5 s contre 0,5 s ensuite —
    # connexion HTTPS froide + préchauffage du prompt côté fournisseur). On envoie
    # 1 token AVEC le même client HTTP et le même prompt système, pendant l'accueil :
    # le 1er vrai tour retombe au régime nominal. Best-effort, coût ~0,02 ct.
    async def _warm_llm():
        import time as _time

        try:
            t0 = _time.monotonic()
            client = getattr(llm_service, "_client", None) or llm.get_client()
            await client.chat.completions.create(
                model=llm.MODEL,
                messages=[
                    {"role": "system", "content": llm.build_system_prompt(tenant)},
                    {"role": "user", "content": "Bonjour"},
                ],
                max_tokens=1,
            )
            logger.info(f"warmup LLM : OK en {_time.monotonic() - t0:.2f}s")
        except Exception as exc:
            logger.warning(f"warmup LLM échoué (sans conséquence): {exc}")

    asyncio.create_task(_warm_llm())

    messages = [{"role": "system", "content": llm.build_system_prompt(tenant)}]
    if os.getenv("TTS_PROVIDER", "").strip().lower() == "moshi_server":
        # Le flux « standardiste » a déjà salué et mis en relation (accueil pré-rendu +
        # « merci d'avoir patienté ») : on l'inscrit au contexte pour que le modèle
        # enchaîne directement sur la demande du client, sans re-saluer.
        messages.append(
            {"role": "assistant", "content": f"{tenant.greeting} Merci d'avoir patienté, je vous écoute."}
        )
    context = LLMContext(
        messages=messages,
        tools=build_function_schemas(),
    )
    # Relance douce si le client reste muet après une réponse (comble le « blanc »).
    idle_timeout = float(os.getenv("USER_IDLE_TIMEOUT", "8"))
    context_aggregator = LLMContextAggregatorPair(
        context,
        # VAD Silero pour le début de tour ; fin de tour via smart-turn v3 (défaut
        # Pipecat 1.5, modèle ONNX embarqué) -> barge-in et coupures naturelles.
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            # Émet on_user_turn_idle si le client est inactif ce délai APRÈS que le bot
            # a fini de parler (donc jamais pendant la musique d'attente).
            user_idle_timeout=idle_timeout,
        ),
    )

    # Relances progressives, plafonnées ; le compteur repart dès que le client parle.
    _idle = {"n": 0}
    _user_agg = context_aggregator.user()

    @_user_agg.event_handler("on_user_turn_idle")
    async def _on_user_idle(aggregator):
        _idle["n"] += 1
        if _idle["n"] == 1:
            await task.queue_frames([TTSSpeakFrame("Je vous écoute, que puis-je faire pour vous ?")])
        elif _idle["n"] == 2:
            await task.queue_frames([TTSSpeakFrame("Êtes-vous toujours en ligne ?")])
        # Au-delà : on n'insiste plus (on laisse le client raccrocher).

    @_user_agg.event_handler("on_user_turn_stopped")
    async def _reset_idle(aggregator, *args):
        _idle["n"] = 0

    # Réf. sur le transport de sortie : le flux « standardiste » y injecte l'accueil et
    # la musique via send_audio() (DIRECT vers Twilio), sans traverser STT/VAD.
    output_transport = transport.output()
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm_service,
            tts,
            output_transport,
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

    # Phase 3 — flux « standardiste » : accueil pré-rendu (latence 0) → musique d'attente
    # pendant le réveil du GPU (décorrélée du barge-in) → reprise proactive. Lancé en
    # tâche de fond, en parallèle du pipeline, pour injecter les frames au fil de l'eau.
    from . import greeting as greeting_mod

    intro_task = None
    if greeting_mod.is_moshi_server():
        # Le transport de sortie jette l'audio reçu avant le StartFrame : l'intro
        # attend ce signal avant d'émettre l'accueil (sinon il part dans le vide).
        pipeline_ready = asyncio.Event()

        @task.event_handler("on_pipeline_started")
        async def _on_pipeline_started(task, frame):
            pipeline_ready.set()

        intro_task = asyncio.create_task(
            greeting_mod.run_switchboard_intro(task, output_transport, tenant, pipeline_ready)
        )
        # Pré-rendu de secours si le WAV d'accueil n'est pas encore en cache (le flux
        # retombe alors sur du TTS live ; ceci le rend instantané dès l'appel suivant).
        if greeting_mod.cached_greeting_path(tenant) is None:
            asyncio.create_task(greeting_mod.ensure_greeting_wav(tenant))
    else:
        await task.queue_frames([TTSSpeakFrame(tenant.greeting)])

    runner = PipelineRunner(handle_sigint=False)
    status = "completed"
    try:
        await runner.run(task)
    except Exception:
        status = "failed"
        raise
    finally:
        if intro_task is not None:
            intro_task.cancel()
        # Journal des appels : clôture best-effort, hors chemin de latence (l'appel est
        # déjà terminé) et en thread (écriture SQLite hors event loop).
        if call_sid:
            try:
                from .. import calls as calls_mod

                await asyncio.to_thread(
                    calls_mod.finish_call,
                    call_sid,
                    status,
                    _extract_transcript(context),
                    created_reservations[0] if created_reservations else None,
                )
            except Exception as exc:
                logger.warning(f"[calls] finish_call KO (sans conséquence): {exc}")


def _extract_transcript(context) -> list[dict] | None:
    """Extrait les tours user/assistant textuels du LLMContext Pipecat (sans le
    prompt système ni les appels d'outils). Défensif : au pire None, jamais d'erreur."""
    try:
        get_messages = getattr(context, "get_messages", None)
        messages = get_messages() if callable(get_messages) else getattr(context, "messages", [])
        transcript = []
        for message in messages:
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            content = (
                message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            )
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                transcript.append({"role": role, "content": content.strip()})
        return transcript or None
    except Exception:
        return None
