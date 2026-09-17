# AccountCreator — Realistic Discord Account Creator

> Fast, realistic Discord account creator. Generates human-like usernames (like `maskierter.`, `z5087z`, `tjardan97`) — Supports firstmail + cheap solver split.

## Features
- **Realistic usernames** — mimics real Discord joins (`lsd.6`, `costazzz.z`, `97b27`, `hyper123...`, `tungtung...`) from wordlist + digits/dot, not random gibberish
- **Account creator** — `imap.firstmail.ltd` verification, `zrx` for register + cheap `89.167.31.16:5000` for verify, firstmail support, proxy-verified
- **Smart logs** — `[HH:MM:SS] [1/3] mail@... | Solving captcha (12.3s)` with proxy, duration, field errors

## Referrals (support us)

If you use these services, please use our referral links — cheaper for you, supports development:

- **Solver (zrx)** — 1 credit per solve, fast: **https://zrxsolver.online/register?ref=P384WsLLhfs**
- **Proxies (DataImpulse)** — residential, good for Discord: **https://dataimpulse.com/?aff=d65d1bc9-e5c6-44dc-8019-b0af59c435c5**
- For mail verify we also use the cheap `http://89.167.31.16:5000` (no referral, `MAIL_VERIFY_SOLVER_HOST` in `config.py`)

## Quick Start

```bash
pip install aiohttp curl_cffi
# or
pip install -r requirements.txt
```

### 1. Configure

Edit `config.py`:

```py
JOIN_SOLVER_KEY = "sk_..._zrxsolver"          # from https://zrxsolver.online/register?ref=P384WsLLhfs
MAIL_VERIFY_SOLVER_KEY = "65a2b3c..._cheap"   # from http://89.167.31.16:5000
MAIL_VERIFY_SOLVER_HOST = "http://89.167.31.16:5000"
MAIL_IMAP_HOST = "imap.firstmail.ltd"        # for firstmail
```

### 2. Mails & Proxies

```
data/mails.txt              # email:password per line (firstmail)
data/joiner_proxies.txt     # creator proxies (user:pass@ip:port)
data/names.txt              # realistic names (optional, auto-generated)
```

### 3. Create Accounts

```bash
python create_accounts.py 5
# or
start_creator.bat  # asks count
```

Log example:

```
[15:42:11] [1/3] eringlazier@barziniagw.com | Checking proxy [1.2.3.4]...
[15:42:13] [1/3] eringlazier@barziniagw.com | Solving captcha...
[15:42:27] [1/3] eringlazier@barziniagw.com | captcha solved (14.2s)
[15:42:29] [1/3] eringlazier@barziniagw.com | Waiting for verify email...
[OK] eringlazier@barziniagw.com
```

Output: `accounts.txt` → `email:password | token`  (also `results/` for checker)

## How it works

Pops `data/mails.txt` (auto-removed after use, stays in `accounts.txt`), gets fingerprint/build/super_properties (`services.py:_create_one_account`), warmup register, real register → `zrx` solve → retry with `captcha_key`+`global_name` → IMAP `imap.firstmail.ltd` poll for `click.discord.com` → token → `solve_mail_verify` (cheap) if needed → `accounts.txt`

## Tips

- Use fresh firstmail + residential proxies — Discord flags `mail.shadowvault.global` + DataImpulse heavily (instant disable: `spam and/or platform abuse`)
- If `captcha unsolved` after 150s → zrx queued (check dashboard)

## License

MIT — use at own risk, respect Discord ToS and provider ToS.

---
Made for educational purposes. Star & share if it helped (via referrals above)!
