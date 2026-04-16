from studioos.events.registry import registry


def test_all_app_events_registered() -> None:
    import studioos.events.schemas_app  # noqa: F401
    expected = [
        "app.growth.weekly_report", "app.growth.anomaly_detected",
        "app.discovery.completed", "app.experiment.proposed",
        "app.experiment.launched", "app.ceo.weekly_brief",
        "app.pricing.recommendation", "app.task.growth_intel",
        "app.task.pricing", "app.task.growth_exec",
    ]
    for event_type in expected:
        assert registry.get(event_type, 1) is not None, f"{event_type} not registered"
