"""Essai de GPT-Live, la voix-à-voix d'OpenAI, sur des établissements choisis.

Pourquoi un essai (docs/TEMPS_REEL.md, 04/10/2026) : notre défaut mesuré est le découpage
en tours — l'assistante attend la fin d'une phrase, se trompe sur cette fin une fois sur
trois sans délai de garde. GPT-Live écoute pendant qu'elle parle et décide seule quand
répondre et quand se taire. Reste à l'entendre en français, sur de vrais appels.

Ce module ne passe PAS par Pipecat : GPT-Live accepte le format du téléphone tel quel
(µ-law 8 kHz), rend un flux continu au rythme de la parole, silences compris, et gère
elle-même les interruptions. Il n'y a donc rien à détecter ni à assembler : on relaie
l'audio dans les deux sens, et on répond à ses *délégations* — le travail qu'elle confie
à un second modèle, lequel appelle nos outils. Notre Pipecat 1.5 ne connaît pas ce
service, et monter de version pour un essai aurait touché tous les appels.

GPT-Live n'est que la voix. Le cerveau reste le nôtre : le modèle de `llm.MODEL`
(Gemini 2.5 Flash par OpenRouter), celui qui sert déjà tous les appels et pour lequel
le prompt a été réglé (décision de Helmi, 04/10/2026). `GPT_LIVE_MODELE` permet de
confier ce rôle à un modèle hébergé par OpenAI, pour comparer.

Ce qui ne change pas : `llm.run_tool` reste le seul endroit où un outil écrit, avec tous
ses refus ; l'appel est au journal, enregistré, chiffré. Et si la session ne s'ouvre pas
(clé, crédit, réseau), l'appel est servi par le pipeline habituel : `ouvrir` rend None.

Le protocole suit le service `OpenAILiveLLMService` de Pipecat 1.12, lu le 04/10/2026.
"""
import asyncio
import base64
import json
import os
import time
import uuid
from typing import Optional

from loguru import logger
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as _connexion

from .. import llm, rgpd
from ..tenants import Tenant
from . import ulaw

URL = "wss://api.openai.com/v1/live/sessions"
MODELE = "gpt-live-1"
FOURNISSEUR = "gpt-live"

# Silence qui sépare deux tours d'un même locuteur : les fragments de transcription
# arrivent par trames de 200 ms, ce délai doit couvrir les pauses ordinaires d'une phrase.
_ECART_DE_TOUR_MS = 800
# L'accueil est donné au modèle comme un propos à dire (`session.commentary.append`). Une
# CONSIGNE (« dis l'accueil ») ne suffit pas : le 04/10/2026, six voix sur huit sont
# restées muettes avec elle, alors que le propos à dire les a toutes fait parler, mot pour
# mot. Passé ce délai sans un mot, il lui est redonné une fois.
_ATTENTE_DE_L_ACCUEIL = 2.5
# Après un « au revoir » de l'assistante, combien de silence avant de raccrocher.
_SILENCE_AVANT_DE_RACCROCHER = 2.0
# Plus personne ne parle depuis ce temps-là : la ligne est rendue. Un combiné posé sans
# raccrocher coûterait sinon cinq centimes de dollar par minute jusqu'à ce que Twilio coupe.
_SILENCE_MAXIMAL = 45.0
# Le cerveau a ce temps-là pour rendre son travail. Au-delà, l'assistante le dit au client
# plutôt que de le laisser attendre dans le vide.
_DELAI_DU_CERVEAU = 20.0
# L'enregistreur absorbe des tranches, pas des trames : on lui en donne cinq par seconde.
_TRANCHE_ENREGISTREMENT = 1600


def cle() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def etablissements() -> set[int]:
    """Les établissements à l'essai : `GPT_LIVE_ETABLISSEMENTS=3,7`. Vide = personne."""
    return {int(m) for m in os.getenv("GPT_LIVE_ETABLISSEMENTS", "").replace(";", ",").split(",")
            if m.strip().isascii() and m.strip().isdigit()}


def actif(tenant: Optional[Tenant]) -> bool:
    """Cet établissement est-il servi par GPT-Live ? Il faut qu'il soit nommé ET qu'une
    clé existe : un essai ne s'allume jamais par défaut."""
    return bool(tenant is not None and cle() and tenant.id in etablissements())


def voix() -> str:
    return os.getenv("GPT_LIVE_VOIX", "").strip() or "marin"


def modele_openai() -> str:
    """Un modèle de texte hébergé par OpenAI pour raisonner à la place du nôtre
    (`GPT_LIVE_MODELE=gpt-6-luna`). Vide, le cas normal : c'est notre modèle qui raisonne."""
    return os.getenv("GPT_LIVE_MODELE", "").strip()


def modele_arriere() -> str:
    """Le modèle de texte qui raisonne et appelle les outils pendant l'appel."""
    return modele_openai() or llm.MODEL


def _delai_ouverture() -> float:
    try:
        return max(1.0, float(os.getenv("GPT_LIVE_DELAI_OUVERTURE", "").strip() or 4.0))
    except ValueError:
        return 4.0


# ---- Ce que chacun des deux modèles reçoit ---------------------------------------------

_CONSIGNES_VOIX = """

# Comment tu travailles
Quand on te donne l'accueil à dire, dis-le MOT POUR MOT, puis écoute.
Tu parles un français de France, standard et naturel, comme une Parisienne dont c'est la
langue maternelle : aucun accent étranger, aucune intonation anglophone. Débit posé.
Tu n'as AUCUN outil toi-même. Chaque fois que les consignes ci-dessus disent d'appeler
un outil — vérifier un créneau, enregistrer, retrouver, modifier ou annuler une
réservation, prendre un message — DÉLÈGUE ce travail, et dis une phrase courte pendant
l'attente (« Je vérifie tout de suite. »). N'annonce jamais un résultat avant le retour
de la délégation : ni « c'est possible », ni « c'est enregistré », ni « c'est annulé ».
Ne devine pas. Si la délégation rend un refus, dis-le simplement et propose la suite.
"""

_CONSIGNES_ARRIERE = """

# Ton rôle ici
Tu es l'arrière-plan de l'assistante vocale : tu ne parles pas au client, c'est elle qui
lui parle. Fais le travail demandé avec les outils, en suivant les règles ci-dessus, puis
rends le résultat en une ou deux phrases factuelles : ce qui est fait, ce qui est refusé
et pourquoi, ou l'information qu'il te manque. Écris les dates et les heures en toutes
lettres, comme elles se disent. N'écris PAS de phrase d'attente (« Je vérifie tout de
suite. ») : l'assistante vient de la dire, elle la redirait.
"""


def outils(numero_connu: bool = True) -> list[dict]:
    """`llm.TOOLS` au format des fonctions de l'API Responses.

    Quand le numéro de l'appelant est connu, le champ `callback_number` est retiré : le
    serveur l'ajoute lui-même (`llm.run_tool`). Laissé là, le modèle d'arrière-plan le
    croyait à remplir et réclamait « le numéro où vous rappeler » au lieu d'enregistrer
    la table (essai du 04/10/2026)."""
    fonctions = []
    for outil in llm.TOOLS:
        schema = outil["input_schema"]
        if numero_connu and "callback_number" in schema.get("properties", {}):
            schema = {**schema, "properties": {
                nom: champ for nom, champ in schema["properties"].items() if nom != "callback_number"}}
        fonctions.append({"type": "function", "name": outil["name"],
                          "description": outil["description"], "parameters": schema})
    return fonctions


_NUMERO_CONNU = """Le numéro de téléphone du client est déjà connu du système et joint
d'office à sa réservation ou à son message : ne le demande jamais, et n'attends pas de
l'avoir pour appeler un outil.
"""


def configuration(tenant: Tenant, prompt_systeme: str, numero_connu: bool = True) -> dict:
    """La session, fixée une fois pour toutes à l'ouverture : modèle, voix, format du
    téléphone, et le modèle d'arrière-plan avec nos outils."""
    return {
        "model": MODELE,
        "instructions": prompt_systeme + _CONSIGNES_VOIX,
        "audio": {"format": {"type": "audio/pcmu", "rate": 8000},
                  "output": {"voice": voix()}},
        "delegation": delegation(prompt_systeme, numero_connu),
    }


def consignes_du_cerveau(prompt_systeme: str, numero_connu: bool = True) -> str:
    return prompt_systeme + _CONSIGNES_ARRIERE + (_NUMERO_CONNU if numero_connu else "")


def delegation(prompt_systeme: str, numero_connu: bool = True) -> dict:
    """À qui GPT-Live confie le travail. Par défaut à nous (`client`) : c'est alors
    `Appel._travail_delegue` qui fait raisonner notre modèle. Avec `GPT_LIVE_MODELE`, à
    un modèle hébergé par OpenAI, qui reçoit nos consignes et nos outils."""
    if not modele_openai():
        return {"type": "client"}
    return {"type": "responses", "responses": {
        "model": modele_openai(),
        "instructions": consignes_du_cerveau(prompt_systeme, numero_connu),
        "tools": outils(numero_connu), "tool_choice": "auto", "parallel_tool_calls": False}}


def accueil(tenant: Tenant) -> str:
    """Ce que l'assistante dit en décrochant : l'accueil de l'établissement, avec la
    mention d'information (`rgpd.accueil`)."""
    return rgpd.accueil(tenant)


async def preparer(tenant: Tenant, caller_number: Optional[str],
                   demonstration: bool = False) -> tuple[Tenant, str]:
    """L'établissement tel que l'appel le voit (horaires du carnet) et son prompt système,
    comme le fait `bot.run_bot` : même fiche, mêmes règles, même nom du dernier passage."""
    from .. import connecteurs

    tenant = connecteurs.avec_horaires_du_carnet(tenant)
    nom_connu = None
    if caller_number:
        try:
            nom_connu = await connecteurs.pour(tenant).dernier_nom(caller_number)
        except Exception as exc:
            logger.warning(f"[gpt-live] nom du dernier passage introuvable (sans conséquence): {exc}")
    return tenant, llm.build_system_prompt(
        tenant, appelant=nom_connu, numero_masque=not (caller_number or "").strip(),
        demonstration=demonstration)


# ---- La session -----------------------------------------------------------------------

async def _connecter():
    """La connexion à OpenAI. Isolée pour que les tests la remplacent."""
    return await _connexion(URL, additional_headers={"Authorization": f"Bearer {cle()}"},
                            open_timeout=_delai_ouverture(), max_size=None)


def _evenement(genre: str, **champs) -> str:
    return json.dumps({"type": genre, "event_id": uuid.uuid4().hex, **champs})


async def ouvrir(tenant: Tenant, prompt_systeme: str, numero_connu: bool = True):
    """Ouvre la session et attend qu'elle soit prête. Rend la connexion, ou None — et
    alors l'appel est servi par le pipeline habituel. NE LÈVE JAMAIS."""
    ws = None
    try:
        ws = await _connecter()
        await ws.send(_evenement("session.start",
                                 session=configuration(tenant, prompt_systeme, numero_connu)))
        fin = time.monotonic() + _delai_ouverture()
        while time.monotonic() < fin:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(0.1, fin - time.monotonic())))
            if evt.get("type") == "session.started":
                return ws
            if evt.get("type") == "error":
                erreur = evt.get("error") or {}
                logger.warning(f"[gpt-live] session refusée ({erreur.get('code') or erreur.get('type')})"
                               " : l'appel passe par le pipeline habituel")
                break
        else:
            logger.warning("[gpt-live] session trop longue à s'ouvrir : pipeline habituel")
    except Exception as exc:
        logger.warning(f"[gpt-live] ouverture impossible ({type(exc).__name__}) : pipeline habituel")
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass
    return None


class Appel:
    """Un appel relayé entre Twilio et GPT-Live."""

    def __init__(self, twilio, session, stream_sid: str, call_sid: Optional[str], tenant: Tenant,
                 caller_number: Optional[str], call_id: Optional[int], prompt_systeme: str = ""):
        self.consignes_du_cerveau = consignes_du_cerveau(prompt_systeme, bool(caller_number))
        self.twilio = twilio
        self.session = session
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.tenant = tenant
        self.caller_number = caller_number
        self.call_id = call_id
        self.fragments: list[dict] = []      # {"role", "texte", "debut", "fin"}
        self.outils_appeles: list[dict] = []
        self.reservations: list = []
        self.secondes_voix: Optional[float] = None
        self.jetons = {"generations": 0, "jetons_entree": 0, "jetons_cache": 0, "jetons_sortie": 0}
        self.raccroche_par_nous = False
        self.cout_annonce: Optional[float] = None
        self._reponses: dict[str, dict] = {}  # délégation → appels d'outils en attente
        self._outils_en_cours: set[asyncio.Task] = set()
        self._dernier_controle = 0.0
        self._debut = time.monotonic()
        self.accueil_relance = False
        self._dernier_son = {"user": 0.0, "assistant": 0.0}
        self._tampons = {"appelant": bytearray(), "assistante": bytearray()}
        self.enregistreur = None

    # -- Twilio → GPT-Live ---------------------------------------------------------------

    async def ecouter_twilio(self) -> None:
        """Relaie la voix du client, telle que Twilio l'envoie. Finit quand il raccroche."""
        while True:
            try:
                message = json.loads(await self.twilio.receive_text())
            except (WebSocketDisconnect, RuntimeError):
                return
            except (ValueError, TypeError):
                continue
            genre = message.get("event")
            if genre == "media":
                charge = (message.get("media") or {}).get("payload")
                if charge:
                    await self.session.send(_evenement("session.input_audio.append", audio=charge))
                    self._enregistrer("appelant", charge)
            elif genre == "stop":
                return

    # -- GPT-Live → Twilio ---------------------------------------------------------------

    async def ecouter_la_session(self) -> None:
        """Relaie la voix de l'assistante, note ce qui se dit, exécute les outils. Finit
        quand la session se ferme, ou quand l'assistante a pris congé."""
        while True:
            # Le flux de la session est continu, silences compris : on ne peut pas
            # compter sur un silence du réseau pour regarder si l'appel est fini.
            if time.monotonic() - self._dernier_controle >= 0.25:
                self._dernier_controle = time.monotonic()
                if self._a_pris_conge():
                    self.raccroche_par_nous = True
                    return
                await self._relancer_l_accueil_si_muette()
            try:
                brut = await asyncio.wait_for(self.session.recv(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            evt = json.loads(brut)
            genre = evt.get("type", "")
            if genre == "session.output_audio.delta":
                charge = evt.get("delta") or ""
                await self.twilio.send_text(json.dumps({
                    "event": "media", "streamSid": self.stream_sid,
                    "media": {"payload": charge}}))
                self._enregistrer("assistante", charge)
            elif genre in ("session.input_transcript.delta", "session.output_transcript.delta"):
                self._noter_fragment("user" if "input" in genre else "assistant", evt)
            elif genre == "response.event":
                await self._evenement_d_arriere_plan(evt)
            elif genre == "session.delegation.created":
                confie = evt.get("delegation") or {}
                if confie.get("target") == "client" and confie.get("id"):
                    tache = asyncio.create_task(self._travail_delegue(confie["id"]))
                    self._outils_en_cours.add(tache)
                    tache.add_done_callback(self._outils_en_cours.discard)
            elif genre == "session.usage.updated":
                self._noter_usage(evt.get("usage"))
            elif genre == "session.closed":
                self._noter_usage(evt.get("usage"))
                return
            elif genre == "error":
                erreur = evt.get("error") or {}
                logger.warning(f"[gpt-live] erreur de session : {erreur.get('code') or erreur.get('type')} "
                               f"— {str(erreur.get('message'))[:160]}")

    async def _relancer_l_accueil_si_muette(self) -> None:
        """Personne n'a encore rien dit et le délai est passé : l'accueil est redonné, une
        seule fois, comme un propos à prononcer."""
        if (self.accueil_relance or self.fragments
                or time.monotonic() - self._debut < _ATTENTE_DE_L_ACCUEIL):
            return
        self.accueil_relance = True
        logger.warning("[gpt-live] l'assistante n'a pas ouvert l'appel : accueil redonné")
        await self.session.send(_evenement("session.commentary.append", delegation_id=None,
                                           content=accueil(self.tenant)))

    def _noter_usage(self, usage) -> None:
        secondes = (usage or {}).get("seconds")
        if isinstance(secondes, (int, float)):
            self.secondes_voix = float(secondes)   # cumul rendu par l'API, pas un incrément

    def _noter_fragment(self, role: str, evt: dict) -> None:
        texte = evt.get("delta") or ""
        if not texte:
            return
        self.fragments.append({"role": role, "texte": texte,
                               "debut": evt.get("start_ms"), "fin": evt.get("end_ms")})
        self._dernier_son[role] = time.monotonic()

    def _a_pris_conge(self) -> bool:
        """L'assistante a dit au revoir et plus personne ne parle : on raccroche, comme le
        fait le pipeline habituel (`bot.has_taken_leave`)."""
        from .bot import has_taken_leave

        dernier = max(self._dernier_son.values())
        if time.monotonic() - (dernier or self._debut) >= _SILENCE_MAXIMAL:
            logger.info(f"[gpt-live] appel {self.call_sid} : plus personne ne parle, ligne rendue")
            return True
        if not dernier or time.monotonic() - dernier < _SILENCE_AVANT_DE_RACCROCHER:
            return False
        return has_taken_leave(self.transcription())

    # -- Les outils ----------------------------------------------------------------------

    async def _evenement_d_arriere_plan(self, evt: dict) -> None:
        """Le modèle d'arrière-plan a produit quelque chose. Une fonction à exécuter part
        en tâche de fond — la voix de l'assistante continue d'être relayée pendant que
        l'outil travaille — et quand tous ses appels ont leur réponse, on lui dit de
        continuer."""
        interne = evt.get("event") or {}
        genre = interne.get("type") or ""
        cle_suivi = evt.get("delegation_id") or "sans"
        if genre == "response.output_item.done":
            element = interne.get("item") or {}
            if element.get("type") != "function_call" or element.get("status") != "completed":
                return
            identifiant, nom = element.get("call_id"), element.get("name")
            if not identifiant or not nom:
                return
            suivi = self._suivi(cle_suivi)
            if identifiant in suivi["vus"]:
                return                      # le même appel annoncé deux fois : une seule exécution
            suivi["vus"].add(identifiant)
            suivi["attendus"].add(identifiant)
            try:
                arguments = json.loads(element.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            tache = asyncio.create_task(self._repondre_a_l_outil(
                cle_suivi, identifiant, nom, arguments if isinstance(arguments, dict) else {}))
            self._outils_en_cours.add(tache)
            tache.add_done_callback(self._outils_en_cours.discard)
        elif genre in ("response.completed", "response.incomplete", "response.failed"):
            if genre == "response.completed":
                self._noter_jetons((interne.get("response") or {}).get("usage"))
            else:
                logger.warning(f"[gpt-live] travail délégué interrompu ({genre})")
            self._suivi(cle_suivi)["finie"] = True
            await self._continuer_si_pret(cle_suivi)

    def _suivi(self, cle_suivi: str) -> dict:
        return self._reponses.setdefault(cle_suivi, {"attendus": set(), "vus": set(), "finie": False})

    async def _repondre_a_l_outil(self, cle_suivi: str, identifiant: str, nom: str,
                                  arguments: dict) -> None:
        resultat = await self._executer(nom, arguments)
        await self.session.send(_evenement("response.item.create", item={
            "type": "function_call_output", "call_id": identifiant, "output": resultat}))
        self._suivi(cle_suivi)["attendus"].discard(identifiant)
        await self._continuer_si_pret(cle_suivi)

    async def _continuer_si_pret(self, cle_suivi: str) -> None:
        """La réponse a fini d'énumérer ses appels ET chacun a reçu son résultat : le
        modèle d'arrière-plan peut reprendre. Avant, l'API refuserait la commande."""
        suivi = self._reponses.get(cle_suivi)
        if suivi and suivi["finie"] and suivi["vus"] and not suivi["attendus"]:
            del self._reponses[cle_suivi]
            await self.session.send(_evenement("response.create"))

    # -- Le cerveau est le nôtre ---------------------------------------------------------

    async def _travail_delegue(self, delegation_id: str) -> None:
        """GPT-Live confie un travail sans dire lequel : c'est à nous de le lire dans la
        conversation. Notre modèle raisonne, appelle les outils, et ce qu'il rend est
        donné à dire à l'assistante. Elle garde la parole tant que rien ne revient : un
        cerveau en panne rend donc lui aussi une phrase."""
        try:
            texte = await asyncio.wait_for(self._raisonner(), timeout=_DELAI_DU_CERVEAU)
        except Exception as exc:
            logger.warning(f"[gpt-live] le cerveau n'a pas rendu son travail ({type(exc).__name__})")
            texte = ""
        if not texte:
            texte = ("Le travail demandé n'a pas pu être fait. Dis-le simplement au client, sans "
                     "inventer de raison, et propose-lui de rappeler dans quelques minutes.")
        await self.session.send(_evenement("session.commentary.append", delegation_id=delegation_id,
                                           content=texte[:1800]))

    def _demande_au_cerveau(self) -> str:
        """La conversation telle qu'elle s'est dite, et ce que les outils ont déjà rendu
        pendant cet appel : le cerveau repart de là à chaque fois."""
        lignes = ["Voici l'appel en cours, tel qu'il a été entendu :"]
        for tour in self.transcription():
            lignes.append(("Client : " if tour["role"] == "user" else "Assistante : ") + tour["content"])
        if self.outils_appeles:
            lignes.append("")
            lignes.append("Outils déjà appelés pendant cet appel :")
            for outil in self.outils_appeles:
                lignes.append(f"- {outil['nom']}({json.dumps(outil['arguments'], ensure_ascii=False)}) "
                              f"→ {str(outil.get('resultat'))[:600]}")
        lignes.append("")
        lignes.append("L'assistante vocale te confie la suite : fais ce que le client vient de demander.")
        return "\n".join(lignes)

    async def _raisonner(self) -> str:
        """Un tour de notre modèle (`llm.MODEL`), outils compris — la même boucle que
        `llm.respond`, sur la conversation entendue."""
        from .bot import llm_extra_body

        client = llm.get_client()
        messages = [{"role": "system", "content": self.consignes_du_cerveau},
                    {"role": "user", "content": self._demande_au_cerveau()}]
        message = None
        for _ in range(llm.MAX_TOOL_ROUNDS):
            reponse = await client.chat.completions.create(
                model=llm.MODEL, max_tokens=400, tools=llm._openai_tools(), messages=messages,
                **llm_extra_body())
            self._noter_jetons_du_cerveau(getattr(reponse, "usage", None))
            message = reponse.choices[0].message
            if not message.tool_calls:
                break
            messages.append({"role": "assistant", "content": message.content, "tool_calls": [
                {"id": a.id, "type": "function",
                 "function": {"name": a.function.name, "arguments": a.function.arguments}}
                for a in message.tool_calls]})
            for a in message.tool_calls:
                try:
                    arguments = json.loads(a.function.arguments or "{}")
                except ValueError:
                    arguments = {}
                resultat = await self._executer(a.function.name,
                                                arguments if isinstance(arguments, dict) else {})
                messages.append({"role": "tool", "tool_call_id": a.id, "content": resultat})
        return (getattr(message, "content", None) or "").strip()

    def _noter_jetons_du_cerveau(self, usage) -> None:
        if usage is None:
            return
        details = getattr(usage, "prompt_tokens_details", None)
        self.jetons["generations"] += 1
        self.jetons["jetons_entree"] += int(getattr(usage, "prompt_tokens", 0) or 0)
        self.jetons["jetons_cache"] += int(getattr(details, "cached_tokens", 0) or 0)
        self.jetons["jetons_sortie"] += int(getattr(usage, "completion_tokens", 0) or 0)
        cout = getattr(usage, "cost", None)
        if isinstance(cout, (int, float)):       # ce qu'OpenRouter dit avoir facturé, s'il le dit
            self.cout_annonce = (self.cout_annonce or 0.0) + float(cout)

    async def _executer(self, nom: str, arguments: dict) -> str:
        try:
            resultat = await llm.run_tool(self.tenant, nom, arguments, self.caller_number, self.call_id)
        except Exception as exc:
            resultat = f"Erreur outil {nom}: {exc}"
        self.outils_appeles.append({"nom": nom, "arguments": arguments, "resultat": resultat})
        if nom == "create_reservation":
            try:
                reference = json.loads(resultat).get("reservation_id")
                if reference:
                    self.reservations.append(reference)
            except (ValueError, TypeError, AttributeError):
                pass
        return resultat

    def _noter_jetons(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        self.jetons["generations"] += 1
        self.jetons["jetons_entree"] += int(usage.get("input_tokens") or 0)
        self.jetons["jetons_cache"] += int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        self.jetons["jetons_sortie"] += int(usage.get("output_tokens") or 0)

    # -- Ce qui reste de l'appel ----------------------------------------------------------

    def _enregistrer(self, piste: str, charge_b64: str) -> None:
        if self.enregistreur is None:
            return
        try:
            tampon = self._tampons[piste]
            tampon += base64.b64decode(charge_b64)
            if len(tampon) >= _TRANCHE_ENREGISTREMENT:
                self.enregistreur.ecrire(piste, ulaw.decoder(bytes(tampon)))
                tampon.clear()
        except Exception:  # l'enregistrement ne doit jamais faire échouer un appel
            pass

    def vider_les_tampons(self) -> None:
        """La dernière fraction de seconde de chaque piste, avant de fermer les fichiers."""
        if self.enregistreur is None:
            return
        for piste, tampon in self._tampons.items():
            if tampon:
                self.enregistreur.ecrire(piste, ulaw.decoder(bytes(tampon)))
                tampon.clear()

    def tours(self) -> list[dict]:
        """Les fragments regroupés en tours : même locuteur, moins de 0,8 s d'écart."""
        tours: list[dict] = []
        for f in self.fragments:
            dernier = tours[-1] if tours else None
            proche = (dernier is not None and dernier["role"] == f["role"]
                      and (f["debut"] is None or dernier["fin"] is None
                           or f["debut"] - dernier["fin"] <= _ECART_DE_TOUR_MS))
            if proche:
                dernier["texte"] += f["texte"]
                dernier["fin"] = f["fin"] if f["fin"] is not None else dernier["fin"]
            else:
                tours.append(dict(f))
        return tours

    def transcription(self) -> list[dict]:
        return [{"role": t["role"], "content": " ".join(t["texte"].split())}
                for t in self.tours() if t["texte"].strip()]

    def blancs_ms(self) -> list[int]:
        """Le silence entre la fin d'une phrase du client et le début de la réponse, tour
        par tour : la mesure qui se compare au pipeline habituel (`turn_latencies`)."""
        blancs = []
        tours = self.tours()
        for avant, apres in zip(tours, tours[1:]):
            if (avant["role"] == "user" and apres["role"] == "assistant"
                    and avant["fin"] is not None and apres["debut"] is not None
                    and apres["debut"] > avant["fin"]):
                blancs.append(int(apres["debut"] - avant["fin"]))
        return blancs

    def premiere_parole_ms(self) -> Optional[int]:
        """Quand l'assistante a commencé à parler, sur la ligne de temps de la session."""
        debuts = [f["debut"] for f in self.fragments
                  if f["role"] == "assistant" and f["debut"] is not None]
        return int(min(debuts)) if debuts else None

    def journal(self, etat_enregistrement: dict) -> dict:
        """Le journal de bord, dans la forme que l'admin sait lire (voice/journal.py)."""
        blancs = self.blancs_ms()
        return {
            "version": 2, "tronque": False, "enregistrement": etat_enregistrement or {},
            "accueil": {"premiere_parole_ms": self.premiere_parole_ms(),
                        "relance": self.accueil_relance}, "langue": None,
            "voix": {"fournisseur": FOURNISSEUR, "voix": voix(), "modele": MODELE,
                     "arriere_plan": modele_arriere(),
                     "cerveau": "openai" if modele_openai() else "le nôtre"},
            "consommation": {**self.jetons, "secondes_voix": self.secondes_voix,
                             "cout_cerveau_annonce": self.cout_annonce},
            "compteurs": {"blanc_median_ms": sorted(blancs)[len(blancs) // 2] if blancs else None,
                          "outils": len(self.outils_appeles)},
            "tours": [], "evenements": [],
        }


async def run_live(websocket, session, stream_sid: str, call_sid: Optional[str], tenant: Tenant,
                   caller_number: Optional[str] = None, call_id: Optional[int] = None,
                   prompt_systeme: str = "") -> None:
    """Sert l'appel avec la session déjà ouverte, puis le clôt au journal."""
    from .. import calls as calls_mod
    from .enregistrement import Enregistreur

    appel = Appel(websocket, session, stream_sid, call_sid, tenant, caller_number, call_id,
                  prompt_systeme)
    enregistreur = Enregistreur(tenant.id, call_id)
    if await enregistreur.demarrer():
        appel.enregistreur = enregistreur
    texte_accueil = accueil(tenant)
    appel._debut = time.monotonic()      # le délai de relance court à partir de l'accueil demandé
    await session.send(_evenement("session.commentary.append", delegation_id=None,
                                  content=texte_accueil))
    logger.info(f"[gpt-live] appel {call_sid} (établissement {tenant.id}) : voix « {voix()} », "
                f"arrière-plan {modele_arriere()}")
    taches = [asyncio.create_task(appel.ecouter_twilio()),
              asyncio.create_task(appel.ecouter_la_session())]
    erreur: Optional[BaseException] = None
    try:
        finies, _ = await asyncio.wait(taches, return_when=asyncio.FIRST_COMPLETED)
        erreur = next((t.exception() for t in finies if not t.cancelled() and t.exception()), None)
    finally:
        for tache in taches:
            tache.cancel()
        await asyncio.gather(*taches, return_exceptions=True)
        if appel._outils_en_cours:
            # Un outil en train d'écrire (une réservation) finit son travail : l'annuler
            # ici laisserait au journal un appel sans la réservation qu'il vient de créer.
            _, restants = await asyncio.wait(appel._outils_en_cours, timeout=5)
            for tache in restants:
                tache.cancel()
        await _fermer(session, appel)
        etat = {}
        try:
            appel.vider_les_tampons()
            await enregistreur.fermer()
            etat = enregistreur.etat()
        except Exception as exc:
            logger.warning(f"[gpt-live] fermeture de l'enregistrement KO (sans conséquence): {exc}")
        if call_sid:
            try:
                from .bot import _octets_enregistres

                identifiant = await asyncio.to_thread(
                    calls_mod.finish_call, call_sid, "completed", appel.transcription() or None,
                    appel.reservations[0] if appel.reservations else None,
                    appel.blancs_ms() or None, appel.journal(etat), _octets_enregistres(etat))
                from .. import resume

                resume.planifier(identifiant)
            except Exception as exc:
                logger.warning(f"[gpt-live] clôture au journal KO (sans conséquence): {exc}")
    if erreur is not None:
        # La session est tombée en cours d'appel : main.py passe le client au restaurant.
        raise erreur
    if appel.raccroche_par_nous:
        try:
            await websocket.close()
        except RuntimeError:
            pass


async def _fermer(session, appel: Appel) -> None:
    """Demande la fermeture et attend le décompte final des secondes, sans s'éterniser."""
    try:
        await session.send(_evenement("session.close"))
        fin = time.monotonic() + 2.0
        while time.monotonic() < fin:
            evt = json.loads(await asyncio.wait_for(session.recv(), timeout=max(0.05, fin - time.monotonic())))
            if evt.get("type") == "session.closed":
                appel._noter_usage(evt.get("usage"))
                break
    except Exception:
        pass
    try:
        await session.close()
    except Exception:
        pass
