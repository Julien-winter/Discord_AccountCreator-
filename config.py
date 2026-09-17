"""PandaChecker Account Creator — Config. Fill in your keys."""

# --- Captcha Solver (zrxsolver.online) ---
JOIN_SOLVER_PROVIDER = "zrx"
JOIN_SOLVER_KEY = "YOUR_ZRX_KEY_HERE"
JOIN_SOLVER_ENDPOINT = "https://zrxsolver.online"
JOIN_SOLVER_TOKEN_FIELD = "solution"

# --- Mail verify solver (cheap) ---
MAIL_VERIFY_SOLVER_KEY = "YOUR_MAIL_VERIFY_KEY_HERE"
MAIL_VERIFY_SOLVER_HOST = "http://89.167.31.16:5000"

# --- SmakMail (optional) ---
SMAKMAIL_API_KEY = "YOUR_SMAKMAIL_KEY_HERE"
SMAKMAIL_PRODUCT = "Eternal"

# --- Proxies & Mails ---
CREATE_PROXY_FILE = "joiner_proxies.txt"
MAILS_FILE = "data/mails.txt"
MAIL_IMAP_HOST = "imap.firstmail.ltd"
MAIL_IMAP_PORT = 993
MAIL_IMAP_SSL = True

# --- Account Creator ---
PRICE_CREATE_EUR = 0.05
PRICE_CREATE_BYOP = 0.03
CREATE_MIN_ACCOUNTS = 1
CREATE_MAX_ACCOUNTS = 50

HOST = "0.0.0.0"
PORT = 50903
