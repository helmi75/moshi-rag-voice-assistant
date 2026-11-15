"""Les traces de la recette d'appels automatique (ASSISTANTE-192) : la liste des passages, le
détail de chacun (scénarios, conversations) et l'audio des vrais appels.

Réservé au super-admin : ce sont des appels d'essai de l'établissement de recette, pas des
données d'un restaurateur. L'audio est relu depuis la base puis le chemin est reconstruit côté
serveur (`recettes_archive.chemin_audio`) : le nom d'un fichier ne vient jamais de l'URL."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response

from .. import db, recettes_archive
from ..voice import ulaw
from . import deps

router = APIRouter(dependencies=[Depends(deps.require_superadmin)])


@router.get("/admin/recettes")
async def liste(request: Request):
    passages = await db.hors_boucle(recettes_archive.lister)
    return deps.templates.TemplateResponse(request, "recettes.html", {"passages": passages})


# Un objet Path par paramètre : FastAPI y inscrit le nom du paramètre, il ne se partage pas.
@router.get("/admin/recettes/{recette_id}")
async def detail(request: Request, recette_id: int = Path(ge=1, le=10**12)):
    recette = await db.hors_boucle(recettes_archive.obtenir, recette_id)
    if recette is None:
        raise HTTPException(status_code=404)
    return deps.templates.TemplateResponse(request, "recette.html", {"recette": recette})


@router.get("/admin/recettes/audio/{scenario_id}.wav")
async def audio(scenario_id: int = Path(ge=1, le=10**12)):
    nom = await db.hors_boucle(recettes_archive.scenario_audio, scenario_id)
    if not nom:
        raise HTTPException(status_code=404)

    def _assembler() -> bytes:
        def lire(piste: str) -> bytes:
            chemin = recettes_archive.chemin_audio(nom, piste)
            return ulaw.decoder(chemin.read_bytes()) if chemin.exists() else b""
        return ulaw.mixer(lire("appelant"), lire("assistante"))

    # Dans un thread : le même processus porte l'audio des appels en cours.
    try:
        pcm = await asyncio.to_thread(_assembler)
    except (OSError, ValueError):
        raise HTTPException(status_code=404) from None
    if not pcm:
        raise HTTPException(status_code=404)
    return Response(content=ulaw.entete_wav(len(pcm), canaux=1) + pcm, media_type="audio/wav")
