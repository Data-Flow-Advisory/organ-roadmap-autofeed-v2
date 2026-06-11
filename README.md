# organ-roadmap-autofeed-v2

A pure, stdlib-only **decision organ** extracted from discovery-engine's
`app/services/roadmap_autofeed.py`.

This is a clean re-fork of `organ-roadmap-autofeed`. Per the "a failed organ
can be forked" rule, the v1 repo was not patched in place — its conformance
Action was RED because the workflow ran `python -m pytest` **without ever
installing pytest** (`No module named pytest`). v2 fixes that, exposes the
canonical `decide(state, context)` contract entry point, and tightens the
conformance gate.

## What is an organ?

A small, self-contained decision-maker conforming to the orchestrator
pure-organ contract:

```
decide(state, context) -> {"output", "rationale", "self_metric"}
```

- **Pure** — no DB, network, filesystem, env reads, or clock. Everything
  arrives via `state` / `context`.
- **Deterministic** — same input always yields the same output.
- **Fail-safe** — never raises; bad/empty input returns a valid structure with
  low `confidence` and an explanatory `rationale`.
- **Stdlib-only** — Python standard library only.
- **`self_metric.confidence`** is a float in `[0.0, 1.0]`.

## What this organ decides

Whether — and which — **structural** roadmap streams to feed into the
persona-work queue for scoping. It mirrors the live wire's invariants:

1. Only the **structural** (dependency-forced) `ranked` bucket is ever fed.
   The strategic `human_decisions` bucket is the operator's call and is
   **never** enqueued here.
2. Gated OFF by default (`enabled`), bounded by `top_k`, fires only when the
   queue is genuinely low, and dedups against in-flight `correlation_id`s.

Each fed stream is an **initiative** routed to the scoper persona (Sam, Head of
Product) to decompose — never executed as a monolith.

## Gate sequence

| Order | Condition | `skipped_reason` |
|-------|-----------|------------------|
| 1 | `enabled` is false | `flag_off` |
| 2 | queue not below threshold | `queue_not_low` |
| 3 | no active scoper persona | `no_scoper_persona` |
| 4 | structural bucket empty | `no_structural_streams` |
| 5 | `top_k <= 0` | `top_k_zero` |
| 6 | every stream already in-flight | `all_structural_streams_already_represented` |
| — | otherwise | feed up to `top_k` (skipped_reason `null`) |

## Input / output

**Input** (`state`):

```json
{
  "state": {
    "enabled": true,
    "pending_count": 1,
    "queue_threshold": 5,
    "scoper_available": true,
    "scoper_name": "Sam",
    "top_k": 3,
    "existing_correlations": ["roadmap-autofeed-stream-40"],
    "ranked_streams": [
      {"id": "stream_41", "title": "Human-in-the-loop", "c4_unblocks_count": 3}
    ]
  },
  "context": {"reason": "queue drained"}
}
```

**Output**:

```json
{
  "output": {
    "fed": [
      {"stream_id": "stream_41", "title": "Human-in-the-loop",
       "correlation_id": "roadmap-autofeed-stream-41"}
    ],
    "skipped_reason": null
  },
  "rationale": "Fed 1 structural stream(s) to 'Sam' for scoping ...",
  "self_metric": {
    "confidence": 0.95,
    "items_considered": 1,
    "items_fed": 1,
    "items_deduped": 0,
    "decision": "feed"
  }
}
```

## Usage

```bash
# As a library
python -c "from organ import decide; print(decide({'enabled': True}, {}))"

# As a CLI organ (stdin JSON in, stdout JSON out)
python organ.py < samples/low_queue_feed.json

# Or via the ORGAN_INPUT env var
ORGAN_INPUT="$(cat samples/low_queue_feed.json)" python organ.py
```

## Tests

```bash
pip install pytest
python -m pytest test_organ.py -v
```

The `conformance` GitHub Action runs the suite on Python 3.10–3.12 plus
signature / fail-safe / determinism / stdlib-only checks and exercises the
organ against every sample.

## Samples

- `samples/low_queue_feed.json` — queue low, three fresh streams → feeds top-K.
- `samples/queue_healthy_skip.json` — queue healthy → holds (`queue_not_low`).
- `samples/dedup_partial_feed.json` — two streams in-flight → feeds only the new one.
