# Voix saccadée : diagnostic du goulot d'étranglement et voies de résolution

Ce document explique **comment lire les nouveaux logs mouchards** ajoutés au service
Pocket TTS, et liste **toutes les voies** pour supprimer le saccadage, de la moins
chère à la plus radicale.

---

## 1. Le problème en une phrase

Le saccadage vient d'un **débit de génération sous le temps réel** : Pocket TTS
`french_24l` sur la GTX 980 Ti produit l'audio à environ **×0,7 temps réel** (0,7 s
d'audio par seconde de calcul). Le téléphone vide le tampon plus vite qu'on ne le
remplit → micro-coupures. Il faut atteindre **×1,0 minimum**, idéalement **×1,3+**
pour une marge confortable.

---

## 2. Lire les logs mouchards

À chaque énoncé, deux lignes `INFO` sont désormais écrites :

```
Pocket TTS : 42 chunks, 3.36s d'audio généré en 4.60s (x0.73 temps réel).
Pocket TTS profil : [producteur] génération 4.10s (98ms/pas) | copie GPU→CPU 0.30s | total 4.45s || [consommateur] attente file 4.20s | resample 0.05s || mur 4.60s | 42 pas
```

### Comment interpréter

| Observation dans le profil | Goulot | Conclusion |
|---|---|---|
| `producteur total` ≈ `mur` **et** `attente file` élevée | La **génération** (le producteur) | Le consommateur est affamé : le modèle ne produit pas assez vite. |
| Dans le producteur : `génération` ≫ `copie GPU→CPU` | Le **modèle lui-même** | La 980 Ti (Maxwell) est trop lente sur ce modèle autorégressif. Aucun correctif logiciel simple. |
| `copie GPU→CPU` élevée | La **synchro GPU→CPU** | Chaque morceau force un `cudaSync`. Optimisable (voir §3.2). |
| `resample` élevé **et** `producteur` < `mur` | Le **rééchantillonnage CPU** | Rare ; optimisable côté CPU. |
| `ms/pas` très variable | Contention GIL / thread | Optimisable (voir §3.1). |

Pour un **log par pas** (très verbeux), mettre `POCKET_TTS_PROFILE=1` dans `.env`.

> **Attendu sur la 980 Ti :** `génération` domine tout (≈ 90-100 ms/pas), `attente file`
> ≈ `mur`. Cela **confirmerait** que le modèle sur cette carte est la limite, et oriente
> vers les voies §4 (autre TTS / autre GPU) plutôt que §3 (micro-optimisations).

---

## 3. Voies logicielles (gratuites, sur cette carte)

### 3.1 Pipeline producteur/consommateur — ✅ déjà fait
La génération tourne dans un thread dédié qui alimente une file pendant que la boucle
asyncio consomme. A fait remonter l'utilisation GPU (avant : ~30 %, le GPU attendait le
traitement aval). Gain réel mais insuffisant seul.

### 3.2 Copie GPU→CPU non bloquante
Si le profil montre `copie GPU→CPU` significative : utiliser un tenseur en mémoire
épinglée + `.to("cpu", non_blocking=True)`. Gain modéré sur Maxwell (bande passante PCIe
Gen3). À tenter seulement si `conv` pèse dans le profil.

### 3.3 `torch.compile` / quantification
- `TTSModel` expose `.compile()` (fusion de noyaux, moins de lancements). **Risqué sur
  Maxwell** : Triton/Inductor ne supportent officiellement plus sm_52 ; peut échouer ou
  ne rien gagner.
- `TTSModel.load_model(..., quantize=True)` (int8 dynamique) : pensé surtout pour le CPU,
  peu utile sur GPU Maxwell (pas d'unités int8 rapides).
- **Verdict** : à essayer, faible espérance de gain sur cette carte précise.

### 3.4 Tampon d'amorçage (jitter buffer)
Accumuler ~1-2 s d'audio avant de commencer à jouer. **Ne corrige PAS** un débit
durablement < 1,0 (l'audio finira toujours par manquer sur les longues phrases), mais
**masque** les petites phrases. Rustine, pas une solution.

---

## 4. Voies matérielles / fournisseur (les vraies solutions)

### 4.1 Cartesia — recommandé pour un premier client tout de suite
TTS temps réel dans le cloud, voix françaises de qualité proche de Kyutai.
```
VOICE_MODE=stream
TTS_PROVIDER=cartesia
CARTESIA_API_KEY=...
CARTESIA_VOICE_ID=...
```
- ✅ Débit temps réel **garanti** (leur infra GPU), zéro saccade, latence faible.
- 💶 ~1-2 centimes/minute. Crédits d'essai à l'inscription.
- La 980 Ti reste parfaite pour développer/tester en local.

### 4.2 GPU plus récent (Pascal GTX 10xx / RTX)
Sur une carte récente, Pocket TTS `french_24l` passe **largement** le temps réel, et le
**vrai 1.6B d'unmute.sh** (via `moshi-server`, ~5,3 Go VRAM) devient jouable — la voix
« extraordinaire » que vous visez depuis le début. `Dockerfile.gpu` fonctionne déjà sur
ces cartes (rétro-compatible).

### 4.3 gather + Polly — repli gratuit et fluide
```
VOICE_MODE=gather
TWILIO_VOICE=Polly.Lea-Neural
```
- ✅ Fluidité parfaite, gratuit, zéro dépendance GPU.
- ➖ Voix « neuronale correcte » plutôt que « waouh ». Suffit largement pour valider un
  premier client pendant qu'on branche mieux.

---

## 5. Tableau de décision rapide

| Votre priorité | Voie |
|---|---|
| Un client **maintenant**, budget quasi nul | **gather + Polly** (§4.3) |
| Un client **maintenant**, belle voix, quelques centimes/min | **Cartesia** (§4.1) |
| Garder Pocket TTS **local et gratuit**, voix fluide | **GPU récent** (§4.2) |
| La voix **exacte** d'unmute.sh (1.6B) | **GPU récent** + moshi-server (phase B) |
| Continuer à optimiser la 980 Ti par curiosité | §3.2 → §3.3 (gains incertains) |

---

## 6. Annexe — voix disponibles

### Pocket TTS (catalogue français, `POCKET_TTS_VOICE=`)
`estelle` (défaut, voix Unmute), `cosette`, `marius`, `alba`, `jean`, `anna`, `vera`,
`fantine`, `paul`, `eponine`, `george`… Un nom = chargé depuis un préréglage (aucun
clonage). Un chemin/URL audio = clonage de voix (poids de cloning requis).

### Cartesia (`TTS_PROVIDER=cartesia`)
Voix françaises du catalogue Cartesia via `CARTESIA_VOICE_ID` (voir leur console).

### Amazon Polly via Twilio (`TWILIO_VOICE=`, mode gather)
`Polly.Lea-Neural` (femme, fr), `Polly.Remi-Neural` (homme, fr), et les voix standard.
