from studioos.workflows.app_studio_marketing import flag_underperforming_countries


def test_flags_negative_roi_countries() -> None:
    data = [
        {"country": "US", "roi": 2.5, "spend": 10},
        {"country": "IT", "roi": -0.3, "spend": 5},
        {"country": "IN", "roi": -1.2, "spend": 8},
    ]
    flags = flag_underperforming_countries(data, min_roi=0)
    assert len(flags) == 2
    assert all(f["country"] in ("IT", "IN") for f in flags)


def test_healthy_countries_no_flags() -> None:
    data = [{"country": "US", "roi": 2.5, "spend": 10}, {"country": "TR", "roi": 1.5, "spend": 5}]
    flags = flag_underperforming_countries(data, min_roi=0)
    assert len(flags) == 0


def test_workflow_imports() -> None:
    from studioos.workflows.app_studio_marketing import build_graph
    from studioos.workflows.app_studio_hub_dev import build_graph as hd_graph
    assert build_graph() is not None
    assert hd_graph() is not None


def test_marketing_event_registered() -> None:
    import studioos.events.schemas_app  # noqa: F401
    from studioos.events.registry import registry
    assert registry.get("app.marketing.report", 1) is not None
