#!/usr/bin/env python3
"""Génère une musique d'attente douce et bouclable (Phase 3, secours cold start).

Pas de fichier sous licence : on synthétise un pad discret (accord majeur, léger
trémolo) à 8 kHz mono 16 bits — le débit de Twilio, jouable tel quel par le
pipeline. Toutes les fréquences sont entières sur une durée entière : chaque
sinusoïde boucle donc sans claquement. Sortie : api/app/assets/hold_music.wav
(embarqué dans l'image Docker, cf. Dockerfile qui copie ./app).
"""
import wave
from pathlib import Path

import numpy as np

RATE = 8000
DURATION = 8.0  # secondes ; bouclé pendant l'attente
OUT = Path(__file__).resolve().parent.parent / "api" / "app" / "assets" / "hold_music.wav"


def main() -> None:
    t = np.linspace(0, DURATION, int(RATE * DURATION), endpoint=False)
    # Accord majeur doux (La3 / Do#4 / Mi4), partiels décroissants pour un timbre feutré.
    chord = [(220, 1.0), (277, 0.7), (330, 0.5)]
    sig = sum(amp * np.sin(2 * np.pi * f * t) for f, amp in chord)
    # Trémolo lent (0,25 Hz = 2 cycles sur 8 s, entier -> boucle propre) : mouvement léger.
    tremolo = 0.85 + 0.15 * np.sin(2 * np.pi * 0.25 * t)
    sig = sig * tremolo
    # Volume modeste : c'est un fond d'attente, pas une réponse. Pic à ~0,18.
    sig = sig / np.max(np.abs(sig)) * 0.18
    pcm = (sig * 32767).astype(np.int16).tobytes()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    print(f"écrit {OUT} ({DURATION:.0f}s, {RATE} Hz, {OUT.stat().st_size} octets)")


if __name__ == "__main__":
    main()
