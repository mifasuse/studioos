"""AMZ Dev tech map — service architecture, Celery beats, open bugs.

Sourced from OpenClaw's amz-arbitrage/agents/DEV.md. The amz-dev
workflow seeds its memory with these facts on first run so the
reflector and other agents can reason about them consistently.

Updating this file is the right place to record:
  - New services / renamed containers / updated image paths
  - New Celery beat schedules or task renames
  - Known bugs & their severity (🔴 critical / 🟡 medium / 🟢 minor)
  - SP-API / Ads-API / eBay-API rate limits worth remembering
"""
from __future__ import annotations

SERVICES = [
    {
        "name": "pricefinder",
        "repo": "mifasuse/pricefinder",
        "path": "/srv/projects/pricefinder",
        "domain": "pricefinder.mifasuse.com",
        "backend_internal": "http://pricefinder-backend:8000",
        "purpose": (
            "TR→US arbitrage scanner: 44 TR scrapers, Keepa/SP-API US "
            "prices, arbitrage_opportunities calculator."
        ),
        "stack": "FastAPI async + SQLAlchemy async + Celery (3 queues)",
        "celery_beat": [
            {"cron": "03:00 daily", "task": "discover_asins"},
            {"cron": "every 4h", "task": "update_tr_prices (44 sites)"},
            {"cron": "every 6h :30", "task": "update_us_prices (SP-API batch)"},
            {"cron": "every 30m", "task": "calculate_opportunities"},
            {"cron": "hourly :05", "task": "update_exchange_rate"},
            {"cron": "04:00 daily", "task": "cleanup_expired_opportunities"},
            {"cron": "Sun 02:00", "task": "discover_by_category (70+ keywords)"},
            {"cron": "Sun 02:30", "task": "discover_bestsellers (HTML)"},
        ],
        "critical_bugs": [
            {
                "severity": "fixed",
                "title": "SP-API invalid_client",
                "detail": (
                    "getCompetitivePricing batch now works (20 ASIN/req); "
                    "fix landed earlier. Monitor for regressions."
                ),
            }
        ],
    },
    {
        "name": "buyboxpricer",
        "repo": "mifasuse/buyboxpricer",
        "path": "/srv/projects/buyboxpricer",
        "domain": "buyboxpricer.mifasuse.com",
        "backend_internal": "http://buyboxpricer-backend:8000",
        "purpose": "Automated repricing + Buy Box competitive tracking.",
        "stack": "FastAPI async + SQLAlchemy async + Celery + SQS + SP-API",
        "celery_beat": [
            {"cron": "hourly :00", "task": "sync_all_listings (FBA + pricing + BuyBox)"},
            {"cron": "hourly :30", "task": "run_repricing (batch offers)"},
            {"cron": "03:00 daily", "task": "fetch_all_fees"},
            {"cron": "every 30s", "task": "poll_sqs_notifications"},
            {"cron": "04:00 daily", "task": "cleanup_old_task_logs"},
            {"cron": "02:00 daily", "task": "calculate_all_accounts_health"},
            {"cron": "02:30 daily", "task": "snapshot_health_history"},
            {"cron": "every 4h :15", "task": "detect_and_resolve fair-pricing violations"},
        ],
        "critical_bugs": [],
    },
    {
        "name": "adsoptimizer",
        "repo": "mifasuse/adsoptimizer",
        "path": "/srv/projects/adsoptimizer",
        "domain": "adsoptimizer.mifasuse.com",
        "backend_internal": "http://adsoptimizer-backend:8000",
        "purpose": "Amazon PPC campaign management (Sponsored Products).",
        "stack": "FastAPI async + SQLAlchemy async + python-amazon-ad-api",
        "celery_beat": [
            {"cron": "every 4h", "task": "sync_all_campaigns (🔴 PLACEHOLDER)"},
            {"cron": "04:00 daily", "task": "cleanup_old_task_logs (🔴 PLACEHOLDER)"},
        ],
        "critical_bugs": [
            {
                "severity": "critical",
                "title": "Bid optimization missing",
                "detail": (
                    "Celery tasks are placeholders. No ACOS reporting, "
                    "no auto bid optimization, no keyword harvesting. "
                    "Campaign creation works but ongoing management is blind."
                ),
            }
        ],
    },
    {
        "name": "ebaycrosslister",
        "repo": "mifasuse/ebaycrosslister",
        "path": "/home/deployer/ebaycrosslister",
        "domain": "ebaycrosslister.mifasuse.com",
        "backend_internal": "http://ebaycrosslister-backend:8000",
        "purpose": "Amazon FBA → eBay cross-list + MCF order fulfillment.",
        "stack": "FastAPI sync + SQLAlchemy sync + Celery + eBay REST + Trading API",
        "celery_beat": [
            {"cron": "MANUAL ONLY", "task": "sync_all_inventory"},
            {"cron": "MANUAL ONLY", "task": "sync_listing_quantities"},
            {"cron": "MANUAL ONLY", "task": "sync_prices_with_amazon"},
            {"cron": "MANUAL ONLY", "task": "sync_all_orders"},
            {"cron": "MANUAL ONLY", "task": "update_mcf_tracking"},
        ],
        "critical_bugs": [
            {
                "severity": "critical",
                "title": "Celery Beat schedule missing",
                "detail": (
                    "No scheduled sync — everything triggers manually. "
                    "Stock/price drift from Amazon is not caught automatically."
                ),
            },
            {
                "severity": "critical",
                "title": "MCF create is placeholder",
                "detail": (
                    "Real Amazon MCF API call is not wired up. "
                    "eBay orders do not auto-fulfill from FBA stock."
                ),
            },
            {
                "severity": "medium",
                "title": "eBay token refresh path unclear",
                "detail": (
                    "Refresh mechanism is not documented; token expiry "
                    "could break sync silently."
                ),
            },
        ],
    },
]

SP_API_LIMITS = [
    ("getCompetitivePricing", "5 req/s, 20 ASIN/batch — primary bulk path"),
    ("getItemOffers", "5 req/s, 1 ASIN/req — detailed seller info"),
    ("Reports API", "async, 15–30 min — full-catalog pulls"),
    ("GET_COMPETITIVE_PRICING_FOR_ALL_ACTIVE_OFFERS", "catalog-wide prices"),
    ("GET_MERCHANT_LISTINGS_ALL_DATA", "catalog-wide listings"),
    ("Catalog API", "5 req/s — product detail / ASIN lookup"),
]


PROACTIVE_CHECKLIST = [
    "Rate limit underused? (e.g. 1 req/s while SP-API allows 5/s)",
    "Sequential work that could parallelize via ThreadPool or asyncio?",
    "Repeated identical queries that should hit Redis cache?",
    "Per-item API calls where a bulk endpoint exists?",
    "Reports API could replace per-ASIN polling?",
    "Celery task timeouts / schedules misaligned with actual runtime?",
    "Slow DB query missing an index (EXPLAIN ANALYZE)?",
]


def tech_map_memories() -> list[dict]:
    """Return a list of memory rows to seed amz-dev with tech-map facts."""
    mems: list[dict] = []
    for svc in SERVICES:
        mems.append(
            {
                "content": (
                    f"Service {svc['name']}: {svc['purpose']} "
                    f"Stack: {svc['stack']}. Internal URL: {svc['backend_internal']}. "
                    f"Repo: {svc['repo']}. Path: {svc['path']}."
                ),
                "tags": ["amz", "dev", "tech_map", svc["name"]],
                "importance": 0.9,
            }
        )
        for beat in svc["celery_beat"]:
            mems.append(
                {
                    "content": (
                        f"{svc['name']} Celery beat: {beat['cron']} → {beat['task']}"
                    ),
                    "tags": ["amz", "dev", "celery_beat", svc["name"]],
                    "importance": 0.7,
                }
            )
        for bug in svc["critical_bugs"]:
            mems.append(
                {
                    "content": (
                        f"{svc['name']} [{bug['severity']}] {bug['title']}: "
                        f"{bug['detail']}"
                    ),
                    "tags": ["amz", "dev", "bug", svc["name"], bug["severity"]],
                    "importance": 0.95,
                }
            )
    for endpoint, note in SP_API_LIMITS:
        mems.append(
            {
                "content": f"SP-API {endpoint}: {note}",
                "tags": ["amz", "dev", "sp_api", "rate_limit"],
                "importance": 0.75,
            }
        )
    for item in PROACTIVE_CHECKLIST:
        mems.append(
            {
                "content": f"Proactive engineering: {item}",
                "tags": ["amz", "dev", "proactive"],
                "importance": 0.6,
            }
        )
    return mems
