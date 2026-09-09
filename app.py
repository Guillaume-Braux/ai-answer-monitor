"""
Lundi — prototype built for the Datawords "AI Use Case: AI Answer Monitor" test.
What AI assistants say about a brand, market by market, and what the brand changes on Monday.

One file. Pipeline: fixed probe bank × markets × repetitions → answer call → judge call (JSON)
→ per-market scores → deterministic Monday-morning action plan → run history (JSONL) for
month-over-month deltas.

Run:
    cp .env.example .env      # fill the key
    pip install -r requirements.txt
    python app.py             # http://127.0.0.1:7860
    python app.py --share     # temporary public URL

Env (.env): MISTRAL_API_KEY (or OPENAI_API_KEY + OPENAI_BASE_URL=https://api.mistral.ai/v1), OPENAI_REAL_API_KEY,
JUDGE (assistant name used as the single judge, default OpenAI), MAX_CALLS, WORKERS, DEMO_USER/DEMO_PASSWORD.
LLM_PROVIDER=mock runs without any key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as esc
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

load_dotenv()

PROBE_BANK_VERSION = "2026-09-09.1"  # bump when a probe wording changes → runs stay comparable
HISTORY_PATH = Path(os.getenv("HISTORY_PATH", "runs/history.jsonl"))

# ───────────────────────────── PROBE BANK (what we probe, frozen) ─────────────────────────────
# Four probes per market, written natively. {brand} / {category} are the only variables.
# P1 unprompted recommendation → presence + rank · P2 opinion → sentiment · P3 product lines →
# factual errors · P4 comparison → sources + rank.

MARKETS: dict[str, dict] = {
    "FR": {"country": "France", "lang": "fr", "lang_name": "français", "probes": {
        "P1_reco": "Je veux acheter {category}. Quelles marques me recommandes-tu ? Donne un classement des 5 meilleures marques, de la meilleure à la moins bonne, avec une phrase par marque.",
        "P2_opinion": "Que penses-tu de la marque {brand} pour {category} ? Points forts, points faibles, et à qui tu la recommanderais.",
        "P3_products": "Quelles sont les gammes et produits de {brand} en {category} ? Liste-les avec leurs caractéristiques principales.",
        "P4_compare": "Entre {brand} et ses concurrents en {category}, lequel choisir et pourquoi ? Précise sur quelles sources (sites, tests, avis) tu t'appuies.",
    }},
    "DE": {"country": "Deutschland", "lang": "de", "lang_name": "Deutsch", "probes": {
        "P1_reco": "Ich möchte {category} kaufen. Welche Marken empfiehlst du? Gib eine Rangliste der 5 besten Marken, von der besten zur schwächsten, mit je einem Satz pro Marke.",
        "P2_opinion": "Was hältst du von der Marke {brand} bei {category}? Stärken, Schwächen, und wem du sie empfehlen würdest.",
        "P3_products": "Welche Produktlinien und Produkte hat {brand} im Bereich {category}? Liste sie mit ihren wichtigsten Merkmalen auf.",
        "P4_compare": "{brand} oder die Konkurrenz bei {category} – was soll ich wählen und warum? Nenne die Quellen (Websites, Tests, Bewertungen), auf die du dich stützt.",
    }},
    "JP": {"country": "日本", "lang": "ja", "lang_name": "日本語", "probes": {
        "P1_reco": "{category}を買いたいです。おすすめのブランドはどれですか？おすすめ順にトップ5のブランドを、各ブランド一文の説明付きでランキングしてください。",
        "P2_opinion": "{category}における{brand}というブランドをどう思いますか？強み、弱み、そしてどんな人におすすめか教えてください。",
        "P3_products": "{brand}の{category}のラインナップと製品にはどんなものがありますか？主な特徴とともに列挙してください。",
        "P4_compare": "{category}で{brand}と競合ブランドのどちらを選ぶべきですか？理由と、参考にした情報源（サイト、レビュー、比較記事）を教えてください。",
    }},
    "ES": {"country": "España", "lang": "es", "lang_name": "español", "probes": {
        "P1_reco": "Quiero comprar {category}. ¿Qué marcas me recomiendas? Dame un ranking de las 5 mejores marcas, de mejor a peor, con una frase por marca.",
        "P2_opinion": "¿Qué opinas de la marca {brand} en {category}? Puntos fuertes, puntos débiles y a quién se la recomendarías.",
        "P3_products": "¿Cuáles son las gamas y productos de {brand} en {category}? Enuméralos con sus características principales.",
        "P4_compare": "Entre {brand} y sus competidores en {category}, ¿cuál elegir y por qué? Indica en qué fuentes (webs, pruebas, opiniones) te basas.",
    }},
    "IT": {"country": "Italia", "lang": "it", "lang_name": "italiano", "probes": {
        "P1_reco": "Voglio comprare {category}. Quali marche mi consigli? Fammi una classifica delle 5 migliori marche, dalla migliore alla peggiore, con una frase per marca.",
        "P2_opinion": "Cosa pensi del marchio {brand} per {category}? Punti di forza, punti deboli e a chi lo consiglieresti.",
        "P3_products": "Quali sono le gamme e i prodotti di {brand} in {category}? Elencali con le loro caratteristiche principali.",
        "P4_compare": "Tra {brand} e i suoi concorrenti in {category}, quale scegliere e perché? Indica su quali fonti (siti, test, recensioni) ti basi.",
    }},
    "US": {"country": "United States", "lang": "en", "lang_name": "English", "probes": {
        "P1_reco": "I want to buy {category}. Which brands do you recommend? Give me a ranking of the 5 best brands, from best to worst, with one sentence per brand.",
        "P2_opinion": "What do you think of the brand {brand} for {category}? Strengths, weaknesses, and who you would recommend it to.",
        "P3_products": "What are {brand}'s product lines and products in {category}? List them with their main features.",
        "P4_compare": "{brand} or its competitors for {category} — which should I choose and why? Say which sources (websites, tests, reviews) you rely on.",
    }},
}
def _market(mk: str) -> dict:
    """Tolère un code marché absent du banc : un historique ancien doit rester lisible."""
    return MARKETS.get(mk) or {"country": mk, "lang": mk.lower(), "lang_name": mk, "probes": {}}


PROBE_LABELS = {"P1_reco": "Recommandation spontanée", "P2_opinion": "Avis sur la marque",
                "P3_products": "Gammes & produits", "P4_compare": "Comparaison + sources"}

ASSISTANT_SYSTEM = (
    "You are a general-purpose AI assistant used by a consumer living in {country}. "
    "Answer in {lang_name}, exactly as you would to any user: helpful, concrete, no disclaimers about being an AI. "
    "At the very end, add a line starting with 'SOURCES:' listing the websites, publications or kinds of sources "
    "your answer relies on (or 'SOURCES: none' if you cannot name any)."
)

JUDGE_SYSTEM = """You are a strict, literal evaluator. You receive an AI assistant's answer about a product category and must extract facts about how a given BRAND is treated. Output ONLY a JSON object with these keys:
- "brand_mentioned": true/false — is BRAND named in the answer (any spelling/case)?
- "brands_listed": ordered list of brand names as they appear (first mentioned first). Empty list if none.
- "brand_rank": 1-based position of BRAND in a ranking/list if the answer ranks or lists brands, else null.
- "sentiment": "positive" | "neutral" | "negative" | "absent" — how the answer portrays BRAND (absent if not mentioned).
- "sources": list of sources the answer says it relies on (from its SOURCES line or the text). Empty if none/unspecified.
- "claims": list of up to 6 concrete factual claims the answer makes ABOUT BRAND (products, lines, prices, origin, features). Each item: {"claim": "...", "status": "consistent" | "contradicted" | "unverifiable"} judged ONLY against the FACT SHEET: consistent = supported by it, contradicted = conflicts with it (e.g. a product line the fact sheet says does not exist), unverifiable = the fact sheet says nothing about it. If the fact sheet gives a CLOSED list (e.g. "any other line does not exist"), any product line, range or product family attributed to BRAND that is not in that list is "contradicted". Prefer listing product/line claims first. Empty list if BRAND is not mentioned.
- "top_competitor": the competitor brand the answer favours most, or null.
Never invent. Answer with JSON only."""

DEFAULT_FACTS = (
    "Marque française de cosmétiques fondée en 1976 à Manosque (Provence). Produit iconique : la crème mains au karité. "
    "LISTE FERMÉE des gammes de crèmes mains : Karité (Shea), Amande, Verveine, Rose, Fleurs de Cerisier, Lavande, Néroli & Orchidée. "
    "Toute autre gamme de crème mains citée (ex. algues, vitamine C, lait de jument, miel, tournesol, argan, huiles précieuses, hydra-végétale) N'EXISTE PAS. "
    "N'a PAS de gamme maquillage. N'a PAS de compléments alimentaires. Formats crème mains : 30 ml et 150 ml. "
    "Ingrédients emblématiques : beurre de karité du Burkina Faso, amande de Provence, immortelle de Corse, lavande de Haute-Provence. "
    "Vend en boutiques en propre, en ligne et en grands magasins."
)
DEFAULT_ALIASES = "L'Occitane en Provence, ロクシタン, L'OCCITANE"

# ───────────────────────────── ASSISTANTS (one adapter each) ─────────────────────────────


@dataclass
class Assistant:
    name: str
    kind: str          # openai-compatible | anthropic | mock
    model: str
    api_key: str = ""
    base_url: str | None = None
    price_in: float = 0.0   # $ per million tokens
    price_out: float = 0.0
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: int = 0

    @property
    def cost(self) -> float:
        return (self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1_000_000


def _build_assistants() -> dict[str, Assistant]:
    out: dict[str, Assistant] = {}
    if os.getenv("LLM_PROVIDER", "").lower() == "mock":
        out["mock"] = Assistant("mock", "mock", "mock")
        return out
    if os.getenv("MISTRAL_API_KEY") or "mistral" in os.getenv("OPENAI_BASE_URL", ""):
        out["Mistral"] = Assistant("Mistral", "openai-compatible", os.getenv("MISTRAL_MODEL", os.getenv("LLM_MODEL", "ministral-8b-latest")),
                                   os.getenv("MISTRAL_API_KEY") or os.getenv("OPENAI_API_KEY", ""), "https://api.mistral.ai/v1",
                                   float(os.getenv("MISTRAL_PRICE_IN", "0.15")), float(os.getenv("MISTRAL_PRICE_OUT", "0.15")))
    if os.getenv("OPENAI_REAL_API_KEY"):
        out["OpenAI"] = Assistant("OpenAI", "openai-compatible", os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
                                  os.getenv("OPENAI_REAL_API_KEY"), None,
                                  float(os.getenv("OPENAI_PRICE_IN", "0.10")), float(os.getenv("OPENAI_PRICE_OUT", "0.60")))
    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_MODEL"):
        out["Anthropic"] = Assistant("Anthropic", "anthropic", os.getenv("ANTHROPIC_MODEL"), os.getenv("ANTHROPIC_API_KEY"), None,
                                     float(os.getenv("ANTHROPIC_PRICE_IN", "0")), float(os.getenv("ANTHROPIC_PRICE_OUT", "0")))
    if not out:
        out["mock"] = Assistant("mock", "mock", "mock")
    return out


ASSISTANTS = _build_assistants()
# One fixed judge for every assistant, so scores stay comparable across assistants and months.
JUDGE = ASSISTANTS.get(os.getenv("JUDGE", "OpenAI")) or next(iter(ASSISTANTS.values()))
MAX_CALLS = int(os.getenv("MAX_CALLS", "600"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "90"))  # un appel qui pend bloque un worker
# Garde-fous d'instance publique : le mot de passe de démo est écrit dans le README, donc tout
# visiteur peut dépenser la clé. On borne la taille d'un run, le coût de la session, la longueur
# des champs libres (la fiche de faits est renvoyée dans CHAQUE appel au juge : c'est le vrai
# amplificateur de facture) et le nombre de sondages simultanés.
MAX_RUN_CALLS = int(os.getenv("MAX_RUN_CALLS", "200"))       # appels d'un seul run
MAX_SESSION_COST = float(os.getenv("MAX_SESSION_COST", "2"))  # $ cumulés avant blocage
MAX_FACTS_CHARS = int(os.getenv("MAX_FACTS_CHARS", "4000"))
MAX_FIELD_CHARS = int(os.getenv("MAX_FIELD_CHARS", "200"))
RUN_SLOT = __import__("threading").Semaphore(1)              # un sondage à la fois par instance
RUNS_PAR_JURE = int(os.getenv("RUNS_PER_USER", "5"))         # quota statique, par identifiant
_RUNS_CONSOMMES: dict[str, int] = {}


def comptes_demo() -> list[tuple[str, str]]:
    """Un identifiant par juré : DEMO_ACCOUNTS="nom:motdepasse,nom2:motdepasse2".
    Chacun a son propre quota, et on sait qui a lancé quoi.
    Repli sur DEMO_USER/DEMO_PASSWORD (compte unique) si la variable est absente."""
    brut = os.getenv("DEMO_ACCOUNTS", "").strip()
    comptes = [(c.split(":", 1)[0].strip(), c.split(":", 1)[1].strip())
               for c in brut.split(",") if ":" in c]
    if comptes:
        return comptes
    u, m = os.getenv("DEMO_USER"), os.getenv("DEMO_PASSWORD")
    return [(u, m)] if u and m else []


def quota_restant(qui: str) -> int:
    return max(0, RUNS_PAR_JURE - _RUNS_CONSOMMES.get(qui, 0))
WORKERS = int(os.getenv("WORKERS", "4"))
LOCK = __import__("threading").Lock()


def total_calls() -> int:
    return sum(a.calls for a in ASSISTANTS.values())


def scrub(err: object) -> str:
    """Message d'erreur affichable. Le SDK recopie le corps de la réponse du fournisseur,
    qui peut contenir la clé ; l'app tourne sur une URL publique — on ne prend pas le risque."""
    return re.sub(r"(sk-|key-|Bearer )[A-Za-z0-9_\-]{6,}", r"\1***", str(err))[:300]


def call_model(a: Assistant, system: str, user: str, json_mode: bool = False, temperature: float = 0.2) -> tuple[str, int, int]:
    """Returns (text, tokens_in, tokens_out). Retries with back-off (free tiers rate-limit)."""
    if total_calls() >= MAX_CALLS:
        raise RuntimeError(f"Plafond de {MAX_CALLS} appels atteint pour cette session")
    if a.kind == "mock":
        time.sleep(0.2)
        if json_mode:
            return json.dumps({"brand_mentioned": True, "brands_listed": ["A", "BRAND", "B"], "brand_rank": 2,
                               "sentiment": "positive", "sources": ["site officiel"],
                               "claims": [{"claim": "vend du maquillage", "status": "contradicted"}],
                               "top_competitor": "A"}), 200, 60
        return f"[MOCK] Réponse simulée : {user[:80]}…\nSOURCES: none", 120, 40
    last = None
    for attempt in range(4):
        try:
            if a.kind == "anthropic":
                from anthropic import Anthropic
                resp = Anthropic(api_key=a.api_key, timeout=HTTP_TIMEOUT, max_retries=0).messages.create(model=a.model, max_tokens=900, temperature=temperature, system=system,
                                                                   messages=[{"role": "user", "content": user}])
                return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text"), resp.usage.input_tokens, resp.usage.output_tokens
            from openai import OpenAI
            # max_retries=0 : le SDK relance 2 fois par défaut, ce qui doublerait les appels
            # facturés sans que nos compteurs le voient. La relance est gérée ci-dessus, visiblement.
            client = OpenAI(api_key=a.api_key, base_url=a.base_url, timeout=HTTP_TIMEOUT, max_retries=0)
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
            try:
                resp = client.chat.completions.create(model=a.model, temperature=temperature, max_tokens=2000, messages=msgs, **kwargs)
            except Exception as e:  # newer OpenAI models reject temperature / max_tokens → minimal call
                if "temperature" in str(e) or "max_tokens" in str(e) or "unsupported" in str(e).lower():
                    resp = client.chat.completions.create(model=a.model, max_completion_tokens=4000, messages=msgs, **kwargs)
                else:
                    raise
            u = resp.usage
            content = resp.choices[0].message.content or ""
            if not content.strip() and getattr(resp.choices[0], "finish_reason", "") == "length":
                # reasoning model spent the budget before answering → one uncapped retry
                resp = client.chat.completions.create(model=a.model, messages=msgs, **kwargs)
                u = resp.usage
                content = resp.choices[0].message.content or ""
            return content, u.prompt_tokens, u.completion_tokens
        except Exception as e:
            last = e
            with LOCK:                     # seul compteur qui était incrémenté hors verrou
                a.errors += 1
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{a.name} : appel échoué après 4 tentatives — {scrub(last)}")


def timed_call(a: Assistant, *args, **k) -> tuple[str, int, int, float]:
    t0 = time.perf_counter()
    text, tin, tout = call_model(a, *args, **k)
    dt = time.perf_counter() - t0
    with LOCK:
        a.calls += 1
        a.tokens_in += tin
        a.tokens_out += tout
        a.latencies.append(dt)
    return text, tin, tout, dt


# ───────────────────────────── PIPELINE ─────────────────────────────


def parse_json(text: str) -> dict:
    """Balaye chaque « { » et garde le premier objet qui décode vraiment. Un regex glouton du
    premier « { » au dernier « } » cassait dès que le juge préfaçait sa réponse d'un exemple :
    le verdict tombait en repli neutre et une erreur factuelle réelle disparaissait en silence."""
    attendus = ("brand_mentioned", "brands_listed", "brand_rank", "sentiment", "sources", "claims", "top_competitor")
    dec, meilleur, score_max = json.JSONDecoder(), None, -1
    for i, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        score = sum(k in obj for k in attendus)
        if score >= score_max:      # >= : à égalité on garde le dernier, le vrai verdict suit l'exemple
            meilleur, score_max = obj, score
    if meilleur is not None:
        return meilleur
    return {"brand_mentioned": None, "brands_listed": [], "brand_rank": None, "sentiment": "absent",
                "sources": [], "claims": [], "top_competitor": None, "_judge_error": text[:200]}


def _norm(t: str) -> str:
    import unicodedata
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[\s'\-–—_.]", "", t.lower())


def _fold(t: str) -> str:
    """Minuscules, apostrophes uniformisées, accents retirés — les espaces sont conservés."""
    import unicodedata
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'")
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


_SEP = r"[\s'\-–—_.]*"   # séparateurs tolérés À L'INTÉRIEUR d'un nom de marque


def brand_in(answer: str, brand: str, aliases: str) -> bool:
    """Présence littérale, tolérante à la typographie (apostrophes courbes, accents, espaces,
    tirets) et aux noms locaux. Les frontières interdisent le faux positif d'une marque courte
    trouvée à l'intérieur d'un mot (« RoC » dans « reproche », « Roche » dans « Yves Rocher ») :
    ce test écrase le verdict du juge, il ne doit gonfler la présence sous aucun prétexte."""
    hay = _fold(answer)
    names = [brand] + [x for x in re.split(r"[,;\n]", aliases or "") if x.strip()]
    for nom in names:
        coeur = re.sub(r"[\s'\-–—_.]", "", _fold(nom))
        if not coeur:
            continue
        motif = _SEP.join(re.escape(c) for c in coeur)
        if re.search(rf"(?<![0-9a-z]){motif}(?![0-9a-z])", hay):
            return True
    return False


def run_probe(brand: str, category: str, market: str, probe_id: str, rep: int, facts: str, competitors: str, assistant: str, aliases: str = "") -> dict:
    m = MARKETS[market]
    a = ASSISTANTS[assistant]
    question = m["probes"][probe_id].format(brand=brand, category=category)
    answer, _, _, dt1 = timed_call(a, ASSISTANT_SYSTEM.format(**m), question, temperature=0.7)
    if not answer.strip():
        # Une réponse vide n'est PAS une absence de la marque : c'est une sonde ratée. La compter
        # comme « absente » abaisserait le chiffre mis en avant, sans que rien ne le signale.
        raise RuntimeError(f"{assistant} : réponse vide sur {market}/{probe_id} — sonde non mesurée")
    judge_user = (f"BRAND: {brand} (also written: {aliases or 'no alias'})\nKNOWN COMPETITORS: {competitors or 'not provided'}\nFACT SHEET (ground truth): {facts or 'not provided'}\n\n"
                  f"ASSISTANT ANSWER ({m['lang_name']}):\n\"\"\"\n{answer}\n\"\"\"")
    verdict_txt, _, _, dt2 = timed_call(JUDGE, JUDGE_SYSTEM, judge_user, json_mode=True, temperature=0.0)
    v = parse_json(verdict_txt)
    # deterministic guard: presence is checked literally, never left to the judge alone
    mentioned = brand_in(answer, brand, aliases) or bool(v.get("brand_mentioned"))
    v["brand_mentioned"] = mentioned
    if not mentioned:
        v["brand_rank"], v["sentiment"], v["claims"] = None, "absent", []
    return {"market": market, "assistant": assistant, "model": a.model, "probe": probe_id, "rep": rep, "question": question, "answer": answer,
            "verdict": v, "latency_answer": round(dt1, 2), "latency_judge": round(dt2, 2)}


SENT_SCORE = {"positive": 1, "neutral": 0, "negative": -1}


def aggregate(results: list[dict]) -> dict[str, dict]:
    """Keys are 'MARKET|Assistant'. Also used per market (pooled) for the action plan."""
    per: dict[str, dict] = {}
    for key in sorted({f'{r["market"]}|{r["assistant"]}' for r in results}):
        mk, asst = key.split("|")
        rs = [r for r in results if r["market"] == mk and r["assistant"] == asst]
        v = [r["verdict"] for r in rs]
        reco = [r["verdict"] for r in rs if r["probe"] == "P1_reco"]
        presence_reco = [bool(x["brand_mentioned"]) for x in reco]
        ranks = [x["brand_rank"] for x in reco if isinstance(x.get("brand_rank"), int)]
        sents = [SENT_SCORE[x["sentiment"]] for x in v if x.get("sentiment") in SENT_SCORE]
        claims = [c for x in v for c in (x.get("claims") or []) if isinstance(c, dict)]
        contradicted = [c["claim"] for c in claims if c.get("status") == "contradicted"]
        unverifiable = [c["claim"] for c in claims if c.get("status") == "unverifiable"]
        sources: dict[str, int] = {}
        for x in v:
            for s in x.get("sources") or []:
                if isinstance(s, str) and s.strip() and s.strip().lower() not in ("none", "aucune", "n/a"):
                    sources[s.strip()] = sources.get(s.strip(), 0) + 1
        comps: dict[str, int] = {}
        for x in v:
            c = x.get("top_competitor")
            if isinstance(c, str) and c.strip():
                comps[c.strip()] = comps.get(c.strip(), 0) + 1
        per[key] = {
            "market": mk, "assistant": asst, "n_answers": len(rs),
            "presence_all": round(sum(bool(x["brand_mentioned"]) for x in v) / max(len(v), 1), 2),
            "presence_reco": round(sum(presence_reco) / max(len(presence_reco), 1), 2) if presence_reco else None,
            "rank_reco": round(statistics.mean(ranks), 1) if ranks else None,
            "rank_spread": (max(ranks) - min(ranks)) if len(ranks) > 1 else 0,
            "stability_reco": round(1 - statistics.pstdev([int(p) for p in presence_reco]), 2) if len(presence_reco) > 1 else None,
            "sentiment": round(statistics.mean(sents), 2) if sents else None,
            "n_claims": len(claims), "contradicted": contradicted, "unverifiable": unverifiable,
            "sources": sorted(sources, key=sources.get, reverse=True)[:5],
            "top_competitors": sorted(comps, key=comps.get, reverse=True)[:3],
        }
    return per


def pool_by_market(results: list[dict]) -> dict[str, dict]:
    pooled = {}
    for mk in sorted({r["market"] for r in results}):
        rs = [dict(r, assistant="all") for r in results if r["market"] == mk]
        pooled[mk] = aggregate(rs)[f"{mk}|all"]
    return pooled


# Sévérités d'une action : bad = à traiter · warn = à surveiller · info = à documenter · ok = rien à faire.
SEV_RANK = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
SEV_WORD = {"bad": "à traiter", "warn": "à surveiller", "info": "à documenter", "ok": "rien à faire"}


def _plan_items(category: str, per_market: dict[str, dict]) -> dict[str, list[tuple[str, str | None, str]]]:
    """Les règles du plan lundi matin, en un seul endroit : (sévérité, titre, suite).

    Déterministe, aucun appel modèle, tous assistants confondus. `action_plan()` (markdown, export)
    et `plan_html()` (écran) ne sont que deux mises en forme de cette même liste — les seuils et les
    branches ci-dessous font seuls foi.
    """
    items: dict[str, list[tuple[str, str | None, str]]] = {}
    for mk, s in per_market.items():
        m = _market(mk)
        acts: list[tuple[str, str | None, str]] = []
        if s["presence_reco"] is not None and s["presence_reco"] < 0.5:
            acts.append(("bad", "Absent des recommandations spontanées",
                         f" ({int(s['presence_reco']*100)} % des réponses) → publier en {m['lang_name']} une page comparative « meilleures marques de {category} » sur le site et chez 2-3 médias/comparateurs locaux ; créer ou compléter la fiche Wikipédia {m['lang']}."))
        elif s["rank_reco"] is not None and s["rank_reco"] > 3:
            acts.append(("warn", "Cité mais mal classé",
                         f" (rang moyen {s['rank_reco']}) → obtenir des tests/avis tiers en {m['lang_name']} ; renforcer la page catégorie locale avec des preuves (labels, chiffres)."))
        for c in list(dict.fromkeys(s["contradicted"]))[:3]:
            acts.append(("bad", "Erreur factuelle",
                         f" : « {c} » → corriger les sources publiques que les modèles lisent : page produit officielle en {m['lang_name']}, Wikidata/Wikipédia, fiches distributeurs ; demander la correction aux revendeurs."))
        if s["unverifiable"]:
            acts.append(("info", f"{len(s['unverifiable'])} affirmation(s) non couverte(s) par la fiche de faits",
                         f" → la marque vérifie et enrichit la fiche (ex. « {s['unverifiable'][0]} »)."))
        if s["sentiment"] is not None and s["sentiment"] < 0:
            acts.append(("warn", "Sentiment négatif",
                         " → identifier les points faibles cités dans les réponses (onglet détails) et y répondre publiquement (FAQ, avis, SAV)."))
        if s["top_competitors"]:
            acts.append(("info", "Concurrent favori des assistants",
                         f" : {', '.join(s['top_competitors'])} → auditer ses sources citées et s'y faire référencer."))
        if s["sources"]:
            acts.append(("info", "Sources déclarées par l'assistant",
                         f" : {', '.join(s['sources'][:4])} → s'assurer d'y être présent et à jour en {m['lang_name']}."))
        if s["rank_spread"] >= 2 or (s["stability_reco"] is not None and s["stability_reco"] < 0.7):
            acts.append(("warn", "Réponses instables entre répétitions",
                         " → ne pas conclure sur une seule sonde ; suivre la tendance sur 3 mois."))
        if not acts:
            acts.append(("ok", None, "Rien d'urgent : présence et faits corrects. Reconduire la sonde le mois prochain."))
        items[mk] = acts
    return items


def action_plan(brand: str, category: str, per_market: dict[str, dict]) -> str:
    """Le plan lundi matin en markdown (format stable, repris dans l'export JSON)."""
    out = [f"## Plan lundi matin — {brand} / {category}\n"]
    for mk, acts in _plan_items(category, per_market).items():
        lines = [f"**{title}**{rest}" if title else rest for _, title, rest in acts]
        out.append(f"### {mk} — {MARKETS[mk]['country']}\n" + "\n".join(f"- {a}" for a in lines) + "\n")
    return "\n".join(out)


# ───────────────────────── PRÉSENTATION (aucun calcul, que de la mise en forme) ─────────────────────────
# Tout ce qui suit ne fait que rendre lisible ce que `aggregate()` et `_plan_items()` ont produit.


def _e(x) -> str:
    """Échappe tout ce qui vient de l'utilisateur ou du modèle avant de l'écrire dans du HTML."""
    return esc("" if x is None else str(x))


def _pct(x) -> str:
    return "—" if x is None else f"{round(x * 100)} %"


def _sentiment_word(x: float | None) -> str:
    if x is None:
        return "—"
    return "positif" if x > 0.33 else ("négatif" if x < -0.33 else "neutre")


def market_status(s: dict) -> tuple[str, str]:
    """Verdict d'un marché en un mot — mêmes seuils que le plan (présence < 50 %, rang > 3)."""
    if s.get("presence_reco") is None:
        return "none", "Non mesurée"
    if s["presence_reco"] < 0.5:
        return "bad", "Absente des recommandations"
    if s.get("rank_reco") is None:
        return "warn", "Citée, jamais classée"
    if s["rank_reco"] > 3:
        return "warn", "Citée, mal classée"
    return "ok", "Recommandée"


def _rank_strip(rank, tone: str) -> str:
    """Les 5 places de la sonde « recommandation », avec celle qu'occupe la marque."""
    pos = int(round(rank)) if isinstance(rank, (int, float)) else None
    slots = "".join(f'<i class="aam-slot{" is-here" if pos == i else ""}">{i}</i>' for i in range(1, 6))
    if pos is None:
        note = "hors classement"
    elif pos > 5:
        note = f"rang moyen {rank} — hors du top 5"
    else:
        note = f"rang moyen {rank} sur 5"
    return f'<div class="aam-strip tone-{tone}">{slots}<span class="aam-strip-note">{_e(note)}</span></div>'


def market_card(mk: str, s: dict, deltas: dict[str, str]) -> str:
    m = _market(mk)
    tone, verdict = market_status(s)
    errs = len(s["contradicted"])
    card_tone = "bad" if errs else tone
    err_txt = f"{errs} <span>sur {s['n_claims']} pesée(s)</span>" if s["n_claims"] else "—"
    bits = [f'<div class="aam-card tone-{card_tone}">',
            f'<div class="aam-card-head"><span class="aam-code">{_e(mk)}</span>'
            f'<span class="aam-place">{_e(m["country"])}<em>{_e(m["lang_name"])}</em></span></div>',
            f'<p class="aam-verdict tone-{tone}">{_e(verdict)}</p>',
            f'<p class="aam-flag">{errs} affirmation(s) contredite(s) par la fiche de faits</p>' if errs else "",
            _rank_strip(s["rank_reco"], tone),
            '<dl class="aam-stats">',
            f'<div><dt>Citée</dt><dd>{_pct(s["presence_all"])} <span>des {s["n_answers"]} réponses</span></dd></div>',
            f'<div><dt>Erreurs factuelles</dt><dd{" class=\"is-bad\"" if errs else ""}>{err_txt}</dd></div>',
            f'<div><dt>Sentiment</dt><dd>{_e(_sentiment_word(s["sentiment"]))}'
            + (f' <span>({s["sentiment"]:+.2f})</span>' if s["sentiment"] is not None else "") + '</dd></div>',
            '</dl>']
    if s["top_competitors"]:
        bits.append(f'<p class="aam-comp">Préféré par les assistants : <b>{_e(", ".join(s["top_competitors"]))}</b></p>')
    d = [f'{key.split("|")[1]} {v}' for key, v in deltas.items() if key.startswith(f"{mk}|")]
    bits.append(f'<p class="aam-delta">{_e(" · ".join(d)) if d else "Premier run sur cette marque — le mois prochain, cette ligne portera l’écart."}</p>')
    bits.append("</div>")
    return "".join(bits)


def score_table(per: dict[str, dict], deltas: dict[str, str]) -> str:
    head = ["Marché", "Assistant", "n", "Citée", "Citée en reco", "Rang moyen",
            "Sentiment", "Erreurs / affirm.", "Stabilité", "vs run précédent"]
    rows = [f"<tr>{''.join(f'<th>{_e(h)}</th>' for h in head)}</tr>"]
    for key, s in per.items():
        tone, _ = market_status(s)
        cells = [f'<td class="c-mk"><span class="aam-dot tone-{tone}"></span>{_e(s["market"])}</td>',
                 f'<td>{_e(s["assistant"])}</td>', f'<td class="num">{s["n_answers"]}</td>',
                 f'<td class="num">{_pct(s["presence_all"])}</td>', f'<td class="num">{_pct(s["presence_reco"])}</td>',
                 f'<td class="num">{"—" if s["rank_reco"] is None else s["rank_reco"]}</td>',
                 f'<td class="num">{"—" if s["sentiment"] is None else f"{s['sentiment']:+.2f}"}</td>',
                 f'<td class="num">{len(s["contradicted"])} / {s["n_claims"]}</td>',
                 f'<td class="num">{_pct(s["stability_reco"])}</td>',
                 f'<td class="c-delta">{_e(deltas.get(key, "premier run"))}</td>']
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f'<table class="aam-table">{"".join(rows)}</table>'


def board_html(brand: str, category: str, n_answers: int, per: dict[str, dict],
               per_market: dict[str, dict], deltas: dict[str, str], reps: int, ts: str) -> str:
    order = {"bad": 0, "warn": 1, "none": 2, "ok": 3}
    mks = sorted(per_market, key=lambda mk: (order[("bad" if per_market[mk]["contradicted"] else market_status(per_market[mk])[0])], mk))
    cards = "".join(market_card(mk, per_market[mk], deltas) for mk in mks)
    n_asst = len({s["assistant"] for s in per.values()})
    return f"""<section class="aam-board">
  <div class="aam-runline">
    <h2>{_e(brand)} <span>·</span> {_e(category)}</h2>
    <p>{n_answers} réponses lues · {len(per_market)} marché(s) · {n_asst} assistant(s) · {reps} répétition(s) par sonde · run du {_e(ts.replace('T', ' à ')[:19])} UTC</p>
  </div>
  <div class="aam-cards">{cards}</div>
  <p class="aam-noise">Un marché = 4 sondes figées, posées dans sa langue. Sur n = {n_answers} réponses, un écart de moins de ~20 points est du bruit : lisez les verdicts, pas les décimales.</p>
  <details class="aam-details">
    <summary>Scores détaillés, marché par assistant — et écart avec le run précédent</summary>
    {score_table(per, deltas)}
    <p class="aam-key">Citée = la marque est nommée dans la réponse, sur les 4 sondes ; citée en reco = sur la seule sonde
    « recommandation spontanée ». Présence vérifiée littéralement en Python (alias compris), pas laissée au juge.
    Rang = position moyenne dans la liste que produit la sonde « recommandation spontanée ».
    Sentiment = moyenne sur [-1 ; 1].
    Erreurs = affirmations contredites par la fiche de faits, sur le nombre d’affirmations que le juge a pesées.
    Stabilité = accord entre répétitions ; « — » avec une seule répétition.</p>
  </details>
</section>"""


def plan_html(brand: str, category: str, per_market: dict[str, dict]) -> str:
    items = _plan_items(category, per_market)
    worst = {mk: min(SEV_RANK[sev] for sev, _, _ in acts) for mk, acts in items.items()}
    blocks = []
    for mk in sorted(items, key=lambda k: (worst[k], k)):
        acts = sorted(items[mk], key=lambda a: SEV_RANK[a[0]])
        lis = []
        for sev, title, rest in acts:
            finding, _, todo = rest.partition(" → ")
            head = f"<b>{_e(title)}</b>{_e(finding)}" if title else _e(finding)
            body = f'<p class="aam-do">{_e(todo)}</p>' if todo else ""
            lis.append(f'<li class="tone-{sev}"><span class="aam-sev">{SEV_WORD[sev]}</span>'
                       f'<p class="aam-find">{head}</p>{body}</li>')
        blocks.append(f'<article class="aam-pm tone-{acts[0][0]}">'
                      f'<h3><span class="aam-code sm">{_e(mk)}</span>{_e(MARKETS[mk]["country"])}</h3>'
                      f'<ul class="aam-acts">{"".join(lis)}</ul></article>')
    return f"""<section class="aam-plan">
  <div class="aam-plan-head">
    <h2>Ce que {_e(brand)} change lundi matin</h2>
    <p>Écrit par règles depuis les scores ci-dessus, sans aucun appel modèle : mêmes chiffres, même plan, à chaque fois.
    Marchés classés du plus urgent au plus calme.</p>
  </div>
  <div class="aam-plan-grid">{"".join(blocks)}</div>
</section>"""


PROBE_SECONDS = 36.0  # 1 appel assistant + 1 appel juge, mesuré le 09/09/2026 : 16 s (Mistral) + 20 s (juge
# OpenAI). Sert tant que la session n'a pas ses propres latences ; ensuite l'estimation les utilise.


def estimate_html(markets: list[str], assistants: list[str], reps) -> str:
    """Ce que le run va coûter, AVANT de cliquer. Recalculé à chaque coche (paliers gratuits obligent)."""
    n_probes = sum(len(MARKETS[mk]["probes"]) for mk in (markets or [])) * int(reps or 1) * len(assistants or [])
    n_calls = n_probes * 2
    if not n_probes:
        return ('<div class="aam-est tone-warn"><p>Cochez au moins un marché et un assistant : '
                "le run n'a rien à sonder.</p></div>")
    lat = [x for a in ASSISTANTS.values() for x in a.latencies]
    per_probe = 2 * statistics.median(lat) if len(lat) >= 4 else PROBE_SECONDS
    secs = n_probes / max(WORKERS, 1) * per_probe
    dur = f"{round(secs)} s" if secs < 90 else f"{round(secs / 60)} min"
    left = MAX_CALLS - total_calls()
    over = n_calls > left
    budget = (f"Il reste {left} appels sur les {MAX_CALLS} de la session : ce run n’y tient pas."
              if over else f"Session : {total_calls()} appels déjà passés, plafond {MAX_CALLS}.")
    return (f'<div class="aam-est{" is-over" if over else ""}"><div class="aam-est-row">'
            f'<div><b>{n_probes}</b><span>sondes</span></div>'
            f'<div><b>{n_calls}</b><span>appels API</span></div>'
            f'<div><b>{dur}</b><span>≈ à {WORKERS} en parallèle</span></div></div>'
            f'<p>Une sonde = 1 appel « assistant » + 1 appel « juge ». {budget}</p></div>')


def running_board(markets: list[str], assistants: list[str], reps) -> str:
    """Ce que l'écran montre pendant le run, à la place du spinner Gradio : le périmètre réellement
    lancé, une carte fantôme par marché, et une barre calée sur la durée *estimée* — dite comme telle,
    car rien ici ne mesure l'avancement réel (le run n'est pas un générateur)."""
    mks = list(markets or [])
    n_probes = sum(len(MARKETS[mk]["probes"]) for mk in mks) * int(reps or 1) * len(assistants or [])
    if not n_probes:
        return empty_board()
    lat = [x for a in ASSISTANTS.values() for x in a.latencies]
    per_probe = 2 * statistics.median(lat) if len(lat) >= 4 else PROBE_SECONDS
    secs = max(n_probes / max(WORKERS, 1) * per_probe, 3)
    dur = f"{round(secs)} s" if secs < 90 else f"{round(secs / 60)} min"
    ghosts = "".join(f'<div class="aam-ghost" style="--d:{i * .15:.2f}s">'
                     f'<span class="aam-code">{_e(mk)}</span>'
                     f'<i class="g1"></i><i class="g2"></i><i class="g3"></i></div>'
                     for i, mk in enumerate(mks))
    return (f'<section class="aam-run" style="--dur:{secs:.0f}s">'
            f'<div class="aam-run-head"><h2>Sondage en cours</h2>'
            f'<p>{n_probes} sondes · {n_probes * 2} appels · {len(mks)} marché(s) · ≈ {_e(dur)}</p></div>'
            f'<div class="aam-bar"><i></i></div>'
            f'<p class="aam-run-note">Chaque sonde part dans la langue du marché, puis la réponse est relue par le juge '
            f'{_e(JUDGE.name)}, qui en extrait des faits en JSON. Le plan lundi matin s’écrit à la fin, par règles. '
            f'La barre suit la durée estimée, pas l’avancement réel.</p>'
            f'<div class="aam-cards">{ghosts}</div></section>')


def notice(title: str, body: str, tone: str = "warn") -> str:
    return f'<div class="aam-notice tone-{tone}"><h2>{_e(title)}</h2><p>{_e(body)}</p></div>'


def empty_board() -> str:
    probes = "".join(f'<li><b>{_e(PROBE_LABELS[pid])}</b>{_e(txt.format(brand="⟨marque⟩", category="⟨catégorie⟩"))}</li>'
                     for pid, txt in MARKETS["FR"]["probes"].items())
    return f"""<div class="aam-empty">
  <h2>Aucun run pour l’instant</h2>
  <p>Le banc pose les mêmes 4 questions dans chaque marché, dans sa langue, puis un juge unique lit chaque
  réponse et en extrait des faits. Vous obtiendrez, par marché : la marque est-elle recommandée, à quel rang,
  ce que l’assistant dit s’appuyer sur, et ce qu’il affirme de faux.</p>
  <ol>
    <li>Vérifiez la marque et sa fiche de faits — c’est elle qui rend une erreur factuelle détectable.</li>
    <li>Choisissez les marchés et les assistants ; l’estimateur donne le nombre d’appels avant de partir.</li>
    <li>Lancez le sondage. Le plan lundi matin s’écrit tout seul, par règles, à partir des scores.</li>
  </ol>
  <h3>Les 4 sondes du banc v{PROBE_BANK_VERSION}, ici en français</h3>
  <ul class="aam-probes">{probes}</ul>
</div>"""


# ───────────────────────────── HISTORY (month after month) ─────────────────────────────


def load_previous(brand: str, category: str) -> dict | None:
    if not HISTORY_PATH.exists():
        return None
    prev = None
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if str(rec.get("brand") or "").lower() == brand.lower() and str(rec.get("category") or "").lower() == category.lower():
            prev = rec
    return prev


def save_run(rec: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def compute_deltas(prev: dict | None, per: dict[str, dict]) -> dict[str, str]:
    if not prev:
        return {}
    out = {}
    for key, s in per.items():
        p = prev.get("scores", {}).get(key)
        if not p:
            out[key] = "nouveau marché/assistant"
            continue
        parts = []
        if s["presence_all"] is not None and p.get("presence_all") is not None:
            d = int((s["presence_all"] - p["presence_all"]) * 100)
            parts.append(f"présence {'+' if d >= 0 else ''}{d} pts")
        pn, sn = p.get("n_answers"), s.get("n_answers")
        if pn is not None and sn is not None and pn != sn:
            # comparer des comptes bruts entre volumes inégaux ferait apparaître des erreurs
            # qui n'existent pas : 1 répétition puis 7, c'est « +6 » sans rien de nouveau.
            parts.append(f"erreurs n/c ({pn} réponses au run précédent, {sn} maintenant)")
        else:
            d2 = len(s["contradicted"]) - len(p.get("contradicted", []))
            parts.append(f"erreurs {'+' if d2 >= 0 else ''}{d2}")
        out[key] = ", ".join(parts) + f" (run du {prev['ts'][:16].replace('T', ' à ')} UTC)"
    return out


# ───────────────────────────── MEASURES BAND ─────────────────────────────


def counters_snapshot() -> dict[str, tuple[int, float]]:
    """Photo des compteurs avant un run, pour pouvoir distinguer « ce run » de « la session »."""
    with LOCK:
        return {a.name: (a.calls, a.cost) for a in ASSISTANTS.values()}


def measures_md(avant: dict[str, tuple[int, float]] | None = None) -> str:
    """Bande de mesures (HTML). Les compteurs sont cumulés depuis le démarrage du processus ;
    `avant` permet d'afficher en plus ce que le run qu'on regarde a coûté à lui seul — sans quoi
    un lecteur attribue au run affiché le coût de tous les runs de la session."""
    rows = ['<tr><th>Assistant</th><th>Modèle</th><th>Rôle</th><th>Appels</th><th>Relances</th>'
            '<th>Tokens in/out</th><th>Latence méd.</th><th>p95</th><th>Coût</th></tr>']
    for a in ASSISTANTS.values():
        lat = sorted(a.latencies)
        med = statistics.median(lat) if lat else 0
        p95 = lat[max(int(len(lat) * 0.95) - 1, 0)] if lat else 0
        role = "assistant + juge" if a is JUDGE else "assistant"
        rows.append(f'<tr><td class="c-name">{_e(a.name)}</td><td class="mono">{_e(a.model)}</td><td>{role}</td>'
                    f'<td class="num">{a.calls}</td><td class="num">{a.errors}</td>'
                    f'<td class="num">{a.tokens_in} / {a.tokens_out}</td>'
                    f'<td class="num">{med:.2f} s</td><td class="num">{p95:.2f} s</td>'
                    f'<td class="num">{a.cost:.4f} $</td></tr>')
    total = sum(a.cost for a in ASSISTANTS.values())
    prices = " · ".join(f"{_e(a.name)} {a.price_in}/{a.price_out} $ par million de tokens" for a in ASSISTANTS.values())
    ligne_run = ""
    if avant is not None:
        n_run = total_calls() - sum(c for c, _ in avant.values())
        cout_run = total - sum(x for _, x in avant.values())
        ligne_run = f'<b>Ce run : {n_run} appels, {cout_run:.4f} $.</b> '
    return (f'<div class="aam-meas"><div class="aam-scroll"><table class="aam-table">{"".join(rows)}</table></div>'
            f'<p class="aam-meas-foot">{ligne_run}'
            f'<b>Cumul depuis le démarrage : {total_calls()} appels, {total:.4f} $</b> (plafond {MAX_CALLS}). '
            f'Juge unique : {_e(JUDGE.name)} <code>{_e(JUDGE.model)}</code> — le même note tous les assistants, '
            f'sinon les scores ne seraient pas comparables. Banc de sondes v{PROBE_BANK_VERSION}, figé.<br>'
            f'Tarifs appliqués : {prices} — tarifs publics lus le 09/09/2026, saisis en variables '
            f"d'environnement pour être revérifiés avant chaque campagne.</p></div>")


# ───────────────────────────── ORCHESTRATION ─────────────────────────────


def monitor(brand: str, category: str, markets: list[str], assistants: list[str], facts: str,
            competitors: str, reps: int, aliases: str = "",
            request: gr.Request | None = None, progress=gr.Progress()):
    """Enveloppe : un quota par juré, puis un seul sondage à la fois. Sans le verrou, N visiteurs
    lancent N pools de WORKERS threads — 429 en rafale et facture multipliée par N."""
    qui = getattr(request, "username", None) or "anonyme"
    if quota_restant(qui) <= 0:
        return (notice(f"Quota atteint pour l\'accès « {qui} »",
                       f"Chaque accès est limité à {RUNS_PAR_JURE} sondages, pour que la démonstration "
                       "reste disponible pour tous et que son coût reste borné. "
                       "Écrivez-moi si vous souhaitez en relancer un.", "bad"),
                "", None, "", measures_md(), None)
    if not RUN_SLOT.acquire(blocking=False):
        return (notice("Un sondage est déjà en cours",
                       "Cette instance de démonstration n'exécute qu'un run à la fois. "
                       "Réessayez dans une à quatre minutes.", "warn"),
                "", None, "", measures_md(), None)
    try:
        return _run_monitor(brand, category, markets, assistants, facts, competitors, reps,
                            aliases, progress=progress, qui=qui)
    finally:
        RUN_SLOT.release()


def _run_monitor(brand: str, category: str, markets: list[str], assistants: list[str], facts: str, competitors: str, reps: int,
                 aliases: str = "", progress=gr.Progress(), qui: str = "anonyme"):
    brand, category = brand.strip(), category.strip()
    if not brand or not category or not markets or not assistants:
        return notice("Il manque une entrée", "Renseignez la marque, la catégorie, au moins un marché et au moins un assistant.",
                      "warn"), "", None, "", measures_md(), None
    reps = max(1, min(int(reps), 7))
    tronques = []

    def borne(valeur: str, limite: int, nom: str) -> str:
        valeur = (valeur or "").strip()
        if len(valeur) > limite:
            tronques.append(f"{nom} ({len(valeur)} → {limite} car.)")
            return valeur[:limite]
        return valeur

    brand = borne(brand, MAX_FIELD_CHARS, "marque")
    category = borne(category, MAX_FIELD_CHARS, "catégorie")
    aliases = borne(aliases, MAX_FIELD_CHARS * 2, "alias")
    competitors = borne(competitors, MAX_FIELD_CHARS * 2, "concurrents")
    facts = borne(facts, MAX_FACTS_CHARS, "fiche de faits")

    markets = [mk for mk in markets if mk in MARKETS]
    assistants = [asst for asst in assistants if asst in ASSISTANTS]
    if not markets or not assistants:
        return notice("Marché ou assistant inconnu", "Sélection invalide.", "warn"), "", None, "", measures_md(), None

    n_appels = len(markets) * len(assistants) * 4 * reps * 2
    if n_appels > MAX_RUN_CALLS:
        return (notice(f"Run trop large pour cette instance ({n_appels} appels)",
                       f"Le plafond par run est de {MAX_RUN_CALLS} appels, pour que la démonstration reste "
                       f"disponible pour tout le monde. Réduisez les marchés, les assistants ou les répétitions.",
                       "warn"), "", None, "", measures_md(), None)
    cout_session = sum(a.cost for a in ASSISTANTS.values())
    if cout_session >= MAX_SESSION_COST or total_calls() + n_appels > MAX_CALLS:
        return (notice("Budget de démonstration épuisé",
                       f"Cette instance a déjà consommé {total_calls()} appels et {cout_session:.2f} $ "
                       f"(plafonds : {MAX_CALLS} appels, {MAX_SESSION_COST:.2f} $). "
                       "Le budget se remet à zéro au redémarrage de l'instance.", "bad"),
                "", None, "", measures_md(), None)

    avant = counters_snapshot()   # pour chiffrer ce run seul, pas toute la session
    _RUNS_CONSOMMES[qui] = _RUNS_CONSOMMES.get(qui, 0) + 1   # décompté seulement si le run part
    jobs = [(mk, pid, r, asst) for asst in assistants for mk in markets for pid in MARKETS[mk]["probes"] for r in range(1, reps + 1)]
    results: list[dict] = []
    progress(0, desc=f"{len(jobs)} sondes × 2 appels…")
    echecs: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(run_probe, brand, category, mk, pid, r, facts, competitors, asst, aliases) for mk, pid, r, asst in jobs]
        for i, fu in enumerate(futs):
            try:
                results.append(fu.result())
            except Exception as e:   # un 429 sur une sonde ne doit pas jeter les réponses déjà payées
                echecs.append(scrub(e))
            progress((i + 1) / len(jobs), desc=f"{i+1}/{len(jobs)} sondes")
    if not results:
        return notice("Run interrompu", f"Aucune sonde n’a abouti sur {len(jobs)}. "
                      f"{echecs[0] if echecs else ''} — la bande de mesures ci-dessous dit où le run s’est arrêté.",
                      "bad"), "", None, "", measures_md(avant), None

    per = aggregate(results)
    prev = load_previous(brand, category)
    deltas = compute_deltas(prev, per)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    per_market = pool_by_market(results)
    rec = {"ts": ts, "brand": brand, "category": category, "markets": markets, "assistants": assistants, "reps": reps,
           "judge": f"{JUDGE.name}/{JUDGE.model}",
           "probe_bank": PROBE_BANK_VERSION, "scores": per, "plan": action_plan(brand, category, per_market),
           "results": results}
    save_run(rec)
    slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-") or hashlib.md5(brand.encode()).hexdigest()[:8]
    export = HISTORY_PATH.parent / f"run_{ts.replace(':', '')}_{slug}.json"   # suit HISTORY_PATH, pas « runs/ » en dur
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")

    head = board_html(brand, category, len(results), per, per_market, deltas, reps, ts)
    head = (f'<p class="aam-quota">Sondage {_RUNS_CONSOMMES.get(qui, 0)} sur {RUNS_PAR_JURE} '
            f'pour l\'accès « {_e(qui)} » — il vous en reste {quota_restant(qui)}.</p>') + head
    if tronques:   # une entrée tronquée change la mesure : le dire plutôt que de la subir
        head = notice("Entrée(s) tronquée(s)", "Champs raccourcis pour borner le coût : "
                      + ", ".join(tronques) + ".", "warn") + head
    illisibles = sum(1 for r in results if r["verdict"].get("_judge_error"))
    if illisibles:   # un verdict non lu efface silencieusement des erreurs factuelles : le dire
        head = notice(f"{illisibles} verdict(s) de juge sur {len(results)} illisibles",
                      "Le juge n'a pas rendu de JSON exploitable pour ces réponses : leurs affirmations et "
                      "leur sentiment sont absents des scores. Seule la présence, vérifiée en Python, reste fiable "
                      "pour elles. Relancer, ou baisser la charge sur le juge.", "warn") + head
    if echecs:   # le score porte sur ce qui a abouti : le dire, ne pas le masquer
        head = notice(f"{len(echecs)} sonde(s) sur {len(jobs)} n’ont pas abouti",
                      f"Les scores portent sur les {len(results)} réponses obtenues. Première erreur : {echecs[0]}",
                      "warn") + head
    table = [[r["market"], r["assistant"], PROBE_LABELS[r["probe"]], r["rep"], "oui" if r["verdict"]["brand_mentioned"] else "non",
              r["verdict"].get("brand_rank") or "", r["verdict"].get("sentiment", ""),
              " · ".join(x.get("claim", "") for x in (r["verdict"].get("claims") or []) if x.get("status") == "contradicted"),
              ", ".join(r["verdict"].get("sources") or [])[:120], r["verdict"].get("top_competitor") or "",
              f"{r['latency_answer']} s"] for r in results]
    raw = "\n\n---\n\n".join(f"### {r['market']} · {r['assistant']} · {PROBE_LABELS[r['probe']]} · rép. {r['rep']}\n**Q :** {r['question']}\n\n{r['answer']}"
                             for r in results)
    return head, plan_html(brand, category, per_market), table, raw, measures_md(avant), str(export)


# ───────────────────────────── UI ─────────────────────────────

TITLE = "Lundi"          # le proto porte le nom de son seul livrable : le plan du lundi matin
CASE = "AI Answer Monitor"   # l'intitulé du cas, tel que Datawords l'a écrit
LEDE = ("Ce que les assistants IA répondent sur une marque, marché par marché — "
        "et ce que la marque change lundi matin.")

DEF_MARKETS = ["FR", "DE", "JP"]
DEF_ASSISTANTS = list(ASSISTANTS)
MODELS_TXT = ", ".join(f"<code>{esc(a.model)}</code>" for a in ASSISTANTS.values())

# Identité visuelle : porcelaine + encre vert-noir, une seule couleur par état (pin / ocre / carmin),
# filets d'1 px et pas d'ombre — un instrument de mesure, pas un tableau de bord décoratif.
CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

:root, .gradio-container{
  --paper:#E7EBEC; --paper-2:#F2F5F6; --panel:#FFFFFF;
  --ink:#121A20; --ink-2:#3C4A54; --ink-3:#6E7D88;
  --rule:#C6D0D5; --rule-2:#E0E7EA; --accent:#0B5C74; --accent-2:#08475A;
  --ok:#0E6B4F; --ok-bg:#E4EFEA; --warn:#8A5A08; --warn-bg:#F6EEDA;
  --bad:#A32536; --bad-bg:#F7E3E6; --none:#8494A0;
  --sans:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,'SFMono-Regular',monospace;
}
/* On repeint le châssis Gradio, on ne touche pas à sa mécanique. */
.gradio-container{
  background:var(--paper)!important; color:var(--ink)!important; font-family:var(--sans)!important;
  max-width:1340px!important; margin:0 auto!important; padding:0 26px 30px!important;
  --body-background-fill:var(--paper); --background-fill-primary:var(--panel);
  --background-fill-secondary:var(--paper); --block-background-fill:var(--panel);
  --border-color-primary:var(--rule); --block-border-color:var(--rule); --panel-border-color:var(--rule);
  --input-background-fill:var(--panel); --input-border-color:var(--rule);
  --body-text-color:var(--ink); --body-text-color-subdued:var(--ink-3);
  --block-label-text-color:var(--ink-2); --block-title-text-color:var(--ink);
  --button-primary-background-fill:var(--accent); --button-primary-background-fill-hover:var(--accent-2);
  --button-primary-text-color:#FFFFFF; --button-primary-border-color:var(--accent);
  --radius-sm:2px; --radius-md:2px; --radius-lg:2px; --radius-xl:3px; --link-text-color:var(--accent);
  --color-accent:var(--accent); --color-accent-soft:#E2EDF1; --slider-color:var(--accent);
  --block-title-text-weight:600; --layout-gap:10px; --spacing-lg:8px; --spacing-xl:10px;
  --checkbox-background-color-selected:var(--accent); --checkbox-border-color-selected:var(--accent);
  --checkbox-background-color-focus:#FFFFFF; --checkbox-border-color-focus:var(--accent);
  --checkbox-label-background-fill-selected:#E2EDF1; --checkbox-label-border-color-selected:var(--accent);
  --checkbox-label-background-fill-hover:#EAEFF1;
}
body{background:var(--paper)!important;}
.gradio-container :focus-visible{outline:2px solid var(--accent); outline-offset:2px;}
.gradio-container label span, .gradio-container .info{font-family:var(--sans)!important;}
.gradio-container button.primary{font-weight:600; letter-spacing:.005em;}
.gradio-container .gap{gap:10px!important;}
@media (prefers-reduced-motion:reduce){.gradio-container *{animation:none!important; transition:none!important;}}

/* ── Formulaire : châssis Gradio mis à plat, seuls les champs gardent un cadre ── */
.aam-form .block{padding:0!important; border:0!important; background:transparent!important; box-shadow:none!important;}
.aam-form .form{gap:9px!important; border:0!important; background:transparent!important; box-shadow:none!important;}
.aam-form .gap{gap:9px!important;}
.aam-form label > span:first-child, .aam-form span[data-testid="block-info"]{
  font-size:12px!important; font-weight:600!important; color:var(--ink)!important; margin-bottom:2px!important;}
.aam-form .info-text{margin:0 0 4px!important; font-size:11px!important;
  line-height:1.45!important; color:var(--ink-3)!important;}
.aam-form textarea, .aam-form input[type="number"]{
  font-size:13px!important; line-height:1.45!important; padding:6px 9px!important;}
/* Deux champs côte à côte : l'entrée est calée en bas de sa cellule, sinon une aide de deux
   lignes décale son champ par rapport à celui d'à côté. */
.aam-form .form > .block > label.container{display:flex; flex-direction:column; height:100%;}
.aam-form .form > .block > label.container .input-container{margin-top:auto;}
.aam-form textarea{font-size:12.5px!important;}
.aam-form .wrap label, .aam-form fieldset label{padding:3px 9px!important; font-size:12px!important;}
.aam-form .wrap{gap:5px!important;}
.aam-form button.primary{width:auto; min-width:250px; padding:11px 30px!important;
  font-size:14px!important; margin:4px 0 0; align-self:flex-start;}

/* Gradio 6 met les <p> en display:inline : deux paragraphes voisins se collent
   (« …maquillage »corriger… »). On rétablit le bloc dans nos propres conteneurs. */
.aam-hero p,.aam-board p,.aam-plan p,.aam-spec p,.aam-empty p,.aam-est p,.aam-meas p,
.aam-notice p,.aam-run p,.aam-bank,.aam-cap{display:block;}

/* ── En-tête ── */
.aam-hero,.aam-board,.aam-plan,.aam-spec,.aam-empty,.aam-notice,.aam-est,.aam-meas,.aam-bank,.aam-run{font-family:var(--sans);}
.aam-hero{border-bottom:2px solid var(--ink); padding:20px 0 12px; margin-bottom:16px;
  display:flex; gap:26px; align-items:flex-end; flex-wrap:wrap;}
.aam-hero h1{margin:0 0 5px; font:600 38px/1 var(--sans); letter-spacing:-.032em;}
.aam-case{margin:7px 0 0; font-size:11.5px; line-height:1.4; color:var(--ink-3);}
.aam-lede{margin:0; max-width:52ch; font-size:14px; line-height:1.45; color:var(--ink-2);}
.aam-plate{display:flex; margin:0 0 2px auto; border-left:1px solid var(--rule);}
.aam-plate div{padding:0 14px; border-right:1px solid var(--rule);}
.aam-plate dt{margin:0 0 3px; font-size:10.5px; color:var(--ink-3);}
.aam-plate dd{margin:0; font:500 12px/1.3 var(--mono); color:var(--ink);}

/* ── Titres de section du formulaire ── */
.aam-fs{margin:15px 0 5px; padding-bottom:5px; border-bottom:1px solid var(--ink);
  font:600 13.5px/1.2 var(--sans); letter-spacing:-.01em;}
.aam-fs:first-child{margin-top:0;}
.aam-fs em{display:block; margin-top:3px; font:400 11px/1.4 var(--sans); font-style:normal; color:var(--ink-3);}
.gradio-container .aam-fs em{font-weight:400!important; font-style:normal; color:var(--ink-3);}

/* ── Estimateur ── */
.aam-est{border:1px solid var(--rule); background:var(--panel); padding:11px 13px; margin:4px 0 2px;}
.aam-est.is-over, .aam-est.tone-warn{border-color:var(--bad); background:var(--bad-bg);}
.aam-est-row{display:flex; gap:18px;}
.aam-est-row div{display:flex; flex-direction:column;}
.aam-est b{font:600 22px/1 var(--sans); letter-spacing:-.02em; font-variant-numeric:tabular-nums;}
.aam-est span{margin-top:3px; font-size:10.5px; color:var(--ink-3);}
.aam-est p{margin:9px 0 0; font-size:11px; line-height:1.45; color:var(--ink-2);}
.aam-bank{margin:9px 0 0; font-size:11px; line-height:1.5; color:var(--ink-3);}
.aam-bank code{font-family:var(--mono); font-size:10.5px;}

/* ── Sondage en cours ── */
.aam-run{border:1px solid var(--rule); background:var(--panel); padding:17px 19px 19px;}
.aam-run-head{display:flex; justify-content:space-between; align-items:baseline; gap:14px; flex-wrap:wrap;}
.aam-run-head h2{margin:0; font:600 19px/1.2 var(--sans); letter-spacing:-.018em;}
.aam-run-head p{margin:0; font:400 12px/1.4 var(--mono); color:var(--ink-3); font-variant-numeric:tabular-nums;}
.aam-bar{position:relative; height:3px; margin:13px 0 10px; background:var(--rule-2); overflow:hidden;}
.aam-bar i{position:absolute; inset:0 auto 0 0; width:0; background:var(--accent);
  animation:aam-fill var(--dur,60s) cubic-bezier(.15,.75,.3,1) forwards;}
.aam-bar::after{content:""; position:absolute; inset:0; background:linear-gradient(90deg,transparent,rgba(11,92,116,.28),transparent);
  animation:aam-sweep 1.5s linear infinite;}
@keyframes aam-fill{from{width:2%} to{width:97%}}
@keyframes aam-sweep{from{transform:translateX(-100%)} to{transform:translateX(100%)}}
.aam-run-note{margin:0 0 15px; max-width:78ch; font-size:12px; line-height:1.5; color:var(--ink-2);}
.aam-ghost{background:var(--panel); border:1px solid var(--rule); border-left:5px solid var(--rule);
  padding:13px 14px; animation:aam-breathe 1.8s ease-in-out infinite; animation-delay:var(--d,0s);}
.aam-ghost .aam-code{color:var(--ink-3);}
.aam-ghost i{display:block; height:9px; margin-top:11px; background:var(--rule-2);}
.aam-ghost i.g1{width:62%; height:15px;} .aam-ghost i.g2{width:88%;} .aam-ghost i.g3{width:44%;}
@keyframes aam-breathe{0%,100%{opacity:.45} 50%{opacity:1}}
@media (prefers-reduced-motion:reduce){
  .aam-bar i{width:97%;} .aam-bar::after{display:none;} .aam-ghost{opacity:.7;}
}

/* ── Verdict par marché ── */
.aam-runline{display:flex; justify-content:space-between; align-items:baseline; gap:14px; flex-wrap:wrap;
  padding-bottom:8px; border-bottom:1px solid var(--rule); margin-bottom:12px;}
.aam-runline h2{margin:0; font:600 19px/1.2 var(--sans); letter-spacing:-.018em;}
.aam-runline h2 span{color:var(--ink-3); font-weight:400;}
.aam-runline p{margin:0; font:400 11.5px/1.4 var(--mono); color:var(--ink-3);}
.aam-cards{display:grid; grid-template-columns:repeat(auto-fit,minmax(212px,1fr)); gap:10px;}
.aam-card{background:var(--panel); border:1px solid var(--rule); border-left:5px solid var(--none);
  padding:11px 13px 11px;}
.aam-card.tone-ok{border-left-color:var(--ok);}
.aam-card.tone-warn{border-left-color:var(--warn);}
.aam-card.tone-bad{border-left-color:var(--bad);}
.aam-card-head{display:flex; align-items:center; gap:9px; margin-bottom:8px;}
.aam-code{font:700 20px/1 var(--sans); letter-spacing:.015em;}
.aam-code.sm{font-size:14px; margin-right:7px;}
.aam-place{font-size:11.5px; line-height:1.25; color:var(--ink-2);}
.aam-place em{display:block; font-style:normal; font-size:11px; color:var(--ink-3);}
.aam-verdict{margin:0 0 8px; font:600 16.5px/1.2 var(--sans); letter-spacing:-.018em; color:var(--none);}
.aam-verdict.tone-ok{color:var(--ok);} .aam-verdict.tone-warn{color:var(--warn);} .aam-verdict.tone-bad{color:var(--bad);}
.aam-flag{margin:-4px 0 8px; padding:3px 6px; background:var(--bad-bg); font-size:11px; line-height:1.35;
  color:var(--bad); font-weight:500;}
.aam-stats dd.is-bad{color:var(--bad);}
.aam-strip{display:flex; align-items:center; gap:3px; margin-bottom:9px; flex-wrap:wrap;}
.aam-slot{width:19px; height:19px; display:flex; align-items:center; justify-content:center;
  border:1px solid var(--rule); background:var(--paper-2); font:500 10px/1 var(--mono); font-style:normal; color:var(--ink-3);}
.aam-slot.is-here{background:var(--ink); border-color:var(--ink); color:#fff;}
.aam-strip.tone-ok .aam-slot.is-here{background:var(--ok); border-color:var(--ok);}
.aam-strip.tone-warn .aam-slot.is-here{background:var(--warn); border-color:var(--warn);}
.aam-strip.tone-bad .aam-slot.is-here{background:var(--bad); border-color:var(--bad);}
.aam-strip-note{margin-left:6px; font-size:10.5px; color:var(--ink-3);}
.aam-stats{margin:0; padding-top:8px; border-top:1px solid var(--rule-2); display:grid; gap:4px;}
.aam-stats div{display:flex; justify-content:space-between; align-items:baseline; gap:10px;}
.aam-stats dt{margin:0; font-size:11.5px; color:var(--ink-3);}
.aam-stats dd{margin:0; text-align:right; font:500 12.5px/1.3 var(--sans); font-variant-numeric:tabular-nums;}
.aam-stats dd span{font-weight:400; font-size:10.5px; color:var(--ink-3);}
.aam-comp{margin:8px 0 0; font-size:11.5px; line-height:1.4; color:var(--ink-2);}
.aam-delta{margin:7px 0 0; padding-top:7px; border-top:1px dotted var(--rule); font-size:10.5px; line-height:1.4; color:var(--ink-3);}
.aam-noise{margin:10px 0 0; max-width:86ch; font-size:12px; line-height:1.5; color:var(--ink-2);}

/* ── Preuve : scores détaillés ── */
.aam-details{margin-top:9px; border-top:1px solid var(--rule);}
.aam-details summary{padding:8px 0; cursor:pointer; font-size:12.5px; font-weight:600; color:var(--accent);}
.aam-details summary:hover{text-decoration:underline;}
.aam-table{width:100%; border-collapse:collapse; font-size:12px; background:var(--panel);}
.aam-table th{padding:6px 8px; text-align:left; font:600 10.5px/1.25 var(--sans); color:var(--ink-2);
  border-bottom:1.5px solid var(--ink); vertical-align:bottom; white-space:normal; hyphens:auto;}
.aam-table td{padding:5px 8px; border-bottom:1px solid var(--rule-2); vertical-align:top;}
.aam-table tbody tr:nth-child(even) td{background:var(--paper-2);}
.aam-table td.num{font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap;}
.aam-table td.c-mk{font-weight:600; white-space:nowrap;}
.aam-table td.c-name{font-weight:600;}
.aam-table td.c-delta{font-size:11px; color:var(--ink-3);}
.aam-dot{display:inline-block; width:7px; height:7px; margin-right:6px; border-radius:50%; background:var(--none);}
.aam-dot.tone-ok{background:var(--ok);} .aam-dot.tone-warn{background:var(--warn);} .aam-dot.tone-bad{background:var(--bad);}
.aam-key{margin:7px 0 3px; max-width:104ch; font-size:11px; line-height:1.55; color:var(--ink-3);}
.aam-scroll{overflow-x:auto;}

/* ── Plan lundi matin ── */
.aam-plan{margin-top:20px; padding-top:14px; border-top:2px solid var(--ink);}
.aam-plan-head{display:flex; gap:24px; align-items:baseline; flex-wrap:wrap; margin-bottom:12px;}
.aam-plan-head h2{margin:0; font:600 22px/1.15 var(--sans); letter-spacing:-.023em;}
.aam-plan-head p{margin:0; flex:1 1 320px; max-width:70ch; font-size:12px; line-height:1.5; color:var(--ink-2);}
.aam-plan-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); gap:11px; align-items:start;}
.aam-pm{background:var(--panel); border:1px solid var(--rule); border-top:3px solid var(--none); padding:12px 14px 10px;}
.aam-pm.tone-bad{border-top-color:var(--bad);} .aam-pm.tone-warn{border-top-color:var(--warn);}
.aam-pm.tone-info{border-top-color:var(--ink-3);} .aam-pm.tone-ok{border-top-color:var(--ok);}
.aam-pm h3{display:flex; align-items:center; margin:0 0 9px; font:600 14px/1.2 var(--sans);}
.aam-acts{list-style:none; margin:0; padding:0;}
.aam-acts li{margin-bottom:9px; padding:0 0 9px 10px; border-left:3px solid var(--rule);
  border-bottom:1px solid var(--rule-2);}
.aam-acts li:last-child{margin-bottom:0; padding-bottom:0; border-bottom:none;}
.aam-acts li.tone-bad{border-left-color:var(--bad);} .aam-acts li.tone-warn{border-left-color:var(--warn);}
.aam-acts li.tone-info{border-left-color:var(--rule);} .aam-acts li.tone-ok{border-left-color:var(--ok);}
.aam-sev{display:block; margin-bottom:3px; font-size:10px; color:var(--ink-3);}
li.tone-bad .aam-sev{color:var(--bad); font-weight:600;} li.tone-warn .aam-sev{color:var(--warn); font-weight:600;}
li.tone-ok .aam-sev{color:var(--ok); font-weight:600;}
.aam-find{margin:0 0 3px; font-size:12.5px; line-height:1.4;}
.aam-do{margin:0; font-size:11.5px; line-height:1.5; color:var(--ink-2);}

/* ── Mesures, états, spécifications ── */
.aam-meas-foot{margin:8px 0 0; font-size:11px; line-height:1.6; color:var(--ink-3);}
.aam-meas-foot b{color:var(--ink);}
.aam-meas code{padding:1px 4px; background:var(--paper); font-family:var(--mono); font-size:10.5px;}
.aam-empty{background:var(--panel); border:1px solid var(--rule); border-left:5px solid var(--ink-3); padding:18px 20px;}
.aam-empty h2{margin:0 0 6px; font:600 17px/1.25 var(--sans); letter-spacing:-.016em;}
.aam-empty p{margin:0 0 10px; max-width:66ch; font-size:13px; line-height:1.5; color:var(--ink-2);}
.aam-empty ol{margin:0; padding-left:16px; font-size:12.5px; line-height:1.65; color:var(--ink-2);}
.aam-notice{border:1px solid var(--warn); background:var(--warn-bg); padding:12px 14px; margin-bottom:10px;}
.aam-notice.tone-bad{border-color:var(--bad); background:var(--bad-bg);}
.aam-notice h2{margin:0 0 4px; font:600 14px/1.2 var(--sans);}
.aam-notice p{margin:0; font-size:12.5px; line-height:1.45;}
.aam-spec{margin-top:20px; padding-top:14px; border-top:1px solid var(--rule);
  display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:24px;}
.aam-spec h2{margin:0 0 8px; font:600 13px/1.2 var(--sans);}
.aam-spec ul{margin:0; padding-left:15px;}
.aam-spec li{margin-bottom:6px; font-size:11.5px; line-height:1.55; color:var(--ink-2);}
.aam-spec li b{color:var(--ink); font-weight:600;}
.aam-spec code{font-family:var(--mono); font-size:11px;}

/* ── Onglets de preuve (détail, réponses brutes, mesures) ── */
.aam-tabs{margin-top:16px;}
.aam-tabs .tab-nav, .aam-tabs .tab-container{border-bottom:1px solid var(--rule)!important; gap:0!important;}
.aam-tabs .tab-nav button{padding:8px 14px!important; font-size:12.5px!important; font-weight:500!important;
  color:var(--ink-3)!important; border:0!important; background:transparent!important; border-radius:0!important;}
.aam-tabs .tab-nav button.selected{color:var(--ink)!important; font-weight:600!important;
  box-shadow:inset 0 -2px 0 var(--accent)!important;}
.aam-tabs .tabitem{padding:12px 0 0!important; border:0!important; background:transparent!important;}

/* ── Tableau « détail par sonde » (Gradio Dataframe) : en-têtes lisibles, jamais rognés ── */
/* Gradio coupe les mots au milieu dans les en-têtes (« Sentime/nt », « Marc/hé ») : un en-tête
   se replie entre les mots ou pas du tout, jamais à l'intérieur d'un mot. */
.aam-tabs table th, .aam-tabs table th *, #aam-detail table th, #aam-detail table th *{
  word-break:normal!important; overflow-wrap:normal!important; hyphens:none!important;
  white-space:normal!important;}
.aam-tabs table th{padding:6px 7px!important; vertical-align:bottom!important; height:auto!important;
  font:600 10.5px/1.25 var(--sans)!important; color:var(--ink-2)!important; text-align:left!important;}
#aam-detail table th{white-space:normal!important; vertical-align:bottom!important; height:auto!important;
  padding:6px 7px!important; font:600 10.5px/1.25 var(--sans)!important; color:var(--ink-2)!important;}
#aam-detail table th *{white-space:normal!important; overflow:visible!important; text-overflow:clip!important;
  max-height:none!important; text-align:left!important;}
#aam-detail table td{padding:5px 7px!important; font-size:11.5px!important; line-height:1.35!important;
  vertical-align:top!important;}
#aam-detail table td *{white-space:normal!important; overflow-wrap:anywhere;}
#aam-detail .cell-wrap{white-space:normal!important; overflow:visible!important;}
#aam-detail .block, #aam-detail{border-color:var(--rule)!important;}
#aam-raw{max-height:400px; overflow-y:auto; border:1px solid var(--rule); background:var(--panel); padding:2px 14px;}
#aam-raw h3{margin-top:14px!important; font-size:12.5px!important; font-weight:600!important; color:var(--ink-3)!important;}
#aam-raw p, #aam-raw li{font-size:12.5px!important; line-height:1.5!important;}
.gradio-container [id^="aam-export"] .block, #aam-export{max-height:104px;}

/* ── Aperçu des sondes (état vide) et légendes d'onglet ── */
.aam-empty h3{margin:15px 0 7px; padding-top:12px; border-top:1px solid var(--rule-2);
  font:600 11.5px/1.2 var(--sans); color:var(--ink-3);}
.aam-probes{list-style:none; margin:0; padding:0; display:grid; gap:7px;}
.aam-probes li{padding-left:10px; border-left:2px solid var(--rule); font-size:11.5px; line-height:1.45; color:var(--ink-3);}
.aam-probes b{display:block; margin-bottom:1px; font-size:11px; font-weight:600; color:var(--ink-2);}
.aam-cap{margin:0 0 8px; font-size:11.5px; line-height:1.45; color:var(--ink-3);}

@media (max-width:880px){
  .gradio-container{padding:0 14px 26px!important; max-width:100%!important; width:100%!important;}
  .aam-row{flex-wrap:wrap!important; flex-direction:column!important;}
  .aam-col{flex:1 1 auto!important; min-width:0!important; width:100%!important;}
  .gradio-container .block, .gradio-container .form{min-width:0!important;}
  .aam-hero{padding-top:18px;} .aam-hero h1{font-size:25px;}
  .aam-plate{margin:10px 0 0; flex-wrap:wrap; border-left:none;}
  .aam-plate div{padding:2px 14px 2px 0; border-right:none; border-left:1px solid var(--rule); padding-left:12px;}
  .aam-plate div:first-child{border-left:none; padding-left:0;}
  .aam-plan-head h2{font-size:19px;}
  .aam-cards, .aam-plan-grid{grid-template-columns:1fr;}
}
.aam-details{overflow-x:auto;}
"""

THEME = gr.themes.Base(
    primary_hue="emerald", secondary_hue="stone", neutral_hue="gray",
    font=[gr.themes.GoogleFont("IBM Plex Sans"), "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("IBM Plex Mono"), "ui-monospace", "monospace"],
)

HEADER = f"""<header class="aam-hero">
  <div>
    <h1>{TITLE}</h1>
    <p class="aam-lede">{LEDE}</p>
    <p class="aam-case">Prototype du cas « {CASE} », écrit pour Datawords le 9 septembre 2026.</p>
  </div>
  <dl class="aam-plate">
    <div><dt>Banc de sondes</dt><dd>v{PROBE_BANK_VERSION} · figé</dd></div>
    <div><dt>Par marché</dt><dd>4 sondes natives</dd></div>
    <div><dt>Juge unique</dt><dd>{esc(JUDGE.name)}</dd></div>
  </dl>
</header>"""

SPEC = f"""<section class="aam-spec">
  <div>
    <h2>Ce qui est réellement mesuré</h2>
    <ul>
      <li><b>Chaque chiffre vient d’un texte.</b> Une sonde = un appel au modèle, puis un second appel où un juge
      unique en extrait des faits en JSON. Les deux textes sont sous « détail par sonde » et « réponses brutes ».</li>
      <li><b>La présence n’est pas laissée au modèle</b> : elle est vérifiée littéralement en Python, alias et
      graphies locales compris, et écrase le verdict du juge.</li>
      <li><b>Le plan lundi matin ne passe par aucun modèle</b> : des règles lisibles, appliquées aux scores.
      Mêmes chiffres, même plan.</li>
      <li><b>Le banc est figé et versionné</b> (<code>v{PROBE_BANK_VERSION}</code>) : c’est ce qui rend deux runs
      comparables d’un mois à l’autre. Changer une sonde impose de changer la version.</li>
    </ul>
  </div>
  <div>
    <h2>Limites assumées — à lire avant de citer un chiffre</h2>
    <ul>
      <li><b>API, pas produit grand public.</b> On interroge l’API d’un modèle, pas ChatGPT ou Perplexity avec
      navigation web : ce que voit un consommateur peut différer.</li>
      <li><b>Modèles petits et bon marché</b> ({MODELS_TXT}), choisis pour le budget d’un après-midi — pas des
      modèles phares.</li>
      <li><b>Les sources sont déclarées par le modèle, pas vérifiées.</b> Sans navigation, il cite ce qu’il croit
      être ses sources ; c’est un signal d’influence, pas une bibliographie.</li>
      <li><b>Le juge est un modèle</b>, calé sur 6 exemples seulement (<code>eval_judge.py</code>), et il note aussi
      ses propres réponses en tant qu’assistant. Biais assumé, pas caché.</li>
      <li><b>Petit n.</b> 3 marchés × 4 sondes × 2 assistants = 24 réponses : un écart de moins de ~20 points est
      du bruit. Pour un suivi mensuel, monter les répétitions.</li>
    </ul>
  </div>
</section>"""

with gr.Blocks(title=f"{TITLE} ({CASE})", fill_width=True) as demo:
    gr.HTML(HEADER, padding=False, container=False)
    with gr.Row(equal_height=False, elem_classes="aam-row"):
        with gr.Column(scale=4, min_width=330, elem_classes="aam-col aam-form"):
            gr.HTML('<h2 class="aam-fs">La marque<em>Ce qu’on cherche dans les réponses.</em></h2>',
                    padding=False, container=False)
            with gr.Row():
                brand = gr.Textbox(label="Marque", value="L'Occitane",
                                   info="Le nom tel qu’un client l’écrirait.")
                category = gr.Textbox(label="Catégorie de produit", value="crème pour les mains",
                                      info="Insérée telle quelle dans les sondes, qui restent dans la langue du marché.")
            aliases = gr.Textbox(label="Autres graphies et noms locaux", value=DEFAULT_ALIASES,
                                 info="Séparés par des virgules. Sans « ロクシタン », un run japonais conclurait « absente » à tort.")

            gr.HTML('<h2 class="aam-fs">Vérité terrain<em>Sans référentiel, une erreur factuelle n’existe pas.</em></h2>',
                    padding=False, container=False)
            facts = gr.Textbox(label="Fiche de faits de la marque", lines=4, max_lines=12, value=DEFAULT_FACTS,
                               info="Seul référentiel des erreurs. Hors de son périmètre, une affirmation est « non vérifiable », jamais fausse. Une liste fermée (« toute autre gamme n’existe pas ») rend les inventions détectables.")
            competitors = gr.Textbox(label="Concurrents connus (optionnel)",
                                     value="Nivea, Neutrogena, Rituals, Yves Rocher, Shiseido",
                                     info="Aide le juge à reconnaître un concurrent cité dans une réponse.")

        # Le périmètre du run vit au-dessus du tableau de bord, pas au bas d'un formulaire :
        # la colonne de droite n'est plus vide avant le premier run, et les deux colonnes se
        # terminent à peu près à la même hauteur.
        with gr.Column(scale=7, elem_classes="aam-col aam-form"):
            gr.HTML('<h2 class="aam-fs">Périmètre du run<em>Ce que ça coûte est calculé avant le clic.</em></h2>',
                    padding=False, container=False)
            with gr.Row(equal_height=False):
                with gr.Column(min_width=240):
                    markets = gr.CheckboxGroup(choices=list(MARKETS), value=DEF_MARKETS, label="Marchés",
                                               info="Un marché = un pays et sa langue. Les 4 sondes y sont écrites nativement, jamais traduites.")
                    assistants = gr.CheckboxGroup(choices=list(ASSISTANTS), value=DEF_ASSISTANTS, label="Assistants sondés",
                                                  info="Découverts au démarrage : une clé d’API présente = un assistant de plus.")
                with gr.Column(min_width=240):
                    reps = gr.Slider(1, 7, value=1, step=1, label="Répétitions par sonde",
                                     info="1 = démonstration rapide. ≥ 7 pour un suivi mensuel : deux exécutions simultanées du même prompt ne partagent que 46–48 % des marques citées et 32–43 % des sources (Schulte et al., Univ. de Saint-Gall, avr. 2026, arXiv 2604.07585).")
                    est = gr.HTML(estimate_html(DEF_MARKETS, DEF_ASSISTANTS, 1), padding=False, container=False)
            btn = gr.Button("Lancer le sondage", variant="primary", size="lg")
            gr.HTML(f'<p class="aam-bank">Banc de sondes <code>v{PROBE_BANK_VERSION}</code> : 4 questions par marché '
                    "— recommandation spontanée, avis, gammes, comparaison — figées et versionnées. C’est ce qui rend "
                    "la mesure comparable d’un mois à l’autre.</p>", padding=False, container=False)
            board = gr.HTML(empty_board(), padding=False, container=False)
    plan = gr.HTML(padding=False, container=False)
    # Onglets plutôt que trois accordéons empilés : même traçabilité (chaque chiffre reste à un clic
    # du texte dont il vient), une seule barre de titres au lieu de trois, et pas de vide entre elles.
    with gr.Tabs(elem_classes="aam-tabs"):
        with gr.Tab("Détail par sonde"):
            gr.HTML('<p class="aam-cap">Une ligne par réponse lue — c’est d’ici que sortent tous les chiffres ci-dessus.</p>',
                    padding=False, container=False)
            detail = gr.Dataframe(headers=["Marché", "Assistant", "Sonde", "Rép.", "Citée", "Rang", "Sentiment",
                                           "Contredit par la fiche", "Sources citées", "Concurrent", "Latence"],
                                  column_widths=["7%", "9%", "11%", "6%", "6%", "6%", "8%", "16%", "14%", "11%", "6%"],
                                  wrap=True, max_height=430, elem_id="aam-detail")
        with gr.Tab("Réponses brutes"):
            gr.HTML('<p class="aam-cap">Le texte intégral renvoyé par chaque assistant, question comprise.</p>',
                    padding=False, container=False)
            raw = gr.Markdown(elem_id="aam-raw")
        with gr.Tab("Mesures de la session"):
            meas = gr.HTML(measures_md(), padding=False, container=False)
        with gr.Tab("Export du run"):
            export = gr.File(label="JSON du run — scores, plan et réponses ; même format que runs/history.jsonl",
                             height=100, elem_id="aam-export")
    gr.HTML(SPEC, padding=False, container=False)

    for c in (markets, assistants, reps):
        c.change(estimate_html, [markets, assistants, reps], est, show_progress="hidden")

    # Le spinner Gradio est coupé (show_progress) : l’attente est rendue par running_board(), qui dit
    # ce qui tourne. Premier maillon = instantané, il pose l’écran d’attente et vide le run précédent.
    run = btn.click(lambda mk, asst, r: (gr.update(interactive=False, value="Sondage en cours…"),
                                         running_board(mk, asst, r), "", None, "", None),
                    [markets, assistants, reps], [btn, board, plan, detail, raw, export],
                    show_progress="hidden")
    run = run.then(monitor, inputs=[brand, category, markets, assistants, facts, competitors, reps, aliases],
                   outputs=[board, plan, detail, raw, meas, export], show_progress="hidden")
    run.then(lambda: gr.update(interactive=True, value="Lancer le sondage"), None, btn, show_progress="hidden")


if __name__ == "__main__":
    share = "--share" in sys.argv
    comptes = comptes_demo()          # un couple par juré ; Gradio accepte une liste
    auth = comptes or None
    # La page de login est rendue par Gradio, hors de notre CSS : sans ce message, le visiteur
    # arrive sur un formulaire nu qui ne dit ni ce qu'est l'outil ni comment entrer.
    # Gradio 6 affiche ce message tel quel, sans rendu markdown : pas d'astérisques ni de
    # backticks, sinon le client lit « **AI Answer Monitor** » sur le premier écran de la démo.
    auth_message = (f"{TITLE}, le prototype du cas « {CASE} » : ce que les assistants IA répondent "
                    "sur une marque, marché par marché, et ce qu'il faut changer lundi matin.\n\n"
                    "Démo Datawords du 9 septembre 2026 — accès nominatif, "
                    f"{RUNS_PAR_JURE} sondages par identifiant. Vos identifiants sont dans l\'email de rendu.")
    # Gradio 6 : le thème et le CSS se passent à launch(), plus au constructeur Blocks.
    demo.launch(share=share, auth=auth, auth_message=auth_message if auth else None,
                theme=THEME, css=CSS,
                server_name="0.0.0.0" if os.getenv("SPACE_ID") else None)
