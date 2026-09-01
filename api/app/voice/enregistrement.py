"""Enregistrement des appels, deux pistes séparées (#88).

**Ce module a une règle qui prime sur toutes les autres : il ne doit JAMAIS faire échouer
un appel.** Un disque plein, un dossier non inscriptible, un quota atteint, une exception
inattendue — dans tous les cas l'appel continue, la réservation se crée, et la raison est
notée pour être lue plus tard dans le journal de bord. Perdre un enregistrement de
diagnostic est sans conséquence ; raccrocher au nez d'un client en coûte un.

**Deux pistes séparées, pas un mixage.** L'appelant d'un côté, l'assistante de l'autre :
c'est ce qui permet d'entendre qui parle par-dessus qui, donc de diagnostiquer les
coupures. Un fichier mixé laisserait deviner ; deux pistes font entendre.

**L'enregistrement est subordonné à l'annonce.** `actif()` exige `rgpd.mention_active()` :
couper la mention coupe l'enregistrement. Ce n'est pas une politesse d'implémentation,
c'est la seule façon de garantir qu'on n'enregistre jamais quelqu'un qui n'en a pas été
informé — et c'est vérifié par un garde-fou de mutation.
"""
import asyncio
import os
from pathlib import Path
from typing import Optional

from loguru import logger

from . import ulaw

# Les seules pistes qui existent. Liste fermée : elle sert aussi de liste blanche à la
# route qui sert l'audio dans l'admin — une piste ne se choisit jamais librement.
PISTES = ("appelant", "assistante")

_OCTETS_PAR_SECONDE = 8000  # µ-law 8 kHz, un octet par échantillon

# Raisons possibles d'un enregistrement absent ou interrompu. Elles sont écrites dans le
# journal de bord : « pas d'enregistrement » sans motif ne se diagnostique pas.
DESACTIVE = "desactive"
DISQUE = "disque"
ECRITURE = "ecriture"
DUREE = "duree"


def _entier(nom: str, defaut: int) -> int:
    try:
        return int(os.getenv(nom, "").strip() or defaut)
    except ValueError:
        return defaut


def actif() -> bool:
    """Enregistre-t-on ? Point de décision UNIQUE, volontairement.

    Deux conditions, et la seconde est juridique : sans mention d'information au
    décroché, on n'enregistre pas. Le même interrupteur pilote donc ce que l'appelant
    entend et ce qui est écrit sur le disque — ils ne peuvent pas diverger."""
    from .. import rgpd

    demande = os.getenv("ENREGISTREMENT_APPELS", "0").strip().lower() in ("1", "true", "oui")
    return demande and rgpd.mention_active()


def dossier() -> Path:
    return Path(os.getenv("ENREGISTREMENT_DIR", "/app/data/enregistrements"))


def duree_maximale() -> int:
    """Au-delà, on cesse d'écrire — l'appel, lui, continue. Un appel de plusieurs heures
    (ligne restée ouverte, boucle) ne doit pas remplir le disque à lui seul."""
    return _entier("ENREGISTREMENT_MAX_SECONDES", 900)


def disque_minimal_mo() -> int:
    """En dessous, on n'enregistre pas du tout. Un disque plein n'emporterait pas que les
    enregistrements : SQLite cesserait d'écrire, donc les réservations seraient perdues."""
    return _entier("ENREGISTREMENT_DISQUE_MINIMUM_MO", 2000)


def chemin(tenant_id: int, call_id: int, piste: str) -> Path:
    """Chemin d'une piste. Entièrement construit côté serveur.

    `call_id` est l'identifiant SQLite, jamais le `call_sid` de Twilio : celui-ci arrive
    dans le message `start` du websocket, donc il est fourni par l'appelant du websocket.
    Un entier issu de notre propre base ne peut pas désigner un fichier ailleurs."""
    if piste not in PISTES:
        raise ValueError(f"piste inconnue : {piste!r}")
    return dossier() / f"tenant{int(tenant_id)}" / f"appel{int(call_id)}-{piste}.ulaw"


def espace_libre_mo(chemin_reference: Optional[Path] = None) -> Optional[int]:
    """Espace libre en mégaoctets, ou None si la question n'a pas de réponse ici."""
    cible = chemin_reference or dossier()
    while not cible.exists() and cible != cible.parent:
        cible = cible.parent
    try:
        st = os.statvfs(cible)
    except OSError:
        return None
    return int(st.f_bavail * st.f_frsize / 1_000_000)


class Enregistreur:
    """Écrit les deux pistes d'un appel, sans jamais bloquer la boucle d'événements.

    Le chemin de l'audio dépose dans une file et repart immédiatement (`ecrire` ne fait
    qu'un `put_nowait`). Une tâche dédiée vide la file et écrit dans un thread : la
    conversion µ-law et l'appel système sortent tous deux de l'event loop, où quelques
    millisecondes suffiraient à faire bégayer la voix.
    """

    # Assez de tranches pour absorber un à-coup disque, assez peu pour que la mémoire
    # reste bornée si l'écriture s'effondre : on préfère perdre des tranches — et les
    # compter — plutôt que gonfler indéfiniment.
    _FILE_MAX = 64

    def __init__(self, tenant_id: int, call_id: Optional[int]):
        self.tenant_id = tenant_id
        self.call_id = call_id
        self.octets = 0
        self.chunks_perdus = 0
        self.raison: Optional[str] = None
        self._fichiers: dict = {}
        self._file: Optional[asyncio.Queue] = None
        self._tache: Optional[asyncio.Task] = None
        self._demarre = False

    # -- cycle de vie ---------------------------------------------------------

    async def demarrer(self) -> bool:
        """Ouvre les deux fichiers. Renvoie False sans rien lever si on n'enregistre pas."""
        if not actif():
            self.raison = DESACTIVE
            return False
        if self.call_id is None:
            # Pas d'identifiant en base : l'appel a bien lieu, mais on n'a pas de nom de
            # fichier sûr. On préfère ne pas enregistrer plutôt qu'inventer une clé.
            self.raison = ECRITURE
            return False
        libre = espace_libre_mo()
        if libre is not None and libre < disque_minimal_mo():
            logger.warning(
                f"enregistrement : {libre} Mo libres, seuil {disque_minimal_mo()} Mo — "
                "appel non enregistré (l'appel, lui, continue)")
            self.raison = DISQUE
            return False
        try:
            await asyncio.to_thread(self._ouvrir)
        except Exception as exc:
            logger.warning(f"enregistrement : ouverture impossible ({exc}) — appel non enregistré")
            self.raison = ECRITURE
            return False
        self._file = asyncio.Queue(maxsize=self._FILE_MAX)
        self._tache = asyncio.create_task(self._boucle_ecriture())
        self._demarre = True
        return True

    def _ouvrir(self) -> None:
        cible = chemin(self.tenant_id, self.call_id, PISTES[0]).parent
        cible.mkdir(parents=True, exist_ok=True)
        for piste in PISTES:
            self._fichiers[piste] = open(chemin(self.tenant_id, self.call_id, piste), "wb")

    def ecrire(self, piste: str, pcm16: bytes) -> None:
        """Dépose une tranche. **Ne bloque jamais, ne lève jamais.**

        Appelée depuis un gestionnaire d'événement du pipeline : tout ce qui ressemble à
        un `await` ou à une exception ici se paierait en qualité de voix."""
        if not self._demarre or not pcm16 or self._file is None:
            return
        try:
            self._file.put_nowait((piste, pcm16))
        except asyncio.QueueFull:
            self.chunks_perdus += 1
        except Exception:  # une file cassée ne doit pas remonter dans le pipeline
            self.chunks_perdus += 1

    async def _boucle_ecriture(self) -> None:
        maximum = duree_maximale() * _OCTETS_PAR_SECONDE * len(PISTES)
        try:
            while True:
                element = await self._file.get()
                if element is None:  # sentinelle de fermeture
                    return
                piste, pcm16 = element
                if self.octets >= maximum:
                    if self.raison is None:
                        logger.info(
                            f"enregistrement : plafond de {duree_maximale()} s atteint, "
                            "on cesse d'écrire (l'appel continue)")
                        self.raison = DUREE
                    continue
                try:
                    self.octets += await asyncio.to_thread(self._ecrire_bloc, piste, pcm16)
                except Exception as exc:
                    logger.warning(f"enregistrement : écriture échouée ({exc}) — on arrête là")
                    self.raison = ECRITURE
                    self._demarre = False
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # la boucle ne doit jamais mourir bruyamment
            logger.warning(f"enregistrement : boucle interrompue ({exc})")
            self.raison = ECRITURE

    def _ecrire_bloc(self, piste: str, pcm16: bytes) -> int:
        fichier = self._fichiers.get(piste)
        if fichier is None:
            return 0
        octets = ulaw.encoder(pcm16)
        fichier.write(octets)
        return len(octets)

    async def fermer(self) -> None:
        """Vide la file et ferme les fichiers. Ne lève jamais : appelée dans le `finally`
        de l'appel, elle ne doit pas masquer l'erreur qui l'a amenée là."""
        try:
            if self._file is not None and self._tache is not None:
                self._file.put_nowait(None)
                # Borne courte : l'appel est terminé, l'appelant a raccroché. Attendre
                # une écriture bloquée retarderait la clôture en base.
                await asyncio.wait_for(self._tache, timeout=5)
        except (asyncio.TimeoutError, asyncio.QueueFull):
            if self._tache is not None:
                self._tache.cancel()
            self.raison = self.raison or ECRITURE
        except Exception as exc:
            logger.warning(f"enregistrement : fermeture imparfaite ({exc})")
        finally:
            self._demarre = False
            for fichier in self._fichiers.values():
                try:
                    fichier.close()
                except Exception:
                    pass
            self._fichiers.clear()

    def etat(self) -> dict:
        """Ce qui sera lu dans le journal de bord. Des faits, pas un « ok »."""
        return {
            "actif": self._demarre or self.octets > 0,
            "octets": self.octets,
            "chunks_perdus": self.chunks_perdus,
            "raison": self.raison,
        }


def supprimer(tenant_id: int, call_id: int) -> int:
    """Efface les deux pistes d'un appel. Renvoie le nombre de fichiers réellement
    supprimés — un compteur, pas un « fait » : c'est ce que la purge RGPD rapporte."""
    supprimes = 0
    for piste in PISTES:
        try:
            fichier = chemin(tenant_id, call_id, piste)
            if fichier.exists():
                fichier.unlink()
                supprimes += 1
        except OSError as exc:
            logger.warning(f"enregistrement : suppression impossible ({exc})")
    return supprimes
