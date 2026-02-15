from __future__ import annotations

import os
import re
import time
import difflib
from typing import Any, Dict, List, Optional, Tuple

import httpx


BASE_URL_ENV = "APPLIANCE_API_BASE_URL"


def _base_url() -> str:
    url = (os.getenv(BASE_URL_ENV) or "http://localhost:4040").strip()
    return url.rstrip("/")


def _norm(s: str) -> str:
    s = (s or "").lower()
    # common voice aliases
    s = s.replace("air conditioner", "ac")
    s = s.replace("airconditioner", "ac")
    s = s.replace("television", "tv")
    s = s.replace("lights", "light")
    s = s.replace("lamp", "bulb")
    s = s.replace("light", "bulb")  # treat "light" as bulb in your device set
    s = s.replace("pc", "computer")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _candidate_text(d: Dict[str, Any]) -> str:
    # build a string the resolver can match against
    parts = [
        d.get("room", ""),
        d.get("name", ""),
        d.get("type", ""),
        f'{d.get("type","")} {d.get("id","")}',
    ]
    return _norm(" ".join(str(p) for p in parts if p))


def _score(a: str, b: str) -> float:
    # difflib is good enough for short strings and avoids extra deps
    return difflib.SequenceMatcher(None, a, b).ratio()


async def get_devices(client: httpx.AsyncClient) -> Dict[str, Any]:
    """
    Returns:
      { ok: bool, ts: int, devices: [...] } on success
      { ok: false, error: "..." } on error
    """
    url = f"{_base_url()}/api/devices"
    try:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        # Ensure consistent shape
        if isinstance(data, dict) and "devices" in data:
            return data
        return {"ok": True, "ts": int(time.time() * 1000), "devices": data}
    except Exception as e:
        return {"ok": False, "error": f"get_devices failed: {type(e).__name__}: {e}"}


def _resolve_device(devices: List[Dict[str, Any]], target: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns: (best_match_or_none, ambiguous_options[])
    """
    t = _norm(target)
    if not t:
        return None, []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for d in devices:
        cand = _candidate_text(d)
        s = _score(t, cand)

        # bonus for token containment
        tokens = set(t.split())
        cand_tokens = set(cand.split())
        if tokens and tokens.issubset(cand_tokens):
            s += 0.15

        scored.append((s, d))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None, []

    best_score, best = scored[0]
    if best_score < 0.55:
        return None, []

    # If multiple devices are very close, treat as ambiguous
    options = [d for s, d in scored[:5] if (best_score - s) <= 0.08 and s >= 0.60]
    if len(options) >= 2:
        return None, options

    return best, []


async def control_device(client: httpx.AsyncClient, action: str, target: str) -> Dict[str, Any]:
    """
    action: on|off|toggle
    target: free-form device text e.g. "living room tv"

    Calls:
      GET {BASE}/application/{type}/{id}?status=true|false

    Returns a structured result for the model to speak from.
    """
    action = (action or "toggle").strip().lower()
    if action not in {"on", "off", "toggle"}:
        action = "toggle"

    snapshot = await get_devices(client)
    if not snapshot.get("ok"):
        return {"ok": False, "result": "error", "message": snapshot.get("error", "Unknown error")}

    devices = snapshot.get("devices") or []
    device, ambiguous = _resolve_device(devices, target)

    if ambiguous:
        # ask user to clarify
        return {
            "ok": False,
            "result": "needs_clarification",
            "message": f"I found multiple matches for '{target}'. Please pick one.",
            "options": [
                {
                    "type": d.get("type"),
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "room": d.get("room"),
                    "status": d.get("status"),
                }
                for d in ambiguous
            ],
        }

    if not device:
        return {
            "ok": False,
            "result": "not_found",
            "message": f"I couldn't find a device matching '{target}'.",
            "known_devices": [
                f"{d.get('room')} - {d.get('name')} ({d.get('type')} {d.get('id')})"
                for d in devices[:12]
            ],
        }

    dev_type = device.get("type")
    dev_id = device.get("id")
    before = bool(device.get("status"))

    if action == "toggle":
        after = not before
    elif action == "on":
        after = True
    else:
        after = False

    # Primary endpoint (per your instruction)
    primary = f"{_base_url()}/application/{dev_type}/{dev_id}"
    params = {"status": "true" if after else "false"}

    try:
        r = await client.get(primary, params=params)
        if r.status_code == 404:
            # fallback (older style) - some of your earlier tools used /{type}/{id}
            fallback = f"{_base_url()}/{dev_type}/{dev_id}"
            r = await client.get(fallback, params=params)

        r.raise_for_status()

        return {
            "ok": True,
            "result": "success",
            "message": f"{device.get('name')} is now {'ON' if after else 'OFF'}.",
            "device": {
                "type": dev_type,
                "id": dev_id,
                "name": device.get("name"),
                "room": device.get("room"),
                "status_before": before,
                "status_after": after,
            },
        }
    except Exception as e:
        return {
            "ok": False,
            "result": "error",
            "message": f"Failed to control {device.get('name')} via API: {type(e).__name__}: {e}",
            "device": {
                "type": dev_type,
                "id": dev_id,
                "name": device.get("name"),
                "room": device.get("room"),
            },
        }
