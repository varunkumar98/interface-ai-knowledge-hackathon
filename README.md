# BankGPT knowledge-hackathon submission

This repository is the take-home submission: a standard-library Python 3.9+/3.11 knowledge system in `submission/`.

## Contents

- `submission/solution.py` — implements `build()` / `ingest()` / `answer()` / `conflicts()`
- `submission/requirements.txt` — no third-party packages
- `submission/NOTES.md` — approach, how “current” is decided, how to run, results, limitations

Website snapshots and the organizer kit are **not** in this repo and must not be redistributed.

## How to run

With the official kit (its `snapshots/` or `snapshots_zip/`, `harness/`, and `eval/`):

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
```

Optional selftest:

```bash
python harness/run_eval.py --submission submission \
  --snapshots harness/selftest/snapshots \
  --eval harness/selftest/eval.jsonl \
  --conflicts-gold harness/selftest/conflicts_gold.json
```

No API keys are required.
