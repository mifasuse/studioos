"""App Studio build event schemas + dev workflow import."""
from __future__ import annotations

from studioos.events.registry import registry


def test_build_events_registered() -> None:
    import studioos.events.schemas_app  # noqa: F401
    for event_type in ["app.build.completed", "app.build.failed", "app.qa.passed", "app.qa.failed"]:
        assert registry.get(event_type, 1) is not None, f"{event_type} not registered"


def test_dev_workflow_imports() -> None:
    from studioos.workflows.app_studio_dev import build_graph
    graph = build_graph()
    assert graph is not None
