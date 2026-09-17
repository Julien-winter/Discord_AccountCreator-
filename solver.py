"""Captcha solving hook for the web joiner (join captcha wall).

Provider comes from config: JOIN_SOLVER_PROVIDER = none | custom | 2captcha.
The operator plugs in whatever solver they use (their solver handles
Discord hCaptcha flows); without one, captcha steps report cleanly
instead of hanging the job.
"""
from __future__ import annotations

import asyncio

import aiohttp

import config


def has_solver() -> bool:
    p = (getattr(config, "JOIN_SOLVER_PROVIDER", "none") or "none").lower()
    if p == "2captcha":
        return bool(getattr(config, "JOIN_SOLVER_KEY", ""))
    if p == "custom":
        return bool(getattr(config, "JOIN_SOLVER_ENDPOINT", ""))
    if p == "zrx":
        return bool(getattr(config, "JOIN_SOLVER_KEY", ""))
    return False


async def check_balance() -> int | None:
    """Check solver balance. Returns credits or None if unavailable."""
    return None


async def solve(sitekey: str, rqdata: str | None, pageurl: str, proxy: str | None = None) -> str | None:
    """Solve one hCaptcha, return the token or None."""
    p = (getattr(config, "JOIN_SOLVER_PROVIDER", "none") or "none").lower()
    if p == "2captcha":
        return await _solve_2captcha(sitekey, rqdata, pageurl)
    if p == "custom":
        return await _solve_custom(sitekey, rqdata, pageurl)
    if p == "zrx":
        return await _solve_zrx(sitekey, rqdata, pageurl, proxy)
    return None


async def _solve_custom(sitekey: str, rqdata: str | None, pageurl: str) -> str | None:
    url = (getattr(config, "JOIN_SOLVER_ENDPOINT", "") or "").strip()
    key = getattr(config, "JOIN_SOLVER_KEY", "") or ""
    field = getattr(config, "JOIN_SOLVER_TOKEN_FIELD", "token") or "token"
    if not url or not sitekey:
        return None
    try:
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.post(
                url,
                json={"sitekey": sitekey, "rqdata": rqdata,
                      "pageurl": pageurl, "key": key},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                try:
                    d = await r.json()
                except Exception:
                    return None
                tok = (d or {}).get(field)
                return tok if isinstance(tok, str) and tok else None
    except Exception:
        return None


async def _solve_zrx(sitekey: str, rqdata: str | None, pageurl: str,
                     proxy: str | None = None) -> str | None:
    """zrx solver (zrxsolver.online) — createTask + getResult."""
    import time as _time

    key = (getattr(config, "JOIN_SOLVER_KEY", "") or "").strip()
    if not key or not sitekey:
        return None

    task: dict = {"sitekey": sitekey, "website": "discord.com"}
    if proxy:
        try:
            p = proxy.strip()
            for prefix in ("http://", "https://"):
                if p.startswith(prefix):
                    p = p[len(prefix):]
                    break
            task["proxy"] = p
        except Exception:
            pass
    if rqdata:
        task["rqdata"] = rqdata

    try:
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.post(
                "https://zrxsolver.online/createTask",
                headers={"X-API-Key": key, "Content-Type": "application/json"},
                json={"task": task},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                try:
                    d = await r.json()
                except Exception:
                    return None
                tid = (d or {}).get("taskId") or (d or {}).get("task_id")
                if not tid:
                    return None

            end = _time.time() + 150
            while _time.time() < end:
                await asyncio.sleep(3)
                try:
                    async with s.post(
                        "https://zrxsolver.online/getResult",
                        headers={"X-API-Key": key, "Content-Type": "application/json"},
                        json={"taskId": tid},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        try:
                            d = await r.json()
                        except Exception:
                            continue
                        if (d or {}).get("status") == "success":
                            sol = (d or {}).get("solution") or (d or {}).get("token")
                            if isinstance(sol, str) and sol:
                                return sol
                        if (d or {}).get("status") == "failed":
                            return None
                except Exception:
                    pass
    except Exception:
        return None
    return None


async def _solve_2captcha(sitekey: str, rqdata: str | None, pageurl: str,
                          poll: float = 5.0, cap: float = 120.0) -> str | None:
    key = (getattr(config, "JOIN_SOLVER_KEY", "") or "").strip()
    if not key or not sitekey:
        return None
    submit = {"key": key, "method": "hcaptcha", "sitekey": sitekey,
              "pageurl": pageurl or "https://discord.com/channels/@me",
              "json": 1}
    if rqdata:
        submit["data"] = rqdata
    try:
        async with aiohttp.ClientSession(trust_env=False) as s:
            async with s.post("https://2captcha.com/in.php", data=submit,
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                try:
                    d = await r.json()
                except Exception:
                    return None
            tid = (d or {}).get("request")
            if not d or d.get("status") != 1 or not tid:
                return None
            waited = 0.0
            while waited < cap:
                await asyncio.sleep(poll)
                waited += poll
                try:
                    async with s.get(
                        "https://2captcha.com/res.php",
                        params={"key": key, "action": "get", "id": tid, "json": 1},
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as r:
                        d = await r.json()
                except Exception:
                    continue
                if (d or {}).get("status") == 1:
                    tok = d.get("request")
                    return tok if isinstance(tok, str) and tok else None
                if (d or {}).get("request") not in ("CAPCHA_NOT_READY", None):
                    return None
    except Exception:
        return None
    return None
