"""
Batterie de tests de la logique métier — sans pytest, sans réseau, sans clé d'API.

    LLM_PROVIDER=mock .venv/bin/python test_app.py

Le script force lui-même LLM_PROVIDER=mock et HISTORY_PATH vers un dossier temporaire avant
d'importer `app` : aucun appel réseau, aucune écriture dans runs/.

Trois statuts :
    PASS  l'assertion tient
    FAIL  l'assertion ne tient pas → bug (code de sortie 1)
    INFO  comportement observé, ni bon ni mauvais en soi, documenté dans rapport-tests.md

Ce qui n'est PAS testé ici : la couche Gradio (widgets, callbacks) et les appels réels aux
fournisseurs — voir eval_judge.py pour le juge réel.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path

# ── isolation : mock + historique temporaire, AVANT l'import de app ──
os.environ["LLM_PROVIDER"] = "mock"
_TMP = Path(tempfile.mkdtemp(prefix="aam-tests-"))
os.environ["HISTORY_PATH"] = str(_TMP / "history.jsonl")
os.environ.pop("DEMO_USER", None)
os.environ.pop("DEMO_PASSWORD", None)

import app  # noqa: E402

# ───────────────────────────── mini-harnais ─────────────────────────────

BILAN = {"pass": 0, "fail": 0, "info": 0}
ECHECS: list[str] = []


def section(titre: str) -> None:
    print(f"\n── {titre} " + "─" * max(0, 66 - len(titre)))


def verif(libelle: str, condition: bool, detail: str = "") -> None:
    if condition:
        BILAN["pass"] += 1
        print(f"PASS  {libelle}")
    else:
        BILAN["fail"] += 1
        ECHECS.append(libelle)
        print(f"FAIL  {libelle}")
        if detail:
            print(f"      → {detail}")


def obs(libelle: str, detail: str) -> None:
    """Observation : comportement constaté, à arbitrer dans le rapport, sans faire échouer."""
    BILAN["info"] += 1
    print(f"INFO  {libelle}\n      → {detail}")


def groupe(fn) -> None:
    """Exécute un groupe de tests ; une exception non rattrapée devient un FAIL, pas un arrêt."""
    section(fn.__name__.replace("t_", "").replace("_", " "))
    try:
        fn()
    except Exception:
        BILAN["fail"] += 1
        ECHECS.append(fn.__name__ + " (exception)")
        print(f"FAIL  {fn.__name__} a levé une exception")
        print("      " + traceback.format_exc().replace("\n", "\n      "))


# ───────────────────────────── fabriques de données ─────────────────────────────


def R(market="FR", assistant="A", probe="P1_reco", rep=1, mentioned=True, rank=None,
      sentiment="neutral", claims=None, sources=None, comp=None) -> dict:
    """Un résultat de sonde fabriqué à la main, au format exact de run_probe()."""
    return {"market": market, "assistant": assistant, "model": "m", "probe": probe, "rep": rep,
            "question": "q", "answer": "a",
            "verdict": {"brand_mentioned": mentioned, "brands_listed": [], "brand_rank": rank,
                        "sentiment": sentiment, "sources": sources or [], "claims": claims or [],
                        "top_competitor": comp},
            "latency_answer": 0.1, "latency_judge": 0.1}


def S(market="FR", **kw) -> dict:
    """Un bloc de scores au format aggregate(), pour tester action_plan() branche par branche."""
    base = {"market": market, "assistant": "all", "n_answers": 4, "presence_all": 1.0,
            "presence_reco": 1.0, "rank_reco": 1.0, "rank_spread": 0, "stability_reco": None,
            "sentiment": 0.0, "n_claims": 0, "contradicted": [], "unverifiable": [],
            "sources": [], "top_competitors": []}
    base.update(kw)
    return base


# ───────────────────────────── 1. brand_in ─────────────────────────────


def t_brand_in():
    B = "L'Occitane"
    AL = app.DEFAULT_ALIASES

    verif("apostrophe courbe dans la réponse, droite dans la marque",
          app.brand_in("J'aime bien L’Occitane pour les mains.", B, ""))
    verif("apostrophe droite dans la réponse, courbe dans la marque",
          app.brand_in("J'aime bien L'Occitane.", "L’Occitane", ""))
    verif("casse indifférente (L'OCCITANE)", app.brand_in("Essaie L'OCCITANE.", B, ""))
    verif("accents ignorés (Crème / Creme)", app.brand_in("La creme Nuxe est bien.", "Crème", ""))
    verif("accents ignorés dans l'autre sens", app.brand_in("La crème Nuxé.", "Nuxe", ""))
    verif("espace parasite (L' Occitane)", app.brand_in("Prends L' Occitane.", B, ""))
    verif("tiret parasite (L-Occitane)", app.brand_in("Prends L-Occitane.", B, ""))
    verif("tiret cadratin (L—Occitane)", app.brand_in("Prends L—Occitane.", B, ""))
    verif("apostrophe absente (LOccitane)", app.brand_in("Prends LOccitane.", B, ""))
    verif("alias japonais katakana", app.brand_in("ロクシタンのハンドクリームが人気です。", B, AL))
    verif("alias katakana demi-largeur (NFKD)",
          app.brand_in("ﾛｸｼﾀﾝ のクリーム。", B, AL))
    verif("alias séparés par ; et retour ligne",
          app.brand_in("Try LOCCITANE.", "Marque X", "alias1;\nL'OCCITANE"))
    verif("marque absente → False", not app.brand_in("Nivea, Neutrogena et Rituals.", B, AL))
    verif("marque vide → False (pas de match universel)", not app.brand_in("n'importe quoi", "", ""))
    verif("alias vide/None toléré", app.brand_in("L'Occitane", B, None) and
          app.brand_in("L'Occitane", B, ""))
    verif("alias vides ignorés (', ,')", not app.brand_in("Nivea", "Zzz", " , ,  "))

    # cas limites qui méritent une décision, pas un verdict automatique
    obs("faux positif : marque courte incluse dans un mot",
        f"brand_in('Cette crème est sans reproche.', 'RoC') = "
        f"{app.brand_in('Cette crème est sans reproche.', 'RoC', '')} — 'reproche' contient 'roc'. "
        "La présence littérale écrase le verdict du juge (run_probe()) : un faux positif gonfle la présence.")
    obs("faux positif : alias inclus dans le nom d'un concurrent",
        f"brand_in('Yves Rocher domine le marché.', 'La Roche-Posay', 'Roche') = "
        f"{app.brand_in('Yves Rocher domine le marché.', 'La Roche-Posay', 'Roche')} — "
        "'yvesrocher' contient 'roche'.")
    obs("faux positif inter-mots : _norm supprime les espaces",
        f"brand_in('un soin sans odeur', 'Sod') = {app.brand_in('un soin sans odeur', 'Sod', '')} — "
        "'sans odeur' → 'sansodeur' contient 'sod'.")
    obs("ponctuation non normalisée",
        f"brand_in(\"L'Occitane, en Provence\", 'X', \"L'Occitane en Provence\") = "
        f"{app.brand_in('L Occitane, en Provence', 'X', 'L Occitane en Provence')} — "
        "la virgule n'est pas retirée par _norm (_norm()), l'alias long ne matche pas.")


# ───────────────────────────── 2. parse_json ─────────────────────────────


NEUTRE = {"brand_mentioned": None, "brand_rank": None, "sentiment": "absent"}


def _est_neutre(v: dict) -> bool:
    return all(v.get(k) == val for k, val in NEUTRE.items()) and "_judge_error" in v


def t_parse_json():
    propre = '{"brand_mentioned": true, "brand_rank": 2, "sentiment": "positive"}'
    verif("JSON propre", app.parse_json(propre)["brand_rank"] == 2)

    entoure = "Voici mon analyse :\n" + propre + "\nVoilà."
    verif("JSON entouré de texte", app.parse_json(entoure)["brand_rank"] == 2)

    fence = "```json\n" + propre + "\n```"
    verif("JSON dans un bloc ```json", app.parse_json(fence)["brand_rank"] == 2)

    imbrique = '{"claims": [{"claim": "a", "status": "contradicted"}], "meta": {"x": {"y": 1}}}'
    verif("accolades imbriquées", app.parse_json(imbrique)["claims"][0]["status"] == "contradicted")

    v = app.parse_json("Je ne peux pas répondre à cette demande.")
    verif("texte non-JSON → verdict neutre + _judge_error", _est_neutre(v), repr(v))
    verif("verdict neutre : toutes les clés attendues sont présentes",
          all(k in v for k in ("brand_mentioned", "brands_listed", "brand_rank", "sentiment",
                               "sources", "claims", "top_competitor")))

    v = app.parse_json('{"brand_mentioned": true, "brand_rank": 2')
    verif("JSON tronqué → verdict neutre", _est_neutre(v), repr(v))

    v = app.parse_json("")
    verif("chaîne vide → verdict neutre", _est_neutre(v), repr(v))

    # le regex \{.*\} est gourmand : deux objets dans la même réponse = tout est perdu
    deux = 'Exemple de format : {"brand_mentioned": false}\nRéponse : ' + propre
    v = app.parse_json(deux)
    verif("deux objets JSON : le vrai verdict est extrait, pas l'exemple qui le préface",
          v.get("brand_mentioned") is True and v.get("brand_rank") == 2,
          f"parse_json a renvoyé {v!r} — attendu le verdict réel {propre}")

    v = app.parse_json('  \n{"sentiment": "negative"}\n  ')
    verif("espaces autour du JSON", v.get("sentiment") == "negative")


# ───────────────────────────── 3. aggregate ─────────────────────────────


def t_aggregate():
    verif("liste vide → dict vide (pas de division par zéro)", app.aggregate([]) == {})

    # présence 0 %
    res = [R(mentioned=False, sentiment="absent") for _ in range(4)]
    s = app.aggregate(res)["FR|A"]
    verif("présence 0 %", s["presence_all"] == 0.0 and s["presence_reco"] == 0.0)
    verif("sentiment None si aucun sentiment notable ('absent' hors barème)", s["sentiment"] is None)
    verif("rank_reco None si aucun rang", s["rank_reco"] is None)
    verif("rank_spread = 0 si moins de 2 rangs", s["rank_spread"] == 0)

    # présence 100 %
    res = [R(probe=p, rank=1 if p == "P1_reco" else None) for p in app.PROBE_LABELS]
    s = app.aggregate(res)["FR|A"]
    verif("présence 100 %", s["presence_all"] == 1.0 and s["presence_reco"] == 1.0)
    verif("n_answers = nombre de sondes", s["n_answers"] == 4)

    # présence mixte 2/4, dont 1 P1 sur 2 présents
    res = [R(probe="P1_reco", rep=1, mentioned=True, rank=3),
           R(probe="P1_reco", rep=2, mentioned=False, sentiment="absent"),
           R(probe="P2_opinion", mentioned=True, sentiment="negative"),
           R(probe="P3_products", mentioned=False, sentiment="absent")]
    s = app.aggregate(res)["FR|A"]
    verif("présence mixte 50 %", s["presence_all"] == 0.5)
    verif("présence reco mixte 50 %", s["presence_reco"] == 0.5)
    verif("stabilité calculée sur 2 répétitions P1 (1 présent / 1 absent)",
          s["stability_reco"] == 0.5, f"stability_reco={s['stability_reco']}")
    verif("sentiment moyen sur les seules réponses notables (neutral 0 + negative -1 → -0.5)",
          s["sentiment"] == -0.5, f"sentiment={s['sentiment']}")

    # une seule répétition → stabilité None
    s = app.aggregate([R(probe="P1_reco", rank=2)])["FR|A"]
    verif("1 seule mesure P1 → stabilité None", s["stability_reco"] is None)
    verif("1 seul rang → rank_reco renseigné, spread 0",
          s["rank_reco"] == 2 and s["rank_spread"] == 0)

    # plusieurs répétitions, rangs dispersés
    res = [R(probe="P1_reco", rep=1, rank=1), R(probe="P1_reco", rep=2, rank=5),
           R(probe="P1_reco", rep=3, rank=3)]
    s = app.aggregate(res)["FR|A"]
    verif("rang moyen sur 3 répétitions", s["rank_reco"] == 3.0)
    verif("rank_spread = max - min", s["rank_spread"] == 4)
    verif("stabilité 1.0 si présence constante", s["stability_reco"] == 1.0)

    # rangs partiellement manquants / mal typés
    res = [R(probe="P1_reco", rep=1, rank=2), R(probe="P1_reco", rep=2, rank=None),
           R(probe="P1_reco", rep=3, rank="deuxième")]
    s = app.aggregate(res)["FR|A"]
    verif("rangs None et non-entiers ignorés", s["rank_reco"] == 2.0, f"rank_reco={s['rank_reco']}")

    # claims contredits / non vérifiables + claims mal formés
    claims = [{"claim": "maquillage", "status": "contradicted"},
              {"claim": "maquillage", "status": "contradicted"},
              {"claim": "vendu au Japon", "status": "unverifiable"},
              {"claim": "karité", "status": "consistent"},
              "chaîne parasite"]
    s = app.aggregate([R(probe="P2_opinion", claims=claims)])["FR|A"]
    verif("claims non-dict ignorés", s["n_claims"] == 4, f"n_claims={s['n_claims']}")
    verif("contradicted collectés (doublons conservés tels quels)",
          s["contradicted"] == ["maquillage", "maquillage"])
    verif("unverifiable collectés", s["unverifiable"] == ["vendu au Japon"])

    # sources : dédup + comptage + filtre des 'none'
    res = [R(probe="P1_reco", sources=["  Wikipedia ", "none", "Sephora"]),
           R(probe="P2_opinion", sources=["Wikipedia", "aucune", "n/a", "", 42])]
    s = app.aggregate(res)["FR|A"]
    verif("sources dédupliquées et triées par fréquence", s["sources"][0] == "Wikipedia",
          f"sources={s['sources']}")
    verif("sources vides / 'none' / 'aucune' / 'n/a' / non-str filtrées",
          set(s["sources"]) == {"Wikipedia", "Sephora"}, f"sources={s['sources']}")

    # concurrents
    res = [R(comp="Nivea"), R(probe="P2_opinion", comp="Nivea"),
           R(probe="P3_products", comp=" Rituals "), R(probe="P4_compare", comp=None)]
    s = app.aggregate(res)["FR|A"]
    verif("concurrent favori compté et trié", s["top_competitors"][0] == "Nivea",
          f"{s['top_competitors']}")
    verif("concurrent None ignoré, espaces coupés", "Rituals" in s["top_competitors"])

    # séparation marché × assistant
    res = [R(market="FR", assistant="Mistral"), R(market="FR", assistant="OpenAI"),
           R(market="JP", assistant="Mistral")]
    per = app.aggregate(res)
    verif("clés 'MARCHÉ|Assistant' séparées et triées",
          list(per) == ["FR|Mistral", "FR|OpenAI", "JP|Mistral"], str(list(per)))

    # absence de sonde P1 → presence_reco None
    s = app.aggregate([R(probe="P2_opinion")])["FR|A"]
    verif("aucune sonde P1 → presence_reco None", s["presence_reco"] is None)


# ───────────────────────────── 4. pool_by_market ─────────────────────────────


def t_pool_by_market():
    res = [R(market="FR", assistant="Mistral", probe="P1_reco", rank=2),
           R(market="FR", assistant="OpenAI", probe="P1_reco", rank=4),
           R(market="JP", assistant="Mistral", probe="P1_reco", mentioned=False, sentiment="absent")]
    pooled = app.pool_by_market(res)
    verif("une entrée par marché", list(pooled) == ["FR", "JP"], str(list(pooled)))
    verif("assistant poolé = 'all'", pooled["FR"]["assistant"] == "all")
    verif("le pool agrège les deux assistants", pooled["FR"]["n_answers"] == 2)
    verif("rang moyen poolé", pooled["FR"]["rank_reco"] == 3.0, str(pooled["FR"]["rank_reco"]))
    verif("marché sans présence poolé à 0", pooled["JP"]["presence_all"] == 0.0)
    verif("aggregate() interne produit bien la clé 'MARCHÉ|all'",
          app.aggregate([dict(r, assistant="all") for r in res if r["market"] == "FR"]).get("FR|all")
          is not None)
    verif("liste vide → dict vide", app.pool_by_market([]) == {})


# ───────────────────────────── 5. action_plan ─────────────────────────────


def _plan(**kw) -> str:
    return app.action_plan("L'Occitane", "crème pour les mains", {"FR": S(**kw)})


def t_action_plan():
    verif("branche absence des recommandations",
          "Absent des recommandations" in _plan(presence_reco=0.25))
    verif("branche mal classé (rang > 3)",
          "mal classé" in _plan(presence_reco=1.0, rank_reco=4.5))
    verif("pas de branche rang si le rang est bon",
          "mal classé" not in _plan(presence_reco=1.0, rank_reco=2.0))
    verif("branche erreur factuelle",
          "Erreur factuelle" in _plan(contradicted=["gamme maquillage"]))
    verif("erreurs factuelles dédupliquées et plafonnées à 3",
          _plan(contradicted=["a", "a", "b", "c", "d", "e"]).count("Erreur factuelle") == 3)
    verif("branche non-vérifiable",
          "non couverte" in _plan(unverifiable=["vendu en pharmacie"]))
    verif("branche sentiment négatif", "Sentiment négatif" in _plan(sentiment=-0.6))
    verif("pas de branche sentiment si neutre/positif", "Sentiment négatif" not in _plan(sentiment=0.5))
    verif("branche concurrent favori", "Concurrent favori" in _plan(top_competitors=["Nivea"]))
    verif("branche sources déclarées", "Sources déclarées" in _plan(sources=["Wikipedia"]))
    verif("branche instabilité par rank_spread", "instables" in _plan(rank_spread=3))
    verif("branche instabilité par stability_reco", "instables" in _plan(stability_reco=0.5))
    verif("pas d'instabilité si stabilité haute et spread nul",
          "instables" not in _plan(stability_reco=1.0, rank_spread=0))
    verif("branche 'rien d'urgent' atteignable", "Rien d'urgent" in _plan())

    # robustesse aux None (marché où aucune sonde P1 n'a été jouée, juge muet…)
    tout_none = _plan(presence_all=None, presence_reco=None, rank_reco=None, stability_reco=None,
                      sentiment=None, rank_spread=0)
    verif("aucune branche ne plante sur des None", "Rien d'urgent" in tout_none, tout_none)

    # présence sous le seuil ET rang mauvais : la règle est un elif, une seule action sort
    p = _plan(presence_reco=0.0, rank_reco=5.0)
    verif("absence et mauvais rang : l'absence prime (elif)",
          "Absent des recommandations" in p and "mal classé" not in p)

    # plusieurs marchés → un titre par marché
    txt = app.action_plan("X", "y", {"FR": S("FR"), "JP": S("JP")})
    verif("un bloc par marché, libellé pays inclus",
          "### FR — France" in txt and "### JP — 日本" in txt, txt[:200])

    # marché inconnu → KeyError sur MARKETS
    try:
        app.action_plan("X", "y", {"ZZ": S("ZZ")})
        obs("marché inconnu dans action_plan", "n'a pas levé — inattendu")
    except KeyError:
        obs("marché inconnu → KeyError (action_plan())",
            "action_plan fait MARKETS[mk] sans garde : un historique relu avec un marché retiré "
            "du banc ferait planter le plan. Non atteignable depuis l'UI actuelle.")


# ───────────────────────────── 6. compute_deltas ─────────────────────────────


def t_compute_deltas():
    per = {"FR|A": S("FR", presence_all=0.75, contradicted=["x", "y"])}
    verif("pas d'historique → aucun delta", app.compute_deltas(None, per) == {})
    verif("historique vide → aucun delta", app.compute_deltas({}, per) == {})

    prev = {"ts": "2026-08-09T10:00:00+00:00",
            "scores": {"FR|A": {"presence_all": 0.5, "contradicted": ["x"]}}}
    d = app.compute_deltas(prev, per)["FR|A"]
    verif("delta présence en points", "présence +25 pts" in d, d)
    verif("delta erreurs signé", "erreurs +1" in d, d)
    verif("date du run précédent affichée", "2026-08-09" in d, d)

    per2 = {"FR|A": S("FR", presence_all=0.25, contradicted=[])}
    d = app.compute_deltas(prev, per2)["FR|A"]
    verif("delta négatif sans + parasite", "présence -25 pts" in d and "erreurs -1" in d, d)

    per3 = {"FR|A": S("FR"), "JP|A": S("JP")}
    d = app.compute_deltas(prev, per3)
    verif("marché absent du run précédent → 'nouveau marché/assistant'",
          d["JP|A"] == "nouveau marché/assistant", str(d))

    prev_partiel = {"ts": "2026-08-09T10:00:00+00:00", "scores": {"FR|A": {}}}
    d = app.compute_deltas(prev_partiel, per)
    verif("scores précédents vides → traités comme nouveaux (pas de crash)",
          d["FR|A"] == "nouveau marché/assistant", str(d))

    prev_sans_presence = {"ts": "2026-08-09T10:00:00+00:00",
                          "scores": {"FR|A": {"presence_all": None, "contradicted": []}}}
    d = app.compute_deltas(prev_sans_presence, per)["FR|A"]
    verif("presence_all précédente None → seul le delta erreurs sort", d.startswith("erreurs"), d)

    obs("comparaison des erreurs entre runs de tailles différentes (compute_deltas())",
        "compute_deltas compare len(contradicted) brut : un run 1 répétition (4 réponses) puis un "
        "run 7 répétitions (28 réponses) affichera « erreurs +6 » sans qu'aucune erreur nouvelle "
        "n'existe. Le delta n'est lisible qu'à banc ET répétitions constants — rien ne le vérifie.")


# ───────────────────────────── 7. historique (load/save) ─────────────────────────────


def t_historique():
    hist = _TMP / "hist_test.jsonl"
    ancien, app.HISTORY_PATH = app.HISTORY_PATH, hist
    try:
        if hist.exists():
            hist.unlink()
        verif("fichier absent → None", app.load_previous("L'Occitane", "crème") is None)

        rec = {"ts": "2026-09-09T12:00:00+00:00", "brand": "L'Occitane", "category": "crème",
               "scores": {"FR|A": {"presence_all": 0.5, "contradicted": []}}}
        app.save_run(rec)
        verif("dossier parent créé et aller-retour identique",
              app.load_previous("L'Occitane", "crème") == rec)
        verif("casse marque/catégorie indifférente",
              app.load_previous("l'occitane", "CRÈME") == rec)
        verif("marque inconnue → None", app.load_previous("Nivea", "crème") is None)
        verif("catégorie différente → None", app.load_previous("L'Occitane", "parfum") is None)

        rec2 = dict(rec, ts="2026-10-09T12:00:00+00:00")
        app.save_run(rec2)
        verif("le dernier run de la paire marque/catégorie gagne",
              app.load_previous("L'Occitane", "crème")["ts"] == rec2["ts"])

        with hist.open("a", encoding="utf-8") as fh:
            fh.write("{ceci n'est pas du json\n\n")
        verif("ligne JSONL corrompue ignorée",
              app.load_previous("L'Occitane", "crème")["ts"] == rec2["ts"])

        verif("accents et apostrophes préservés (ensure_ascii=False)",
              "L'Occitane" in hist.read_text(encoding="utf-8"))

        with hist.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "x", "brand": None, "category": "crème"}) + "\n")
        try:
            app.load_previous("L'Occitane", "crème")
            obs("champ brand null dans l'historique", "toléré")
        except AttributeError:
            obs("champ brand null dans l'historique → AttributeError (load_previous())",
                "rec.get('brand', '').lower() : une ligne JSON valide mais avec brand=null casse "
                "toute la lecture de l'historique, alors qu'une ligne illisible, elle, est ignorée. "
                "Non atteignable depuis l'app (save_run écrit toujours une chaîne).")
    finally:
        app.HISTORY_PATH = ancien


# ───────────────────────────── 8. banc de sondes ─────────────────────────────


def t_banc_de_sondes():
    verif("PROBE_BANK_VERSION non vide",
          isinstance(app.PROBE_BANK_VERSION, str) and app.PROBE_BANK_VERSION.strip() != "")
    ids_ref = set(app.PROBE_LABELS)
    verif("4 libellés de sondes déclarés", len(ids_ref) == 4, str(ids_ref))
    verif("au moins 3 marchés", len(app.MARKETS) >= 3, str(list(app.MARKETS)))

    for mk, m in app.MARKETS.items():
        verif(f"{mk} : clés country/lang/lang_name/probes présentes",
              all(k in m for k in ("country", "lang", "lang_name", "probes")))
        verif(f"{mk} : exactement 4 sondes", len(m["probes"]) == 4, str(list(m["probes"])))
        verif(f"{mk} : mêmes identifiants de sondes que PROBE_LABELS",
              set(m["probes"]) == ids_ref, str(set(m["probes"]) ^ ids_ref))
        for pid, texte in m["probes"].items():
            verif(f"{mk}/{pid} : contient {{category}}", "{category}" in texte)
            if pid == "P1_reco":
                verif(f"{mk}/P1 : ne nomme PAS la marque (reco spontanée)", "{brand}" not in texte)
            else:
                verif(f"{mk}/{pid} : contient {{brand}}", "{brand}" in texte)
            try:
                rendu = texte.format(brand="L'Occitane", category="crème pour les mains")
                verif(f"{mk}/{pid} : .format() sans variable orpheline",
                      "{" not in rendu and "}" not in rendu, rendu[:80])
            except (KeyError, IndexError) as e:
                verif(f"{mk}/{pid} : .format() sans variable orpheline", False, repr(e))

    verif("ASSISTANT_SYSTEM se formate avec un marché",
          "{" not in app.ASSISTANT_SYSTEM.format(**app.MARKETS["FR"]))
    verif("mode mock actif : un seul assistant 'mock'",
          list(app.ASSISTANTS) == ["mock"], str(list(app.ASSISTANTS)))
    verif("juge de repli = le seul assistant disponible", app.JUDGE.name == "mock")


# ───────────────────────────── 9. plafond MAX_CALLS ─────────────────────────────


def t_plafond_max_calls():
    a = app.ASSISTANTS["mock"]
    old_max, old_calls = app.MAX_CALLS, a.calls
    try:
        app.MAX_CALLS = 1
        a.calls = 1
        leve = False
        try:
            app.call_model(a, "sys", "user")
        except RuntimeError as e:
            leve = "Plafond" in str(e)
        verif("call_model lève au-delà du plafond", leve)

        a.calls = 0
        txt, tin, tout = app.call_model(a, "sys", "user")
        verif("sous le plafond, l'appel passe", txt.startswith("[MOCK]"))
        verif("call_model renvoie (texte, tokens_in, tokens_out)",
              isinstance(tin, int) and isinstance(tout, int))
        v = app.parse_json(app.call_model(a, "sys", "user", json_mode=True)[0])
        verif("mode JSON du mock parsable par parse_json", v["brand_rank"] == 2)

        obs("le plafond ne compte que les appels réussis (call_model() + timed_call())",
            "total_calls() somme a.calls, incrémenté après succès dans timed_call ; les 4 tentatives "
            "d'un appel en échec ne sont pas comptées, et avec WORKERS threads le plafond peut être "
            f"dépassé de WORKERS-1 appels (WORKERS={app.WORKERS}).")
    finally:
        app.MAX_CALLS, a.calls = old_max, old_calls


# ───────────────────────────── 10. fuite de clé dans les erreurs ─────────────────────────────


def t_fuite_de_cle():
    """Un échec réseau doit produire un message affichable sur une URL publique."""
    canari = "sk-CANARI-NE-DOIT-PAS-FUITER-0123456789"
    faux = app.Assistant("Canari", "openai-compatible", "modele-x", canari, "http://127.0.0.1:9/v1")
    sleep_orig = app.time.sleep
    app.time.sleep = lambda *_: None  # pas d'attente de back-off dans les tests
    try:
        msg = ""
        try:
            app.call_model(faux, "sys", "user")
        except RuntimeError as e:
            msg = str(e)
        except Exception as e:  # openai absent / autre → on rapporte quand même
            msg = f"{type(e).__name__}: {e}"
        verif("le message d'erreur d'un appel raté ne contient pas la clé d'API",
              canari not in msg, f"message rendu à l'écran : {msg[:300]}")
        verif("le message d'erreur nomme l'assistant et le nombre de tentatives",
              "Canari" in msg and "4 tentatives" in msg, msg[:200])
        obs("contenu réel du message d'erreur affiché",
            msg[:220] + ("…" if len(msg) > 220 else ""))
        verif("4 tentatives comptées dans a.errors", faux.errors == 4, f"errors={faux.errors}")
    finally:
        app.time.sleep = sleep_orig


# ───────────────────────────── 11. monitor() bout en bout (mock) ─────────────────────────────


NOOP = lambda *a, **k: None  # noqa: E731  (remplace gr.Progress hors contexte Gradio)


def _monitor(**kw):
    args = {"brand": "L'Occitane", "category": "crème pour les mains", "markets": ["FR"],
            "assistants": ["mock"], "facts": app.DEFAULT_FACTS, "competitors": "Nivea",
            "reps": 1, "aliases": app.DEFAULT_ALIASES, "progress": NOOP}
    args.update(kw)
    return app.monitor(**args)


def t_monitor_nominal():
    cwd = os.getcwd()
    box = _TMP / "cwd_ok"
    (box / "runs").mkdir(parents=True, exist_ok=True)
    hist, app.HISTORY_PATH = app.HISTORY_PATH, box / "runs" / "history.jsonl"
    os.chdir(box)
    try:
        out = _monitor()
        verif("monitor renvoie un tuple de 6 éléments (autant que d'outputs Gradio)",
              isinstance(out, tuple) and len(out) == 6, f"type={type(out)} len={len(out)}")
        head, plan, table, raw, meas, export = out
        verif("1/6 tableau de bord : Markdown non vide", isinstance(head, str) and "Marché" in head)
        verif("2/6 plan d'action : un bloc pour le marché sondé",
              "FR" in plan and "France" in plan, plan[:200])
        verif("3/6 détail : 4 lignes (1 marché × 4 sondes × 1 rép.)",
              isinstance(table, list) and len(table) == 4, str(type(table)))
        verif("3/6 détail : 11 colonnes par ligne", all(len(r) == 11 for r in table),
              str([len(r) for r in table]))
        verif("4/6 réponses brutes : les 4 réponses mock", raw.count("[MOCK]") == 4)
        verif("5/6 bande de mesures : appels comptés", "appels" in meas)
        verif("6/6 export : chemin de fichier existant",
              isinstance(export, str) and Path(export).exists(), str(export))
        rec = json.loads(Path(export).read_text(encoding="utf-8"))
        verif("export : banc de sondes et juge tracés",
              rec["probe_bank"] == app.PROBE_BANK_VERSION and rec["judge"].startswith("mock"))
        verif("export : 4 résultats bruts", len(rec["results"]) == 4)
        verif("historique écrit", app.HISTORY_PATH.exists())

        # second run → deltas calculés contre le premier
        out2 = _monitor()
        verif("second run : delta affiché au lieu de « premier run »",
              "premier run" not in out2[0], out2[0][:300])
        verif("2 lignes dans l'historique après 2 runs",
              len(app.HISTORY_PATH.read_text(encoding="utf-8").strip().splitlines()) == 2)

        # plusieurs répétitions
        out3 = _monitor(reps=2)
        verif("2 répétitions → 8 réponses", len(out3[2]) == 8, str(len(out3[2])))
        verif("2 répétitions → colonne stabilité renseignée", "Stabilité" in out3[0])

        # gr.Progress() par défaut, hors contexte Gradio
        args = {"brand": "X", "category": "y", "markets": ["FR"], "assistants": ["mock"],
                "facts": "", "competitors": "", "reps": 1, "aliases": ""}
        try:
            app.monitor(**args)
            verif("monitor fonctionne avec le gr.Progress() par défaut", True)
        except Exception as e:
            verif("monitor fonctionne avec le gr.Progress() par défaut", False, repr(e))
    finally:
        os.chdir(cwd)
        app.HISTORY_PATH = hist


def t_monitor_entrees_invalides():
    cas = [("marque vide", {"brand": "   "}), ("catégorie vide", {"category": ""}),
           ("aucun marché coché", {"markets": []}), ("aucun assistant coché", {"assistants": []}),
           ("tout vide", {"brand": "", "category": "", "markets": [], "assistants": []})]
    for libelle, kw in cas:
        try:
            out = _monitor(**kw)
            ok = (isinstance(out, tuple) and len(out) == 6 and isinstance(out[0], str)
                  and out[0].strip() != "" and out[2] is None and out[5] is None)
            verif(f"{libelle} → message d'erreur, pas d'exception", ok, repr(out)[:250])
        except Exception as e:
            verif(f"{libelle} → message d'erreur, pas d'exception", False,
                  "".join(traceback.format_exception_only(type(e), e)).strip())

    # aucun appel modèle ne doit partir sur une entrée invalide
    avant = app.total_calls()
    _monitor(brand="")
    verif("entrée invalide → aucun appel modèle", app.total_calls() == avant)


def t_monitor_sonde_en_echec():
    """Une sonde qui échoue après 4 tentatives (429 d'un palier gratuit) ne doit pas
    faire perdre les autres réponses déjà payées."""
    cwd = os.getcwd()
    box = _TMP / "cwd_echec"
    (box / "runs").mkdir(parents=True, exist_ok=True)
    hist, app.HISTORY_PATH = app.HISTORY_PATH, box / "runs" / "history.jsonl"
    vrai_run_probe = app.run_probe
    os.chdir(box)

    def run_probe_capricieux(brand, category, market, probe_id, *a, **k):
        if probe_id == "P3_products":
            raise RuntimeError("mock : appel échoué après 4 tentatives — 429 rate limit")
        return vrai_run_probe(brand, category, market, probe_id, *a, **k)

    app.run_probe = run_probe_capricieux
    try:
        out = _monitor()
        partiel = isinstance(out[2], list) and len(out[2]) == 3
        verif("1 sonde sur 4 en échec → les 3 autres réponses sont conservées", partiel,
              f"BUG CONNU — monitor() renvoie « {str(out[0])[:90]} » et jette les 3 réponses "
              "déjà obtenues (monitor(), la boucle sur les futures : le try englobe toute la boucle).")
    except Exception as e:
        verif("1 sonde sur 4 en échec → les 3 autres réponses sont conservées", False, repr(e))
    finally:
        app.run_probe = vrai_run_probe
        os.chdir(cwd)
        app.HISTORY_PATH = hist


def t_monitor_sans_dossier_runs():
    """HISTORY_PATH hors de runs/ (cas documenté du Space : stockage persistant)."""
    cwd = os.getcwd()
    box = _TMP / "cwd_sans_runs"
    box.mkdir(parents=True, exist_ok=True)
    if (box / "runs").exists():
        shutil.rmtree(box / "runs")
    hist, app.HISTORY_PATH = app.HISTORY_PATH, _TMP / "persistant" / "history.jsonl"
    os.chdir(box)
    try:
        out = _monitor()
        verif("HISTORY_PATH hors de runs/ → le run aboutit quand même", isinstance(out, tuple),
              repr(out)[:200])
    except FileNotFoundError as e:
        verif("HISTORY_PATH hors de runs/ → le run aboutit quand même", False,
              f"BUG CONNU — l'export écrit dans Path('runs') en dur (monitor(), export = Path('runs')) sans mkdir : {e}. "
              "Les appels modèle sont consommés, l'historique est écrit, puis l'UI plante.")
    except Exception as e:
        verif("HISTORY_PATH hors de runs/ → le run aboutit quand même", False, repr(e))
    finally:
        os.chdir(cwd)
        app.HISTORY_PATH = hist


def t_export_nom_de_fichier():
    """Nom d'export : marque non-ASCII, caractères interdits, ':' de l'horodatage."""
    cwd = os.getcwd()
    box = _TMP / "cwd_noms"
    (box / "runs").mkdir(parents=True, exist_ok=True)
    hist, app.HISTORY_PATH = app.HISTORY_PATH, box / "runs" / "history.jsonl"
    os.chdir(box)
    try:
        out = _monitor(brand="ロクシタン", aliases="")
        nom = Path(out[5]).name
        verif("marque non-ASCII : l'export s'écrit quand même", Path(out[5]).exists(), nom)
        verif("nom de fichier sans ':' (compatible macOS/Windows)", ":" not in nom, nom)
        obs("slug de marque non-ASCII",
            f"« ロクシタン » → fichier « {nom} » : re.sub(r'[^a-z0-9]+', '-') (monitor(), export = Path('runs')) vide le "
            "slug ; deux marques japonaises différentes produisent le même nom dans la même seconde.")
        out2 = _monitor(brand="Crème & Co / 100%", aliases="")
        verif("marque avec / et % : pas de sous-dossier créé par accident",
              Path(out2[5]).parent.name == "runs", out2[5])
    finally:
        os.chdir(cwd)
        app.HISTORY_PATH = hist


# ───────────────────────────── 12. cohérence écran / code ─────────────────────────────


def _tableau(per, deltas):
    """La couche de présentation est en cours de réécriture : on prend l'helper qui existe."""
    for nom in ("score_table", "scoreboard"):
        fn = getattr(app, nom, None)
        if callable(fn):
            return nom, fn(per, deltas)
    return None, None


def t_quota_par_jure():
    """Chaque juré a un nombre fixe de sondages ; le dépassement doit être refusé et expliqué,
    et un run refusé pour une autre raison ne doit pas consommer de quota."""
    cwd = os.getcwd()
    box = _TMP / "cwd_quota"
    (box / "runs").mkdir(parents=True, exist_ok=True)
    hist, app.HISTORY_PATH = app.HISTORY_PATH, box / "runs" / "history.jsonl"
    quota, app.RUNS_PAR_JURE = app.RUNS_PAR_JURE, 2
    app._RUNS_CONSOMMES.clear()
    os.chdir(box)
    try:
        b1 = str(_monitor()[0])
        verif("le quota restant est affiché après un run", "il vous en reste 1" in b1.lower(), b1[:160])
        _monitor()
        b3 = str(_monitor()[0])
        verif("au-delà du quota, le sondage est refusé", "Quota atteint" in b3, b3[:160])
        verif("le refus explique la règle", str(app.RUNS_PAR_JURE) in b3 and "sondages" in b3, b3[:160])

        app._RUNS_CONSOMMES.clear()
        _monitor(markets=list(app.MARKETS), reps=7)       # refusé : run trop large
        verif("un run refusé ne consomme pas de quota",
              app._RUNS_CONSOMMES.get("anonyme", 0) == 0, str(app._RUNS_CONSOMMES))

        verif("les comptes sont lus depuis DEMO_ACCOUNTS", callable(app.comptes_demo))
    finally:
        app.RUNS_PAR_JURE = quota
        app._RUNS_CONSOMMES.clear()
        os.chdir(cwd)
        app.HISTORY_PATH = hist


def t_garde_fous_publics():
    """L'app tourne sur une URL publique avec une clé payante : un visiteur ne doit pas pouvoir
    faire exploser la facture, ni bloquer la démonstration pour les autres."""
    cwd = os.getcwd()
    box = _TMP / "cwd_abus"
    (box / "runs").mkdir(parents=True, exist_ok=True)
    hist, app.HISTORY_PATH = app.HISTORY_PATH, box / "runs" / "history.jsonl"
    os.chdir(box)
    try:
        b = str(_monitor(markets=list(app.MARKETS), reps=7)[0])
        verif("run démesuré refusé avant le moindre appel", "Run trop large" in b, b[:150])

        b = str(_monitor(facts="X" * 200_000)[0])
        verif("fiche de faits géante tronquée, et l'écran le dit",
              "tronqué" in b.lower(), b[:150])

        b = str(_monitor(markets=["FR", "MARCHÉ_INEXISTANT"])[0])
        verif("marché inconnu filtré sans planter", "Traceback" not in b and "réponses lues" in b, b[:150])

        cost, app.MAX_SESSION_COST = app.MAX_SESSION_COST, 0.0
        b = str(_monitor()[0])
        app.MAX_SESSION_COST = cost
        verif("budget de session épuisé → refus explicite", "Budget de démonstration épuisé" in b, b[:150])

        verrou = app.RUN_SLOT.acquire(blocking=False)
        b = str(_monitor()[0])
        if verrou:
            app.RUN_SLOT.release()
        verif("un seul sondage à la fois par instance", "déjà en cours" in b, b[:150])
    finally:
        os.chdir(cwd)
        app.HISTORY_PATH = hist


def t_pannes_silencieuses():
    """Les pannes bruyantes (429, plafond) sont bien traitées ailleurs. Ici on vérifie les pannes
    SILENCIEUSES : celles qui produisent un tableau de bord d'apparence normale mais faux."""
    cwd = os.getcwd()
    box = _TMP / "cwd_silence"
    (box / "runs").mkdir(parents=True, exist_ok=True)
    hist, app.HISTORY_PATH = app.HISTORY_PATH, box / "runs" / "history.jsonl"
    vrai = app.call_model
    os.chdir(box)
    try:
        # 1. Réponse vide de l'assistant. La compter « marque absente » abaisserait le chiffre mis
        #    en avant sans que rien ne le signale — c'est arrivé en réel le 09/09 à 12h42.
        app.call_model = lambda a, sys_, usr, json_mode=False, temperature=0.2: (
            ("", 10, 0) if not json_mode else vrai(a, sys_, usr, json_mode, temperature))
        out = _monitor()
        verif("réponse vide → sonde écartée, jamais comptée comme « marque absente »",
              "Absente des recommandations" not in str(out[0]), str(out[0])[:160])
        verif("réponse vide → l'écran le dit",
              "Run interrompu" in str(out[0]) or "n’ont pas abouti" in str(out[0]), str(out[0])[:160])

        # 2. Juge qui ne rend pas de JSON : le verdict tombe en repli neutre, donc les affirmations
        #    contredites disparaissent. Silencieux, ce serait une sous-estimation invisible.
        app.call_model = lambda a, sys_, usr, json_mode=False, temperature=0.2: (
            ("Je ne peux pas répondre à cette demande.", 10, 5) if json_mode
            else vrai(a, sys_, usr, json_mode, temperature))
        out = _monitor()
        verif("verdict de juge illisible → l'écran le signale",
              "illisibles" in str(out[0]), str(out[0])[:200])
    finally:
        app.call_model = vrai
        os.chdir(cwd)
        app.HISTORY_PATH = hist

    # 3. Un appel qui pend bloque un worker ; et le SDK relance tout seul, ce qui rendrait le
    #    compteur d'appels — et donc le coût affiché — optimiste.
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
    verif("un délai d'expiration est imposé aux clients HTTP",
          "timeout=HTTP_TIMEOUT" in src, "aucun timeout : un appel qui pend bloque un worker")
    verif("les relances internes du SDK sont désactivées (sinon les compteurs mentent)",
          "max_retries=0" in src)


def t_coherence_affichage():
    per = app.aggregate([R(probe="P1_reco", rank=2, sources=["Wikipedia"],
                           claims=[{"claim": "maquillage", "status": "contradicted"}])])
    nom, tab = _tableau(per, {})
    if tab is None:
        obs("tableau de scores", "ni score_table() ni scoreboard() dans app — helper renommé ?")
    else:
        verif(f"{nom} : le marché sondé apparaît", "FR" in tab)
        verif(f"{nom} : « premier run » quand il n'y a pas de delta", "premier run" in tab.lower(), tab[:200])
        verif(f"{nom} : erreurs affichées sur le total d'affirmations", "1 / 1" in tab, tab[:400])
        _, vide = _tableau(app.aggregate([R(probe="P2_opinion")]), {})
        verif(f"{nom} : None affiché « — » et jamais « None »", "None" not in vide, vide[:300])
        verif(f"{nom} : présence en pourcentage", "100 %" in tab, tab[:300])

    m = app.measures_md()
    verif("measures_md : version du banc affichée", app.PROBE_BANK_VERSION in m)
    verif("measures_md : juge unique nommé", app.JUDGE.name in m)
    verif("measures_md : ne fuit aucune clé d'API",
          not any(k and k in m for k in (os.getenv("MISTRAL_API_KEY"), os.getenv("OPENAI_REAL_API_KEY"))))

    verif("SENT_SCORE couvre positive/neutral/negative et pas 'absent'",
          set(app.SENT_SCORE) == {"positive", "neutral", "negative"})

    # injection : la marque et les sorties du modèle finissent dans une page publique
    per_xss = app.aggregate([R(probe="P1_reco", assistant="<script>alert(1)</script>")])
    nom, tab = _tableau(per_xss, {})
    if tab is not None:
        verif("l'entrée utilisateur est échappée dans la sortie HTML",
              "<script>" not in tab, tab[:300])

    nb = len(app.MARKETS["FR"]["probes"])
    verif("le README annonce 4 sondes par marché, le code en a 4", nb == 4, str(nb))
    readme = open("README.md", encoding="utf-8").read()
    verif("le README annonce le même nombre de workers que le code",
          f"Concurrency {app.WORKERS} workers" in readme, f"app.WORKERS={app.WORKERS}")
    source = open("app.py", encoding="utf-8").read()
    verif("l'écran n'annonce plus une durée démentie par la mesure",
          "1 à 2 min" not in source and abs(app.PROBE_SECONDS - 36.0) < 0.01,
          f"PROBE_SECONDS={app.PROBE_SECONDS}")


# ───────────────────────────── main ─────────────────────────────


def main() -> int:
    print("Lundi — batterie de tests (mode mock, hors ligne)")
    print(f"assistants={list(app.ASSISTANTS)} · juge={app.JUDGE.name} · banc v{app.PROBE_BANK_VERSION}")
    print(f"historique de test : {_TMP}")
    app.RUNS_PAR_JURE = 10**6   # neutralisé partout sauf dans t_quota_par_jure

    for fn in (t_brand_in, t_parse_json, t_aggregate, t_pool_by_market, t_action_plan,
               t_compute_deltas, t_historique, t_banc_de_sondes, t_plafond_max_calls,
               t_fuite_de_cle, t_monitor_nominal, t_monitor_entrees_invalides,
               t_monitor_sonde_en_echec, t_monitor_sans_dossier_runs, t_export_nom_de_fichier,
               t_pannes_silencieuses, t_garde_fous_publics, t_quota_par_jure,
               t_coherence_affichage):
        groupe(fn)

    total = BILAN["pass"] + BILAN["fail"]
    print("\n" + "═" * 72)
    print(f"{BILAN['pass']}/{total} assertions passent · {BILAN['fail']} échec(s) · "
          f"{BILAN['info']} observation(s)")
    if ECHECS:
        print("\nÉchecs :")
        for e in ECHECS:
            print(f"  - {e}")
        print("\nLes échecs marqués « BUG CONNU » sont documentés dans ../rapport-tests.md.")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if BILAN["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
