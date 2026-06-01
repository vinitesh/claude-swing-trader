"""FastAPI app — read-only dashboard, charts, runs history, config viewer.

Run with:
    uvicorn web.main:app --host 0.0.0.0 --port 8082

Or via swingbot CLI:
    swingbot serve

In production (Docker/EC2) it's launched as a long-running service via
docker-compose.yml — see the `web` service there.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import init
from persistence.db import session_scope
from web.auth import require_auth
from web import explainers as exp
from web import queries as q

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
CONFIG_DIR = PROJECT_ROOT / "config"

app = FastAPI(title="swing_platform")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Expose builtins to templates that we actually use
templates.env.globals["float"] = float


# ------------- startup health check -------------
@app.on_event("startup")
def _startup_check() -> None:
    settings, _ = init()
    if not (settings.web_password or "").strip():
        # Don't crash — let the app start but every protected route returns 503.
        # Keeps deploys recoverable: edit .env, restart container, no rebuild.
        log.warning(
            "WEB_PASSWORD is empty — web service disabled. "
            "Set WEB_PASSWORD in .env and restart to enable."
        )


def _ensure_enabled() -> None:
    settings, _ = init()
    if not (settings.web_password or "").strip():
        raise HTTPException(
            status_code=503,
            detail="Web UI disabled (WEB_PASSWORD not set in .env)",
        )


# ------------- routes -------------
@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    """Unauthenticated liveness probe — never auths to avoid log spam."""
    return "ok"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    with session_scope() as s:
        ctx = {
            "health": q.health_summary(s),
            "strategies": q.strategy_reports(s),
            "open_positions": q.open_positions(s),
            "active_page": "dashboard",
        }
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/charts", response_class=HTMLResponse)
def charts_page(request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    with session_scope() as s:
        bundle = q.charts_bundle(s)
    return templates.TemplateResponse(
        request,
        "charts.html",
        {
            "active_page": "charts",
            "has_data": bool(bundle.per_strategy),
            "data_json": json.dumps(_chart_payload(bundle), default=str),
        },
    )


@app.get("/charts.json")
def charts_json(_user: str = Depends(require_auth)) -> JSONResponse:
    _ensure_enabled()
    with session_scope() as s:
        bundle = q.charts_bundle(s)
    return JSONResponse(_chart_payload(bundle))


def _chart_payload(bundle) -> dict:
    return {
        "starting_capital": bundle.starting_capital,
        "combined_equity": [
            {"day": ep.day.isoformat(), "cum_pnl": ep.cumulative_pnl}
            for ep in bundle.combined_equity_curve
        ],
        "per_strategy": [
            {
                "name": sd.strategy_name,
                "equity": [{"day": ep.day.isoformat(), "cum_pnl": ep.cumulative_pnl}
                           for ep in sd.equity_curve],
                "daily": [{"day": dp.day.isoformat(), "pnl": dp.pnl, "n": dp.n_trades}
                          for dp in sd.daily_pnl],
                "trade_pnls": list(sd.win_loss_pnls),
            }
            for sd in bundle.per_strategy
        ],
        "signals_per_day": [
            {"day": d.isoformat(), "n": n} for (d, n) in bundle.signals_per_day
        ],
    }


@app.get("/strategies", response_class=HTMLResponse)
def strategies_index(request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    items = exp.list_explainers()
    return templates.TemplateResponse(
        request, "strategies_index.html",
        {"strategies": items, "active_page": "strategies"},
    )


@app.get("/strategies/{name}", response_class=HTMLResponse)
def strategy_detail(name: str, request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    item = exp.get_explainer(name)
    if item is None:
        raise HTTPException(404, f"Strategy {name!r} not found")
    return templates.TemplateResponse(
        request, "strategy_detail.html",
        {"strategy": item, "active_page": "strategies"},
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    with session_scope() as s:
        runs = q.recent_runs(s, limit=100)
    return templates.TemplateResponse(
        request, "runs.html", {"runs": runs, "active_page": "runs"},
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    with session_scope() as s:
        run = q.get_run(s, run_id)
        if run is None:
            raise HTTPException(404, f"Run {run_id} not found")
        sigs = q.signals_for_run(s, run_id)
    return templates.TemplateResponse(
        request, "run_detail.html",
        {"run": run, "signals": sigs, "active_page": "runs"},
    )


@app.get("/config", response_class=HTMLResponse)
def config_index(request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    files = q.list_config_files(CONFIG_DIR)
    return templates.TemplateResponse(
        request, "config_index.html",
        {"files": files, "active_page": "config"},
    )


@app.get("/config/{rel_path:path}", response_class=HTMLResponse)
def config_view(rel_path: str, request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    _ensure_enabled()
    try:
        body = q.read_config_file(CONFIG_DIR, rel_path)
    except (FileNotFoundError, PermissionError) as e:
        raise HTTPException(404, str(e))
    return templates.TemplateResponse(
        request, "config_view.html",
        {"rel_path": rel_path, "body": body, "active_page": "config"},
    )
