#!/usr/bin/env python3
"""Roadmap Autofeed Organ (v2).

A pure, stdlib-only decision organ extracted from discovery-engine's
``app/services/roadmap_autofeed.py``.

CONTRACT (orchestrator pure-organ protocol)
--------------------------------------------
    decide(state, context) -> {"output", "rationale", "self_metric"}

  * Pure: no DB, no network, no filesystem, no env reads, no clock — every
    input arrives via ``state`` / ``context``.
  * Deterministic: same (state, context) always yields the same result.
  * Fail-safe: never raises. Bad/empty input returns a valid structure with a
    low ``confidence`` and an explanatory ``rationale``.
  * Stdlib-only: imports nothing outside the Python standard library.
  * ``self_metric.confidence`` is a float in ``[0.0, 1.0]``.

WHAT THIS ORGAN DECIDES
-----------------------
Whether (and which) STRUCTURAL roadmap streams should be fed into the
persona-work queue for scoping. It mirrors the live wire's load-bearing
invariants:

  * Only the **structural** (dependency-forced) ``ranked`` bucket is ever fed.
    The strategic ``human_decisions`` bucket is the operator's call and is
    NEVER enqueued here.
  * Gated OFF by default (``enabled``), bounded by ``top_k``, fires only when
    the queue is genuinely low, and dedups against in-flight correlation ids.

The CLI (``python organ.py < input.json``) reads ``{state, context}`` on
stdin (or ``$ORGAN_INPUT``) and writes ``{output, rationale, self_metric}`` to
stdout, so the orchestrator can shell out to it like any other organ.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

# correlation_id prefix — the dedup key. Matches the live wire.
_CORRELATION_PREFIX = "roadmap-autofeed-"
_CORRELATION_MAX_LEN = 64  # the LocalCronWorkItem.correlation_id column cap.

# Defaults applied when state omits a field. Kept identical to the live wire's
# in-code defaults so the organ's decision matches production.
_DEFAULT_QUEUE_THRESHOLD = 5
_DEFAULT_TOP_K = 3
_DEFAULT_SCOPER_NAME = "Sam"


# ---------------------------------------------------------------------------
# Pure helpers — no IO, deterministic, testable in isolation.
# ---------------------------------------------------------------------------

def stream_correlation_id(stream_id: Any) -> Optional[str]:
    """Derive the dedup ``correlation_id`` for a roadmap stream.

    ``f"roadmap-autofeed-<slug>"`` where the slug is the stream id lowercased
    with non-alphanumerics collapsed to single hyphens (so ``"stream_29"`` →
    ``"roadmap-autofeed-stream-29"``), truncated to the 64-char column cap.
    Returns ``None`` for an empty / non-string id so the caller skips it rather
    than minting a degenerate key.
    """
    if not stream_id or not isinstance(stream_id, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", stream_id.strip().lower()).strip("-")
    if not slug:
        return None
    return (_CORRELATION_PREFIX + slug)[:_CORRELATION_MAX_LEN]


def queue_is_low(pending_count: Any, threshold: int) -> bool:
    """Return True iff ``pending_count`` is below ``threshold``.

    Defensive: every malformed input returns False (the SAFE direction — a
    broken count must NOT trigger auto-feeding). None, bool, non-int and
    negative counts all return False.
    """
    if pending_count is None or isinstance(pending_count, bool):
        return False
    if not isinstance(pending_count, int):
        return False
    if pending_count < 0:
        return False
    return pending_count < threshold


def build_scoping_directive(stream_id: str, title: str,
                            unblocks_count: Optional[int] = None) -> str:
    """Compose the scoping directive text for one structural stream.

    Pure string builder. The directive is explicit that a stream is an
    INITIATIVE to be decomposed by the scoper persona, never executed whole.
    """
    title = (title or stream_id or "this roadmap stream").strip()
    sid = (stream_id or "").strip()
    lines = [
        f"Roadmap stream **{title}** ({sid}) surfaced as a STRUCTURAL "
        "(dependency-forced) priority in the C4 roadmap ranking: it unblocks "
        "other live streams, so it is safe to act on without a strategic call.",
        "",
        "This stream is a big INITIATIVE, NOT an atomic task. Your job is to "
        "SCOPE it — decompose it into concrete, CLEAR sub-task work items and "
        "spawn each one via POST /api/v1/persona-work, routed to the right "
        "persona. Do NOT enqueue or execute the whole stream as one monolith, "
        "and do NOT scope any other stream.",
        "",
        "Carry this item's correlation_id onto every sub-task you spawn so the "
        "whole initiative stays on one trace.",
    ]
    if isinstance(unblocks_count, int) and not isinstance(unblocks_count, bool) and unblocks_count > 0:
        lines.append("")
        lines.append(
            f"(Structural signal: this stream unblocks {unblocks_count} other live stream(s).)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The decision — the pure organ entry point.
# ---------------------------------------------------------------------------

def _empty_report() -> Dict[str, Any]:
    return {
        "output": {"fed": [], "skipped_reason": None},
        "rationale": "",
        "self_metric": {
            "confidence": 0.0,
            "items_considered": 0,
            "items_fed": 0,
            "items_deduped": 0,
            "decision": "hold",
        },
    }


def decide(state: Optional[Dict[str, Any]],
           context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide whether and which structural roadmap streams to feed.

    The canonical pure-organ contract function: ``decide(state, context)``.

    ``state`` keys (all optional; defensive defaults applied):
      - ``enabled`` (bool)            — master flag; default False.
      - ``pending_count`` (int)       — current queue depth; default -1.
      - ``queue_threshold`` (int)     — feed only when pending < this; default 5.
      - ``ranked_streams`` (list)     — structural streams, each
        ``{"id", "title", "c4_unblocks_count"}``; default [].
      - ``scoper_available`` (bool)   — is the scoper persona active; default False.
      - ``scoper_name`` (str)         — display name; default "Sam".
      - ``top_k`` (int)               — max streams fed per event; default 3.
      - ``existing_correlations`` (list[str]) — in-flight correlation ids to
        dedup against; default [].

    Returns ``{output, rationale, self_metric}``. Never raises.
    """
    report = _empty_report()
    try:
        if not isinstance(state, dict):
            state = {}

        enabled = bool(state.get("enabled", False))
        pending_count = state.get("pending_count", -1)
        queue_threshold = state.get("queue_threshold", _DEFAULT_QUEUE_THRESHOLD)
        if not isinstance(queue_threshold, int) or isinstance(queue_threshold, bool):
            queue_threshold = _DEFAULT_QUEUE_THRESHOLD
        ranked_streams = state.get("ranked_streams", [])
        if not isinstance(ranked_streams, list):
            ranked_streams = []
        scoper_available = bool(state.get("scoper_available", False))
        scoper_name = (state.get("scoper_name") or _DEFAULT_SCOPER_NAME)
        if not isinstance(scoper_name, str) or not scoper_name.strip():
            scoper_name = _DEFAULT_SCOPER_NAME
        top_k = state.get("top_k", _DEFAULT_TOP_K)
        if not isinstance(top_k, int) or isinstance(top_k, bool):
            top_k = _DEFAULT_TOP_K
        raw_corr = state.get("existing_correlations", [])
        existing = {c for c in raw_corr if isinstance(c, str)} if isinstance(raw_corr, list) else set()

        # Gate 1 — master flag.
        if not enabled:
            report["output"]["skipped_reason"] = "flag_off"
            report["rationale"] = "Roadmap autofeed is disabled (flag_off); no streams fed."
            report["self_metric"].update({"confidence": 0.95, "decision": "skip_flag"})
            return report

        # Gate 2 — queue must be genuinely low (backfill-on-idle only).
        if not queue_is_low(pending_count, queue_threshold):
            report["output"]["skipped_reason"] = "queue_not_low"
            report["rationale"] = (
                f"Queue depth {pending_count} is not below threshold {queue_threshold}; "
                "backfill only fires when the queue is genuinely low."
            )
            report["self_metric"].update({"confidence": 0.95, "decision": "skip_queue_health"})
            return report

        # Gate 3 — an active scoper persona must exist to decompose streams.
        if not scoper_available:
            report["output"]["skipped_reason"] = "no_scoper_persona"
            report["rationale"] = (
                f"No active scoper persona '{scoper_name}' is available to scope streams; "
                "feeding would strand the items, so we hold."
            )
            report["self_metric"].update({"confidence": 0.95, "decision": "skip_scoper_missing"})
            return report

        # Gate 4 — structural bucket must be non-empty.
        if not ranked_streams:
            report["output"]["skipped_reason"] = "no_structural_streams"
            report["rationale"] = "The structural (ranked) bucket is empty; nothing to feed."
            report["self_metric"].update({"confidence": 0.9, "decision": "skip_no_streams"})
            return report

        # Gate 5 — top_k bound (0 is a deliberate park).
        if top_k <= 0:
            report["output"]["skipped_reason"] = "top_k_zero"
            report["rationale"] = f"top_k={top_k} parks feeding; no streams fed."
            report["self_metric"].update({"confidence": 0.95, "decision": "skip_config"})
            return report

        # Feed loop — top_k structural streams not already represented.
        fed: List[Dict[str, str]] = []
        deduped = 0
        considered = 0
        for stream in ranked_streams:
            if len(fed) >= top_k:
                break
            stream_id = stream.get("id") if isinstance(stream, dict) else None
            corr = stream_correlation_id(stream_id)
            if not corr:
                continue  # malformed stream entry — skip, don't count.
            considered += 1
            if corr in existing:
                deduped += 1
                continue
            title = stream.get("title") or stream_id
            fed.append({
                "stream_id": stream_id,
                "title": title,
                "correlation_id": corr,
            })

        report["output"]["fed"] = fed
        report["self_metric"]["items_considered"] = considered
        report["self_metric"]["items_fed"] = len(fed)
        report["self_metric"]["items_deduped"] = deduped

        if fed:
            report["output"]["skipped_reason"] = None
            report["rationale"] = (
                f"Fed {len(fed)} structural stream(s) to '{scoper_name}' for scoping "
                f"(queue depth {pending_count} < threshold {queue_threshold}, "
                f"{deduped} deduped)."
            )
            report["self_metric"].update({"confidence": 0.95, "decision": "feed"})
        else:
            report["output"]["skipped_reason"] = "all_structural_streams_already_represented"
            report["rationale"] = (
                f"All {considered} structural stream(s) already have in-flight scoping "
                "items (dedup gate); none fed."
            )
            report["self_metric"].update({"confidence": 0.9, "decision": "skip_dedup"})
        return report

    except Exception as exc:  # fail-safe — a broken decision must never raise.
        return {
            "output": {"fed": [], "skipped_reason": "error_fail_open"},
            "rationale": f"decide() failed open: {exc}",
            "self_metric": {
                "confidence": 0.0,
                "items_considered": 0,
                "items_fed": 0,
                "items_deduped": 0,
                "decision": "error",
                "error": str(exc),
            },
        }


# Backward-compatible alias for callers expecting the v1 name.
decide_feed = decide


# ---------------------------------------------------------------------------
# CLI adapter — stdin JSON in, stdout JSON out. Not part of the pure contract.
# ---------------------------------------------------------------------------

def _read_input() -> Dict[str, Any]:
    text = sys.stdin.read()
    if not text.strip():
        import os
        text = os.getenv("ORGAN_INPUT", "{}")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def main() -> int:
    try:
        data = _read_input()
        result = decide(data.get("state") or {}, data.get("context") or {})
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        json.dump({
            "output": {"fed": [], "skipped_reason": "error_fail_open"},
            "rationale": f"CLI fatal error: {exc}",
            "self_metric": {"confidence": 0.0, "decision": "error", "error": str(exc)},
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
