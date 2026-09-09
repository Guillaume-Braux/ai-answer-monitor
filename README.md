# Starter Gradio — proto IA à appels réels

Un fichier, une interface entrée → sortie → statut (traité / à revoir par un humain),
une bande de mesures calculées (appels, tokens, latence, coût). Fournisseur commutable
(Anthropic, OpenAI, mock).

## Jalons
- **J1 — tourne à vide** : `LLM_PROVIDER=mock python app.py`, la page s'ouvre, un clic donne une réponse simulée.
- **J2 — montrable** : clé réelle, SYSTEM_PROMPT et `traiter()` adaptés au cas, trois exemples réels.
- **J3 — capturé** : captures des trois états (chargement, traité, à revoir) + de la bande de mesures.

## Livraison
- Dépôt privé + captures + README.
- Démo : `python app.py --share` depuis le laptop → URL publique temporaire (tunnel Gradio, 72 h, serveur de partage parfois indisponible : garder un enregistrement d'écran).
- URL durable si le temps reste : Hugging Face Spaces (`gradio deploy`), Space privé.

## Règles
- Clé dans `.env` uniquement ; `.env` dans `.gitignore`.
- Mettre `PRIX_*` à jour depuis `shared/prix-modeles.md` avant de montrer un coût.
- Dire à l'écran ce qui est réel et ce qui est simulé.
