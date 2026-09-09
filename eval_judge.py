"""
Minimal evaluation harness for the judge (the second model call that turns an answer into scores).

Six hand-written assistant answers with known expected labels. Run:
    python eval_judge.py
Prints per-field agreement with n. The literal presence check is deterministic and not scored here.
n = 6 → a single miss is ±17 points: this is a smoke test, not a benchmark. Extend fixtures before concluding.
"""

from __future__ import annotations

import json

import app

if app.JUDGE.kind == "mock":
    print("⚠️  JUDGE is the mock assistant: the scores below measure nothing at all.\n"
          "   Set a real key (OPENAI_REAL_API_KEY / MISTRAL_API_KEY) to evaluate the judge.\n")

FACTS = app.DEFAULT_FACTS
BRAND = "L'Occitane"

FIXTURES = [
    {  # FR, ranked list, brand 2nd, positive
        "answer": "Voici mon classement : 1. Nivea — abordable et efficace. 2. L'Occitane — sa crème mains au karité est une référence. "
                  "3. Neutrogena — formule norvégienne. 4. Rituals — parfums agréables. 5. Yves Rocher — bon rapport qualité-prix.\nSOURCES: 60 Millions de consommateurs, avis Sephora",
        "expected": {"brand_rank": 2, "sentiment": "positive", "n_contradicted": 0, "has_sources": True},
    },
    {  # DE, ranked list, brand absent
        "answer": "Meine Rangliste: 1. Nivea 2. Eucerin 3. Weleda 4. Kneipp 5. Balea. Alle sind in deutschen Drogerien erhältlich.\nSOURCES: Stiftung Warentest",
        "expected": {"brand_rank": None, "sentiment": "absent", "n_contradicted": 0, "has_sources": True},
    },
    {  # EN, contradicted claim (makeup line does not exist)
        "answer": "L'Occitane is a French brand from Provence. Its product lines include shea butter body care, immortelle face care, "
                  "and a popular makeup range with foundations and lipsticks. Prices are premium.\nSOURCES: none",
        "expected": {"brand_rank": None, "sentiment": "positive", "n_contradicted": 1, "has_sources": False},
    },
    {  # FR, negative opinion
        "answer": "L'Occitane est trop chère pour ce qu'elle propose : les textures sont grasses, le parfum entêtant, et le rapport qualité-prix "
                  "est mauvais comparé à Nivea. Je ne la recommanderais pas.\nSOURCES: avis clients",
        "expected": {"brand_rank": None, "sentiment": "negative", "n_contradicted": 0, "has_sources": True},
    },
    {  # JP, ranked, brand 1st
        "answer": "おすすめ順：1. ロクシタン（L'Occitane）— シアバターのハンドクリームが定番。2. ニベア 3. ユースキン 4. アトリックス 5. ロクシタン以外では資生堂。\nSOURCES: @cosme",
        "expected": {"brand_rank": 1, "sentiment": "positive", "n_contradicted": 0, "has_sources": True},
    },
    {  # EN, two contradicted claims (supplements + founded in Paris 1990)
        "answer": "L'Occitane was founded in Paris in 1990 and is best known for its dietary supplements and hand creams.\nSOURCES: Wikipedia",
        "expected": {"brand_rank": None, "sentiment": "neutral", "n_contradicted": 2, "has_sources": True},
    },
]


def main() -> None:
    hits = {"brand_rank": 0, "sentiment": 0, "n_contradicted": 0, "has_sources": 0}
    for i, fx in enumerate(FIXTURES, 1):
        user = (f"BRAND: {BRAND}\nKNOWN COMPETITORS: Nivea, Neutrogena, Rituals, Yves Rocher, Shiseido\nFACT SHEET (ground truth): {FACTS}\n\n"
                f"ASSISTANT ANSWER:\n\"\"\"\n{fx['answer']}\n\"\"\"")
        txt, *_ = app.timed_call(app.JUDGE, app.JUDGE_SYSTEM, user, json_mode=True, temperature=0.0)
        v = app.parse_json(txt)
        got = {"brand_rank": v.get("brand_rank"), "sentiment": v.get("sentiment"),
               "n_contradicted": sum(1 for c in (v.get("claims") or []) if isinstance(c, dict) and c.get("status") == "contradicted"),
               "has_sources": bool(v.get("sources"))}
        for k in hits:
            ok = got[k] == fx["expected"][k]
            hits[k] += ok
            if not ok:
                print(f"  fixture {i} · {k}: attendu {fx['expected'][k]!r}, obtenu {got[k]!r}")
    n = len(FIXTURES)
    print(f"\nJuge `{app.JUDGE.name}/{app.JUDGE.model}` sur n = {n} réponses étiquetées à la main :")
    for k, h in hits.items():
        print(f"  {k:15s} {h}/{n}")
    print("\n" + app.measures_md())


if __name__ == "__main__":
    main()
