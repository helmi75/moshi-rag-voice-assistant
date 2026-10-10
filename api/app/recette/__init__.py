"""Recette d'appels automatique : un client joué par la machine, sans téléphone.

Helmi faisait lui-même chaque essai d'appel de `docs/RECETTE.md`. Le 09/10/2026, trois
déploiements dans la journée : personne n'appelle trois fois six scénarios. Ce paquet joue
les scénarios qui se jugent en base et au journal, à deux étages :

- étage 1, sans Twilio : un faux Twilio se branche sur `/ws/voice` d'une copie jetable de
  l'application (base jetable), pour les deux moteurs ;
- étage 2, de vrais appels : Twilio compose le numéro de l'établissement d'essai, et la
  jambe qui appelle est branchée sur `/ws/recette`, où ce même client parle.

Ce qu'il ne juge pas : la voix à l'oreille, un vrai réseau mobile, un accent. Voir
`docs/RECETTE.md`, « Ce que joue la machine ».
"""
