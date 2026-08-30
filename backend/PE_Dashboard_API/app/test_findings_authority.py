from routers.final_judgment import FinalJudgmentRequest, _decision_with_reason, final_judgment
from routers.findings import FindingsRequest, _generate
from services.judgment_engine import build_batch_panel, score_all_pillars


def test_acceptable_sow_rows_score_as_compliant() -> None:
    result = score_all_pillars(sow={
        "metrics": [
            {"label": "Daily DFU", "status": "ACCEPTABLE", "pct": 70.3},
            {"label": "Daily SKU Count", "status": "ACCEPTABLE", "pct": 83.8},
        ],
    })

    assert result.scores["sow"] == 100.0
    assert result.details["sow"].base == 100.0
    baseline = next(item for item in result.evidence_chain if item["signal"] == "over_baseline")
    assert baseline["status"] == "PASS"


def test_one_window_miss_cannot_render_ready_or_no_action() -> None:
    panel = build_batch_panel({
        "window_pct": 96.6,
        "job_pct": 100.0,
        "total_days": 29,
        "breach_days": 1,
        "sla_breaches": 0,
        "exec_failures": 14,
        "fail_rate_pct": 0.8,
        "critical_findings": 0,
    })

    assert panel is not None
    assert panel["verdict"]["status"] == "AT RISK"
    assert "28 of 29" in panel["verdict"]["headline"]
    assert "all 29" not in panel["verdict"]["headline"]
    assert "No action required" not in panel["direction"]


def test_findings_count_is_the_signoff_gate() -> None:
    decision, reason = _decision_with_reason(
        score=83.2,
        pillars={"batch": 96.6, "sla": 100.0, "resource": 92.1, "sow": 100.0},
        critical_findings=2,
        loaded_count=4,
        kpi_evidence={},
    )

    assert decision == "BLOCKED"
    assert "2 CRITICAL finding(s)" in reason


def test_missing_pillars_are_coverage_not_synthetic_scores() -> None:
    result = final_judgment(FinalJudgmentRequest(
        batch={"kpis": {"compliance_pct": 96.6}},
        sla_matrix={"compliance_pct": 100.0, "total_runs": 4, "workflow_summary": []},
        resource={"servers": [{"host": "APP01", "health_score": 92.1}]},
        sow={"metrics": [{"metric": "Daily DFU", "status": "ACCEPTABLE"}]},
        findings={
            "summary": {"critical": 2},
            "findings": [
                {"level": "critical", "source": "batch"},
                {"level": "critical", "source": "sla"},
            ],
        },
    ))

    assert result.score == 96.6
    assert result.evidence_coverage_pct == 82.0
    assert result.missing_pillars == ["correlation", "benchmark"]
    assert result.pillar_statuses["batch"] == "BLOCKED"
    assert result.pillar_statuses["sla"] == "BLOCKED"
    assert result.decision == "BLOCKED"


def test_benchmark_score_uses_declared_transaction_count() -> None:
    result = score_all_pillars(benchmark={
        "total_transactions": 4,
        "degraded": 1,
        "rows": [{}, {}, {}, {}],
    })

    assert result.scores["benchmark"] == 75.0


def test_findings_do_not_default_to_100_percent_confidence() -> None:
    findings, _ = _generate(FindingsRequest())

    assert findings
    assert findings[0].confidence == 95
    assert findings[0].evidence_class == "measured"
