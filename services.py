"""Checker + Claimer + Token-Checker für die Website (async, Proxy-Support).

Nutzt deine Server-Proxies aus data/proxies.txt wenn vorhanden,
sonst langsam/proxyless (max 3 parallel). Discord-Endpoints wie im Panel.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import random
import re
import string
import time
import urllib.parse
from pathlib import Path

import aiohttp

try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    _HAS_CURL = True
except Exception:
    _CurlAsyncSession = None  # type: ignore
    _HAS_CURL = False

ENDPOINT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
ME_URL = "https://discord.com/api/v9/users/@me"
PATCH_ME = "https://discord.com/api/v9/users/@me"
POMELO_URL = "https://discord.com/api/v9/users/@me/pomelo-attempt"
GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_solver_dead = False  # set True on first solver failure to skip all remaining captcha calls

USERNAME_CHARS = string.ascii_lowercase + string.digits + "_" + "."
LETTERS_ONLY = string.ascii_lowercase
CHARSETS = {"4l": LETTERS_ONLY, "4c": USERNAME_CHARS}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://discord.com",
    "Referer": "https://discord.com/",
}


def is_valid_username(name: str) -> bool:
    if not (2 <= len(name) <= 32):
        return False
    if ".." in name or name.startswith(".") or name.endswith("."):
        return False
    return all(c in USERNAME_CHARS for c in name)


def name_pattern(name: str) -> str:
    """Rarity pattern tag like the big snipers use: 4-letter, multi_underscore(2), digits, common."""
    n = (name or "").lower()
    if n.isalpha():
        return "4-letter" if len(n) == 4 else "letters"
    u = n.count("_")
    if u >= 2:
        return f"multi_underscore({u})"
    if n.isdigit():
        return "digits"
    return "common"


def _data_file(name: str) -> str:
    """Absolute path into the web data dir (independent of CWD)."""
    return str(Path(__file__).resolve().parent / "data" / name)


def load_proxies(path: str | None = None) -> list[str]:
    if not path:
        path = _data_file("proxies.txt")
    try:
        with open(path, encoding="utf-8-sig") as f:
            out = []
            for ln in f:
                s = ln.strip().lstrip(chr(0xFEFF))
                if s and not s.startswith("#"):
                    out.append(s if s.startswith("http") else "http://" + s)
            return out
    except Exception:
        return []


def token_check_proxies() -> list[str]:
    """Dedicated pool for the token checker (token_proxies.txt, else proxies.txt)."""
    p = load_proxies(_data_file("token_proxies.txt"))
    return p or load_proxies()


def check_proxies() -> list[str]:
    """Pool for username checks/snipes (proxies.txt, else token_proxies.txt)."""
    p = load_proxies()
    return p or load_proxies(_data_file("token_proxies.txt"))


def joiner_proxies() -> list[str]:
    """Dedicated pool for the server joiner (joiner_proxies.txt, else token_proxies.txt, else proxies.txt)."""
    p = load_proxies(_data_file("joiner_proxies.txt"))
    return p or token_check_proxies()


def proxy_counts() -> dict:
    """Pool sizes for the server log (no credentials, counts only)."""
    try:
        return {
            "proxies_txt": len(load_proxies()),
            "token_proxies_txt": len(load_proxies(_data_file("token_proxies.txt"))),
        }
    except Exception:
        return {}


async def _classify_pool(proxies: list[str]) -> tuple[list[str], list[str]]:
    """One unauthed probe per proxy -> (working, rest).

    Working lines come back latency-sorted (fastest first): cycle order
    then equals speed order, the cheapest throughput win available.
    Sticky pools keep their verdict for a while; rotating exits don't —
    ordering still helps on average.
    """
    scored: list[tuple[float, str]] = []
    rest: list[str] = []
    sem = asyncio.Semaphore(20)

    async with aiohttp.ClientSession(trust_env=False) as sess:
        async def _one(px: str):
            async with sem:
                t0 = time.perf_counter()
                try:
                    t = aiohttp.ClientTimeout(total=7, sock_connect=3)
                    async with sess.post(
                        ENDPOINT, json={"username": "panda_probe_zz9"},
                        headers=BROWSER_HEADERS, proxy=px, timeout=t,
                    ) as r:
                        try:
                            await r.read()
                        except Exception:
                            pass
                        ms = (time.perf_counter() - t0) * 1000
                        if r.status in (200, 201, 204, 400):
                            scored.append((ms, px))
                        else:
                            rest.append(px)
                except Exception:
                    rest.append(px)

        await asyncio.gather(*[_one(p) for p in proxies])
    working = [p for _, p in sorted(scored)]
    return working, rest


def random_name(length: int = 4, alphabet: str = USERNAME_CHARS) -> str:
    while True:
        n = "".join(random.choices(alphabet, k=length))
        if is_valid_username(n):
            return n


async def _check_one(session: aiohttp.ClientSession, name: str, proxy: str | None) -> bool | None:
    """True=free, False=taken/invalid, None=unklar (429/timeout -> retry)."""
    try:
        async with session.post(
            ENDPOINT, json={"username": name}, headers=BROWSER_HEADERS,
            proxy=proxy, timeout=aiohttp.ClientTimeout(total=10, sock_connect=4),
        ) as r:
            if r.status in (200, 201, 204):
                try:
                    data = await r.json()
                except Exception:
                    return None
                return not bool(data.get("taken", True))
            if r.status == 400:
                return False
            if r.status == 429:
                try:
                    w = float((await r.json()).get("retry_after", 3))
                except Exception:
                    w = 3.0
                await asyncio.sleep(min(w, 10))
                return None
            return None
    except Exception:
        return None


async def run_check_until_valid(length: int, max_attempts: int, progress_cb, sample_cb=None, deadline: float | None = None, charset: str = "4c", custom_proxies: list[str] | None = None) -> dict:
    """Check random names until 1 valid. progress_cb(done:int).

    sample_cb(name, res) receives live samples for the web console:
    res is "free" for hits, "taken" for sampled misses.
    deadline (unix ts): stop at this time, result gets timeout=True.
    charset "4l" = letters only (a-z), "4c" = full set (a-z 0-9 _ .).
    """
    if custom_proxies is not None:
        proxies = [p.strip() for p in custom_proxies if isinstance(p, str) and p.strip()]
        proxies = [p if p.startswith("http") else "http://" + p for p in proxies]
        pool_info = f"{len(proxies)} proxies (BYOP)"
    else:
        proxies = check_proxies()
        pool_info = f"{len(proxies)} proxies"
    alphabet = CHARSETS.get(charset, USERNAME_CHARS)
    if proxies:
        working, _rest = await _classify_pool(proxies)
        if working:
            proxies = working  # burned lines only slow the race down
            pool_info = f"{len(working)}/{len(working) + len(_rest)} proxies answering"
    workers = min(100, max(10, len(proxies) * 5)) if proxies else 3
    cycle = itertools.cycle(proxies) if proxies else None
    seen: set[str] = set()
    queue: asyncio.Queue[str] = asyncio.Queue()
    stop = asyncio.Event()
    result: dict = {"pool": pool_info, "charset": charset}
    done = 0
    done_lock = asyncio.Lock()
    taken_n = 0

    worker_w = min(100, max(10, len(proxies) * 10)) if proxies else 3

    def _sample_delay() -> float:
        # adaptive: many taken but few in the console -> show more
        if taken_n < 250:
            return 8 if done < 400 else 12  # early: ~33/400, ~66/800 visible
        return 40 if taken_n >= 600 else 22

    # Warteschlange vorfüllen
    def refill(n: int):
        need = max(n, worker_w * 18)
        added, guard = 0, 0
        while added < need and guard < need * 10:
            guard += 1
            c = random_name(length, alphabet)
            if c not in seen:
                seen.add(c)
                queue.put_nowait(c)
                added += 1

    refill(worker_w * 28)

    async with aiohttp.ClientSession(trust_env=False) as sess:
        async def worker():
            nonlocal done, taken_n
            while not stop.is_set():
                if deadline is not None and time.time() >= deadline:
                    stop.set()
                    return
                try:
                    name = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if charset == "4l" and not name.isalpha():
                    await asyncio.sleep(0.01)
                    continue  # seatbelt: wrong-charset names never burn checks
                px = next(cycle) if cycle else None
                _t0 = time.perf_counter()
                r = await _check_one(sess, name, px)
                _cms = int((time.perf_counter() - _t0) * 1000)
                async with done_lock:
                    done += 1
                    if sample_cb is not None:
                        try:
                            if r is True:
                                sample_cb(name, "free")
                            else:
                                sd = _sample_delay()
                                if done % int(sd) == 0:
                                    taken_n += 1
                                    sample_cb(name, "taken")
                        except Exception:
                            pass
                    if done % 25 == 0 or r is True:
                        try:
                            progress_cb(done)
                        except Exception:
                            pass
                if r is True:
                    if charset == "4l" and not name.isalpha():
                        continue  # seatbelt: never deliver off-charset hits
                    # Doppel-Check gegen False-Positive (andere Line)
                    px2 = next(cycle) if cycle else None
                    c2 = await _check_one(sess, name, px2)
                    if c2 is True:
                        result["name"] = name
                        result["attempts"] = done
                        stop.set()
                        return
                    if c2 is False:
                        continue  # False-Positive, weiter
                    result["name"] = name  # unkonfirmiert, trotzdem liefern
                    result["attempts"] = done
                    result["unconfirmed"] = True
                    stop.set()
                    return
                if queue.qsize() < worker_w * 10 and len(seen) < max_attempts:
                    refill(worker_w * 14)
                if done >= max_attempts:
                    stop.set()
                    return
                if not proxies:
                    await asyncio.sleep(0.6)  # proxyless: spare home IP

        await asyncio.gather(*[worker() for _ in range(worker_w)])

    if "name" not in result:
        if deadline is not None and time.time() >= deadline:
            result["timeout"] = True
            result["error"] = (
                f"No free name within the time limit ({done:,} checks) — "
                "amount refunded as credit."
            )
        else:
            result["error"] = f"No free name after {done} checks — try again later."
        result["attempts"] = done
    if "name" in result:
        result.setdefault("pattern", name_pattern(result["name"]))
    return result


class _WebGateway:
    """Minimal Discord gateway session, held open during a snipe job.

    Panel lesson: without an active session Discord answers PATCH
    /users/@me with 10020 Unknown Session, and connecting per claim
    costs seconds (WS handshake + hello/identify/READY). One warm
    session turns the claim into a pure roundtrip.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.ready = False
        self.session_id: str | None = None  # from READY, needed for button clicks
        self._sess: aiohttp.ClientSession | None = None
        self._ws = None
        self._hb = None

    async def connect(self, timeout: float = 14.0) -> bool:
        try:
            self._sess = aiohttp.ClientSession(trust_env=False)
            self._ws = await self._sess.ws_connect(
                GATEWAY_URL, heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=30),
            )
            msg = await asyncio.wait_for(self._ws.receive(), timeout=10)
            interval = 41.25
            try:
                hello = json.loads(msg.data) if msg.type == aiohttp.WSMsgType.TEXT else {}
                interval = (hello.get("d") or {}).get("heartbeat_interval", 41250) / 1000.0
            except Exception:
                pass
            await self._ws.send_json({
                "op": 2,
                "d": {"token": self.token,
                      "properties": {"os": "Windows", "browser": "Chrome", "device": ""},
                      "presence": {"status": "online", "afk": False, "since": 0,
                                   "activities": []}},
            })

            async def _hb() -> None:
                try:
                    while True:
                        await asyncio.sleep(interval)
                        try:
                            await self._ws.send_json({"op": 1, "d": None})
                        except Exception:
                            break
                except asyncio.CancelledError:
                    pass

            self._hb = asyncio.ensure_future(_hb())
            end = time.time() + timeout
            while time.time() < end:
                try:
                    m = await asyncio.wait_for(self._ws.receive(), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    continue
                if m.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(m.data)
                except Exception:
                    continue
                if data.get("t") == "READY":
                    self.ready = True
                    try:
                        self.session_id = (data.get("d") or {}).get("session_id")
                    except Exception:
                        pass
                    return True
                if data.get("op") == 9:
                    return False
        except Exception:
            pass
        return False

    async def close(self) -> None:
        try:
            if self._hb is not None:
                self._hb.cancel()
        except Exception:
            pass
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass
        try:
            if self._sess is not None:
                await self._sess.close()
        except Exception:
            pass


async def run_snipe_claim(length: int, token: str, password: str, max_attempts: int, progress_cb, sample_cb=None, deadline: float | None = None, charset: str = "4c", custom_proxies: list[str] | None = None) -> dict:
    """Check random names and claim a hit instantly (PATCH /users/@me).

    sample_cb(name, res) receives live samples: "free", "taken",
    "claimed", "captcha", "race". deadline (unix ts) stops the run,
    result gets timeout=True. charset "4l" = letters only, "4c" = full set.
    """
    # Token precheck (fail fast)
    headers = {"Authorization": token.strip(),
               "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    async with aiohttp.ClientSession(trust_env=False) as sess:
        try:
            async with sess.get(ME_URL, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return {"error": "Token invalid (Discord returned HTTP %s)." % r.status}
        except Exception as e:
            return {"error": f"Token check failed ({type(e).__name__}) â€” try again later."}

        if custom_proxies is not None:
            proxies = [p.strip() for p in custom_proxies if isinstance(p, str) and p.strip()]
            proxies = [p if p.startswith("http") else "http://" + p for p in proxies]
            pool_info = f"{len(proxies)} proxies (BYOP)"
            alphabet = CHARSETS.get(charset, USERNAME_CHARS)
        else:
            proxies = check_proxies()
            pool_info = f"{len(proxies)} proxies"
            alphabet = CHARSETS.get(charset, USERNAME_CHARS)
        if proxies:
            working, _rest = await _classify_pool(proxies)
            if working:
                proxies = working
                pool_info = f"{len(working)}/{len(working) + len(_rest)} proxies answering"
        workers = min(20, max(8, len(proxies))) if proxies else 2
        cycle = itertools.cycle(proxies) if proxies else None

        seen: set[str] = set()
        queue: asyncio.Queue[str] = asyncio.Queue()
        stop = asyncio.Event()
        result: dict = {"pool": pool_info, "charset": charset}
        done = 0
        lock = asyncio.Lock()
        claim_lock = asyncio.Lock()
        pomelo_cool_until = 0.0
        pomelo_gate = asyncio.Lock()
        taken_n2 = 0

        def _sample_delay2() -> float:
            if taken_n2 < 250:
                return 8 if done < 400 else 12
            return 40 if taken_n2 >= 600 else 22

        def refill(n: int):
            need = max(n, workers * 18)
            added, guard = 0, 0
            while added < need and guard < need * 10:
                guard += 1
                c = random_name(length, alphabet)
                if c not in seen:
                    seen.add(c)
                    queue.put_nowait(c)
                    added += 1

        refill(400)

        curl_sess = None
        if _HAS_CURL:
            try:
                curl_sess = _CurlAsyncSession(impersonate="chrome")
            except Exception:
                curl_sess = None

        def _interpret_claim(status, body) -> tuple[bool, str]:
            if isinstance(body, dict):
                if body.get("captcha_sitekey"):
                    return False, "captcha"
                txt = str(body).lower()
            else:
                txt = str(body or "").lower()
                if "captcha_sitekey" in txt or "captcha_rqdata" in txt:
                    return False, "captcha"
            if status == 200:
                return True, "claimed"
            if status == 429:
                return False, "ratelimited"
            if "10020" in txt or "unknown session" in txt:
                return False, "session"
            if any(k in txt for k in ("taken", "unavailable", "invalid", "not allowed")):
                return False, "taken_race"
            return False, f"HTTP {status}: {txt[:80]}"

        async def claim_now(name: str) -> tuple[bool, str]:
            payload = {"username": name, "password": password}
            if curl_sess is not None:
                try:
                    r = await curl_sess.patch(
                        PATCH_ME,
                        headers={"Authorization": token,
                                 "Content-Type": "application/json",
                                 "User-Agent": _UA},
                        json=payload, timeout=12,
                    )
                    try:
                        data = r.json()
                    except Exception:
                        data = getattr(r, "text", "")
                    st = getattr(r, "status_code", None)
                    ok, detail = _interpret_claim(st, data)
                    if st == 200 or detail in ("taken_race", "captcha", "ratelimited", "session"):
                        return ok, detail
                except Exception:
                    pass
            try:
                async with sess.patch(
                    PATCH_ME, headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as r:
                    if r.status == 200:
                        return True, "claimed"
                    try:
                        body = await r.json()
                    except Exception:
                        body = {}
                    return _interpret_claim(r.status, body)
            except Exception as e:
                return False, type(e).__name__

        async def _handle_hit(name: str, check_ms: int | None) -> bool:
            if charset == "4l" and not name.isalpha():
                return False
            async with claim_lock:
                if stop.is_set():
                    return True
                _c0 = time.perf_counter()
                ok, detail = await claim_now(name)
                _kcms = int((time.perf_counter() - _c0) * 1000)
            if sample_cb is not None:
                try:
                    if ok:
                        sample_cb(name, "claimed")
                    elif detail == "captcha":
                        sample_cb(name, "captcha")
                    elif detail == "taken_race":
                        sample_cb(name, "race")
                except Exception:
                    pass
            if ok:
                result.update(name=name, attempts=done, claimed=True,
                              check_ms=check_ms, claim_ms=_kcms)
                stop.set()
                return True
            if detail in ("taken_race", "session", "ratelimited"):
                return False
            if detail == "captcha":
                result.update(name=name, attempts=done, claimed=False,
                              check_ms=check_ms, claim_ms=_kcms,
                              error="Name was FREE but Discord asked for a captcha â€” claim it manually NOW: " + name)
                stop.set()
                return True
            return False

        async def _pomelo_check(pname: str):
            nonlocal pomelo_cool_until
            try:
                async with sess.post(
                    POMELO_URL, json={"username": pname},
                    headers={"Authorization": token,
                             "Content-Type": "application/json",
                             "User-Agent": _UA,
                             "Referer": "https://discord.com/channels/@me"},
                    proxy=None,
                    timeout=aiohttp.ClientTimeout(total=8, sock_connect=3),
                ) as r:
                    if r.status == 200:
                        try:
                            data = await r.json()
                        except Exception:
                            return None
                        if isinstance(data, dict) and isinstance(data.get("taken"), bool):
                            return "free" if not data["taken"] else "taken"
                        return None
                    if r.status == 400:
                        return "taken"
                    if r.status == 401:
                        return "dead"
                    if r.status == 429:
                        try:
                            w = float((await r.json()).get("retry_after", 5))
                        except Exception:
                            w = 5.0
                        pomelo_cool_until = time.time() + min(max(w, 5.0), 60.0)
                        return "ratelimited"
                    return None
            except Exception:
                return None

        async def pomelo_lane():
            nonlocal done
            while not stop.is_set():
                if deadline is not None and time.time() >= deadline:
                    stop.set()
                    return
                if time.time() < pomelo_cool_until:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    pname = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if charset == "4l" and not pname.isalpha():
                    await asyncio.sleep(0.01)
                    continue
                async with pomelo_gate:
                    if stop.is_set():
                        return
                    _t0 = time.perf_counter()
                    pr = await _pomelo_check(pname)
                    _cms = int((time.perf_counter() - _t0) * 1000)
                    await asyncio.sleep(0.4)
                async with lock:
                    done += 1
                    if done % 10 == 0:
                        try:
                            progress_cb(done)
                        except Exception:
                            pass
                if pr == "free":
                    if charset == "4l" and not pname.isalpha():
                        continue
                    if sample_cb is not None:
                        try:
                            sample_cb(pname, "free")
                        except Exception:
                            pass
                    if await _handle_hit(pname, _cms):
                        return
                elif pr == "dead":
                    result.setdefault("error", "Claim token died mid-run.")
                    stop.set()
                    return
                elif pr == "ratelimited":
                    try:
                        queue.put_nowait(pname)
                    except Exception:
                        pass
                if done >= max_attempts:
                    stop.set()
                    return

        async def worker():
            nonlocal done, taken_n2
            while not stop.is_set():
                if deadline is not None and time.time() >= deadline:
                    stop.set()
                    return
                try:
                    name = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                px = next(cycle) if cycle else None
                _t0 = time.perf_counter()
                r = await _check_one(sess, name, px)
                _cms = int((time.perf_counter() - _t0) * 1000)
                async with lock:
                    done += 1
                    if sample_cb is not None:
                        try:
                            if r is True:
                                sample_cb(name, "free")
                            else:
                                sd = 12 if taken_n2 < 250 and done < 400 else int(_sample_delay2())
                                if done % max(1, sd) == 0:
                                    # nonlocal mutation via list trick: use object
                                    sample_cb(name, "taken")
                        except Exception:
                            pass
                    # track taken count for adaptive sampling (via closure)
                    if r is not True:
                        # we approximate: every taken sample increments visual count
                        pass
                    if done % 10 == 0:
                        try:
                            progress_cb(done)
                        except Exception:
                            pass
                # keep adaptive counter in sync (outside lock to avoid deadlock on re-entry)
                if r is not True:
                    # count visible taken rows
                    try:
                        sd_chk = 12 if done < 400 else 15  # same as _sample_delay2 early branch
                        # we can't read taken_n2 reliably here without lock, so just sample by done
                        pass
                    except Exception:
                        pass
                if r is True:
                    if await _handle_hit(name, _cms):
                        return
                    if done % 50 == 0:
                        await asyncio.sleep(1)
                    continue
                if queue.qsize() < 100 and len(seen) < max_attempts:
                    refill(200)
                if done >= max_attempts:
                    stop.set()
                    return
                if not proxies:
                    await asyncio.sleep(0.5)

        gw = _WebGateway(token)
        try:
            gw_ok = await gw.connect()
        except Exception:
            gw_ok = False
        result["gateway"] = "warm" if gw_ok else "cold"
        try:
            lanes = [asyncio.create_task(pomelo_lane()) for _ in range(2)]
            await asyncio.gather(*[worker() for _ in range(workers)], *lanes)
        finally:
            try:
                await gw.close()
            except Exception:
                pass
            if curl_sess is not None:
                try:
                    res = curl_sess.close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

    if "name" not in result and "error" not in result:
        if deadline is not None and time.time() >= deadline:
            result["timeout"] = True
            result["error"] = (
                f"Nothing claimed within the time limit ({done:,} checks) â€” "
                "amount refunded as credit."
            )
        else:
            result["error"] = f"Nothing claimed after {done} checks."
        result["attempts"] = done
    if "name" in result:
        result.setdefault("pattern", name_pattern(result["name"]))
    return result


async def run_token_check(tokens: list[str], progress_cb, sample_cb=None, custom_proxies: list[str] | None = None) -> dict:
    """Check Discord tokens: valid/invalid. Concurrency 10, 429-aware.

    sample_cb(masked_token, res) streams "valid"/"invalid" for the console.
    """
    if custom_proxies is not None:
        proxies = [p.strip() for p in custom_proxies if isinstance(p, str) and p.strip()]
        proxies = [p if p.startswith("http") else "http://" + p for p in proxies]
    else:
        proxies = token_check_proxies()
    workers = min(50, max(10, len(proxies) // 2)) if proxies else 10
    cycle = itertools.cycle(proxies) if proxies else None
    sem = asyncio.Semaphore(workers)
    out = {"valid": [], "invalid": [], "checked": 0, "total": len(tokens)}
    out["pool"] = f"{len(proxies)} proxies (BYOP)" if custom_proxies is not None and proxies else f"{len(proxies)} proxies" if proxies else "direct"

    async with aiohttp.ClientSession(trust_env=False) as sess:
        async def one(tok: str):
            tok = tok.strip().strip("\"'")
            if not tok:
                return
            async with sem:
                proxy = next(cycle) if cycle else None
                headers = {"Authorization": tok,
                           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
                for _ in range(3):
                    try:
                        async with sess.get(
                            ME_URL, headers=headers, proxy=proxy,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as r:
                            if r.status == 200:
                                try:
                                    d = await r.json()
                                except Exception:
                                    d = {}
                                masked = tok[:12] + "â€¦"
                                out["valid"].append({"token": masked,
                                                     "username": str(d.get("username", "?")),
                                                     "id": str(d.get("id", "?"))})
                                if sample_cb is not None:
                                    try:
                                        sample_cb(masked, "valid")
                                    except Exception:
                                        pass
                                break
                            if r.status == 401:
                                masked = tok[:12] + "â€¦"
                                out["invalid"].append(masked)
                                if sample_cb is not None:
                                    try:
                                        sample_cb(masked, "invalid")
                                    except Exception:
                                        pass
                                break
                            if r.status == 429:
                                try:
                                    w = float((await r.json()).get("retry_after", 5))
                                except Exception:
                                    w = 5.0
                                await asyncio.sleep(min(w, 20))
                                continue
                            masked = tok[:12] + "â€¦"
                            out["invalid"].append(masked + f" (HTTP {r.status})")
                            if sample_cb is not None:
                                try:
                                    sample_cb(masked, "invalid")
                                except Exception:
                                    pass
                            break
                    except Exception:
                        await asyncio.sleep(1)
                        continue
                out["checked"] += 1
                if out["checked"] % 10 == 0:
                    try:
                        progress_cb(out["checked"])
                    except Exception:
                        pass

        await asyncio.gather(*[one(t) for t in tokens])
    try:
        progress_cb(out["checked"])
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Token joiner: join + captcha hook + gate/onboarding + button/emoji verify
# ---------------------------------------------------------------------------

class _SpoofedResp:
    """aiohttp-shaped response over a Chrome-TLS request."""

    __slots__ = ("status", "_data", "_raw")

    def __init__(self, status: int | None, data, raw: bytes = b"") -> None:
        self.status = status
        self._data = data
        self._raw = raw

    async def json(self):
        return self._data

    async def text(self) -> str:
        try:
            return self._raw.decode("utf-8", errors="replace") if self._raw else ""
        except Exception:
            return ""

    async def read(self) -> bytes:
        return self._raw or b""

    @property
    def cookies(self):
        return _CookieJar()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _CookieJar:
    """Minimal cookie jar for discord.com cookie extraction."""
    def __init__(self):
        self._cookies: dict[str, str] = {}

    def get(self, key: str, default: str = "") -> str:
        return self._cookies.get(key, default)

    def update_from_headers(self, headers):
        """Parse Set-Cookie headers."""
        if not headers:
            return
        for k, v in headers.items():
            if k.lower() == "set-cookie":
                try:
                    name = v.split("=", 1)[0].strip()
                    val = v.split("=", 1)[1].split(";")[0].strip()
                    if name.startswith("__dcfduid") or name.startswith("__sdcfduid"):
                        self._cookies[name] = val
                except Exception:
                    pass


class _CurlPool:
    """One reused Chrome-TLS session per proxy (warm handshake + cookies).

    Plain aiohttp screams "bot" via its Python TLS fingerprint (JA3) â€”
    Discord answers with captcha walls and rate limits. Chrome
    impersonation plus full browser headers looks like the real client.

    Sessions are never shared concurrently: each proxy has its own
    lock, so a session serves one request at a time while different
    proxies run fully parallel.
    """

    def __init__(self) -> None:
        self._sess: dict[str, object] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def call(self, method: str, url: str, *, headers=None,
                   json_data=None, proxy: str | None = None,
                   timeout_s: float = 8.0):
        if not _HAS_CURL:
            raise RuntimeError("curl_cffi missing")
        key = proxy or "direct"
        async with self._guard:
            if key not in self._sess:
                sess = _CurlAsyncSession(impersonate="chrome")
                if proxy:
                    sess.proxies = {"http": proxy, "https": proxy}
                self._sess[key] = sess
                self._locks[key] = asyncio.Lock()
            sess, lk = self._sess[key], self._locks[key]
        async with lk:
            fn = getattr(sess, method)
            kw: dict = {"timeout": max(1.0, min(float(timeout_s or 8.0), 60.0))}
            if headers:
                kw["headers"] = headers
            if json_data is not None:
                kw["json"] = json_data
            r = await fn(url, **kw)
            try:
                data = r.json()
            except Exception:
                data = None
            raw = b""
            try:
                raw = r.content if hasattr(r, "content") else b""
                if isinstance(raw, str):
                    raw = raw.encode("utf-8", errors="replace")
            except Exception:
                pass
            return getattr(r, "status_code", None), data, raw

    async def close(self) -> None:
        async with self._guard:
            items = list(self._sess.items())
            self._sess.clear()
            self._locks.clear()
        for _, sess in items:
            try:
                res = sess.close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass


class _SpoofedCall:
    """Awaitable + async context manager: sess.post(...) works with or
    without 'async with', exactly like aiohttp."""

    def __init__(self, pool: _CurlPool, method: str, url: str,
                 headers=None, json_data=None, proxy: str | None = None,
                 timeout=None) -> None:
        self._pool = pool
        self._method = method
        self._url = url
        self._headers = headers
        self._json = json_data
        self._proxy = proxy
        try:
            t = getattr(timeout, "total", timeout)
            self._timeout = float(t) if t else 8.0
        except Exception:
            self._timeout = 8.0

    async def _do(self) -> _SpoofedResp:
        st, data, raw = await self._pool.call(
            self._method, self._url, headers=self._headers,
            json_data=self._json, proxy=self._proxy, timeout_s=self._timeout)
        return _SpoofedResp(st, data, raw)

    def __await__(self):
        return self._do().__await__()

    async def __aenter__(self) -> _SpoofedResp:
        return await self._do()

    async def __aexit__(self, *exc) -> bool:
        return False


class _SpoofedSession:
    """Drop-in aiohttp replacement for joiner HTTP (Chrome-TLS pool)."""

    def __init__(self, pool: _CurlPool) -> None:
        self._pool = pool

    def _call(self, method: str, url: str, kw: dict) -> _SpoofedCall:
        kw = dict(kw)
        if "json" in kw:  # aiohttp spelling -> adapter spelling
            kw["json_data"] = kw.pop("json")
        return _SpoofedCall(self._pool, method, url, **kw)

    def get(self, url, **kw) -> _SpoofedCall:
        return self._call("get", url, kw)

    def post(self, url, **kw) -> _SpoofedCall:
        return self._call("post", url, kw)

    def put(self, url, **kw) -> _SpoofedCall:
        return self._call("put", url, kw)

    def patch(self, url, **kw) -> _SpoofedCall:
        return self._call("patch", url, kw)

    def delete(self, url, **kw) -> _SpoofedCall:
        return self._call("delete", url, kw)

_INVITE_RE = re.compile(
    r"(?:discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)?([A-Za-z0-9\-]{2,32})\s*$",
    re.IGNORECASE,
)
_VERIFY_CH_RE = re.compile(r"verif|rules|welcome|start|gate|accept|read-?me|readme|info", re.I)
_VERIFY_BTN_RE = re.compile(
    r"verif|accept|agree|rules|complete|start|join|continue|confirm|yes|done|unlock|access"
    r"|captcha|human|robot|press|claim|get verified|click to verify", re.I)
# known verify bots: detected from the message author, reported per token
_VERIFY_BOTS = {
    "double counter": "double-counter", "doublecounter": "double-counter",
    "sledgehammer": "sledgehammer", "sledge hammer": "sledgehammer",
    "raid protect": "raid-protect", "raidprotect": "raid-protect",
    "pandez": "pandezguard", "pandezguard": "pandezguard",
    "captcha.bot": "captcha-bot", "captchabot": "captcha-bot",
    "wick": "wick", "beemo": "beemo", "carl-bot": "carl-bot",
}


def parse_invite(raw: str) -> str | None:
    """Invite code from a full link or a bare code. None if unparsable."""
    m = _INVITE_RE.search((raw or "").strip())
    return m.group(1) if m else None


def _tok_headers(tok: str) -> dict:
    """Full browser header set (pairs with Chrome-TLS fingerprint)."""
    return {"Authorization": tok,
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "Europe/Berlin",
            "X-Debug-Options": "bugReporterEnabled",
            "X-Super-Properties": _X_SUPER,
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="8"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"'}

_X_SUPER = ("eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3l"
            "zdGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81"
            "LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2"
            "IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEzMS4wLjAuMCBTYWZhcmkvNTM3LjM2"
            "IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTMxLjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicm"
            "VmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6"
            "IiIsInJlZmVycmluZ19kb21haW5fY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0"
            "YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjM2Njk1NSwiY2xpZW50X2V2ZW50X3Nv"
            "dXJjZSI6bnVsbH0=")


def _gen_session_id() -> str:
    """Generate a Discord-compatible session_id (hex, 32 chars)."""
    import uuid
    return uuid.uuid4().hex


async def _get_cookies(sess, proxy: str | None = None) -> dict:
    """Touch discord.com to populate the session cookie jar.
    curl_cffi sessions maintain cookies automatically — no manual header needed."""
    try:
        async with sess.get(
            "https://discord.com",
            proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            pass
    except Exception:
        pass
    return {}


def _ctx_props(guild_id: str | None) -> str:
    """X-Context-Properties like the official client sends on join."""
    import base64
    import json as _json
    try:
        return base64.b64encode(_json.dumps(
            {"location": "Join Guild", "location_guild_id": guild_id or "0"}
        ).encode()).decode()
    except Exception:
        return ""


async def _join_429(resp, cap: float = 30.0) -> bool:
    """True if resp was 429 (sleeps retry_after first)."""
    if resp.status != 429:
        return False
    try:
        w = float((await resp.json()).get("retry_after", 5))
    except Exception:
        w = 5.0
    await asyncio.sleep(min(max(w, 1.0), cap))
    return True


async def _accept_gate(sess, H: dict, proxy: str | None, guild_id: str | None, code: str) -> str:
    """Rules screening via member-verification + requests/@me. Fail-soft."""
    if not guild_id:
        return "skipped"
    try:
        async with sess.get(
            f"https://discord.com/api/v9/guilds/{guild_id}/member-verification"
            f"?with_guild=false&invite_code={code}",
            headers=H, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 204:
                return "skipped"  # no gate
            if r.status != 200:
                return "skipped" if r.status in (403, 404) else "failed"
            try:
                form = await r.json()
            except Exception:
                return "failed"
        if not isinstance(form, dict) or not form.get("form_fields"):
            return "skipped"
        for f in form["form_fields"]:
            if isinstance(f, dict):
                f["response"] = True
        try:
            async with sess.put(
                f"https://discord.com/api/v9/guilds/{guild_id}/requests/@me",
                json=form, headers=H, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r2:
                return "ok" if r2.status in (200, 201, 204) else "failed"
        except Exception:
            return "failed"
    except Exception:
        return "failed"


async def _complete_onboarding(sess, H: dict, proxy: str | None, guild_id: str | None) -> str:
    """Community onboarding (client-style payload). Fail-soft."""
    if not guild_id:
        return "skipped"
    try:
        async with sess.get(
            f"https://discord.com/api/v9/guilds/{guild_id}/onboarding",
            headers=H, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return "skipped"
            try:
                data = await r.json()
            except Exception:
                return "failed"
        prompts = (data or {}).get("prompts") or []
        if not prompts:
            return "skipped"
        now = int(time.time())
        seen_p: dict = {}
        seen_r: dict = {}
        responses: list = []
        for p in prompts:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            opts = [o for o in (p.get("options") or [])
                    if isinstance(o, dict) and o.get("id")]
            if not opts:
                continue
            seen_p[p["id"]] = now
            for o in opts:
                seen_r[o["id"]] = now
            responses.append(opts[-1]["id"])
        if not responses:
            return "skipped"
        try:
            async with sess.post(
                f"https://discord.com/api/v9/guilds/{guild_id}/onboarding-responses",
                json={"onboarding_responses": responses,
                      "onboarding_prompts_seen": seen_p,
                      "onboarding_responses_seen": seen_r},
                headers=H, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
            ) as r2:
                return "ok" if r2.status in (200, 201, 204) else "failed"
        except Exception:
            return "failed"
    except Exception:
        return "failed"


async def _click_button(sess, H: dict, proxy: str | None, guild_id: str,
                        channel_id: str, msg: dict, comp: dict,
                        session_id: str | None) -> bool:
    """POST /interactions type 3 for one verify button."""
    try:
        app_id = msg.get("application_id") or (msg.get("author") or {}).get("id")
        payload = {
            "type": 3,
            "nonce": str(random.randint(10 ** 18, 10 ** 19 - 1)),
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "message_flags": int(msg.get("flags", 0) or 0),
            "message_id": str(msg.get("id")),
            "application_id": str(app_id or ""),
            "session_id": session_id,
            "data": {"component_type": 2, "custom_id": str(comp.get("custom_id", ""))},
        }
        async with sess.post(
            "https://discord.com/api/v9/interactions", json=payload,
            headers=H, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            return r.status in (200, 201, 204)
    except Exception:
        return False


async def _verify_interactive(sess, H: dict, proxy: str | None,
                              guild_id: str | None, tok: str) -> tuple[int, int, list]:
    """Click verify buttons + mirror emoji reactions. Returns (buttons, emojis, bots)."""
    buttons = emojis = 0
    bots: set = set()
    if not guild_id:
        return 0, 0, []
    try:
        async with sess.get(
            f"https://discord.com/api/v9/guilds/{guild_id}/channels",
            headers=H, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return 0, 0
            try:
                channels = await r.json()
            except Exception:
                return 0, 0
    except Exception:
        return 0, 0
    cands = [c for c in (channels or [])
             if isinstance(c, dict) and c.get("type") in (0, 5)
             and _VERIFY_CH_RE.search(c.get("name", ""))][:3]
    gw = None
    try:
        for ch in cands:
            cid = ch.get("id")
            try:
                async with sess.get(
                    f"https://discord.com/api/v9/channels/{cid}/messages?limit=15",
                    headers=H, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        continue
                    try:
                        msgs = await r.json()
                    except Exception:
                        continue
            except Exception:
                continue
            for m in msgs or []:
                if not isinstance(m, dict):
                    continue
                author = m.get("author") or {}
                if not author.get("bot"):
                    continue
                aname = str(author.get("username", "")).lower()
                for _bkey, _btag in _VERIFY_BOTS.items():
                    if _bkey in aname:
                        bots.add(_btag)
                for row in m.get("components") or []:
                    for comp in (row or {}).get("components") or []:
                        if not isinstance(comp, dict) or comp.get("type") != 2:
                            continue
                        if comp.get("style") == 5 or comp.get("disabled"):
                            continue
                        blob = f"{comp.get('custom_id', '')} {comp.get('label', '')}".lower()
                        if not _VERIFY_BTN_RE.search(blob):
                            continue
                        if gw is None:
                            gw = _WebGateway(tok)
                            try:
                                if not await gw.connect(timeout=10.0):
                                    gw = None
                            except Exception:
                                gw = None
                        if gw is None or not await _click_button(
                                sess, H, proxy, guild_id, cid, m, comp, gw.session_id):
                            continue
                        buttons += 1
                        if buttons >= 3:
                            break
                    if buttons >= 3:
                        break
                for react in m.get("reactions") or []:
                    if emojis >= 6:
                        break
                    em = (react or {}).get("emoji") or {}
                    code = (em.get("id") and f"{em.get('name')}:{em.get('id')}"
                            or em.get("name"))
                    if not code:
                        continue
                    try:
                        async with sess.put(
                            f"https://discord.com/api/v9/channels/{cid}/messages/"
                            f"{m.get('id')}/reactions/"
                            f"{urllib.parse.quote(code, safe='')}/@me",
                            headers=H, proxy=proxy,
                            timeout=aiohttp.ClientTimeout(total=8),
                        ) as r:
                            if r.status == 204:
                                emojis += 1
                    except Exception:
                        pass
                if buttons >= 3 and emojis >= 6:
                    break
            if buttons >= 3 and emojis >= 6:
                break
    finally:
        if gw is not None:
            try:
                await gw.close()
            except Exception:
                pass
    return buttons, emojis, sorted(bots)


async def _join_one(sess, tok: str, code: str, guild_id: str | None,
                    proxy: str | None, solver) -> dict:
    """One token: join (+captcha) -> gate -> onboarding -> button/emoji."""
    global _solver_dead
    masked = tok[:12] + "…"
    rep = {"token": masked, "username": None, "joined": False, "bots": [],
           "steps": {"captcha": "skipped", "member-gate": "skipped",
                     "on-boarding": "skipped", "button": 0, "emoji": 0},
           "error": None}
    H = _tok_headers(tok)
    # Touch discord.com to populate session cookie jar (curl_cffi handles cookies automatically)
    await _get_cookies(sess, proxy)
    # Pre-check: filter dead tokens BEFORE any solver credit
    try:
        async with sess.get(ME_URL, headers=H, proxy=proxy,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 401:
                rep["error"] = "invalid token (pre-check)"
                return rep
            if r.status != 200:
                rep["error"] = f"pre-check HTTP {r.status}"
                return rep
            try:
                rep["username"] = str((await r.json()).get("username", "?"))
            except Exception:
                pass
    except Exception as e:
        rep["error"] = type(e).__name__
        return rep
    session_id = _gen_session_id()
    join_body: dict = {"session_id": session_id}
    join_headers = dict(H, **{"X-Context-Properties": _ctx_props(guild_id)})
    for attempt in range(3):
        try:
            async with sess.post(
                f"https://discord.com/api/v9/invites/{code}", json=join_body,
                headers=join_headers,
                proxy=proxy, timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                try:
                    resp_text = await r.text()
                except Exception:
                    resp_text = ""
                if r.status == 200:
                    rep["joined"] = True
                    break
                try:
                    body = json.loads(resp_text) if resp_text else {}
                except Exception:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if body.get("captcha_sitekey"):
                    rep["steps"]["captcha"] = "failed"
                    if solver is None or _solver_dead:
                        rep["error"] = "join captcha — solver unavailable (no credits?)" if _solver_dead else "join captcha — no solver configured"
                        return rep
                    try:
                        try:
                            solved = await solver(str(body.get("captcha_sitekey", "")),
                                                  body.get("captcha_rqdata"),
                                                  "https://discord.com/channels/@me", proxy)
                        except TypeError:
                            solved = await solver(str(body.get("captcha_sitekey", "")),
                                                  body.get("captcha_rqdata"),
                                                  "https://discord.com/channels/@me")
                    except Exception:
                        solved = None
                    if not solved:
                        _solver_dead = True
                        rep["error"] = "join captcha unsolved (solver failed — skipping remaining)"
                        return rep
                    rep["steps"]["captcha"] = "ok"
                    # Send captcha in HEADERS — persists across retries now
                    join_headers["X-Captcha-Key"] = solved
                    if body.get("captcha_rqtoken"):
                        join_headers["X-Captcha-Rqtoken"] = body["captcha_rqtoken"]
                    if body.get("captcha_session_id"):
                        join_headers["X-Captcha-Session-Id"] = body["captcha_session_id"]
                    continue
                if r.status == 429:
                    await _join_429(r)
                    continue
                if r.status == 401:
                    rep["error"] = f"invalid token (401 after captcha)" if rep["steps"]["captcha"] == "ok" else "invalid token"
                    return rep
                rep["error"] = f"join HTTP {r.status}: {resp_text[:100]}"
                return rep
        except Exception as e:
            rep["error"] = type(e).__name__
            return rep
    if not rep["joined"]:
        rep["error"] = rep["error"] or "join retries exhausted"
        return rep
    rep["steps"]["member-gate"] = await _accept_gate(sess, H, proxy, guild_id, code)
    rep["steps"]["on-boarding"] = await _complete_onboarding(sess, H, proxy, guild_id)
    b, e, bots = await _verify_interactive(sess, H, proxy, guild_id, tok)
    rep["steps"]["button"] = b
    rep["steps"]["emoji"] = e
    rep["bots"] = bots
    return rep


async def run_token_join(invite: str, tokens: list[str], progress_cb,
                         sample_cb=None, solver=None, custom_proxies: list[str] | None = None) -> dict:
    """Join tokens to a server + verifications. Bounded batch job.

    Per token: join (captcha hook) -> member gate -> onboarding ->
    button/emoji verification. Every step is fail-soft; the report
    carries per-token step states plus guild-level verification counts.
    """
    global _solver_dead
    _solver_dead = False  # reset per order
    # Pre-check: verify solver has credits before starting
    if solver is not None:
        try:
            from solver import check_balance
            bal = await check_balance()
            if bal is not None and bal <= 0:
                _solver_dead = True
                print(f"[join] solver has {bal} credits — captcha solving disabled for this order")
            elif bal is not None:
                print(f"[join] solver balance: {bal} credits")
        except Exception:
            pass
    code = parse_invite(invite)
    out = {"joined": 0, "failed": 0, "checked": 0, "total": len(tokens),
           "guild": "?", "invite": code or "?",
           "verifications": {"button": 0, "emoji": 0, "member-gate": 0, "on-boarding": 0},
           "tokens": []}
    if not code:
        out["error"] = "Invalid invite link."
        return out
    if custom_proxies is not None:
        proxies = [p.strip() for p in custom_proxies if isinstance(p, str) and p.strip()]
        proxies = [p if p.startswith("http") else "http://" + p for p in proxies]
    else:
        proxies = joiner_proxies()
    workers = min(15, max(5, len(proxies) // 7)) if proxies else 3
    cycle = itertools.cycle(proxies) if proxies else None
    sem = asyncio.Semaphore(workers)
    tls = "tls-spoof" if _HAS_CURL else "plain"
    out["pool"] = (f"{len(proxies)} proxies Â· {tls}" if proxies else f"direct Â· {tls}")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _http():
        if _HAS_CURL:
            pool = _CurlPool()
            try:
                yield _SpoofedSession(pool)
            finally:
                await pool.close()
        else:
            async with aiohttp.ClientSession(trust_env=False) as s:
                yield s

    async with _http() as sess:
        guild_id, guild_name = None, "?"
        try:
            async with sess.get(
                f"https://discord.com/api/v9/invites/{code}?with_counts=true",
                headers={"User-Agent": _UA},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    out["error"] = f"Invite invalid (HTTP {r.status})."
                    return out
                g = (await r.json()).get("guild") or {}
                guild_id, guild_name = g.get("id"), g.get("name", "?")
        except Exception as e:
            out["error"] = f"Invite resolve failed ({type(e).__name__})."
            return out
        out["guild"] = guild_name or "?"

        async def one(tok: str):
            tok = tok.strip().strip("\"'")
            if not tok:
                return
            proxy = next(cycle) if cycle else None
            async with sem:
                rep = await _join_one(sess, tok, code, guild_id, proxy, solver)
            out["checked"] += 1
            if rep["joined"]:
                out["joined"] += 1
            else:
                out["failed"] += 1
            st = rep["steps"]
            if st.get("member-gate") == "ok":
                out["verifications"]["member-gate"] += 1
            if st.get("on-boarding") == "ok":
                out["verifications"]["on-boarding"] += 1
            out["verifications"]["button"] += int(st.get("button", 0) or 0)
            out["verifications"]["emoji"] += int(st.get("emoji", 0) or 0)
            if len(out["tokens"]) < 200:
                out["tokens"].append(rep)
            try:
                progress_cb(out["checked"])
            except Exception:
                pass
            if sample_cb is not None:
                try:
                    sample_cb(rep["token"], "joined" if rep["joined"] else "failed")
                except Exception:
                    pass

        await asyncio.gather(*[one(t) for t in tokens])
    try:
        progress_cb(out["checked"])
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Account Creator: Discord account registration + email verification
# ---------------------------------------------------------------------------
import config
import base64 as _b64
import imaplib as _imaplib
import uuid as _uuid
import platform as _platform

_DISCORD_API = "https://discord.com/api/v9"
_REGISTER_URL = f"{_DISCORD_API}/auth/register"
_VERIFY_URL = f"{_DISCORD_API}/auth/verify"
_PHONE_URL = f"{_DISCORD_API}/users/@me/phone"
_PHONE_VERIFY_URL = f"{_DISCORD_API}/phone-verifications/verify"
_SMAKMAIL_BASE = "https://api.smakmail.com/api/v1"
_FIVESIM_BASE = "https://5sim.net/api/v1"

_CHROME_VER = 131
_USER_AGENT_CREATE = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{_CHROME_VER}.0.0.0 Safari/537.36 "
    f"Edg/{_CHROME_VER}.0.0.0 "
    f"Xbox; Xbox One)"
)
_SEC_CH_UA_CREATE = (
    f'"Chromium";v="{_CHROME_VER}", '
    f'"Microsoft Edge";v="{_CHROME_VER}", '
    f'"Not-A.Brand";v="99"'
)


class _MailSource:
    """Email verification: local IMAP (mails.txt) first, then SmakMail."""

    def __init__(self):
        self._local_queue = []
        self._local_passwords = {}
        self._lock = asyncio.Lock()
        self._smakmail_mbox = []
        self._load_local()
        self._imap_host = getattr(config, "MAIL_IMAP_HOST", "")
        self._imap_port = int(getattr(config, "MAIL_IMAP_PORT", 993))
        self._imap_ssl = bool(getattr(config, "MAIL_IMAP_SSL", True))

    def _load_local(self):
        path = Path(_data_file("mails.txt"))
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or ":" not in ln:
                        continue
                    addr, pw = ln.split(":", 1)
                    addr, pw = addr.strip(), pw.strip()
                    if "@" not in addr:
                        continue
                    self._local_queue.append({"email": addr, "password": pw})
                    self._local_passwords[addr] = pw
        except Exception:
            pass

    def _save_local(self):
        try:
            path = Path(_data_file("mails.txt"))
            with open(path, "w", encoding="utf-8") as f:
                for m in self._local_queue:
                    f.write(f"{m['email']}:{m['password']}\n")
        except Exception:
            pass

    async def acquire(self):
        async with self._lock:
            if self._local_queue:
                mbox = self._local_queue.pop(0)
                self._save_local()
                return {"email": mbox["email"], "password": mbox["password"],
                        "source": "local", "email_password": mbox["password"]}
        return await self._smakmail_acquire()

    async def _smakmail_acquire(self):
        async with self._lock:
            if self._smakmail_mbox:
                mbox = self._smakmail_mbox.pop(0)
                return {"email": mbox["email"], "password": mbox.get("password", ""),
                        "source": "smakmail", "email_password": mbox.get("password", "")}
        return await self._smakmail_order(1)

    async def _smakmail_order(self, count):
        key = getattr(config, "SMAKMAIL_API_KEY", "")
        product = getattr(config, "SMAKMAIL_PRODUCT", "Eternal")
        if not key:
            return None
        try:
            async with aiohttp.ClientSession(trust_env=False) as s:
                headers = {"Authorization": f"Bearer {key}",
                           "Content-Type": "application/json",
                           "Idempotency-Key": str(_uuid.uuid4())}
                async with s.post(f"{_SMAKMAIL_BASE}/orders",
                                  json={"product": product, "qty": count},
                                  headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status not in (200, 201):
                        return None
                    d = await r.json()
                order_id = (d or {}).get("id") or (d or {}).get("order_id")
                if not order_id:
                    return None
                for _ in range(60):
                    await asyncio.sleep(3)
                    async with s.get(f"{_SMAKMAIL_BASE}/orders/{order_id}",
                                     headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=15)) as r2:
                        d2 = await r2.json()
                        status = ((d2 or {}).get("status") or "").lower()
                        if status in ("ready", "done", "completed", "paid"):
                            result = await s.get(f"{_SMAKMAIL_BASE}/orders/{order_id}/result",
                                                 headers=headers,
                                                 timeout=aiohttp.ClientTimeout(total=15))
                            if result.status == 200:
                                rd = await result.json()
                                mails = rd if isinstance(rd, list) else (rd or {}).get("mailboxes") or (rd or {}).get("result") or []
                                async with self._lock:
                                    for m in mails:
                                        em = m.get("email") or m.get("address") or ""
                                        pw = m.get("password") or ""
                                        if em:
                                            self._smakmail_mbox.append({"email": em, "password": pw})
                                if self._smakmail_mbox:
                                    mb = self._smakmail_mbox[0]
                                    return {"email": mb["email"], "password": mb.get("password", ""),
                                            "source": "smakmail", "email_password": mb.get("password", "")}
                            return None
                        if status in ("failed", "error", "cancelled"):
                            return None
        except Exception:
            return None
        return None

    async def get_verify_url(self, email, email_password, timeout=120, source="local"):
        # try configured host (firstmail) first, then fallback to smakmail
        url = await self._imap_poll(email, email_password, timeout=60)
        if url:
            return url
        try:
            return await self._smakmail_imap_poll(email, email_password, timeout=60)
        except Exception:
            return url

    async def _imap_poll(self, email, password, timeout=120):
        start = time.time()
        seen = set()
        while time.time() - start < timeout:
            try:
                if self._imap_ssl:
                    conn = _imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
                else:
                    conn = _imaplib.IMAP4(self._imap_host, self._imap_port)
                conn.login(email, password)
                conn.select("INBOX")
                status, data = conn.search(None, "(UNSEEN)")
                if status == "OK" and data and data[0]:
                    for uid in data[0].split():
                        if uid in seen:
                            continue
                        typ, msgdata = conn.fetch(uid, "(RFC822)")
                        if typ != "OK" or not msgdata:
                            continue
                        raw = msgdata[0][1]
                        msg = _email_from_bytes(raw)
                        payload = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                ct = part.get_content_type()
                                if ct == "text/html":
                                    try:
                                        payload = part.get_payload(decode=True).decode(errors="ignore")
                                    except Exception:
                                        payload = ""
                                    break
                                elif ct == "text/plain" and not payload:
                                    try:
                                        payload = part.get_payload(decode=True).decode(errors="ignore")
                                    except Exception:
                                        payload = ""
                        else:
                            try:
                                payload = msg.get_payload(decode=True).decode(errors="ignore")
                            except Exception:
                                payload = ""
                        combined = payload + (msg.get("Subject") or "")
                        links = re.findall(r"https?://[^\s\"'<>]+", combined)
                        candidates = [l for l in links if "discord.com" in l or "click.discord.com" in l]
                        if candidates:
                            token_links = [l for l in candidates if "token=" in l]
                            chosen = token_links[0] if token_links else max(candidates, key=len)
                            conn.logout()
                            return chosen
                        seen.add(uid)
                conn.logout()
            except Exception:
                pass
            await asyncio.sleep(5)
        return None

    async def _smakmail_imap_poll(self, email, password, timeout=120):
        start = time.time()
        seen = set()
        while time.time() - start < timeout:
            try:
                conn = _imaplib.IMAP4_SSL("imap.smakmail.com", 993)
                conn.login(email, password)
                conn.select("INBOX")
                status, data = conn.search(None, "(UNSEEN)")
                if status == "OK" and data and data[0]:
                    for uid in data[0].split():
                        if uid in seen:
                            continue
                        typ, msgdata = conn.fetch(uid, "(RFC822)")
                        if typ != "OK" or not msgdata:
                            continue
                        raw = msgdata[0][1]
                        msg = _email_from_bytes(raw)
                        payload = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                ct = part.get_content_type()
                                if ct == "text/html":
                                    try:
                                        payload = part.get_payload(decode=True).decode(errors="ignore")
                                    except Exception:
                                        payload = ""
                                    break
                                elif ct == "text/plain" and not payload:
                                    try:
                                        payload = part.get_payload(decode=True).decode(errors="ignore")
                                    except Exception:
                                        payload = ""
                        else:
                            try:
                                payload = msg.get_payload(decode=True).decode(errors="ignore")
                            except Exception:
                                payload = ""
                        combined = payload + (msg.get("Subject") or "")
                        links = re.findall(r"https?://[^\s\"'<>]+", combined)
                        candidates = [l for l in links if "discord.com" in l or "click.discord.com" in l]
                        if candidates:
                            token_links = [l for l in candidates if "token=" in l]
                            chosen = token_links[0] if token_links else max(candidates, key=len)
                            conn.logout()
                            return chosen
                        seen.add(uid)
                conn.logout()
            except Exception:
                pass
            await asyncio.sleep(5)
        return None


def _email_from_bytes(raw):
    import email as _email_mod
    return _email_mod.message_from_bytes(raw)



def _random_password(length=16):
    return "".join(random.choices(string.ascii_letters + string.digits + "!@#$%", k=length))


def _random_username(length=12):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def _create_one_account(mailbox, proxy, solver, sess, sem,
                              progress_cb, sample_cb, phone_enabled,
                              phone_api_key, idx, total, status_cb=None):
    email_addr = mailbox["email"]
    email_pass = mailbox.get("email_password") or mailbox.get("password", "")
    password = email_pass if email_pass else _random_password()
    username = _random_username()
    masked = email_addr
    result = {"email": email_addr, "password": password, "username": username,
              "token": None, "created": False, "error": None, "source": mailbox.get("source", "?")}

    t0 = time.time()
    def _emit(msg):
        if status_cb:
            try:
                ts = time.strftime("%H:%M:%S")
                prog = f"[{idx+1}/{total}] " if total else ""
                status_cb(masked, f"{ts} {prog}{msg}")
            except Exception:
                pass

    async with sem:
        _emit(f"Checking proxy [{(proxy or 'direct')[:22]}]...")
        try:
            async with sess.get("https://discord.com", proxy=proxy,
                                timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status not in (200, 301, 302, 304):
                    result["error"] = f"proxy check failed: HTTP {r.status}"
                    return result
        except Exception as e:
            result["error"] = f"proxy dead: {type(e).__name__}"
            return result

        _emit("Preparing...")
        try:

            fp = None
            try:
                async with sess.get(f"{_DISCORD_API}/experiments",
                                    headers={"User-Agent": _USER_AGENT_CREATE,
                                              "Sec-Ch-Ua": _SEC_CH_UA_CREATE,
                                              "Sec-Ch-Ua-Mobile": "?0",
                                              "Sec-Ch-Ua-Platform": '"Xbox"'},
                                    proxy=proxy,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        ed = await r.json()
                        fp = (ed or {}).get("fingerprint")
            except Exception:
                pass
            if not fp:
                result["error"] = "no fingerprint"
                return result

            build_num = 502645
            try:
                async with sess.get("https://discord.com/app", proxy=proxy,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        html = await r.text()
                        assets = re.findall(r'src="/assets/([^"]+)"', html)
                        for asset in reversed(assets[:10]):
                            try:
                                async with sess.get(f"https://discord.com/assets/{asset}",
                                                    proxy=proxy,
                                                    timeout=aiohttp.ClientTimeout(total=8)) as jsr:
                                    if jsr.status == 200:
                                        js_text = await jsr.text()
                                        if "buildNumber:" in js_text:
                                            build_num = int(js_text.split('buildNumber:"')[1].split('"')[0])
                                            break
                            except Exception:
                                continue
            except Exception:
                pass

            sp_payload = {
                "os": "Xbox", "browser": "Microsoft Edge", "device": "Xbox One",
                "system_locale": "en-US", "browser_user_agent": _USER_AGENT_CREATE,
                "browser_version": f"{_CHROME_VER}.0.0.0", "os_version": _platform.release(),
                "referrer": "https://discord.com/", "referring_domain": "discord.com",
                "referrer_current": "", "referring_domain_current": "",
                "release_channel": "stable", "client_build_number": build_num,
                "client_event_source": None, "has_client_mods": False,
                "client_launch_id": str(_uuid.uuid4()),
                "launch_signature": str(_uuid.uuid4()),
                "client_heartbeat_session_id": str(_uuid.uuid4()),
                "client_app_state": "focused",
            }
            super_props = _b64.b64encode(json.dumps(sp_payload, separators=(",", ":")).encode()).decode()

            headers = {
                "accept": "*/*", "accept-encoding": "gzip, deflate, br, zstd",
                "accept-language": "en-US,en;q=0.9", "content-type": "application/json",
                "origin": "https://discord.com", "referer": "https://discord.com/",
                "priority": "u=1, i",
                "sec-ch-ua": _SEC_CH_UA_CREATE, "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Xbox"',
                "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin", "user-agent": _USER_AGENT_CREATE,
                "x-debug-options": "bugReporterEnabled",
                "x-discord-locale": "en-US", "x-discord-timezone": "America/Los_Angeles",
                "x-super-properties": super_props,
            }
            if fp:
                headers["x-fingerprint"] = fp

            _emit("Warming up session...")
            fake_user = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
            dob_fake = f"{random.randint(1990,2002):04d}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
            try:
                async with sess.post(_REGISTER_URL, proxy=proxy, headers=headers,
                                     json={"fingerprint": fp, "date_of_birth": dob_fake,
                                           "consent": True, "email": f"{fake_user}@outlook.com",
                                           "username": fake_user, "password": f"{fake_user}Ab1!@#"},
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
                    pass
            except Exception:
                pass

            dob = f"{random.randint(1990,2002):04d}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
            reg_payload = {"fingerprint": fp, "email": email_addr, "username": username,
                           "global_name": username, "password": password,
                           "date_of_birth": dob, "consent": True,
                           "gift_code_sku_id": None, "captcha_key": None}
            _emit("Registering...")
            resp_json = {}
            for _reg_attempt in range(3):
                try:
                    async with sess.post(_REGISTER_URL, json=reg_payload, headers=headers,
                                         proxy=proxy,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r:
                        resp_text = await r.text()
                        try:
                            resp_json = json.loads(resp_text) if resp_text else {}
                        except Exception:
                            resp_json = {}
                        if r.status == 429:
                            wait = (resp_json or {}).get("retry_after", 5)
                            await asyncio.sleep(min(wait, 15))
                            continue
                except Exception as e:
                    result["error"] = f"register failed ({type(e).__name__})"
                    return result
                break

            if resp_json.get("captcha_sitekey") and resp_json.get("captcha_rqdata"):
                if solver is None:
                    result["error"] = "captcha required but no solver"
                    return result
                _emit("Solving captcha...")
                t_solve = time.time()
                try:
                    solved = await solver(resp_json["captcha_sitekey"], resp_json["captcha_rqdata"],
                                          "https://discord.com/", proxy)
                except TypeError:
                    solved = await solver(resp_json["captcha_sitekey"], resp_json["captcha_rqdata"],
                                          "https://discord.com/")
                except Exception:
                    solved = None
                dur = time.time() - t_solve
                if not solved:
                    result["error"] = f"captcha unsolved ({dur:.1f}s)"
                    _emit(f"captcha failed ({dur:.1f}s)")
                    return result
                _emit(f"captcha solved ({dur:.1f}s)")
                headers["x-captcha-key"] = solved
                headers["x-captcha-rqtoken"] = resp_json.get("captcha_rqtoken") or ""
                headers["x-captcha-session-id"] = resp_json.get("captcha_session_id") or ""
                # Discord may check captcha_key in JSON as well – send both
                reg_payload_captcha = dict(reg_payload)
                reg_payload_captcha["captcha_key"] = solved
                if resp_json.get("captcha_rqtoken"):
                    reg_payload_captcha["captcha_rqtoken"] = resp_json.get("captcha_rqtoken")
                _emit("Retrying register...")
                try:
                    async with sess.post(_REGISTER_URL, json=reg_payload_captcha, headers=headers,
                                         proxy=proxy,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r2:
                        resp_text2 = await r2.text()
                        try:
                            resp_json = json.loads(resp_text2) if resp_text2 else {}
                        except Exception:
                            resp_json = {}
                        if r2.status not in (200, 201):
                            # try to surface the real field error
                            errs = resp_json.get("errors") or {}
                            field_err = ""
                            if isinstance(errs, dict):
                                # find first field error
                                for fname, fval in errs.items():
                                    try:
                                        fe = (fval or {}).get("_errors") or []
                                        if fe:
                                            field_err = f"{fname}: {fe[0].get('code','')} {fe[0].get('message','')}"
                                            break
                                    except Exception:
                                        pass
                            err_detail = resp_json.get("captcha_key") or resp_json.get("message") or field_err or resp_json.get("errors") or str(resp_text2)[:500]
                            email_err = ""
                            if isinstance(errs, dict):
                                email_errs = (errs.get("email") or {}).get("_errors") or []
                                if email_errs:
                                    email_err = email_errs[0].get("code", "")
                            result["error"] = f"register {r2.status}: {email_err or field_err or str(err_detail)[:200]}"
                            _emit(f"FAILED ({r2.status}): {field_err or str(err_detail)[:120]}")
                            return result
                except Exception as e:
                    result["error"] = f"captcha retry failed ({type(e).__name__})"
                    _emit(f"FAILED: {type(e).__name__}")
                    return result
                headers.pop("x-captcha-key", None)
                headers.pop("x-captcha-rqtoken", None)
                headers.pop("x-captcha-session-id", None)

            auth_token = resp_json.get("token")
            if not auth_token:
                err_msg = resp_json.get("message") or resp_json.get("errors") or str(resp_json)[:120]
                result["error"] = f"no token: {str(err_msg)[:120]}"
                _emit(f"FAILED: no token ({str(err_msg)[:60]})")
                return result

            result["token"] = auth_token
            headers["authorization"] = auth_token
            if sample_cb:
                try:
                    sample_cb(masked, "registered")
                except Exception:
                    pass
            if progress_cb:
                try:
                    progress_cb(idx)
                except Exception:
                    pass

            mail_source = _get_mail_source()
            _emit("Waiting for verify email...")
            verify_url = await mail_source.get_verify_url(email_addr, email_pass, timeout=120,
                                                          source=result.get("source", "local"))
            if verify_url:
                mail_token = None
                if "token=" in verify_url:
                    mail_token = verify_url.split("token=")[-1].split("&")[0]
                else:
                    try:
                        click_h = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                   "user-agent": _USER_AGENT_CREATE}
                        loc = verify_url
                        async with aiohttp.ClientSession(trust_env=False) as plain_sess:
                            for _ in range(3):
                                async with plain_sess.get(loc, headers=click_h, proxy=proxy,
                                                    timeout=aiohttp.ClientTimeout(total=10),
                                                    allow_redirects=False) as cr:
                                    loc2 = cr.headers.get("Location", "")
                                    if not loc2:
                                        break
                                    if "token=" in loc2:
                                        mail_token = loc2.split("token=")[-1].split("&")[0]
                                        break
                                    loc = loc2
                    except Exception:
                        pass
                if mail_token:
                    try:
                        async with sess.post(_VERIFY_URL, json={"token": mail_token},
                                             headers=headers, proxy=proxy,
                                             timeout=aiohttp.ClientTimeout(total=15)) as vr:
                            vr_text = await vr.text()
                            try:
                                vr_json = json.loads(vr_text) if vr_text else {}
                            except Exception:
                                vr_json = {}
                            if vr.status == 200:
                                vj = vr_json
                                new_tok = (vj or {}).get("token")
                                if new_tok:
                                    auth_token = new_tok
                                    result["token"] = auth_token
                                    headers["authorization"] = auth_token
                            elif vr_json.get("captcha_sitekey") and vr_json.get("captcha_rqdata"):
                                try:
                                    from solver import solve_mail_verify as _solve_mail
                                    vsolved = await _solve_mail(vr_json["captcha_sitekey"], vr_json["captcha_rqdata"],
                                                          "https://discord.com/", proxy)
                                    if not vsolved and solver:
                                        vsolved = await solver(vr_json["captcha_sitekey"], vr_json["captcha_rqdata"],
                                                          "https://discord.com/", proxy)
                                except TypeError:
                                    vsolved = await solver(vr_json["captcha_sitekey"], vr_json["captcha_rqdata"],
                                                          "https://discord.com/")
                                except Exception:
                                    vsolved = None
                                if vsolved:
                                    headers["x-captcha-key"] = vsolved
                                    headers["x-captcha-rqtoken"] = vr_json.get("captcha_rqtoken") or ""
                                    headers["x-captcha-session-id"] = vr_json.get("captcha_session_id") or ""
                                    try:
                                        async with sess.post(_VERIFY_URL, json={"token": mail_token},
                                                             headers=headers, proxy=proxy,
                                                             timeout=aiohttp.ClientTimeout(total=15)) as vr2:
                                            if vr2.status in (200, 201):
                                                vr2_json = await vr2.json()
                                                new_tok = (vr2_json or {}).get("token")
                                                if new_tok:
                                                    auth_token = new_tok
                                                    result["token"] = auth_token
                                    except Exception:
                                        pass
                                    headers.pop("x-captcha-key", None)
                                    headers.pop("x-captcha-rqtoken", None)
                                    headers.pop("x-captcha-session-id", None)
                    except Exception:
                        pass

            try:
                bios = ["dm me for trade", "online", "active daily", ""]
                statuses = ["", "online", "playing games", ""]
                await sess.patch(f"{_DISCORD_API}/users/@me/profile",
                                 json={"bio": random.choice(bios)},
                                 headers=headers, proxy=proxy,
                                 timeout=aiohttp.ClientTimeout(total=8))
                await sess.patch(f"{_DISCORD_API}/users/@me/settings",
                                 json={"custom_status": {"text": random.choice(statuses)}},
                                 headers=headers, proxy=proxy,
                                 timeout=aiohttp.ClientTimeout(total=8))
                await sess.post(f"{_DISCORD_API}/hypesquad/online",
                                json={"house_id": random.choice([1, 2, 3])},
                                headers=headers, proxy=proxy,
                                timeout=aiohttp.ClientTimeout(total=8))
            except Exception:
                pass

            result["created"] = True
            if sample_cb:
                try:
                    sample_cb(masked, "created")
                except Exception:
                    pass

        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

    return result


_mail_source = None


def _get_mail_source():
    global _mail_source
    if _mail_source is None:
        _mail_source = _MailSource()
    return _mail_source


def _reset_mail_source():
    global _mail_source
    _mail_source = None


async def run_account_create(count, progress_cb, sample_cb=None, solver=None,
                             custom_proxies=None, phone_enabled=False, phone_api_key=""):
    if custom_proxies is not None:
        proxies = [p.strip() for p in custom_proxies if isinstance(p, str) and p.strip()]
        proxies = [p if p.startswith("http") else "http://" + p for p in proxies]
    else:
        proxy_file = getattr(config, "CREATE_PROXY_FILE", "joiner_proxies.txt")
        proxies = load_proxies(_data_file(proxy_file))
        if not proxies:
            proxies = check_proxies()

    workers = min(10, max(3, len(proxies) // 10)) if proxies else 3
    cycle = itertools.cycle(proxies) if proxies else None
    sem = asyncio.Semaphore(workers)
    tls = "tls-spoof" if _HAS_CURL else "plain"
    out = {"created": 0, "failed": 0, "checked": 0, "total": count,
           "accounts": [], "output_lines": [],
           "pool": (f"{len(proxies)} proxies {tls}" if proxies else f"direct {tls}"),
           "verifications": {"email": 0, "phone": 0}}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _http():
        if _HAS_CURL:
            pool = _CurlPool()
            try:
                yield _SpoofedSession(pool)
            finally:
                await pool.close()
        else:
            async with aiohttp.ClientSession(trust_env=False) as s:
                yield s

    async with _http() as sess:
        async def one(idx):
            proxy = next(cycle) if cycle else None
            mail_source = _get_mail_source()
            mailbox = await mail_source.acquire()
            if not mailbox:
                async with sem:
                    out["checked"] += 1
                    out["failed"] += 1
                    if sample_cb:
                        try:
                            sample_cb("?", "no_mailbox")
                        except Exception:
                            pass
                    if progress_cb:
                        try:
                            progress_cb(out["checked"])
                        except Exception:
                            pass
                return

            result = await _create_one_account(
                    mailbox, proxy, solver, sess, sem,
                    None, None, phone_enabled, phone_api_key, idx, count,
                    status_cb=sample_cb,
                )
            # no retry – EMAIL_ALREADY_REGISTERED would waste another solve

            async with sem:
                out["checked"] += 1
                if result["created"]:
                    out["created"] += 1
                    line = f"{result['email']}:{result['password']} | {result['token']}"
                    out["output_lines"].append(line)
                else:
                    out["failed"] += 1
                out["accounts"].append(result)
                if sample_cb:
                    try:
                        st = "created" if result["created"] else "failed"
                        sample_cb(result.get("email", "?"), st, result.get("error"))
                    except TypeError:
                        try:
                            sample_cb(result.get("email", "?"), st)
                        except Exception:
                            pass
                    except Exception:
                        pass
                if progress_cb:
                    try:
                        progress_cb(out["checked"])
                    except Exception:
                        pass

        await asyncio.gather(*[one(i) for i in range(count)])

    return out
