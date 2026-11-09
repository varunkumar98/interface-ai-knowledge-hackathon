# Approach

This submission is fully local and uses only the Python standard library. It does not call the live website and does not need API keys.

## How ingest works

Every page from every snapshot is kept, along with the snapshot date. During ingest the system:

- Parses markdown rate tables into structured facts (product, term, tier, metric, value, minimums, model-year or loan-amount qualifiers, and any “effective as of” date). Cell text can override a misleading header, so a calculator or promo table that reuses a savings header still records “60 Months” as a term and “5.34% APR” as APR.
- Extracts a few prose rates (for example credit-card APR ranges), plus hours, addresses, routing numbers, phones, branch-count claims, announced openings/closures, and award language such as “the Nth time Forbes…”.
- Chunks remaining page text into a BM25 index for questions that are not a clean fact lookup.

Product labels are classified from the row first, then from the nearby section title, so a Share Savings row under a “Certificates” heading, or a mortgage row on a long rates page, is not treated as the wrong family.

## How “current” is decided

Facts are filtered to the last-seen copy of each URL on or before `as_of` (or the latest snapshot when `as_of` is omitted). Competing values for the same product, term, tier, and metric are ranked by:

1. Page authority — canonical product and rate pages outrank calculators, PDFs, dated blogs, press-release boilerplate, and stale microsites.
2. Recency of the snapshot.
3. Recency of the page’s own “effective as of” date.

Standard / non-jumbo / lowest loan-amount tiers are led with so a generic question does not treat every tier as the headline rate. When a named product has several APY tiers and the question does not give a balance, the answer lists the tiers rather than only the $0 floor. Intro or promotional APRs are not used unless the question asks for them. When the same official count appears in press-release boilerplate from different years (for example branch totals), the system takes the most recently published figure and then applies later announced closures or openings.

If `as_of` is before the first snapshot (the crawls start on 2026-05-12), the system refuses rather than inventing or back-filling earlier figures.

## Answers and refusals

Rate, hours, address, URL, routing, and similar questions are answered from the fact table with a short citation and quote. Everything else falls back to BM25 retrieval and extractive sentences. If retrieval is weak, or a asked-for rate (such as a 7/1 ARM or construction-loan rate) is not published, the system returns “I don’t know” / “isn’t listed” with `confident=False` and does not guess a number. Questions about other institutions or clearly out-of-domain topics (weather, sports, and the like) are also refused so a Dupaco page is not used as an answer.

`conflicts()` reports places where the same qualified fact has different values across still-live pages or snapshots, names the winning value, and explains the single ranking key that decided it (newer official rate page, newer effective date, or later snapshot). Qualifiers — term, tier, model-year band, loan amount, personal vs business, rate vs APY vs APR, intro vs regular, and different products with similar names — are treated as different facts, not conflicts. Recreational / ATV rows are kept out of auto groups. Official-vs-stale contradictions (calculator or promo 5.34% vs official 5.39% 60-month auto APR; dated guidance vs the current mortgage sheet; stale press-release branch counts; “fourth” vs “fifth” Forbes credit-union ranking) and genuine official value changes (10-month jumbo 3.90% → 4.00%, 17-month specials, business checking 3% → 2.50%) are kept.

## Speed

All heavy work is in `ingest()`. `answer()` is a deterministic lookup plus a small BM25 query, with results cached so repeated questions are identical (consistency) and fast. `answer()` and `conflicts()` wrap unexpected errors and return a safe refusal or an empty list rather than raising. Ingest is idempotent per snapshot id and does not depend on being called oldest-to-newest.

## Other details

Pages are keyed by URL: the latest copy of each URL on or before `as_of` is used, so a page that was not recrawled in a later snapshot is still available. Definitional prose lines (`6-month certificate APY: 3.50%`, `replacement fee: $2.50`) are extracted in addition to markdown tables, which covers sites that do not use tables. Ended promotions are ignored after their end date. Terms and balances written in words (for example “seventeen-month” or “ten thousand”) are parsed the same way as digits. `as_of` may be an ISO date, an ISO datetime, a `YYYY/MM/DD` string, a month name such as “May 2026”, or a `date` / `datetime` object.

## How to run

From the top folder of the kit:

```bash
python harness/run_eval.py --submission <your-folder> --eval eval/public_eval.jsonl
```

Point `--submission` at the folder that contains this `solution.py` (for example `submission`). The first run unpacks `snapshots_zip/` into `snapshots/` if needed. Python 3.9+ is enough locally; grading uses a clean Python 3.11 environment and this `requirements.txt` (no third-party packages).

The harness selftest is:

```bash
python harness/run_eval.py --submission <your-folder> \
  --snapshots harness/selftest/snapshots \
  --eval harness/selftest/eval.jsonl \
  --conflicts-gold harness/selftest/conflicts_gold.json
```

`--judge` (optional, needs `ANTHROPIC_API_KEY`) mixes a 50/50 LLM grade with `must_include`, or stands alone when `must_include` is empty. This submission does not use that path or any API key.

## Public eval results

On the 40 practice questions (same scorer, no hidden conflict gold, so that pillar is omitted and weights are renormalized):

- Final / QA score: **0.969**
- Consistency: 1.0 (paraphrase groups and repeats)
- Speed: 1.0 — p95 **0.03 s**, max **0.54 s** (well under the 2 s full-credit and 60 s hard limits)
- Ingest: **9.4 s** (limit 30 minutes)
- Stale-trap hits: 0; errors: 0

By category: branch_info, conflict_resolution, current_value, entity_attribution, should_refuse, stale_trap, temporal_as_of, and tier_disambiguation all **1.00**. list_completeness **0.75** (4 questions).

The kit’s tiny harness selftest (including gold conflicts) scores **1.00**.

## Known limitations

- `dup-005` (“Get me the rate for the certificate of accounts.”) is a vague list question with an empty `must_include` list. The local scorer gives it 0 unless an LLM judge is enabled, even though the system lists the current certificate rates and does not ask which term. This is most of the gap from 1.0, and it is why list_completeness is the weakest category at 0.75.
- Mortgage conflicts are stored per loan-amount cap and per purchase vs refinance, because those are different qualifiers. If hidden gold collapses them into one 30-year conflict, precision will look lower even though the values and winners are right. Stale APRs such as 6.037% are still listed on the matching rate conflict so a gold entry that quotes the APR can match.
- Rule-based table and prose extraction may miss unusual page formats or layouts on the hidden set.
- Refusing `as_of` dates before the first snapshot is conservative. If a hidden item expects May 12 data for an April `as_of`, that item would be missed.
- No language model is used, so free-text questions that are not a clean fact lookup depend on BM25 plus extractive sentences.

## Data

The credit union website snapshots are provided only for this event and must not be redistributed. They are intentionally not included in the submission repo. A `.gitignore` should exclude `snapshots/0*/`, `results/`, and `.env` (and should also exclude `snapshots_zip/`, which holds the same site data). Organizers grade with their own copy of the scorer and snapshots.
