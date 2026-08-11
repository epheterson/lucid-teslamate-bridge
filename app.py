"""lucid-bridge — a Tesla Owner API that is secretly a Lucid.

TeslaMate is pointed here with TESLA_API_HOST / TESLA_AUTH_HOST. That is a
supported configuration (it is how MyTeslaMate and Teslemetry work), so
TeslaMate itself runs stock and upgradable — no fork, no patch, no
unauditable binary holding the owner's credentials.

Auth is deliberately a no-op. This service is bound to the compose network
and never published to a host port; the only client that can reach it is
TeslaMate. Implementing real OAuth between two containers that trust each
other by construction would be theatre. The REAL credential — the Lucid
refresh token — is mounted READ-ONLY and never leaves this process.

Reads never wake a Lucid (the library only wakes on commands with
auto_wake=True, which nothing here sets), so TeslaMate is free to poll as
hard as it likes. What we do guard against is amplification: TeslaMate polls
every few seconds while driving, and each poll must not become a separate
round trip to Lucid. Hence the short TTL cache and the single shared
session.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import mapping

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("lucid-bridge")

TOKEN_FILE = Path(os.getenv("LUCID_TOKEN_FILE", "/secrets/lucid-token.json"))

# TeslaMate polls every ~2.5s while driving. Collapsing those onto one
# upstream call per CACHE_TTL keeps us from hammering Lucid without losing
# meaningful resolution — the car's own last_updated_ms rarely moves faster.
CACHE_TTL = float(os.getenv("CACHE_TTL_SECONDS", "5"))

app = FastAPI(title="lucid-bridge")

_api: Any = None
_lock = asyncio.Lock()
_cache: dict[str, Any] = {"at": 0.0, "vehicles": []}


def tesla_id(vin: str) -> int:
    """A stable pseudo-Tesla id derived from the VIN.

    Must be stable forever: TeslaMate keys its car records on it, so a value
    that changed across restarts would orphan all history and start a new
    car. Hashing the VIN gives that for free, with no state to persist.
    """
    return int(hashlib.sha256(vin.encode()).hexdigest()[:12], 16)


def _load_token() -> str:
    try:
        token = json.loads(TOKEN_FILE.read_text())["refresh_token"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"No Lucid refresh token at {TOKEN_FILE}. Mint one on the host with: "
            "python -m lucid_bridge.login"
        ) from exc
    return token


async def _session():
    """One LucidAPI session, refreshed before it lapses.

    The refresh token does not rotate (measured 2026-08-09), so re-reading
    the same file forever is safe and the mount can stay read-only.
    """
    global _api
    from lucidmotors import LucidAPI

    if _api is None:
        _api = LucidAPI()
        await _api.__aenter__()
        _api._refresh_token = _load_token()
        await _api.authentication_refresh()
        log.info("lucid session established")
        return _api

    # Session tokens last 6h. Refresh with an hour to spare rather than
    # waiting for a 401 mid-drive.
    if _api.session_time_remaining.total_seconds() < 3600:
        log.info("refreshing lucid session")
        try:
            await _api.authentication_refresh()
        except Exception:
            log.exception("refresh failed; rebuilding session from token file")
            try:
                await _api.close()
            except Exception:
                pass
            _api = None
            return await _session()
    return _api


async def vehicles() -> list[Any]:
    """Fetch vehicles, collapsing concurrent callers onto one upstream call."""
    async with _lock:
        if time.monotonic() - _cache["at"] < CACHE_TTL and _cache["vehicles"]:
            return _cache["vehicles"]
        api = await _session()
        await api.fetch_vehicles()
        _cache["vehicles"] = list(api.vehicles)
        _cache["at"] = time.monotonic()
        return _cache["vehicles"]


def _find(vs: list[Any], wanted: int):
    for v in vs:
        if tesla_id(v.config.vin) == wanted:
            return v
    return None


# ── auth: accepted unconditionally, see module docstring ──────────────────


@app.post("/oauth2/v3/token")
async def token() -> dict[str, Any]:
    return {
        "access_token": "lucid-bridge",
        "refresh_token": "lucid-bridge",
        "id_token": "lucid-bridge",
        "expires_in": 28800,
        "token_type": "Bearer",
        "state": "of-the-art",
    }


# ── the Owner API surface TeslaMate actually calls ────────────────────────


@app.get("/api/1/products")
async def products() -> dict[str, Any]:
    vs = await vehicles()
    return {
        "response": [mapping.vehicle_summary(v, tesla_id(v.config.vin)) for v in vs]
    }


@app.get("/api/1/vehicles")
async def vehicle_list() -> dict[str, Any]:
    return await products()


@app.get("/api/1/vehicles/{vid}")
async def vehicle(vid: int) -> dict[str, Any]:
    v = _find(await vehicles(), vid)
    if v is None:
        raise HTTPException(404, "not_found")
    return {"response": mapping.vehicle_summary(v, vid)}


@app.get("/api/1/vehicles/{vid}/vehicle_data")
async def vehicle_data(vid: int) -> Any:
    v = _find(await vehicles(), vid)
    if v is None:
        raise HTTPException(404, "not_found")

    # A sleeping car must 408, not return stale data. TeslaMate uses that
    # response to decide the car is asleep and to stop polling hard —
    # returning a payload instead would make it believe the car is awake
    # forever and every idle statistic would be fiction.
    if mapping.is_asleep(getattr(v.state, "power", None)):
        return JSONResponse(
            status_code=408,
            content={"error": 'vehicle unavailable: {:error=>"vehicle unavailable"}'},
        )
    return {"response": mapping.vehicle_data(v, vid)}


@app.get("/health")
async def health() -> Any:
    try:
        vs = await vehicles()
    except Exception as exc:
        return JSONResponse(
            status_code=503, content={"ok": False, "error": type(exc).__name__}
        )
    return {
        "ok": True,
        "vehicles": [
            {
                "name": v.config.nickname,
                "id": tesla_id(v.config.vin),
                "state": mapping.vehicle_summary(v, 0)["state"],
            }
            for v in vs
        ],
    }
