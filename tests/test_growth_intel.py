from studioos.workflows.app_studio_growth_intel import detect_anomalies


def test_trial_zero_is_critical() -> None:
    anomalies = detect_anomalies("quit_smoking", {"trial_starts": 0, "roi": 2.0}, {}, {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0})
    assert any(a["anomaly_type"] == "critical" and a["metric_name"] == "trial_starts" for a in anomalies)


def test_low_roi_is_warning() -> None:
    anomalies = detect_anomalies("quit_smoking", {"trial_starts": 50, "roi": 0.5}, {}, {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0})
    assert any(a["anomaly_type"] == "warning" and a["metric_name"] == "roi" for a in anomalies)


def test_healthy_no_anomalies() -> None:
    anomalies = detect_anomalies("quit_smoking", {"trial_starts": 100, "roi": 2.5}, {"churn_rate": 5.0}, {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0})
    assert len(anomalies) == 0


def test_high_churn_is_warning() -> None:
    anomalies = detect_anomalies("quit_smoking", {"trial_starts": 50, "roi": 2.0}, {"churn_rate": 20.0}, {"min_roi": 1.0, "max_churn_rate": 15.0, "min_retention_d7": 20.0, "max_mrr_drop_pct": 20.0})
    assert any(a["metric_name"] == "churn_rate" for a in anomalies)
