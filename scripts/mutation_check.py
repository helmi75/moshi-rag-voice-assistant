#!/usr/bin/env python3
"""Vérifie que les garde-fous critiques sont RÉELLEMENT tenus par un test.

Une suite verte ne prouve rien : elle resterait verte si la protection disparaissait,
tant qu'aucun test ne la vise. Ce script retire volontairement chaque garde-fou, relance
les tests concernés, et exige qu'ils passent au ROUGE. Un garde-fou qui survit à sa
mutation est protégé par un test décoratif — c'est le résultat utile de ce script.

Les cinq mutations ci-dessous ne sont pas choisies au hasard : chacune reproduit une
panne réellement survenue, ou qui serait passée inaperçue parce qu'elle est SILENCIEUSE
(aucune exception, aucun journal — seul l'appelant l'entend).

    python3 scripts/mutation_check.py                # docker en local, natif en CI
    python3 scripts/mutation_check.py --runner native

⚠️ Le script modifie des fichiers source puis les restaure. Il refuse donc de démarrer
si des fichiers SUIVIS sont modifiés, et vérifie l'arbre après restauration.
"""
import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


@dataclass
class GardeFou:
    nom: str
    fichier: str
    avant: str  # doit apparaître EXACTEMENT une fois
    apres: str
    tests: list[str]
    k: str
    panne: str
    mordu: bool | None = field(default=None)


GARDE_FOUS = [
    GardeFou(
        nom="Voix hors catalogue → voix par défaut",
        fichier="api/app/voice/voices.py",
        avant="    return chosen if get(chosen) else default_id()",
        apres="    return chosen if chosen else default_id()",
        tests=["test_voices.py", "test_greeting.py", "test_admin_voice.py"],
        k="hors_catalogue or outside_catalogue",
        panne="moshi-server remplacerait la voix EN SILENCE par sa voix de repli",
    ),
    GardeFou(
        nom="`reasoning` emballé dans extra_body",
        fichier="api/app/voice/bot.py",
        avant='    return {"extra_body": {"reasoning": {"max_tokens": 0}}}',
        apres='    return {"reasoning": {"max_tokens": 0}}',
        tests=["test_voice_stream.py"],
        k="reasoning or Reasoning",
        panne="TypeError dans le SDK OpenAI → TOUS les appels muets (vécu le 30/07/2026)",
    ),
    GardeFou(
        nom="Fail-safe de l'interruption (seul « voix » désactive)",
        fichier="api/app/voice/bot.py",
        avant='    if os.getenv("INTERRUPTION", "mots").strip().lower() == "voix":',
        apres='    if os.getenv("INTERRUPTION", "mots").strip().lower() != "mots":',
        tests=["test_voice_stream.py"],
        k="interruption or valeur_inconnue or mode_mots or mode_voix",
        panne="une faute de frappe ramènerait les coupures sur bruit sans le dire",
    ),
    GardeFou(
        nom="`keyterm` (et non `keywords`) pour nova-3",
        fichier="api/app/voice/bot.py",
        avant='        if model.startswith("nova-3"):',
        apres='        if False:  # mutation',
        tests=["test_kyutai_stt.py"],
        k="nova3 or nova2 or default_model or keyword",
        panne="HTTP 400 de Deepgram → le STT ne démarre pas, tous les appels muets",
    ),
    GardeFou(
        nom="Scoping tenant (resolve_tenant)",
        fichier="api/app/admin/deps.py",
        avant="        if tenant_id is not None and tenant_id != user.tenant_id:",
        apres="        if False:  # mutation",
        tests=["test_admin_security.py"],
        k="cloisonnement or atteint",
        panne="un restaurateur agirait sur les réglages d'un autre établissement",
    ),
    GardeFou(
        nom="Scoping des objets (check_tenant_access)",
        fichier="api/app/admin/deps.py",
        avant="    if not user.is_superadmin and tenant_id != user.tenant_id:",
        apres="    if False:  # mutation",
        tests=["test_admin_security.py", "test_admin_v3.py"],
        k="cloisonnement or atteint or leak",
        panne="un restaurateur lirait les réservations et appels d'un autre",
    ),
    GardeFou(
        nom="Vérification du jeton CSRF",
        fichier="api/app/admin/deps.py",
        avant="    if not expected or not hmac.compare_digest(expected, provided):",
        apres="    if False:  # mutation",
        tests=["test_admin_security.py"],
        k="csrf",
        panne="un site tiers ferait agir un restaurateur connecté à son insu",
    ),
    GardeFou(
        nom="Limitation des tentatives de connexion",
        fichier="api/app/admin/routes_auth.py",
        avant="    attente = throttle.bloque(request)",
        apres="    attente = 0  # mutation",
        tests=["test_admin_security.py"],
        k="force_brute or tentatives or reessayer or adresse",
        panne="grignotage illimité du mot de passe admin depuis Internet",
    ),
    GardeFou(
        nom="Cloisonnement des établissements",
        fichier="api/app/admin/routes_reservations.py",
        avant="    return {t.id: t.name for t in tenants.list_all()} if user.is_superadmin else {}",
        apres="    return {t.id: t.name for t in tenants.list_all()}",
        tests=["test_admin_v3.py"],
        k="leak or scoping",
        panne="un restaurateur verrait le nom des autres établissements",
    ),
    # --- Supervision (#24) ----------------------------------------------------
    # Une supervision est le garde-fou le plus facile à rendre décoratif : elle a
    # l'air de fonctionner tant qu'on ne provoque pas la panne. Les trois mutations
    # suivantes coupent le fil de l'alarme à trois endroits différents.
    GardeFou(
        nom="La sonde renvoie 503 en panne (sinon l'alerte ne part jamais)",
        fichier="api/app/main.py",
        avant='    code = 503 if etat["niveau"] == supervision.PANNE else 200',
        apres="    code = 200  # mutation",
        tests=["test_supervision.py"],
        k="503 or sonde",
        panne="la sonde répondrait 200 en pleine panne : surveillance parfaitement muette",
    ),
    GardeFou(
        nom="Le verdict global est le PIRE des contrôles",
        fichier="api/app/supervision.py",
        avant='    return max(niveaux, key=lambda n: _RANG.get(n, 0)) if niveaux else OK',
        apres="    return OK  # mutation",
        tests=["test_supervision.py"],
        k="pire or verdict or sonde or ecran",
        panne="un contrôle rouge noyé dans huit verts : tout resterait au vert",
    ),
    GardeFou(
        nom="Détection des appels muets",
        fichier="api/app/supervision.py",
        avant='    return any(isinstance(t, dict) and t.get("role") == "assistant" for t in tours)',
        apres="    return True  # mutation",
        tests=["test_supervision.py"],
        k="muet",
        panne="la panne du 30/07 (appels sans un mot, tout en HTTP 200) redeviendrait invisible",
    ),
    # --- Facturation (#31) ----------------------------------------------------
    GardeFou(
        nom="La formule ne se choisit pas soi-même",
        fichier="api/app/admin/routes_tenants.py",
        avant="    if user.is_superadmin and plans.get(plan) is not None:",
        apres="    if plans.get(plan) is not None:  # mutation",
        tests=["test_quotas.py"],
        k="formule or choisit",
        panne="un restaurateur s'attribuerait la formule à 750 appels : élévation de "
              "privilège qui ne ressemble pas à une faille, et qui coûte de l'argent",
    ),
    GardeFou(
        nom="Un plafond vient d'une formule réellement vendue",
        fichier="api/app/plans.py",
        avant="    choisie = getattr(tenant, \"plan\", None)\n    return get(choisie) or defaut()",
        apres="    return get(getattr(tenant, \"plan\", None)) or CATALOGUE[-1]  # mutation",
        tests=["test_quotas.py", "test_plans.py"],
        k="catalogue or inventee or defaut",
        panne="une valeur hors catalogue en base donnerait le plafond le plus généreux",
    ),
    # --- Données personnelles (#22) -------------------------------------------
    GardeFou(
        nom="La purge efface vraiment les transcriptions",
        fichier="api/app/rgpd.py",
        avant="                 AND (transcript IS NOT NULL OR summary IS NOT NULL\n"
              "                      OR journal IS NOT NULL)\"\"\",",
        apres="                 AND 1 = 0\"\"\",  # mutation",
        tests=["test_rgpd.py"],
        k="transcript or purge",
        panne="la durée de conservation annoncée deviendrait un mensonge : le registre "
              "dirait 30 jours et la base garderait tout",
    ),
    GardeFou(
        nom="L'accueil ne se prononce jamais sans la mention d'information",
        fichier="api/app/rgpd.py",
        avant="    return f\"{texte} {mention()}\".strip()",
        apres="    return texte  # mutation",
        tests=["test_rgpd.py"],
        k="mention",
        panne="l'appelant ignorerait qu'il parle à une machine et que ce qu'il dit est traité",
    ),
    # --- Modification par téléphone (#33) -------------------------------------
    GardeFou(
        nom="Une réservation n'est rendue qu'à son propriétaire",
        fichier="api/app/reservations.py",
        avant="                WHERE id = ? AND tenant_id = ? AND customer_phone = ? AND {ACTIVES}\"\"\",\n            (reservation_id, tenant_id, phone),",
        apres="                WHERE id = ?\"\"\",  # mutation\n            (reservation_id,),",
        tests=["test_reservation_modification.py"],
        k="autre or masque or impossible",
        panne="l'assistante annulerait la réservation de quelqu'un d'autre sur un simple "
              "identifiant — incident qu'aucun journal ne rattrape",
    ),
    GardeFou(
        nom="Une annulation libère vraiment la table",
        fichier="api/app/reservations.py",
        avant='ACTIVES = "cancelled_at IS NULL"',
        apres='ACTIVES = "1 = 1"  # mutation',
        tests=["test_reservation_modification.py"],
        k="annul or libere or couverts",
        panne="une table annulée compterait encore : l'assistante refuserait un créneau "
              "pourtant libre, sans qu'aucune erreur n'apparaisse",
    ),
    GardeFou(
        nom="Le téléphone d'une réservation vient du réseau, pas du modèle",
        fichier="api/app/llm.py",
        avant='            customer_phone=(caller_number or "").strip() or None,',
        apres='            customer_phone=tool_input.get("customer_phone"),  # mutation',
        tests=["test_reservation_modification.py"],
        k="reseau or telephone",
        panne="l'appelant déciderait de qui il est, donc à quelles réservations il accède",
    ),
    # --- Messages pris au téléphone (#32) -------------------------------------
    GardeFou(
        nom="Une promesse de rappel laisse toujours une trace",
        fichier="api/app/llm.py",
        avant="        identifiant = messages.create_message(",
        apres="        identifiant = 1 if True else messages.create_message(  # mutation",
        tests=["test_messages.py"],
        k="enregistre or masque or trace",
        panne="l'assistante annoncerait un rappel que personne n'aurait noté — le "
              "défaut relevé dans 5 vrais appels sur 10 avant que cet outil n'existe",
    ),
    GardeFou(
        nom="Les messages tombent sous la purge comme le reste",
        fichier="api/app/rgpd.py",
        avant="               WHERE created_at < datetime('now', ?) AND details IS NOT NULL\"\"\",",
        apres="               WHERE 0 AND created_at < datetime('now', ?) AND details IS NOT NULL\"\"\",",
        tests=["test_messages.py"],
        k="purge",
        panne="le nom et la demande d'un appelant resteraient en base sans limite de "
              "durée, hors de tout ce que le registre annonce",
    ),
    # --- Enregistrement et journal de bord (#88) ------------------------------
    GardeFou(
        nom="On n'enregistre jamais sans l'avoir annoncé",
        fichier="api/app/voice/enregistrement.py",
        avant="    return demande and rgpd.mention_active()",
        apres="    return demande  # mutation",
        tests=["test_enregistrement.py", "test_rgpd.py"],
        k="mention or annonce",
        panne="on enregistrerait la voix d'appelants qui n'en ont pas été informés — "
              "la faute qu'aucun journal ne rattrape et qu'aucun client ne pardonne",
    ),
    GardeFou(
        nom="La voix ne survit jamais à sa transcription",
        fichier="api/app/rgpd.py",
        avant='    return min(_jours("RETENTION_ENREGISTREMENT_JOURS", 30), jours_transcript())',
        apres='    return _jours("RETENTION_ENREGISTREMENT_JOURS", 30)  # mutation',
        tests=["test_rgpd.py"],
        k="survit or transcription or duree",
        panne="un réglage distrait garderait la voix des mois après le texte qu'elle a "
              "produit, en contradiction avec le registre",
    ),
    GardeFou(
        nom="La purge efface les fichiers, pas seulement les lignes",
        fichier="api/app/rgpd.py",
        avant="        enregistrements=_purger_enregistrements(),",
        apres="        enregistrements=0,  # mutation",
        tests=["test_rgpd.py"],
        k="fichiers or orphelin or efface",
        panne="la durée du registre deviendrait un mensonge sur la donnée la plus "
              "sensible du produit : la voix resterait sur le disque indéfiniment",
    ),
    GardeFou(
        nom="Le journal de bord tombe avec le transcript",
        fichier="api/app/rgpd.py",
        avant='            """UPDATE calls SET transcript = NULL, summary = NULL, journal = NULL',
        apres='            """UPDATE calls SET transcript = NULL, summary = NULL  -- mutation',
        tests=["test_rgpd.py"],
        k="journal",
        panne="des extraits de conversation survivraient au transcript qu'ils citent",
    ),
    GardeFou(
        nom="Une piste audio ne se choisit pas librement",
        fichier="api/app/admin/routes_calls.py",
        avant="    if piste not in _PISTES_SERVIES:",
        apres="    if False:  # mutation",
        tests=["test_admin_diagnostic.py"],
        k="piste",
        panne="un chemin arbitraire servi depuis l'admin, sur la donnée la plus sensible",
    ),
    GardeFou(
        nom="Les métriques restent activées (sans elles, le blanc est indécomposable)",
        fichier="api/app/voice/bot.py",
        avant="            enable_metrics=True,\n            enable_usage_metrics=True,",
        apres="            enable_metrics=False,  # mutation",
        tests=["test_voice_stream.py"],
        k="metriques or enregistrement",
        panne="les services publieraient leur temps jusqu'au premier octet dans le vide : "
              "on retomberait sur un blanc global impossible à attribuer, la panne d'origine",
    ),
]


def arbre_propre() -> bool | None:
    """Fichiers SUIVIS uniquement : les fichiers non suivis ne risquent rien.

    None si git est indisponible : sans lui on ne peut ni garantir qu'on n'écrase pas du
    travail, ni vérifier la restauration. Ce script édite des sources — il vaut mieux
    refuser de tourner que de le faire à l'aveugle."""
    try:
        return all(
            subprocess.run(["git", "diff", "--quiet"] + extra, cwd=RACINE).returncode == 0
            for extra in ([], ["--cached"])
        )
    except FileNotFoundError:
        return None


def lancer_tests(garde: GardeFou, runner: str) -> int:
    """Renvoie le code de sortie de pytest. Non nul = les tests ont mordu."""
    if runner == "native":
        cible = [f"tests/{t}" for t in garde.tests]
        cmd = [sys.executable, "-m", "pytest", *cible, "-k", garde.k, "-q",
               "--no-header", "-p", "no:cacheprovider"]
        return subprocess.run(cmd, cwd=RACINE / "api",
                              env={**os.environ, "VOICE_MODE": "gather"},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode

    cible = " ".join(f"/app/tests/{t}" for t in garde.tests)
    interne = (
        "pip install -q -r /app/tests/requirements-test.txt >/dev/null 2>&1; "
        f"python -m pytest {cible} -k \"{garde.k}\" -q --no-header -p no:cacheprovider"
    )
    cmd = ["docker", "compose", "run", "--rm", "-e", "VOICE_MODE=gather",
           "-v", f"{RACINE}/api/tests:/app/tests", "-v", f"{RACINE}/api/app:/app/app",
           "api", "sh", "-c", interne]
    return subprocess.run(cmd, cwd=RACINE,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--runner", choices=["auto", "docker", "native"], default="auto")
    args = p.parse_args()
    runner = args.runner
    if runner == "auto":
        runner = "native" if os.getenv("CI") else "docker"

    etat = arbre_propre()
    if etat is None:
        print("✗ git est introuvable. Ce script édite des sources et a besoin de git "
              "pour garantir qu'il ne détruit rien.")
        return 2
    if not etat:
        print("✗ Des fichiers suivis sont modifiés. Ce script édite les sources : "
              "commite ou remise ton travail d'abord.")
        return 2

    originaux: dict[str, str] = {}

    def restaurer(*_):
        for chemin, contenu in originaux.items():
            (RACINE / chemin).write_text(contenu, encoding="utf-8")
        originaux.clear()

    signal.signal(signal.SIGINT, lambda *a: (restaurer(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *a: (restaurer(), sys.exit(143)))

    print(f"Vérification de {len(GARDE_FOUS)} garde-fous (runner : {runner}).\n")
    try:
        for garde in GARDE_FOUS:
            chemin = RACINE / garde.fichier
            contenu = chemin.read_text(encoding="utf-8")
            occurrences = contenu.count(garde.avant)
            if occurrences != 1:
                # Le garde-fou a bougé : échouer FORT plutôt que sauter en silence,
                # sinon ce script devient à son tour décoratif.
                print(f"✗ {garde.nom}\n    ancre introuvable ou ambiguë dans "
                      f"{garde.fichier} ({occurrences} occurrence(s)) — script à mettre à jour")
                garde.mordu = False
                continue

            originaux[garde.fichier] = contenu
            chemin.write_text(contenu.replace(garde.avant, garde.apres), encoding="utf-8")
            code = lancer_tests(garde, runner)
            chemin.write_text(contenu, encoding="utf-8")
            originaux.pop(garde.fichier, None)

            garde.mordu = code != 0
            print(f"{'✓' if garde.mordu else '✗'} {garde.nom}")
            print(f"    {'les tests mordent' if garde.mordu else 'AUCUN TEST NE ROUGIT'}"
                  f" — sans lui : {garde.panne}")
    finally:
        restaurer()

    if arbre_propre() is not True:
        print("\n✗ L'arbre n'a PAS été restauré correctement — vérifie `git diff`.")
        return 2

    survivants = [g for g in GARDE_FOUS if not g.mordu]
    print(f"\n{len(GARDE_FOUS) - len(survivants)}/{len(GARDE_FOUS)} garde-fous "
          f"réellement testés.")
    if survivants:
        print("Survivants (protégés par un test décoratif) :")
        for g in survivants:
            print(f"  - {g.nom}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
