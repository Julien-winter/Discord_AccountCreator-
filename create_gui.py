#!/usr/bin/env python3
"""Account Creator — Tkinter GUI Version.

Standalone GUI for creating Discord accounts.
Uses the same services.py as the web version.

Usage: python create_gui.py
"""
import asyncio
import json
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import config


class AccountCreatorGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PandaChecker — Account Creator")
        self.root.geometry("800x650")
        self.root.configure(bg="#0a0e1a")
        self.root.resizable(True, True)
        self.running = False
        self.results = []
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#0a0e1a")
        style.configure("TLabel", background="#0a0e1a", foreground="#e9eef7", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", font=("Consolas", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#34d399")

        # Header
        hdr = ttk.Frame(self.root)
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        ttk.Label(hdr, text="PandaChecker — Account Creator", style="Header.TLabel").pack(side="left")
        ttk.Label(hdr, text=f"{config.PRICE_CREATE_EUR} EUR/account").pack(side="right")

        # Settings
        settings = ttk.Frame(self.root)
        settings.pack(fill="x", padx=16, pady=4)

        ttk.Label(settings, text="Count:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.count_var = tk.StringVar(value="10")
        ttk.Entry(settings, textvariable=self.count_var, width=8).grid(row=0, column=1, sticky="w")

        ttk.Label(settings, text="Proxies (BYOP):").grid(row=0, column=2, sticky="w", padx=(20, 6))
        self.proxies_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.proxies_var, width=50).grid(row=0, column=3, sticky="w")

        ttk.Label(settings, text="Price:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        self.price_label = ttk.Label(settings, text="0.50 EUR")
        self.price_label.grid(row=1, column=1, sticky="w", pady=(6, 0))

        # Update price on count change
        self.count_var.trace_add("write", self._update_price)

        # Buttons
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=16, pady=8)

        self.start_btn = tk.Button(btns, text="START", bg="#10b981", fg="#fff",
                                   font=("Segoe UI", 11, "bold"), relief="flat",
                                   command=self._start, width=14)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = tk.Button(btns, text="STOP", bg="#ef4444", fg="#fff",
                                  font=("Segoe UI", 11, "bold"), relief="flat",
                                  command=self._stop, width=10, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))

        self.copy_btn = tk.Button(btns, text="Copy All", bg="#3b82f6", fg="#fff",
                                  font=("Segoe UI", 10), relief="flat",
                                  command=self._copy_all, width=10)
        self.copy_btn.pack(side="left", padx=(0, 8))

        self.download_btn = tk.Button(btns, text="Download .txt", bg="#8b5cf6", fg="#fff",
                                      font=("Segoe UI", 10), relief="flat",
                                      command=self._download, width=12)
        self.download_btn.pack(side="left")

        # Progress
        prog = ttk.Frame(self.root)
        prog.pack(fill="x", padx=16, pady=(4, 2))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog, variable=self.progress_var,
                                            maximum=100, mode="determinate")
        self.progress_bar.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(prog, textvariable=self.status_var).pack(anchor="w")

        # Console
        console_frame = ttk.Frame(self.root)
        console_frame.pack(fill="both", expand=True, padx=16, pady=(4, 12))

        self.console = tk.Text(console_frame, bg="#0d1117", fg="#c9d1d9",
                               font=("Consolas", 9), relief="flat", wrap="word",
                               state="disabled", height=18)
        scrollbar = ttk.Scrollbar(console_frame, command=self.console.yview)
        self.console.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.console.pack(side="left", fill="both", expand=True)

        # Tag for colored output
        self.console.tag_configure("created", foreground="#34d399")
        self.console.tag_configure("failed", foreground="#ef4444")
        self.console.tag_configure("info", foreground="#58a6ff")
        self.console.tag_configure("sys", foreground="#8b949e")

    def _update_price(self, *args):
        try:
            count = int(self.count_var.get() or "10")
        except ValueError:
            count = 10
        price = round(count * config.PRICE_CREATE_EUR, 2)
        self.price_label.configure(text=f"{price:.2f} EUR")

    def _log(self, msg, tag="info"):
        self.console.configure(state="normal")
        self.console.insert("end", msg + "\n", tag)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _start(self):
        if self.running:
            return
        try:
            count = int(self.count_var.get())
            if count < 1 or count > 50:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid", "Count must be 1-50")
            return

        self.running = True
        self.results = []
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress_var.set(0)
        self.status_var.set(f"Creating {count} accounts...")
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")
        self._log(f"Starting account creation ({count} accounts)...", "sys")

        proxies = self.proxies_var.get().strip().splitlines() if self.proxies_var.get().strip() else None

        thread = threading.Thread(target=self._run_thread, args=(count, proxies), daemon=True)
        thread.start()

    def _stop(self):
        self.running = False
        self.status_var.set("Stopping...")
        self._log("Stopping...", "sys")

    def _run_thread(self, count, proxies):
        import services

        async def _run():
            created = 0
            failed = 0

            def prog(n):
                pct = n * 100 // max(1, count)
                self.root.after(0, lambda: self.progress_var.set(pct))
                self.root.after(0, lambda: self.status_var.set(f"Checked {n}/{count} (created: {created}, failed: {failed})"))

            def sample(name, res, error=None):
                STATUS_WORDS = ("Preparing", "Registering", "Solving", "Retrying", "Waiting", "Verifying")
                if res in STATUS_WORDS or any(res.startswith(w) for w in STATUS_WORDS):
                    tag = "sys"
                    extra = f"..."
                elif res == "created":
                    tag = "created"
                    extra = ""
                else:
                    tag = "failed"
                    extra = f" ({error})" if error else ""
                self.root.after(0, lambda: self._log(f"  {name} — {res}{extra}", tag))

            try:
                from solver import solve as _solve, has_solver as _has_solver
                solver = _solve if _has_solver() else None
            except Exception:
                solver = None

            res = await services.run_account_create(
                count, prog, sample, solver,
                proxies if proxies else None,
            )

            self.results = res.get("output_lines", [])
            created = res.get("created", 0)
            failed = res.get("failed", 0)
            self.root.after(0, lambda: self._finish(res))

        asyncio.run(_run())

    def _finish(self, res):
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress_var.set(100)
        created = res.get("created", 0)
        failed = res.get("failed", 0)
        total = res.get("checked", 0)
        self.status_var.set(f"Done — {created} created, {failed} failed, {total} total")
        self._log(f"\nDone: {created} created, {failed} failed, {total} total", "sys")
        self._log(f"Pool: {res.get('pool', '?')}", "sys")
        if self.results:
            self._log(f"\n--- Output ({len(self.results)} accounts) ---", "info")
            for line in self.results[:50]:
                self._log(line, "created")

    def _copy_all(self):
        if not self.results:
            messagebox.showinfo("No data", "No accounts to copy yet.")
            return
        text = "\n".join(self.results)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log(f"Copied {len(self.results)} accounts to clipboard", "sys")

    def _download(self):
        if not self.results:
            messagebox.showinfo("No data", "No accounts to download yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"accounts_{len(self.results)}.txt",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.results))
            self._log(f"Downloaded {len(self.results)} accounts to {path}", "sys")

    def _on_close(self):
        if self.running:
            self.running = False
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    gui = AccountCreatorGUI()
    gui.run()
