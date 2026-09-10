# Lundi

**What AI assistants say about a brand, market by market — and what to change on Monday morning.**

Named after its only deliverable. Measuring is the easy half; the tool is judged on the plan it
hands a brand manager on Monday morning, and that plan is written by rules, not by a model.
Built for the Datawords "AI Use Case: AI Answer Monitor" test.

- Live tool: **https://ai-answer-monitor.onrender.com** — one named login per reviewer, 5 runs each;
  credentials are in the submission email. Free hosting: after 15 minutes of inactivity the first request
  takes about a minute to wake the service.
  The repo is public, the demo budget is not: publishing the password here would hand a paid API key to anyone.
- Built in one afternoon (9 Sept 2026, 14:00 → 17:00) for the Datawords "AI Use Case" test. One Python file, Gradio, two assistants probed (Mistral, OpenAI), one fixed judge.

## What it does

Enter a brand, a product category, a few markets (country/language), an optional fact sheet and known competitors. The tool:

1. **Probes** each market with a fixed bank of 4 questions written natively in the language (recommendation without naming the brand · opinion on the brand · the brand's product lines · comparison with sources), as a consumer in that country would ask an assistant.
2. **Judges** every answer with a second, JSON-only call to a single fixed judge model (the same for every assistant, so scores are comparable): presence, rank in the list, sentiment, declared sources, factual claims checked against the fact sheet (consistent / contradicted / unverifiable), favoured competitor. Presence is checked literally in code (aliases and local spellings included); the judge's verdict is only a fallback.
3. **Scores** each market × assistant and shows the raw answers, so every number can be traced to the text it came from.
4. **Writes the Monday-morning plan** with deterministic rules (absent → comparative content + Wikipedia; contradicted claim → fix the public sources models read; negative → answer the weaknesses; etc.). No model call: the plan is auditable and stable.
5. **Stores every run** (`runs/history.jsonl` + one JSON per run) and shows the delta against the previous run for the same brand/category — the month-after-month view.

A session band shows n calls, median / p95 latency, tokens and cost, so the price of one monthly run is visible.

## The three choices you asked for

**What we probe, and how it stays repeatable.** A frozen, versioned probe bank (`PROBE_BANK_VERSION`), 4 questions × market, written natively rather than machine-translated — the wording of a question moves the answer more than the model version does, so the wording is the thing we freeze. Each probe can be repeated 1–7 times in a run: the answers are stochastic — measured across four AI search engines, two simultaneous runs of the same prompt share only 33–48 % of the brands they name — 0.46–0.48 in consumer electronics and telecoms, but 0.33 in sporting goods — and 32–43 % of the sources, and the authors recommend at least 7 runs per prompt per day (8 when source coverage matters) plus a 2–4-week rolling window ([Schulte, Bleeker & Kaufmann, Univ. of St. Gallen, April 2026](https://arxiv.org/abs/2604.07585); their data was collected by scraping the engines' web interfaces, not their APIs — the very gap the first known limit below describes) — so the *stability* column tells the reader when a change is noise. Same bank, same brand, next month → comparable numbers and a delta.

**What we measure.** Presence (named at all), rank in the spontaneous recommendation, sentiment (−1..1), declared sources, and **plain factual errors** — the one metric the market tools do not offer, and the one in the brief's Japanese example. It only works with ground truth, so the brand supplies a short fact sheet; anything the sheet does not cover is reported as *unverifiable*, not guessed. **The default fact sheet in the UI is an illustrative example written from public knowledge, not validated by L'Occitane** — the mechanism is the point; a real campaign starts with the brand's own product list per market.

**What the brand does with it.** The plan is per market and per finding, phrased as an action on public sources (own site in the local language, Wikipedia/Wikidata, retailer listings, local reviews/tests), because that is what assistants read. It is meant for an e-commerce director and their SEO agency, not for a data team.

## Architecture

```
inputs ─► probe bank (frozen, native) ─► answer call per assistant (Mistral ministral-8b, OpenAI gpt-5.6-luna; temp 0.7, consumer persona per market)
                                       ─► judge call, single fixed judge (OpenAI, temp 0, JSON) + literal presence check in code
                                       ─► per market × assistant aggregation ─► pooled per market ─► rules ─► Monday plan
                                       ─► runs/history.jsonl ─► delta vs previous run
```

- `ASSISTANTS` is built from the keys present in `.env`: one `Assistant` per provider (OpenAI-compatible endpoint, Anthropic, or `mock` for tests without a key). Adding a third assistant is one entry.
- Concurrency 4 workers, retries with back-off. Measured on 9 Sept 2026 from the providers' own
  rate-limit headers: the free Mistral tier allows 188 req/min and 625k tokens/min, so a default run
  uses under 4 % of it — 4 workers is safe. The estimator uses a deliberately conservative 36 s per probe
  (one measured call: 16 s answer + 20 s judge) and so announces ~4 min for a default run; the medians of the
  reference run are lower, 14.2 s and 12.5 s.
  A 90 s HTTP timeout is set and the SDKs' own silent retries are disabled (`max_retries=0`), so the call and
  cost counters on screen match what was actually billed.
- **Failures are shown, never absorbed.** A probe that fails does not kill the run — the others are scored and a
  banner says how many were lost. An empty answer is treated as a failed probe, not as "brand absent": it would
  otherwise lower the headline number invisibly. A judge reply that is not valid JSON is counted and reported,
  because a silently neutral verdict erases real factual errors.
- No database, no framework beyond Gradio: half a day of work, and a reviewer can read the whole thing.

## Run it

```bash
git clone <repo> && cd ai-answer-monitor
cp .env.example .env            # add MISTRAL_API_KEY and/or OPENAI_REAL_API_KEY
pip install -r requirements.txt
python app.py                   # http://127.0.0.1:7860
python test_app.py              # 251-assertion test battery — offline, no API key needed
python eval_judge.py            # judge smoke test on 6 hand-labelled answers
```

Deployed on Render (free plan), with the `.env` keys set as service environment variables.

Prices shown in the session band are read from `.env`, never hard-coded, so they can be re-checked before each
campaign: Mistral `ministral-8b-latest` $0.15/$0.15 per M tokens (mistral.ai/pricing/api, read 9 Sept 2026);
OpenAI `gpt-5.6-luna` $0.20/$1.20 per M tokens, standard short-context tier
(developers.openai.com/api/docs/pricing, read 9 Sept 2026). Two things move the real bill: the same model is
billed $0.40/$1.80 above the short-context threshold — a probe never gets close, a much larger fact sheet would —
and the API billed 900 reasoning tokens out of 1,841 output tokens on one measured call — roughly half of what
you pay for on output is never displayed.
Both figures come from the provider's own pricing page; the factor-2 gap between quotes found elsewhere is the
short- versus long-context tier, plus a half-price batch tier.

## Running this in public without handing over your API bill

The app is a public URL backed by a paid key, so abuse is a design constraint, not an afterthought.

- **One named login per reviewer** (`DEMO_ACCOUNTS="name:password,…"`), sent in the submission email and
  never in this repo — so access is nominative rather than a shared secret anyone can pass on.
- **A static quota of runs per login** (`RUNS_PER_USER`, 5). The count and what is left are shown on screen
  after every run, and a refused run costs no quota. Nothing silently degrades: the limit is stated, not hidden.
- **`MAX_RUN_CALLS`** (200) — a single run cannot exceed this; the estimator shows the cost before the click.
- **`MAX_CALLS`** (600) and **`MAX_SESSION_COST`** ($2) — the instance stops and says so, rather than spending on.
- **Input fields are bounded** (`MAX_FACTS_CHARS` 4000, `MAX_FIELD_CHARS` 200) and truncation is announced on
  screen. The fact sheet is resent with *every* judge call, so an unbounded paste is the real cost amplifier.
- **One run at a time per instance** (a semaphore): without it, N visitors start N pools of `WORKERS` threads,
  which means rate-limit errors for everyone and an N-fold bill.
- **90 s HTTP timeout, SDK retries disabled**, so a hung call cannot pin a worker and no call is billed unseen.
- Markets and assistants are validated against the server-side registries, never trusted from the request.

These caps reset when the instance restarts. The backstop that does not is a **hard spend limit on the
provider side**: set a monthly budget on the OpenAI project and use a restricted key dedicated to this instance.

## Known limits — read before trusting a number

- **We query a model's API, not the consumer product.** ChatGPT, Perplexity or Google's AI Mode add web retrieval and geolocation; their answers can differ from the raw model — market tools such as Peec state that they scrape the interface for that reason ([Peec docs](https://docs.peec.ai/intro-to-peec-ai)). The architecture accepts those APIs as extra adapters; the free budget of this test did not.
- **Two models, both small and cheap** (`ministral-8b-latest`, `gpt-5.6-luna`), chosen for the budget of a test afternoon. Flagship models and Gemini/Anthropic are one adapter each; the judge should stay the same model across months.
- **Sources are declared, not verified.** A model without browsing names what it believes it relies on — a signal of influence, not a bibliography. With a browsing assistant, citations become real URLs and this column becomes the most valuable one (see the option set aside below).
- **The judge is a model too** (OpenAI, and it also judges its own answers as an assistant — a bias to keep in mind; the literal presence check and the raw answers are there to audit it). `eval_judge.py` checks it on 6 labelled answers — a smoke test, ±17 points per miss. A real deployment needs ~50 labelled answers per language before conclusions are drawn.
- **Small n.** A default run is 3 markets × 4 probes × 2 assistants × 1 repetition = 24 answers. Differences under ~20 points between two markets or two months are noise; the UI says so. A monthly run should use 7 repetitions (see above).
- **The fact sheet has to be a genuinely closed list, and that is easy only for narrow catalogues.** Tested on a second brand (Longchamp, handbags, 24 answers): a hand-written list of bag collections missed real seasonal lines — Le Pliage Cuir / Green / Filet, Box-Trot, Cavalcade, Roseau Essential — so the tool reported real products as invented. Rewriting the sheet halved the false errors (FR 13 → 6) but did not remove them. The factual-error metric is trustworthy where a brand can state a short, stable range (L'Occitane's seven hand-cream lines); a wide seasonal catalogue needs the brand's own product feed, not a typed paragraph. The tool never guesses either way: anything the sheet does not cover is reported as *unverifiable*, and the run-to-run delta makes a sheet correction visible immediately.
- **Category wording** is inserted as typed; write it in the target language (or in English, and accept a small bias) — the probe bank is native, the category is not translated.
- **History is a file** (`runs/`). On the hosted instance it resets on restart (the free Render plan has no persistent disk); for monthly use, point `HISTORY_PATH` at a mounted disk.

## Options considered and set aside

- **Buy a GEO monitoring tool** (Profound from $99/mo, Otterly from $29/mo, Semrush AI toolkit $99/mo — public pricing pages, 9 Sept 2026). They cover presence / rank / sentiment / citations across several assistants, and would be the right *probe* for production. None checks factual errors against a fact sheet, none writes a per-market action plan, and multi-country is enterprise pricing on quote. For an agency running 25 markets, the differentiating layer is the one built here; the probe can be bought later and plugged in.
- **Scrape the consumer interfaces** (ChatGPT, Perplexity web). Closest to what shoppers see, but against terms of use, brittle, and not half-a-day work.
- **A one-shot "GEO audit" chat** (ask the model what it thinks and what to fix). Fast to build, impossible to repeat or compare month to month: it measures nothing.
- **Probing a search-grounded API first** (Perplexity API, Gemini with Google Search grounding, OpenAI web search). It would turn "sources" into real citations and is the first thing to add. Set aside for this afternoon because grounded answers vary with the live web, which makes the *repeatability* question harder to demonstrate in half a day, and because it adds a third billing account; the adapter is the same `Assistant` class.

## Example run

`examples/run_2026-09-09_loccitane.json` is the full export of the run behind the numbers quoted in the cover email: 3 markets × 4 probes × 2 assistants × 1 repetition = 24 answers, judged by `gpt-5.6-luna`, 48 API calls, ≈$0.06. (The $0.13 quoted in the cover email was the session total for two consecutive runs — the on-screen band now separates the two.) It contains every raw answer and every verdict, so each figure can be traced.

## With two more days

1. Assistants with retrieval (Gemini with search grounding, OpenAI web search, Perplexity API) → sources become real citations.
2. 50 labelled answers per language for the judge; inter-annotator check with a native reviewer.
3. Persistent history + a trend chart per market; monthly scheduled run and an emailed digest.
4. Brand fact sheet built from the official site per market instead of typed by hand.

## Hypotheses made (and how they are handled)

- H1 — "AI assistants" can be approximated by model APIs (two here) for a first monitor → stated in the UI and above.
- H2 — the brand can provide a short, reliable fact sheet per category → required input, with a worked example.
- H3 — a market is a country/language pair and the assistant is prompted with a consumer persona in that country → `MARKETS` table, one line per market to add one.

Questions I would ask before a second iteration: which assistants matter to your clients' customers (ChatGPT, Gemini, Perplexity, Copilot, regional ones in Asia)? Is there a pilot brand with official product data per market? Who receives the plan — the brand, its SEO agency, or Datawords?
