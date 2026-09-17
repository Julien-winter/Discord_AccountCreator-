#!/usr/bin/env python3
"""Account Creator — CLI Version.

Usage: python create_accounts.py [count]

Outputs email:pass | token to accounts.txt
"""
import asyncio
import sys
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
sys.path.insert(0, str(BASE))

import config
import services


async def main():
    count = 10
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            print("Usage: python create_accounts.py [count]")
            return

    print(f"PandaChecker Account Creator")
    print(f"{'=' * 40}")
    print(f"Creating {count} accounts...")

    try:
        from solver import solve as _solve, has_solver as _has_solver
        solver = _solve if _has_solver() else None
        print(f"Solver: {'yes' if solver else 'no'}")
    except Exception:
        solver = None
        print("Solver: no")

    created = 0
    failed = 0
    output_lines = []

    def prog(n):
        pct = n * 100 // max(1, count)
        bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
        print(f"\r  [{bar}] {pct}% ({n}/{count})  created={created} failed={failed}", end="", flush=True)

    def sample(name, res, error=None):
        nonlocal created, failed
        STATUS_WORDS = ("Checking", "Preparing", "Warming", "Registering", "Solving", "Retrying", "Waiting", "Verifying")
        if any(w in res for w in STATUS_WORDS):
            print(f"    {name} -> {res}")
        elif "FAILED" in res:
            print(f"    {name} -> {res}")
        elif res in ("created", "registered"):
            created += 1
            print(f"\n  [OK] {name}")
        elif res == "no_mailbox":
            failed += 1
            print(f"\n  [--] {name} (no mailbox)")
        else:
            failed += 1
            err = f" ({error})" if error else ""
            print(f"\n  [FAIL] {name}{err}")

    res = await services.run_account_create(
        count, prog, sample, solver,
    )

    print(f"\n\n{'=' * 40}")
    print(f"Done!")
    print(f"  Created: {res.get('created', 0)}")
    print(f"  Failed:  {res.get('failed', 0)}")
    print(f"  Pool:    {res.get('pool', '?')}")

    output_lines = res.get("output_lines", [])
    if output_lines:
        out_path = BASE / "accounts.txt"
        existing = ""
        if out_path.exists():
            existing = out_path.read_text(encoding="utf-8").strip()
        with open(out_path, "a", encoding="utf-8") as f:
            if existing:
                f.write("\n")
            f.write("\n".join(output_lines) + "\n")
        total = len(existing.splitlines()) + len(output_lines) if existing else len(output_lines)
        print(f"\n  Saved to: {out_path} ({total} total accounts)")
        print(f"\n  --- New Accounts ---")
        for line in output_lines[:20]:
            print(f"  {line}")
        if len(output_lines) > 20:
            print(f"  ... and {len(output_lines) - 20} more")
    else:
        print("\n  No accounts created.")

    failed_accounts = [a for a in res.get("accounts", []) if not a.get("created")]
    if failed_accounts:
        print(f"\n  --- Failed Accounts ---")
        for acc in failed_accounts[:20]:
            print(f"  {acc.get('email', '?')[:30]} -> {acc.get('error', 'unknown')}")
        if len(failed_accounts) > 20:
            print(f"  ... and {len(failed_accounts) - 20} more")


if __name__ == "__main__":
    asyncio.run(main())
