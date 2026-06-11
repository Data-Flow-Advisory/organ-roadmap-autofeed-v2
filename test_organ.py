"""pytest suite for the Roadmap Autofeed Organ (v2).

Verifies the pure-organ contract: deterministic, fail-safe, stdlib-only,
correct decision logic across every gate.
"""
import inspect
import json
from pathlib import Path

import pytest

from organ import (
    decide,
    decide_feed,
    queue_is_low,
    stream_correlation_id,
    build_scoping_directive,
)


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------

def _assert_valid_report(result):
    assert isinstance(result, dict)
    assert set(result.keys()) == {"output", "rationale", "self_metric"}
    out = result["output"]
    assert isinstance(out, dict)
    assert isinstance(out["fed"], list)
    assert "skipped_reason" in out
    sm = result["self_metric"]
    assert 0.0 <= sm["confidence"] <= 1.0
    assert isinstance(result["rationale"], str)


def test_decide_signature_is_state_context():
    assert list(inspect.signature(decide).parameters.keys())[:2] == ["state", "context"]


def test_decide_feed_is_alias():
    assert decide_feed is decide


def test_empty_state_is_failsafe():
    result = decide({}, {})
    _assert_valid_report(result)
    assert result["output"]["fed"] == []
    assert result["output"]["skipped_reason"] == "flag_off"


def test_none_state_is_failsafe():
    result = decide(None, None)
    _assert_valid_report(result)
    assert result["output"]["fed"] == []


# ---------------------------------------------------------------------------
# stream_correlation_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sid,expected", [
    ("stream_29", "roadmap-autofeed-stream-29"),
    ("Stream 40", "roadmap-autofeed-stream-40"),
    ("foo--bar__baz", "roadmap-autofeed-foo-bar-baz"),
])
def test_correlation_id_valid(sid, expected):
    assert stream_correlation_id(sid) == expected


@pytest.mark.parametrize("bad", ["", None, 123, "   ", "!!!", []])
def test_correlation_id_bad_inputs(bad):
    assert stream_correlation_id(bad) is None


def test_correlation_id_max_length():
    corr = stream_correlation_id("x" * 200)
    assert corr is not None and len(corr) <= 64


# ---------------------------------------------------------------------------
# queue_is_low
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count,threshold,expected", [
    (2, 5, True),
    (5, 5, False),
    (6, 5, False),
    (0, 5, True),
    (-1, 5, False),
    (None, 5, False),
    (True, 5, False),
    ("3", 5, False),
])
def test_queue_is_low(count, threshold, expected):
    assert queue_is_low(count, threshold) is expected


# ---------------------------------------------------------------------------
# build_scoping_directive
# ---------------------------------------------------------------------------

def test_directive_mentions_scope_not_execute():
    d = build_scoping_directive("stream_40", "Live Coach")
    assert "SCOPE" in d and "INITIATIVE" in d
    assert "stream_40" in d and "Live Coach" in d


def test_directive_includes_unblocks_signal():
    d = build_scoping_directive("stream_40", "Live Coach", unblocks_count=3)
    assert "Structural signal: this stream unblocks 3" in d


def test_directive_omits_unblocks_when_zero_or_bad():
    # The static body always says "it unblocks other live streams"; only the
    # appended "(Structural signal: ...)" line is conditional.
    assert "Structural signal" not in build_scoping_directive("s", "t", unblocks_count=0)
    assert "Structural signal" not in build_scoping_directive("s", "t", unblocks_count=True)
    assert "Structural signal" not in build_scoping_directive("s", "t")


# ---------------------------------------------------------------------------
# decide() — the gate sequence
# ---------------------------------------------------------------------------

def _streams(n):
    return [{"id": f"stream_{i}", "title": f"Stream {i}", "c4_unblocks_count": i} for i in range(n)]


def _ready_state(**over):
    base = {
        "enabled": True,
        "pending_count": 1,
        "queue_threshold": 5,
        "ranked_streams": _streams(5),
        "scoper_available": True,
        "scoper_name": "Sam",
        "top_k": 3,
        "existing_correlations": [],
    }
    base.update(over)
    return base


def test_gate_flag_off():
    r = decide(_ready_state(enabled=False), {})
    assert r["output"]["skipped_reason"] == "flag_off"
    assert r["self_metric"]["decision"] == "skip_flag"


def test_gate_queue_not_low():
    r = decide(_ready_state(pending_count=10), {})
    assert r["output"]["skipped_reason"] == "queue_not_low"
    assert r["self_metric"]["decision"] == "skip_queue_health"


def test_gate_no_scoper():
    r = decide(_ready_state(scoper_available=False), {})
    assert r["output"]["skipped_reason"] == "no_scoper_persona"
    assert r["self_metric"]["decision"] == "skip_scoper_missing"


def test_gate_no_structural_streams():
    r = decide(_ready_state(ranked_streams=[]), {})
    assert r["output"]["skipped_reason"] == "no_structural_streams"
    assert r["self_metric"]["decision"] == "skip_no_streams"


def test_gate_top_k_zero():
    r = decide(_ready_state(top_k=0), {})
    assert r["output"]["skipped_reason"] == "top_k_zero"
    assert r["self_metric"]["decision"] == "skip_config"


def test_feed_bounded_by_top_k():
    r = decide(_ready_state(top_k=2), {})
    _assert_valid_report(r)
    assert r["self_metric"]["decision"] == "feed"
    assert len(r["output"]["fed"]) == 2
    assert r["output"]["skipped_reason"] is None
    for item in r["output"]["fed"]:
        assert item["correlation_id"].startswith("roadmap-autofeed-")


def test_feed_dedups_existing():
    state = _ready_state(top_k=3, existing_correlations=[
        "roadmap-autofeed-stream-0",
        "roadmap-autofeed-stream-1",
    ])
    r = decide(state, {})
    fed_ids = {f["stream_id"] for f in r["output"]["fed"]}
    assert "stream_0" not in fed_ids and "stream_1" not in fed_ids
    assert r["self_metric"]["items_deduped"] == 2


def test_all_deduped_skips():
    corrs = [f"roadmap-autofeed-stream-{i}" for i in range(5)]
    r = decide(_ready_state(existing_correlations=corrs), {})
    assert r["output"]["fed"] == []
    assert r["output"]["skipped_reason"] == "all_structural_streams_already_represented"
    assert r["self_metric"]["decision"] == "skip_dedup"


def test_malformed_stream_entries_skipped():
    state = _ready_state(ranked_streams=[
        {"id": "stream_ok", "title": "OK"},
        "not-a-dict",
        {"no_id": True},
        {"id": ""},
    ])
    r = decide(state, {})
    assert r["self_metric"]["decision"] == "feed"
    assert {f["stream_id"] for f in r["output"]["fed"]} == {"stream_ok"}


def test_deterministic():
    state = _ready_state()
    assert decide(state, {}) == decide(state, {})


# ---------------------------------------------------------------------------
# Samples conform
# ---------------------------------------------------------------------------

def test_samples_conform():
    samples_dir = Path(__file__).parent / "samples"
    files = sorted(samples_dir.glob("*.json"))
    assert len(files) >= 3
    for f in files:
        data = json.loads(f.read_text())
        result = decide(data.get("state") or {}, data.get("context") or {})
        _assert_valid_report(result)
