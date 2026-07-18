#!/usr/bin/env python3
"""Génère une musique d'attente téléphonique : Vivaldi, « Le Printemps » (Les Quatre
Saisons, 1725, DOMAINE PUBLIC). On SYNTHÉTISE la partition (aucun enregistrement sous
licence) : timbre type cordes (synthèse additive + enveloppe ADSR + léger vibrato),
mélodie + basse, à 8 kHz mono 16 bits (débit Twilio), bouclable.

Pour une qualité « vraie » (orchestre), remplacer simplement le fichier de sortie par
un enregistrement libre de droits (voir MOSHI_HOLD_MUSIC_PATH côté app) — même format
conseillé : WAV mono 8 kHz.
"""
import wave
from pathlib import Path

import numpy as np

RATE = 8000
OUT = Path(__file__).resolve().parent.parent / "api" / "app" / "assets" / "hold_music.wav"
TEMPO = 132  # BPM (Allegro)
BEAT = 60.0 / TEMPO  # durée d'une noire (s)


def _freq(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12.0)


# Thème principal du 1er mouvement (Allegro), en mi majeur — le motif « le printemps
# est arrivé », énoncé forte puis repris (écho) piano. (midi, noires).
# mi5=76 ré#5=75 do#5=73 si4=71 la4=69 sol#4=68 fa#4=66 mi4=64
_THEME = [
    (76, 0.5), (76, 0.5), (76, 1.0), (76, 0.5), (76, 0.5), (76, 1.0),
    (76, 0.5), (71, 0.5), (68, 0.5), (71, 0.5), (73, 1.0), (73, 1.0),
    (75, 0.5), (75, 0.5), (75, 1.0), (75, 0.5), (75, 0.5), (75, 1.0),
    (75, 0.5), (73, 0.5), (71, 0.5), (73, 0.5), (76, 2.0),
]
# Basse (bourdon) sous chaque groupe, midi + noires. mi3=52 si2=47 do#3=49
_BASS = [(52, 4.0), (48, 4.0), (47, 4.0), (52, 4.0)]


def _note(midi: int, dur_beats: float, harmonics=(1.0, 0.5, 0.33, 0.22, 0.14, 0.09)) -> np.ndarray:
    """Une note « cordes » : partiels décroissants, vibrato léger, enveloppe ADSR."""
    n = int(dur_beats * BEAT * RATE)
    t = np.arange(n) / RATE
    vib = 1.0 + 0.006 * np.sin(2 * np.pi * 5.5 * t)  # vibrato ~5,5 Hz
    f = _freq(midi)
    wave_ = sum(a * np.sin(2 * np.pi * f * h * t * vib) for h, a in enumerate(harmonics, start=1))
    # ADSR : attaque douce, petit decay, sustain, release — évite les clics.
    env = np.ones(n)
    a = min(int(0.02 * RATE), n // 4)
    d = min(int(0.06 * RATE), n // 4)
    r = min(int(0.08 * RATE), n // 3)
    env[:a] = np.linspace(0, 1, a)
    env[a:a + d] = np.linspace(1, 0.8, d)
    env[a + d:n - r] = 0.8
    env[n - r:] = np.linspace(0.8, 0, r)
    return wave_ * env


def _line(notes) -> np.ndarray:
    return np.concatenate([_note(m, d) for m, d in notes]) if notes else np.array([])


def main() -> None:
    melody = _line(_THEME)
    bass = _line([(m, d) for m, d in _BASS]) * 0.45
    n = min(len(melody), len(bass))
    mix = melody[:n] + bass[:n]
    mix = mix / np.max(np.abs(mix)) * 0.85  # remplit la dynamique 16 bits sans clipper
    # Léger fondu aux extrémités pour une boucle sans claquement.
    fade = int(0.05 * RATE)
    mix[:fade] *= np.linspace(0, 1, fade)
    mix[-fade:] *= np.linspace(1, 0, fade)
    pcm = (mix * 32767).astype(np.int16).tobytes()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    print(f"écrit {OUT} ({n / RATE:.1f}s, {RATE} Hz, {OUT.stat().st_size} octets)")


if __name__ == "__main__":
    main()
