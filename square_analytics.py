"""
Zeus OS — Square-backed analytics (REAL DATA ONLY, no simulated values).

Every number this module returns is computed from actual COMPLETED Square
payments on your account. Nothing is mocked, seeded, or projected from
invented data.

Integration (FastAPI):

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from square_analytics import router as square_analytics_router

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[...your origins...],
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(square_analytics_router)

Configuration — environment variables (see .env.example):

    SQUARE_ACCESS_TOKEN   required — Developer Dashboard → Credentials
    SQUARE_ENVIRONMENT    "production" (default) or "sandbox"
    SQUARE_LOCATION_ID    optional — pin to one location

Dependencies:  pip install fastapi uvicorn requests
(The official SDK `pip install squareup` works too — this module uses the
documented REST endpoints directly so behavior is pinned, not SDK-versioned.)

Endpoints:
    GET /v1/analytics/live-financials[?start=ISO&end=ISO]
        Month-to-date by default. Settled revenue, Square processing fees,
        COGS hook, true net profit, operating margin.
    GET /v1/analytics/projection?window_days=90&horizon_days=30
        Real trailing settled run-rate x horizon — projection built from
        actual customer payments only.
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger("zeus.square")

router = APIRouter(prefix="/v1/analytics")

# Square-Version from the current API reference (2026-09-16).
SQUARE_VERSION = "2026-09-16"
_BASE_URLS = {
    "production": "https://connect.squareup.com",
    "sandbox": "https://connect.squareupsandbox.com",
}
_PAGE_SIZE = 100
_MAX_PAGES = 200          # hard stop: ~20k payments per query window
_CACHE_TTL_SECONDS = 60   # frontend polls every 15s — spare Square's rate limit


def _access_token() -> str:
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Square credentials not configured: set SQUARE_ACCESS_TOKEN "
                   "in the backend environment (.env). Never hardcode it.",
        )
    return token


def _base_url() -> str:
    return _BASE_URLS[os.environ.get("SQUARE_ENVIRONMENT", "production").strip().lower()]


# ──────────────────────────────────────────────────────────────
# Square REST access (bearer token, cursor pagination, fee data)
# ──────────────────────────────────────────────────────────────

def _square_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.get(
        _base_url() + path,
        params=params,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Square-Version": SQUARE_VERSION,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if resp.status_code == 429:  # rate limited — one patient retry
        time.sleep(1.5)
        resp = requests.get(
            _base_url() + path,
            params=params,
            headers={
                "Authorization": f"Bearer {_access_token()}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    if resp.status_code != 200:
        # Log the failure server-side; NEVER echo the token back.
        logger.error("Square %s failed: HTTP %s %s", path, resp.status_code, resp.text[:300])
        raise HTTPException(status_code=502, detail=f"Square API error (HTTP {resp.status_code})")
    return resp.json()


def _list_completed_payments(start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    """All COMPLETED payments in [start, end), following the cursor to the end.
    Amounts come back in cents (amount_money.amount)."""
    payments: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        pages += 1
        if pages > _MAX_PAGES:
            logger.warning("Square pagination hit %s pages — truncating window", _MAX_PAGES)
            break
        params: Dict[str, Any] = {"begin_time": start_iso, "end_time": end_iso, "limit": _PAGE_SIZE}
        location_id = os.environ.get("SQUARE_LOCATION_ID", "").strip()
        if location_id:
            params["location_id"] = location_id
        if cursor:
            params["cursor"] = cursor
        data = _square_get("/v2/payments", params)
        # Filter client-side on status so we're correct across API versions.
        payments.extend(p for p in data.get("payments", []) if p.get("status") == "COMPLETED")
        cursor = data.get("cursor")
        if not cursor:
            break
    return payments


def _cents(payment: Dict[str, Any], key: str = "amount_money") -> int:
    money = payment.get(key) or {}
    return int(money.get("amount", 0))


# ──────────────────────────────────────────────────────────────
# COGS & expenses — YOUR ledger, not Square's
# ──────────────────────────────────────────────────────────────

_warned_cogs = False


def get_cogs_and_expenses_usd(start_dt: datetime, end_dt: datetime) -> Optional[float]:
    """INTEGRATION POINT — wire this to your own expense ledger.

    Square knows what customers paid and what Square charged you; it does NOT
    know your cost of goods or operating expenses. Return the real figure for
    the period here (from your DB, spreadsheet, accounting system...).

    Returns 0.0 with a logged warning until wired, so True Net Profit is
    honest-but-understated (it only deducts real Square processing fees).
    """
    global _warned_cogs
    if not _warned_cogs:
        logger.warning(
            "COGS provider not wired — true_net_profit_usd currently deducts only "
            "Square processing fees. Implement get_cogs_and_expenses_usd()."
        )
        _warned_cogs = True
    return 0.0


# ──────────────────────────────────────────────────────────────
# Tiny TTL cache so 15s polling doesn't hammer Square
# ──────────────────────────────────────────────────────────────

_cache: Dict[str, Tuple[float, Any]] = {}


def _cached(key: str, producer: Callable[[], Any]) -> Any:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    value = producer()
    _cache[key] = (time.time(), value)
    return value


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────

def _parse_or_default(start: Optional[str], end: Optional[str]) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
    except ValueError:
        raise HTTPException(status_code=400, detail="start/end must be ISO-8601 timestamps")
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be before end")
    return start_dt, end_dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/live-financials")
def live_financials(
    start: Optional[str] = Query(None, description="ISO-8601; defaults to month-to-date"),
    end: Optional[str] = Query(None, description="ISO-8601; defaults to now"),
) -> Dict[str, Any]:
    """Real settled financials for the period — computed from Square COMPLETED payments."""
    start_dt, end_dt = _parse_or_default(start, end)

    def compute() -> Dict[str, Any]:
        payments = _list_completed_payments(_iso(start_dt), _iso(end_dt))
        gross_cents = sum(_cents(p) for p in payments)
        fee_cents = sum(_cents(f) for p in payments for f in p.get("processing_fee", []))
        cogs = get_cogs_and_expenses_usd(start_dt, end_dt)  # your ledger
        gross = gross_cents / 100.0
        fees = fee_cents / 100.0
        expenses = fees + (cogs if cogs is not None else 0.0)
        net = gross - expenses
        return {
            "timestamp_utc": _iso(datetime.now(timezone.utc)),
            "period": {"start": _iso(start_dt), "end": _iso(end_dt)},
            "total_settled_revenue_usd": round(gross, 2),
            "square_processing_fees_usd": round(fees, 2),
            "cogs_and_expenses_usd": round(expenses, 2),
            "cogs_source": "wired" if cogs else "square_fees_only",
            "true_net_profit_usd": round(net, 2),
            "operating_profit_margin_pct": round((net / gross * 100.0) if gross > 0 else 0.0, 1),
            "settled_payment_count": len(payments),
            "data_source": "square_completed_payments",
        }

    key = f"financials:{_iso(start_dt)}:{_iso(end_dt)}"
    return _cached(key, compute)


@router.get("/projection")
def projection(
    window_days: int = Query(90, ge=7, le=365),
    horizon_days: int = Query(30, ge=1, le=365),
) -> Dict[str, Any]:
    """Projection from REAL customer history only: trailing settled run-rate x horizon.
    No simulated values — the basis window is actual COMPLETED payments."""
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=window_days)

    def compute() -> Dict[str, Any]:
        payments = _list_completed_payments(_iso(start_dt), _iso(now))
        gross_cents = sum(_cents(p) for p in payments)
        trailing = gross_cents / 100.0
        daily_avg = trailing / window_days
        projected = daily_avg * horizon_days
        return {
            "timestamp_utc": _iso(now),
            "basis_window_days": window_days,
            "basis_start": _iso(start_dt),
            "basis_end": _iso(now),
            "trailing_settled_revenue_usd": round(trailing, 2),
            "daily_run_rate_usd": round(daily_avg, 2),
            "horizon_days": horizon_days,
            "projected_revenue_usd": round(projected, 2),
            "settled_payment_count": len(payments),
            "data_source": "square_completed_payments",
        }

    key = f"projection:{window_days}:{horizon_days}"
    return _cached(key, compute)


# ──────────────────────────────────────────────────────────────
# Standalone runner:  uvicorn square_analytics:app --port 8000
# ──────────────────────────────────────────────────────────────

def create_app():
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="Zeus OS — Square Analytics")
    origins = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    ]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)
    return app


app = create_app()
