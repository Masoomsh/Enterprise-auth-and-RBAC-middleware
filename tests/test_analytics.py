import pytest

from semantic_router.analytics import AnalyticsStore


def test_dashboard_tracks_cache_hit_cost_and_ab_quality():
    analytics = AnalyticsStore(":memory:")
    analytics.record_request({
        "request_id": "r1", "arm": "control", "category": "simple", "model": "large",
        "cache_hit": 0, "prompt_tokens": 100, "completion_tokens": 50,
        "actual_cost": 0.01, "baseline_cost": 0.01,
    })
    analytics.record_request({
        "request_id": "r2", "arm": "treatment", "category": "simple", "model": "small",
        "cache_hit": 1, "prompt_tokens": 100, "completion_tokens": 50,
        "actual_cost": 0, "baseline_cost": 0.01,
    })
    analytics.record_feedback("r2", 4.5)

    report = analytics.dashboard()
    assert report["cache_hit_rate"] == 0.5
    assert report["cost_reduction_percent"] == 50
    assert report["ab_test"]["treatment"]["average_quality_score"] == pytest.approx(4.5)
    analytics.close()
