# compression_eval

Offline eval harness for `agent/context_compressor.py`. Runs a real
conversation transcript through the compressor, then probes the
compressed state with targeted questions graded on six dimensions.

## When to run

Before merging changes to:

- `agent/context_compressor.py` — any change to `_template_sections`,
  `_generate_summary`, `compress()`, or its boundary logic
- `agent/auxiliary_client.py` — when changing how compression tasks
  are routed
- `agent/prompt_builder.py` — when the compression-note phrasing
  changes

## Not for CI

This harness makes real model calls (compressor + continuation +
judge = ~3 calls per probe × probes per fixture × runs). Costs ~$0.50
to ~$1.50 per full run depending on models, takes minutes, is
LLM-graded (non-deterministic). It lives in `scripts/` and is
invoked by hand. `tests/` and `scripts/run_tests.sh` do not touch it.

`tests/scripts/test_compression_eval.py` covers the non-LLM code
paths (rubric parsing, report rendering, fixture/probe loading, PII
smoke check on the checked-in fixtures) and DOES run in CI.

## Usage

```bash
# Run all three fixtures, 3 runs each, with your configured provider
python3 scripts/compression_eval/run_eval.py

# Faster iteration — one fixture, one run
python3 scripts/compression_eval/run_eval.py \
    --fixtures=debug-session-feishu-id-model --runs=1

# Pin a cheap model for both compression + judge (recommended)
python3 scripts/compression_eval/run_eval.py \
    --compressor-provider=nous --compressor-model=openai/gpt-5.4-mini \
    --judge-provider=nous      --judge-model=openai/gpt-5.4-mini \
    --runs=3 --label=baseline

# After editing context_compressor.py, rerun with a new label and diff
python3 scripts/compression_eval/run_eval.py \
    --compressor-provider=nous --compressor-model=openai/gpt-5.4-mini \
    --judge-provider=nous      --judge-model=openai/gpt-5.4-mini \
    --runs=3 --label=my-prompt-tweak \
    --compare-to=results/baseline
```

Results land in `results/<label>/report.md` and are intended to be
pasted verbatim into PR descriptions. `--compare-to` renders a delta
column per dimension so reviewers can see "did this actually help?"
at a glance.

Rule of thumb: dimension deltas below ±0.3 are within run-to-run
noise on `runs=3`. Publish a bigger N if you want tighter bounds.

## Fixtures

Three scrubbed session snapshots live under `fixtures/`:

- `feature-impl-context-priority.json` — 75 msgs, investigate →
  patch → test → PR → merge
- `debug-session-feishu-id-model.json` — 59 msgs, PR triage +
  upstream docs + decision
- `config-build-competitive-scouts.json` — 61 msgs, iterative
  config accumulation (11 cron jobs)

Regenerate them from the maintainer's `~/.hermes/sessions/*.jsonl`
with `python3 scripts/compression_eval/scrub_fixtures.py`. The
scrubber pipeline and PII-audit checklist are documented in
`DESIGN.md` under **Scrubber pipeline**.

## Probes

One probe bank per fixture under `probes/`, 10-11 probes each,
covering all four types: **recall**, **artifact**, **continuation**,
**decision**. Each probe carries an `expected_facts` list of concrete
anchors (PR numbers, file paths, error codes, commands run) that the
judge sees alongside the assistant's answer.

## How it scores

Six dimensions, 0-5 per probe:

| Dimension             | What it measures                                     |
|-----------------------|------------------------------------------------------|
| accuracy              | File paths, function names, PR/issue numbers correct |
| context_awareness     | Reflects current session state, not a snapshot       |
| artifact_trail        | Correctly enumerates files / commands / PRs          |
| completeness          | Addresses ALL parts of the probe                     |
| continuity            | Next assistant could continue without re-fetching    |
| instruction_following | Answer in the requested form                         |

Report renders medians across N runs; probes scoring below 3.0
overall surface in a separate section with the judge's specific
complaint noted inline.

## Related

- `agent/context_compressor.py` — the thing under test
- `tests/agent/test_context_compressor.py` — structural unit tests
  that do run in CI
- `scripts/sample_and_compress.py` — the closest existing script in
  shape (offline, credential-requiring, not in CI)
- `DESIGN.md` — full architecture + methodology + open follow-ups
