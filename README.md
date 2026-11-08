# Voicemeeter Background

![Voicemeeter Background](docs/preview.png)

*Illustration : interface simplifiée, pas la vraie fenêtre Voicemeeter.*

Affiche une image ou un diaporama **par-dessus la fenêtre Voicemeeter** (Standard, Banana, Potato) et la **suit en temps réel**. Windows uniquement. Aucun composant VMChroma, aucune DLL injectée, aucun fichier Voicemeeter modifié.

> Projet indépendant, non affilié à VB-Audio. « Voicemeeter » est une marque de VB-Audio Software.

## Ce que fait le suivi

- Position, taille et redimensionnement de la fenêtre, suivis à environ 60 images par seconde.
- Ordre des fenêtres : l'image reste juste au-dessus de Voicemeeter. Une fenêtre placée devant Voicemeeter recouvre aussi l'image.
- Voicemeeter réduit, masqué ou sur un autre bureau virtuel : l'image se cache, puis revient dès qu'il réapparaît.
- Voicemeeter en « toujours au premier plan » : l'image suit.
- Écrans à 125 % / 150 % ou plusieurs écrans : coordonnées en pixels physiques (DPI-aware).
- Redémarrage de Voicemeeter : la nouvelle fenêtre est retrouvée toute seule.
- Fenêtre non cliquable et sans prise de focus : tu continues d'utiliser Voicemeeter normalement.

## Important : limite de la méthode

Windows ne permet pas à une application externe d'insérer une image *derrière* les contrôles d'une autre application. L'image est donc **mélangée par-dessus** toute la zone cliente : elle peut se superposer aux textes, boutons et vumètres. Garde une opacité de **10 à 25 %** pour que tout reste lisible.

## Fonctionnalités

- Image unique ou diaporama depuis un dossier (ordre alphabétique ou aléatoire).
- Opacité, assombrissement, flou, cadrage « remplir » ou « ajuster ».
- Fondu entre les images du diaporama.
- Option pour couvrir aussi la barre de titre.
- Lancement automatique avec Windows.
- Paramètres dans `%LOCALAPPDATA%\VoicemeeterBackground\settings.json`, journal dans `overlay.log`.

## Lancer depuis les sources

1. Installe Python 3 pour Windows.
2. Lance `run.bat`.
3. Choisis une image ou un dossier, puis clique sur **Activer / actualiser le fond**.
4. Règle l'opacité (commence à 10–25 %).

L'application doit rester ouverte (elle peut être réduite) tant que le fond est utilisé.

## Créer un EXE

Lance `build.bat` : l'exécutable est créé dans `dist\VoicemeeterBackground.exe`.

## Vie privée

Aucune connexion réseau, aucune télémétrie. Les chemins d'images et les préférences restent sur ton PC.

---

## English

**Voicemeeter Background** shows an image or slideshow as a click-through overlay that follows the Voicemeeter window (position, size, z-order, minimise/restore, DPI scaling, virtual desktops, always-on-top). Windows only. No injection, no DLL, no changes to Voicemeeter.

Because Windows cannot place a third-party window *behind* another app's controls, the image is blended *over* the window; keep opacity around 10–25 % for readability. Run `run.bat` from source or `build.bat` to build an EXE. Not affiliated with VB-Audio.

## Licence

MIT — voir [LICENSE](LICENSE).
