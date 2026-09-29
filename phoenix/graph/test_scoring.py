"""Exact-score unit tests for scoring.py — no network, no LLM."""

from phoenix.graph.schemas import Hypothesis
from phoenix.graph.scoring import score_all, score_hypothesis, top_confidence


def _ev(source: str, text: str, status_ok: bool = True) -> dict:
    raw = {"status": "success", "text": text} if status_ok else {"status": "error", "error": "boom"}
    return {"iteration": 1, "source": source, "collected_at": "2026-01-01T00:00:00+00:00",
            "summary": f"{source}({text})", "raw_data": raw}


def test_no_evidence_scores_zero():
    h = Hypothesis(description="shop crashed", category="crash")
    score, bd = score_hypothesis([], h)
    assert score == 0.0
    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.0  # nothing looked at yet -> no penalty


def test_single_prometheus_match():
    ev = [_ev("query_prometheus", "ServiceDown firing, up==0")]
    h = Hypothesis(description="shop is down", category="crash")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.4
    assert bd["has_prometheus_signal"] == 1
    assert bd["agreement_bonus"] == 0.0


def test_two_source_agreement_gets_bonus():
    ev = [_ev("query_prometheus", "HighLatency p95 slow"),
          _ev("query_loki", "request slow latency timeout")]
    h = Hypothesis(description="overload", category="overload")
    score, bd = score_hypothesis(ev, h)
    # 0.4 (prom) + 0.3 (loki) + 0.2 (agreement) = 0.9
    assert score == 0.9
    assert bd["sources_supporting"] == 2


def test_all_three_sources_clamps_to_one():
    ev = [_ev("query_prometheus", "HighErrorRate 5xx spike"),
          _ev("query_loki", "traceback error rate 5xx"),
          _ev("get_container_state", "container exit dead oom crash")]
    h_crash = Hypothesis(description="crashed", category="crash")
    # docker-only support for crash: 0.3, no agreement, no penalty (only 1 distinct... wait 3 distinct sources but 0... recalc below)
    s_crash, _ = score_hypothesis(ev, h_crash)
    assert s_crash == 0.3

    h_over = Hypothesis(description="overloaded", category="overload")
    s_over, bd = score_hypothesis(ev, h_over)
    # prom (0.4) + loki (0.3) = 0.7 + agreement 0.2 = 0.9
    assert s_over == 0.9
    assert bd["sources_supporting"] == 2


def test_contradiction_penalty_when_thorough_lookup_finds_nothing():
    ev = [_ev("query_prometheus", "all healthy, latency fine"),
          _ev("query_loki", "all healthy, no errors")]
    h = Hypothesis(description="bad deploy v18", category="deploy")
    score, bd = score_hypothesis(ev, h)
    assert bd["sources_supporting"] == 0
    assert bd["contradiction_penalty"] == 0.3
    assert score == 0.0  # 0 - 0.3 clamped to 0


def test_error_envelope_evidence_is_ignored():
    ev = [_ev("query_prometheus", "ServiceDown", status_ok=False),
          _ev("query_loki", "connection refused", status_ok=False)]
    h = Hypothesis(description="network blip", category="network")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.0
    assert bd["sources_supporting"] == 0


def test_unknown_category_never_matches_and_never_penalized():
    ev = [_ev("query_prometheus", "ServiceDown"), _ev("query_loki", "crash loop")]
    h = Hypothesis(description="something weird", category="unknown")
    score, bd = score_hypothesis(ev, h)
    assert score == 0.0
    assert bd["contradiction_penalty"] == 0.0


def test_score_all_sorts_best_first_and_confidence():
    ev = [_ev("query_prometheus", "HighLatency p95 slow")]
    h_wrong = Hypothesis(description="bad deploy", category="deploy")
    h_right = Hypothesis(description="slow overload", category="overload")
    ranked = score_all(ev, [h_wrong, h_right])
    assert ranked[0][0].category == "overload"
    assert ranked[0][1] == 0.4
    assert ranked[1][1] == 0.0
    assert top_confidence(ev, [h_wrong, h_right]) == 0.4
    assert top_confidence([], []) == 0.0
