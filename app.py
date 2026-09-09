"""
Starter Gradio — prototype IA démontrable, appels API réels.

Le jour J : remplacer SYSTEM_PROMPT, la fonction `traiter` et les EXEMPLES.
Ne pas toucher au reste tant que J1 (ça tourne à vide) n'est pas atteint.

Lancement :
    cp .env.example .env   # puis renseigner la clé
    pip install -r requirements.txt
    python app.py          # local : http://127.0.0.1:7860
    python app.py --share  # + URL publique temporaire (tunnel Gradio, 72 h max — testé avec gradio 6.26)

Variables d'environnement (.env) :
    LLM_PROVIDER = anthropic | openai
    ANTHROPIC_API_KEY / OPENAI_API_KEY
    LLM_MODEL      = identifiant du modèle (voir shared/prix-modeles.md)
    DEMO_USER / DEMO_PASSWORD  = si définis, la page demande un mot de passe
    MAX_CALLS      = plafond d'appels pour la session (défaut 200)
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

import gradio as gr
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────────── À ADAPTER LE JOUR J ─────────────────────────────

TITRE = "Prototype — <nom du cas>"
SOUS_TITRE = "Ce que cette démonstration prouve : <une phrase, la même que dans la note>."

SYSTEM_PROMPT = """Tu es <rôle>. Tu réponds en <langue>.
Règles :
- Réponds uniquement à partir des informations fournies.
- Si l'information manque, dis-le au lieu d'inventer.
- Termine par une ligne 'CONFIANCE: haute|moyenne|basse'.
"""

EXEMPLES = [
    "<exemple d'entrée 1>",
    "<exemple d'entrée 2>",
    "<exemple d'entrée 3>",
]

SEUIL_REVUE_HUMAINE = "basse"  # niveau de confiance qui déclenche « à revoir »

# ───────────────────────────── FOURNISSEUR ─────────────────────────────


@dataclass
class Compteur:
    appels: int = 0
    tokens_entree: int = 0
    tokens_sortie: int = 0
    latences: list[float] = field(default_factory=list)

    @property
    def latence_moyenne(self) -> float:
        return sum(self.latences) / len(self.latences) if self.latences else 0.0


COMPTEUR = Compteur()
MAX_CALLS = int(os.getenv("MAX_CALLS", "200"))
PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
MODEL = os.getenv("LLM_MODEL", "")

# Prix au million de tokens (entrée, sortie) — À METTRE À JOUR depuis shared/prix-modeles.md.
PRIX_ENTREE_PAR_M = float(os.getenv("PRIX_ENTREE_PAR_M", "0"))
PRIX_SORTIE_PAR_M = float(os.getenv("PRIX_SORTIE_PAR_M", "0"))


def appeler_modele(system: str, user: str) -> tuple[str, int, int]:
    """Retourne (texte, tokens_entree, tokens_sortie). Un seul endroit à changer de fournisseur."""
    if PROVIDER == "anthropic":
        from anthropic import Anthropic

        client = Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        texte = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return texte, resp.usage.input_tokens, resp.usage.output_tokens
    if PROVIDER == "openai":
        from openai import OpenAI

        client = OpenAI()
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        u = resp.usage
        return resp.choices[0].message.content or "", u.prompt_tokens, u.completion_tokens
    if PROVIDER == "mock":  # J1 : tourner à vide sans clé
        time.sleep(0.6)
        return f"[MOCK] Réponse simulée pour : {user[:60]}…\nCONFIANCE: moyenne", 120, 40
    raise RuntimeError(f"LLM_PROVIDER inconnu : {PROVIDER}")


# ───────────────────────────── LOGIQUE MÉTIER ─────────────────────────────


def extraire_confiance(texte: str) -> str:
    for ligne in reversed(texte.strip().splitlines()):
        if ligne.upper().startswith("CONFIANCE"):
            return ligne.split(":", 1)[-1].strip().lower()
    return "inconnue"


def traiter(entree: str) -> tuple[str, str, str]:
    """Retourne (sortie, statut, mesures). Remplacer le corps par la logique du cas."""
    if not entree.strip():
        return "", "⚪ En attente d'une entrée", mesures_md()
    if COMPTEUR.appels >= MAX_CALLS:
        return "", f"⛔ Plafond de {MAX_CALLS} appels atteint pour cette session", mesures_md()

    t0 = time.perf_counter()
    texte, tin, tout = appeler_modele(SYSTEM_PROMPT, entree)
    dt = time.perf_counter() - t0

    COMPTEUR.appels += 1
    COMPTEUR.tokens_entree += tin
    COMPTEUR.tokens_sortie += tout
    COMPTEUR.latences.append(dt)

    confiance = extraire_confiance(texte)
    if confiance in (SEUIL_REVUE_HUMAINE, "inconnue"):
        statut = f"🟠 À revoir par un humain — confiance {confiance}"
    else:
        statut = f"🟢 Traité automatiquement — confiance {confiance}"
    return texte, statut, mesures_md(dt)


def mesures_md(derniere_latence: float | None = None) -> str:
    cout = (
        COMPTEUR.tokens_entree * PRIX_ENTREE_PAR_M + COMPTEUR.tokens_sortie * PRIX_SORTIE_PAR_M
    ) / 1_000_000
    lignes = [
        f"**Appels** : {COMPTEUR.appels}",
        f"**Tokens** : {COMPTEUR.tokens_entree} entrée · {COMPTEUR.tokens_sortie} sortie",
        f"**Latence moyenne** : {COMPTEUR.latence_moyenne:.2f} s"
        + (f" (dernier : {derniere_latence:.2f} s)" if derniere_latence else ""),
        f"**Coût cumulé** : {cout:.4f} € — base : {PRIX_ENTREE_PAR_M}/{PRIX_SORTIE_PAR_M} €/M tokens, "
        f"modèle `{MODEL or '?'}`, fournisseur `{PROVIDER}`",
    ]
    return "\n\n".join(lignes)


# ───────────────────────────── INTERFACE ─────────────────────────────

with gr.Blocks(title=TITRE) as demo:
    gr.Markdown(f"# {TITRE}\n{SOUS_TITRE}")
    with gr.Row():
        with gr.Column(scale=1):
            entree = gr.Textbox(label="Entrée", lines=8, placeholder="Coller l'entrée à traiter…")
            gr.Examples(EXEMPLES, inputs=entree, label="Exemples")
            bouton = gr.Button("Traiter", variant="primary")
        with gr.Column(scale=1):
            statut = gr.Markdown("⚪ En attente d'une entrée")
            sortie = gr.Textbox(label="Sortie", lines=12)
    with gr.Accordion("Mesures de la session", open=True):
        mesures = gr.Markdown(mesures_md())
    gr.Markdown(
        "_Ce qui est réel : chaque clic appelle le modèle. "
        "Ce qui est simulé : <à compléter — rien, ou les données d'exemple>._"
    )

    bouton.click(traiter, inputs=entree, outputs=[sortie, statut, mesures])
    entree.submit(traiter, inputs=entree, outputs=[sortie, statut, mesures])


if __name__ == "__main__":
    share = "--share" in sys.argv
    user, pwd = os.getenv("DEMO_USER"), os.getenv("DEMO_PASSWORD")
    auth = (user, pwd) if user and pwd else None
    demo.launch(share=share, auth=auth)
