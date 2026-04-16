"""QA hata-pattern sözlüğü — QA.md lines 96–101.

Pure function tests; no HTTP or network.
"""
from __future__ import annotations

from studioos.workflows.amz_qa import (
    _diagnose,
    _extract_status_from_error,
    _format_report,
)


def test_diagnose_5xx_auto_fail() -> None:
    assert "backend error" in _diagnose("http 500: foo", 500)
    assert "backend error" in _diagnose("", 502)


def test_diagnose_connection_refused() -> None:
    assert "ayakta" in _diagnose("Connection refused on 8000", None)


def test_diagnose_auth_failed() -> None:
    assert "credentials" in _diagnose("401 Unauthorized", 401)


def test_diagnose_nonetype() -> None:
    assert "initialization" in _diagnose(
        "NoneType object has no attribute get", 200
    )


def test_diagnose_not_found() -> None:
    assert "mevcut değil" in _diagnose("404 Not Found", 404)


def test_extract_status_code_from_error_string() -> None:
    assert _extract_status_from_error("http 500: boom") == 500
    assert _extract_status_from_error("http error: Connection refused") is None
    assert _extract_status_from_error(None) is None


def test_format_report_pass_shape() -> None:
    results = [
        {
            "service": "pricefinder",
            "ok": True,
            "checks": [
                {"name": "health", "ok": True},
                {"name": "/products/", "ok": True},
            ],
        }
    ]
    overall, slack, tg = _format_report(results, commit="abc1234")
    assert overall == "PASS"
    assert "✅" in slack
    assert "abc1234" in slack
    assert "@dev" not in slack


def test_format_report_fail_mentions_dev() -> None:
    results = [
        {
            "service": "buyboxpricer",
            "ok": False,
            "checks": [
                {"name": "health", "ok": True},
                {
                    "name": "/listings/",
                    "ok": False,
                    "error": "http 500: boom",
                    "status_code": 500,
                },
            ],
            "diagnoses": [
                {"check": "/listings/", "hint": "backend error — log gerekli"}
            ],
            "log_tail": "ERROR celery task crashed",
        }
    ]
    overall, slack, tg = _format_report(results, commit=None)
    assert overall == "FAIL"
    assert "@dev" in slack
    assert "500" in slack
    assert "celery task crashed" in tg
