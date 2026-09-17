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


async def solve_mail_verify(sitekey: str, rqdata: str | None, pageurl: str = "https://discord.com/", proxy: str | None = None) -> str | None:
    """Mail-verify solver (89.167.31.16:5000) — cheap."""
    import time as _time
    key = (getattr(config, "MAIL_VERIFY_SOLVER_KEY", "") or "").strip()
    host = (getattr(config, "MAIL_VERIFY_SOLVER_HOST", "http://89.167.31.16:5000") or "http://89.167.31.16:5000").strip().rstrip("/")
    if not key or not sitekey:
        return None
    domain = pageurl.split("//")[-1].split("/")[0] if pageurl else "discord.com"
    formatted_proxy = ""
    if proxy:
        p = proxy.strip()
        if not p.startswith("http"):
            formatted_proxy = f"http://{p}"
        else:
            formatted_proxy = p
    payload = {
        "sitekey": sitekey,
        "siteurl": f"https://{domain}",
        "rqdata": rqdata or "",
        "proxy": formatted_proxy,
        "groq_api_key": key,
    }
    # endpoint is /solve per docs.html, host already includes port
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(trust_env=False) as s:
                async with s.post(
                    f"{host}/solve",
                    json=payload,
                    headers={"Content-Type": "application/json", "X-API-Key": key},
                    timeout=aiohttp.ClientTimeout(total=125),
                ) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        # may return plain string token
                        try:
                            txt = await r.text()
                            if txt and txt.startswith("P1_"):
                                return txt.strip()
                        except Exception:
                            pass
                        data = {}
                    if isinstance(data, str) and data.startswith("P1_"):
                        return data
                    if isinstance(data, dict):
                        if data.get("success") and data.get("token"):
                            return data["token"]
                        # docs: direct token string on success, else {"errors":...}
                        tok = data.get("token") or data.get("solution") or data.get("captcha_key")
                        if isinstance(tok, str) and tok.startswith("P1_"):
                            return tok
                    # if returned errors dict, retry
                    if isinstance(data, dict) and data.get("errors"):
                        await asyncio.sleep(2)
                        continue
                    # fallback: if we got any string token
                    if isinstance(data, dict):
                        for v in data.values():
                            if isinstance(v, str) and v.startswith("P1_"):
                                return v
        except Exception:
            await asyncio.sleep(2)
            continue
    return None


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
