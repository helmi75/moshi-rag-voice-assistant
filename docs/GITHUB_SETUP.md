# Instructions pour créer le dépôt GitHub

## Étape 1 : Créer le dépôt sur GitHub

1. Allez sur [GitHub](https://github.com)
2. Cliquez sur le bouton **"+"** en haut à droite → **"New repository"**
3. Remplissez les informations :
   - **Repository name** : `porjet-oshi-vast`
   - **Description** : `Assistant vocal Moshi déployé sur Vast.ai`
   - **Visibilité** : Public ou Private (selon votre choix)
   - **NE PAS** cocher "Initialize this repository with a README"
4. Cliquez sur **"Create repository"**

## Étape 2 : Initialiser Git et pousser le code

Ouvrez un terminal dans le répertoire du projet et exécutez :

```bash
# Aller dans le répertoire du projet
cd /Ubuntu/home/helmi/assistant_AI/projet-moshi-vast/projet-moshi-vast

# Initialiser Git (si pas déjà fait)
git init

# Ajouter tous les fichiers
git add .

# Faire le premier commit
git commit -m "Initial commit: Projet Moshi Voice Assistant pour Vast.ai"

# Ajouter le remote GitHub (remplacez par votre URL si différente)
git remote add origin https://github.com/helmi75/porjet-oshi-vast.git

# Renommer la branche principale en main (si nécessaire)
git branch -M main

# Pousser le code vers GitHub
git push -u origin main
```

## Étape 3 : Vérification

Allez sur https://github.com/helmi75/porjet-oshi-vast pour vérifier que tout est bien en ligne.

## Notes

- Si vous utilisez SSH au lieu de HTTPS, remplacez l'URL par : `git@github.com:helmi75/porjet-oshi-vast.git`
- Si vous avez besoin d'authentification, GitHub vous demandera vos identifiants ou un token d'accès personnel

