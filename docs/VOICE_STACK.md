# Stack voix : état de l'art open source (recherche juillet 2026)

Objectif : pipeline **STT → CAG/RAG → LLM → TTS** en français, latence minimale,
open source de préférence, budget de départ quasi nul, avec un chemin clair vers le
100 % local. Ce document arrête les choix de la phase 2 (voir ROADMAP.md).

---

## 1. STT français — Kyutai STT (retenu)

| Modèle | Streaming | Français | Latence | Licence | Verdict |
|---|---|---|---|---|---|
| **Kyutai `stt-1b-en_fr`** | ✅ natif | ✅ (FR/EN) | ~500 ms après le mot | Open source | ✅ **Retenu** |
| Whisper Large V3 Turbo | ❌ (par blocs) | ✅ (multilingue) | bonne en batch | MIT | Précis mais pas conçu pour le temps réel |
| NVIDIA Parakeet TDT | ✅ | ⚠️ plus faible | excellente | Open | Anglais d'abord |

Pourquoi Kyutai STT gagne pour nous :
- **VAD sémantique intégré** : détecte la fin de tour au *sens* de la phrase, pas au
  silence — c'est le composant le plus dur à régler d'un agent vocal, ici fourni.
- **Batching massif** : ~400 flux temps réel sur un H100 → à notre échelle, une petite
  carte suffit et le coût marginal par appel s'effondre en scalant.
- ~2,5 Go VRAM seulement.

## 2. TTS français — Kyutai TTS 1.6B (retenu), Pocket TTS en éclaireur CPU

| Modèle | Streaming | Français | TTFA | Licence | Verdict |
|---|---|---|---|---|---|
| **Kyutai TTS 1.6B** | ✅ (parle avant la fin du texte) | ✅ | ~450-750 ms | Open source | ✅ **Retenu** |
| **Kyutai Pocket TTS** (100M) | ✅ | ✅ (depuis avr. 2026) | temps réel **sur CPU** | Open source | ✅ Option zéro-GPU |
| Chatterbox / Turbo (Resemble) | ✅ | ⚠️ Turbo = anglais seul | ~rapide | Open | Multilingue hors Turbo, moins bon fit FR |
| Fish Speech / OpenAudio | ✅ | ✅ | ~100 ms (API) | **CC-BY-NC** | ❌ non commercial sans licence payante |
| Kokoro | ✅ | ⚠️ limité | rapide | Apache | Français trop juste |

Points forts Kyutai TTS : *delayed streams modeling* — la synthèse démarre pendant que
le LLM écrit encore ; 100+ voix communautaires (projet de donation de voix) ; ~5,3 Go VRAM.
**Pocket TTS** (janv. 2026) change la donne budget : 100 M de paramètres, temps réel sur
CPU, clonage de voix — permet un pilote sans aucun GPU.

## 3. Pipeline intégré — Unmute (Kyutai) : la référence, pas le produit

[Unmute](https://github.com/kyutai-labs/unmute) (MIT) assemble exactement notre pipeline :
STT Kyutai → n'importe quel LLM OpenAI-compatible → TTS Kyutai, **latence totale < 1 s**
(~450 ms TTFA en multi-GPU), déployable en Docker Compose avec **16 Go de VRAM au total**
(STT 2,5 + TTS 5,3 + LLM 6+).

**Mais deux manques bloquants pour notre produit** (vérifiés dans le repo, juillet 2026) :
1. **Pas de function calling** → impossible de prendre une réservation.
2. **Pas de téléphonie** (ni Twilio ni SIP) → pas d'appels entrants.

Décision : **réutiliser les briques STT/TTS de Kyutai, pas Unmute entier** ; Unmute sert
de référence d'architecture et de preuve que la latence < 1 s est atteignable avec ces briques.

## 4. Orchestration — Pipecat (retenu)

- **Pipecat** (open source, Python, pipeline-first) : notre backend est en Python/FastAPI,
  Pipecat s'intègre à **Twilio Media Streams** sans gérer soi-même l'infra WebRTC, et
  chaque étage (STT/LLM/TTS) est une interface interchangeable — exactement notre
  stratégie de bascule API → local.
- LiveKit Agents : latences équivalentes (~750-950 ms bout-en-bout mesurés sur des stacks
  comparables), pertinent plus tard si on internalise le SIP à gros volume.

## 5. LLM — API d'abord, quantisé local ensuite

**Les modèles quantisés sont mûrs en 2026** :
- **Qwen3 8B** (Q4/AWQ, ~8 Go VRAM) : référence 2026 du tool calling local, très bon
  français. Servi par **vLLM** (API OpenAI-compatible → se branche dans Pipecat et
  dans notre `llm.py` sans refonte).
- Mistral Small / Ministral 8B : alternative française crédible.
- Le tool calling tient bien en GGUF quantisé (llama.cpp/Ollama pour le dev, vLLM en prod).

**Pour le premier client on reste sur l'API Claude** (le `api/app/llm.py` actuel) :
le function calling fiable (réservations) est le cœur du produit, l'API coûte quelques
euros/mois à faible volume, zéro maintenance, et `LLM_MODEL` permet déjà d'ajuster.

**CAG vs RAG** : notre approche actuelle — KB du commerce entière dans le prompt système
(= CAG, avec prompt caching) — reste la bonne : une KB de restaurant fait 1-5 K tokens,
un vector store n'apporterait que des risques de rappel manqué. RAG vectoriel seulement
quand un tenant aura de vrais corpus (phase 4).

## 6. Le 100 % local : oui, mais au bon moment

Coûts GPU cloud (2026) : RTX 4090 ≈ 0,18-0,40 $/h on-demand → **~130-290 $/mois en 24/7**
(RunPod, Vast.ai ; le spot est moins cher mais interruptible — inacceptable au téléphone).

| | APIs (phase A) | 100 % local (phase B) |
|---|---|---|
| Coût fixe/mois | ~0 € | 130-290 € (une RTX 4090 louée) |
| Coût marginal/min | ~0,03-0,08 € | ~0 € |
| Latence | ~1-1,5 s | < 1 s possible (réf. Unmute) |
| Maintenance | nulle | à assumer |
| Argument commercial | rapidité de mise en marché | **souveraineté/RGPD : « aucune donnée ne sort du serveur »** |

**Seuils de bascule vers le local** :
- volume > ~2 000 min/mois facturées (le fixe GPU devient inférieur au variable API), ou
- premier client santé (médecins) où le « on-premise/souverain » se vend, ou
- besoin de latence < 1 s comme différenciateur.

Le point clé : **STT Kyutai (2,5 Go) + TTS Kyutai (5,3 Go) + Qwen3 8B AWQ (~10 Go)
tiennent ensemble sur UNE RTX 4090 24 Go** et servent des dizaines d'appels simultanés.

## 7. Budget latence cible (phase 2)

```
Parole client ──▶ STT Kyutai (VAD sémantique)   ~500 ms après fin de parole
             ──▶ LLM (premier token, CAG)        ~200-400 ms
             ──▶ TTS Kyutai (premier son)        ~450 ms (streaming pendant que le LLM écrit)
             ──▶ transport Twilio                ~100-150 ms
                                        Total ≈ 1,0-1,3 s  (réf. Unmute : < 1 s)
```

La boucle Gather/Say actuelle (phase 1) fait 2-4 s : la phase 2 divise la latence par ~2-3.

## 8. Décision (stack biphasée, même code)

- **Phase A — premier client, ~0 € fixe** : Twilio Media Streams → Pipecat →
  STT/TTS via API à l'usage (ou Kyutai sur GPU à l'heure pendant les heures d'ouverture)
  → LLM Claude via `api/app/llm.py` (inchangé).
- **Phase B — scale / souveraineté** : mêmes interfaces Pipecat, bascule brique par
  brique vers Kyutai STT + Kyutai TTS + Qwen3 8B AWQ (vLLM) sur une 4090 louée.
  Le cerveau multi-tenant (`llm.py`, `tenants.py`) ne change jamais.

## Sources

- Kyutai STT : https://kyutai.org/stt/ · Kyutai TTS : https://kyutai.org/tts/
- Unmute : https://github.com/kyutai-labs/unmute · https://kyutai.org/unmute/
- Benchmarks STT 2026 : https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks
- TTS open source 2026 : https://www.bentoml.com/blog/exploring-the-world-of-open-source-text-to-speech-models
- Pipecat vs LiveKit : https://www.cekura.ai/blogs/pipecat-vs-livekit-the-real-difference ·
  https://webrtc.ventures/2026/03/choosing-a-voice-ai-agent-production-framework/
- Tool calling local 2026 : https://www.promptquorum.com/power-local-llm/best-local-models-tool-calling-2026
- Prix GPU : https://getdeploying.com/gpus/nvidia-rtx-4090 · https://www.runpod.io/pricing
