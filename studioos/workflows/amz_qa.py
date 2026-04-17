"""amz_qa workflow — full smoke test per OpenClaw QA.md.

Beyond the bare /health ping, this workflow exercises each service's
auth flow and a handful of high-signal endpoints:

  - PriceFinder: /auth/login, /products/, /opportunities/
  - BuyBoxPricer: /auth/login, /listings/, /competitors/
  - AdsOptimizer: /auth/login, /campaigns/, /accounts/
  - EbayCrossLister: /auth/login, /amazon/inventory, /listings/

QA.md rules enforced:
  - 500/502 = automatic FAIL (http.get_json returns non-ok).
  - On FAIL, pull the service's celery log endpoint (lines=50,
    search=error) and include the tail in the report.
  - FAIL report mentions @dev so the Dev agent picks it up.
  - Hata pattern sözlüğü (NoneType / connection refused / auth failed
    / 404 / 500) translated into a diagnosis hint.

No-data / unknown passwords are fine — those checks are recorded as
SKIPPED and don't flip the overall verdict to FAIL.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from studioos.approvals.escalation import classify, to_approval_row
from studioos.config import settings
from studioos.logging import get_logger
from studioos.runtime.workflow_registry import register_workflow
from studioos.tools import invoke_from_state

log = get_logger(__name__)


def _svc(
    name: str,
    base: str,
    username: str,
    password: str,
    health_url: str,
    login_path: str,
    endpoints: list[str],
    log_url: str,
    login_format: str = "json",
    login_user_key: str = "email",
) -> dict[str, Any]:
    return {
        "name": name,
        "base": base.rstrip("/"),
        "username": username,
        "password": password,
        "health_url": health_url,
        "login_path": login_path,
        "endpoints": endpoints,
        "log_url": log_url,
        "login_format": login_format,
        "login_user_key": login_user_key,
    }


def _services() -> list[dict[str, Any]]:
    """Build the service matrix from settings.

    Endpoints mirror OpenClaw QA.md kritik endpoint listesi.
    """
    return [
        _svc(
            name="pricefinder",
            base="http://pricefinder-backend:8000/api/v1",
            username=settings.pricefinder_username,
            password=settings.pricefinder_password,
            health_url="http://pricefinder-backend:8000/api/health",
            login_path="/auth/token",
            endpoints=["/products/?limit=1", "/opportunities/?limit=1"],
            log_url="http://pricefinder-backend:8000/api/v1/scrapers/logs/celery?lines=50&search=error",
            login_format="form",
            login_user_key="username",
        ),
        _svc(
            name="buyboxpricer",
            base="http://buyboxpricer-backend:8000/api/v1",
            username=settings.buyboxpricer_username,
            password=settings.buyboxpricer_password,
            health_url="http://buyboxpricer-backend:8000/api/v1/health",
            login_path="/auth/login",
            endpoints=["/listings/?limit=1", "/competitors/?limit=1"],
            log_url="http://buyboxpricer-backend:8000/api/v1/logs/celery?lines=50&search=error",
            login_format="json",
            login_user_key="email",
        ),
        _svc(
            name="adsoptimizer",
            base="http://adsoptimizer-backend:8000/api/v1",
            username=settings.adsoptimizer_username,
            password=settings.adsoptimizer_password,
            health_url="http://adsoptimizer-backend:8000/health",
            login_path="/auth/login",
            endpoints=["/campaigns/?limit=1", "/accounts/"],
            log_url="http://adsoptimizer-backend:8000/api/v1/logs/celery?lines=50&search=error",
            login_format="json",
            login_user_key="email",
        ),
        _svc(
            name="ebaycrosslister",
            base="http://ebaycrosslister-backend:8000/api/v1",
            username=settings.ebaycrosslister_username,
            password=settings.ebaycrosslister_password,
            health_url="http://ebaycrosslister-backend:8000/health",
            login_path="/auth/login",
            endpoints=["/amazon/inventory?limit=1", "/listings/?limit=1"],
            log_url="http://ebaycrosslister-backend:8000/api/v1/logs/celery?lines=50&search=error",
            login_format="json",
            login_user_key="email",
        ),
    ]


class QAState(TypedDict, total=False):
    agent_id: str
    studio_id: str
    correlation_id: str
    run_id: str
    state: dict[str, Any]
    trigger_type: str
    input: dict[str, Any]
    goals: dict[str, Any]
    results: list[dict[str, Any]]
    events: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    kpi_updates: list[dict[str, Any]]
    summary: str


def _extract_status_from_error(err: str | None) -> int | None:
    if not err:
        return None
    import re as _re

    m = _re.search(r"http (\d{3})", err)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _diagnose(err: str, status: int | None) -> str:
    """QA.md hata pattern sözlüğü — turn a raw error into a root-cause hint."""
    low = (err or "").lower()
    if status in (500, 502, 503, 504):
        return "backend error — log gerekli"
    if "connection refused" in low or "connect call failed" in low:
        return "servis ayakta değil veya port yanlış"
    if "authentication" in low or "401" in low or "unauthorized" in low:
        return "credentials hatası (API_ACCESS.md güncel mi?)"
    if "nonetype" in low:
        return "backend initialization eksik (None döndü)"
    if "404" in low or "not found" in low:
        return "endpoint mevcut değil"
    if "timeout" in low:
        return "timeout — servis yavaş veya down"
    return "bilinmeyen hata, log okunmalı"


async def _http_get(
    state: QAState,
    url: str,
    token: str | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    args: dict[str, Any] = {"url": url, "timeout_seconds": timeout}
    if token:
        args["headers"] = {"Authorization": f"Bearer {token}"}
    return await invoke_from_state(state, "http.get_json", args)


async def _http_post(
    state: QAState,
    url: str,
    body: dict[str, Any],
    timeout: float = 8.0,
) -> dict[str, Any]:
    return await invoke_from_state(
        state,
        "http.post_json",
        {"url": url, "json": body, "timeout_seconds": timeout},
    )


async def _check_service(
    state: QAState, svc: dict[str, Any]
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    token: str | None = None

    # 1. Health ping
    health_url = svc["health_url"]
    h = await _http_get(state, health_url)
    health_ok = h["status"] == "ok"
    health_status = (
        (h.get("data") or {}).get("status_code")
        if health_ok
        else _extract_status_from_error(h.get("error"))
    )
    checks.append(
        {
            "name": "health",
            "url": health_url,
            "ok": health_ok,
            "status_code": health_status,
            "error": h.get("error") if not health_ok else None,
        }
    )
    if health_status in (500, 502, 503, 504):
        # QA.md: 500/502 → auto FAIL, skip remaining endpoint tests.
        return {"service": svc["name"], "checks": checks, "token": None}

    # 2. Auth (only if we have creds)
    if svc["username"] and svc["password"]:
        login_url = svc["base"] + svc["login_path"]
        user_key = svc.get("login_user_key", "email")
        if svc.get("login_format") == "form":
            a = await invoke_from_state(
                state,
                "http.post_form",
                {
                    "url": login_url,
                    "data": {user_key: svc["username"], "password": svc["password"]},
                    "timeout_seconds": 8,
                },
            )
        else:
            a = await _http_post(
                state,
                login_url,
                {user_key: svc["username"], "password": svc["password"]},
            )
        auth_ok = a["status"] == "ok"
        token = ((a.get("data") or {}).get("body") or {}).get("access_token") if auth_ok else None
        checks.append(
            {
                "name": "auth",
                "url": login_url,
                "ok": auth_ok and bool(token),
                "status_code": (a.get("data") or {}).get("status_code"),
                "error": a.get("error") if not auth_ok else (
                    None if token else "login ok but no access_token"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "auth",
                "url": svc["base"] + svc["login_path"],
                "ok": True,
                "skipped": True,
                "error": None,
            }
        )

    # 3. Endpoints (auth'd if we got a token, else anonymous attempt)
    for path in svc["endpoints"]:
        url = svc["base"] + path
        r = await _http_get(state, url, token=token)
        ok = r["status"] == "ok"
        sc = (
            (r.get("data") or {}).get("status_code")
            if ok
            else _extract_status_from_error(r.get("error"))
        )
        if sc in (500, 502, 503, 504):
            ok = False
        checks.append(
            {
                "name": path,
                "url": url,
                "ok": ok,
                "status_code": sc,
                "error": r.get("error") if not ok else None,
            }
        )

    return {"service": svc["name"], "checks": checks, "token": token}


async def _fetch_log_tail(
    state: QAState, svc: dict[str, Any], token: str | None
) -> str | None:
    url = svc["log_url"]
    r = await _http_get(state, url, token=token, timeout=12.0)
    if r["status"] != "ok":
        return None
    body = (r.get("data") or {}).get("body")
    if isinstance(body, dict):
        lines = body.get("lines") or body.get("log") or body.get("logs")
        if isinstance(lines, list):
            return "\n".join(str(x) for x in lines[-15:])
        if isinstance(lines, str):
            return lines[-2000:]
    if isinstance(body, str):
        return body[-2000:]
    return None


async def node_check(state: QAState) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for svc in _services():
        svc_result = await _check_service(state, svc)
        failed = [c for c in svc_result["checks"] if not c.get("ok")]
        svc_result["failed_count"] = len(failed)
        svc_result["ok"] = len(failed) == 0
        # On failure, pull the celery log tail for Dev diagnosis.
        if failed:
            tail = await _fetch_log_tail(state, svc, svc_result.get("token"))
            svc_result["log_tail"] = tail
            svc_result["diagnoses"] = [
                {
                    "check": c["name"],
                    "hint": _diagnose(
                        c.get("error") or "", c.get("status_code")
                    ),
                }
                for c in failed
            ]
        results.append(svc_result)
    return {"results": results}


def _escape_tg(s: str) -> str:
    """Escape characters that break Telegram MarkdownV1."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, "")
    return s


def _format_report(
    results: list[dict[str, Any]], commit: str | None
) -> tuple[str, str, str]:
    """Return (overall, slack_text, telegram_text)."""
    total = sum(len(r["checks"]) for r in results)
    passed = sum(
        1 for r in results for c in r["checks"] if c.get("ok")
    )
    failed = total - passed
    overall = "PASS" if failed == 0 else "FAIL"
    icon = "✅" if failed == 0 else "🚨"
    header_commit = f" `{commit}`" if commit else ""
    lines = [
        f"{icon} *AMZ QA — Smoke {overall}*{header_commit}",
        f"_Endpoints: {passed}/{total} ok_",
        "",
    ]
    for r in results:
        svc = r["service"]
        ok_marks = sum(1 for c in r["checks"] if c.get("ok"))
        total_svc = len(r["checks"])
        status_icon = "✓" if r["ok"] else "✗"
        lines.append(f"{status_icon} *{svc}* ({ok_marks}/{total_svc})")
        for c in r["checks"]:
            if c.get("ok"):
                continue
            err = _escape_tg((c.get("error") or "")[:100])
            sc = c.get("status_code")
            sc_str = f" [{sc}]" if sc else ""
            lines.append(f"  ✗ {c['name']}{sc_str} — {err}")
        for d in r.get("diagnoses") or []:
            hint = _escape_tg(d["hint"])
            lines.append(f"  💡 {d['check']}: {hint}")
        if r.get("log_tail"):
            tail = _escape_tg(r["log_tail"][-300:])
            lines.append(f"  {tail}")
    if failed:
        lines.append("")
        lines.append("@dev fix gerekli")
    text = "\n".join(lines)
    return overall, text, text


async def node_report(state: QAState) -> dict[str, Any]:
    results = state.get("results") or []
    inp = state.get("input") or {}
    payload = inp.get("payload") or {}
    commit = payload.get("commit")

    overall, slack_text, tg_text = _format_report(results, commit)
    failed_services = [r["service"] for r in results if not r["ok"]]

    notify = await invoke_from_state(
        state,
        "telegram.notify",
        {
            "text": tg_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
    )
    # Only post to Slack on FAIL — PASS is Telegram-only to reduce noise
    if failed_services:
        slack = await invoke_from_state(
            state,
            "slack.notify",
            {"text": slack_text, "mrkdwn": True, "unfurl_links": False},
        )
    else:
        slack = {"status": "skipped"}

    state_accum = dict(state.get("state") or {})
    state_accum["smoke_runs_total"] = (
        int(state_accum.get("smoke_runs_total", 0)) + 1
    )
    if failed_services:
        state_accum["last_fail_services"] = failed_services

    events: list[dict[str, Any]] = []
    approvals_out: list[dict[str, Any]] = []
    # Prod-down classification: any service where the health check
    # itself failed is treated as a prod-down incident (CEO + Nuri).
    health_down = [
        r for r in results
        if any(
            c.get("name") == "health" and not c.get("ok")
            for c in r.get("checks") or []
        )
    ]
    if health_down:
        esc = classify("prod_down_incident")
        for r in health_down:
            approvals_out.append(
                to_approval_row(
                    esc,
                    reason=(
                        f"QA: {r['service']} prod-down — health check failed"
                    ),
                    payload={
                        "service": r["service"],
                        "commit": commit,
                        "diagnoses": r.get("diagnoses") or [],
                        "log_tail": (r.get("log_tail") or "")[:2000],
                    },
                    expires_in_seconds=60 * 60 * 2,
                )
            )
    if failed_services:
        events.append(
            {
                "event_type": "amz.qa.smoke_failed",
                "event_version": 1,
                "payload": {
                    "failed_services": failed_services,
                    "commit": commit,
                    "details": [
                        {
                            "service": r["service"],
                            "failed_count": r.get("failed_count", 0),
                            "diagnoses": r.get("diagnoses") or [],
                        }
                        for r in results
                        if not r["ok"]
                    ],
                },
                "idempotency_key": f"amz_qa:{state['run_id']}:smoke",
            }
        )

    total = sum(len(r["checks"]) for r in results)
    passed = sum(1 for r in results for c in r["checks"] if c.get("ok"))

    return {
        "events": events,
        "approvals": approvals_out,
        "memories": [
            {
                "content": (
                    f"Smoke {overall} {passed}/{total} endpoints "
                    f"({'fail: ' + ','.join(failed_services) if failed_services else 'all green'})"
                ),
                "tags": ["amz", "qa", "smoke", overall.lower()],
                "importance": 0.8 if failed_services else 0.3,
            }
        ],
        "kpi_updates": [
            {"name": "smoke_pass", "value": 1 if not failed_services else 0},
            {"name": "smoke_failed_services", "value": len(failed_services)},
            {"name": "smoke_endpoints_ok", "value": passed},
            {"name": "smoke_endpoints_total", "value": total},
        ],
        "state": state_accum,
        "summary": (
            f"{overall} {passed}/{total}"
            + (" notified" if notify["status"] == "ok" or slack["status"] == "ok" else "")
        ),
    }


def build_graph() -> Any:
    graph = StateGraph(QAState)
    graph.add_node("check", node_check)
    graph.add_node("report", node_report)
    graph.add_edge(START, "check")
    graph.add_edge("check", "report")
    graph.add_edge("report", END)
    return graph.compile()


compiled = build_graph()

register_workflow("amz_qa", 1, compiled)
