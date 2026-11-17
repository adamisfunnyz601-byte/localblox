#!/usr/bin/env python3
"""
Localhost server for the play.roblox.com site dump (see README.md).

Routing:
  /                     -> Netblox launcher
  /<dumped-host>/<path> -> virtual host (e.g. /games.roblox.com/v1/games)
  /__up/<host>/<path>   -> live bridge: forwarded server-side to the real
                           roblox/rbxcdn host (game traffic; browser stays
                           same-origin so CORS/cookies are a non-issue)
  /games/start          -> play.roblox.com game client
  /<anything else>      -> play.roblox.com (default host, root-relative assets)

The dump stored API responses as "<path>.html" and dropped query strings, so
lookup falls back to +".html" / +".png" and ignores the query. Content types
are re-derived (the dumper lost them) and absolute URLs for dumped hosts are
rewritten to this origin in text bodies; runtime-built URLs are remapped by
/local-shim.js injected into served HTML.

Run:  python3 server.py [port]     (default 8080)
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import yaml
except ImportError:
    yaml = None  # graceful fallback – config values will use defaults only

PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8080))
# Use same-origin relative paths behind HTTPS reverse proxies.
LOCAL = ""
DUMP = Path(__file__).resolve().parent
DEFAULT_HOST = "play.roblox.com"
SHIM_PATH = DUMP / "local-shim.js"

# ---- config file loading (config.yaml overrides CLI defaults) -----------
CONFIG_PATH = DUMP / "config.yaml"
_cfg = None
if CONFIG_PATH.exists() and yaml is not None:
    try:
        with open(CONFIG_PATH) as fh:
            _cfg = yaml.safe_load(fh)
    except Exception:
        _cfg = None

if isinstance(_cfg, dict):
    # CLI args take absolute precedence; config values fill the gaps.
    _port = _cfg.get("port")
    if _port is not None:
        try:
            PORT = int(_port)
        except (ValueError, TypeError):
            pass

    _build = _cfg.get("build")
    if _build is not None:
        BUILD = str(_build)

    _page = _cfg.get("page_build")
    if _page is not None:
        PAGE_BUILD = str(_page)

    # enable_fill and enable_bridge are read-only flags; the actual logic
    # lives in the route handlers and upstream_allowed().

# Static asset dirs that are still live upstream: a miss inside them is filled
# from the real CDN once and persisted into the dump (then served locally).
BUILD = "version-75f1039cb4964863-RobloxWebClient"   # webclient build in the dump
PAGE_BUILD = "038e4bd621bee962e64ed84fac16608407267adc"  # _next build-hash dir
FILL_RULES = {
    "setup.rbxcdn.com": f"webclient/{BUILD}/",
    DEFAULT_HOST: f"{PAGE_BUILD}/",
}

# Top-level dirs of the dump are the hosts it covers.
HOSTS = sorted(p.name for p in DUMP.iterdir() if p.is_dir() and "." in p.name)
HOST_ALT = "|".join(re.escape(h) for h in HOSTS)
# https://<dumped-host> followed by a non-domain char (/, ", end, ...)
ABS_URL_RE = re.compile(rf"https?://({HOST_ALT})(?![A-Za-z0-9.-])")

# The remap shim, baked with the host list: injected in HTML and prepended to
# the engine's JS (its XHR/fetch run in worker contexts too, which never see
# window patches from the page).
SHIM_JS = SHIM_PATH.read_text().replace("__HOSTS__", json.dumps(HOSTS))
ENGINE_DIR = f"setup.rbxcdn.com/webclient/{BUILD}/"

TEXT_EXT = {".js", ".json", ".html", ".css"}
CONTENT_TYPES = {
    ".js": "application/javascript",
    ".json": "application/json",
    ".css": "text/css",
    ".html": "text/html",  # overridden by JSON sniff
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".pack": "application/octet-stream",
    ".rbxm": "application/octet-stream",
}


def sniff_type(path: Path, data: bytes) -> str:
    if path.suffix == ".html":
        head = data.lstrip()[:1]
        if head in (b"{", b"["):
            return "application/json"
        return "text/html"
    return CONTENT_TYPES.get(path.suffix, "application/octet-stream")


def find_file(host: str, rel: str) -> Path | None:
    base = (DUMP / host).resolve()
    rel = rel.strip("/")
    direct = (base / rel) if rel else base
    for cand in (direct, Path(str(direct) + ".html"), Path(str(direct) + ".png"),
                 direct / "index.html"):
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if not (resolved.is_relative_to(base) and resolved.is_file()):
            continue
        # "No Content: <url>" placeholders (dumper failures) count as missing.
        try:
            if resolved.open("rb").read(11) == b"No Content:":
                continue
        except OSError:
            continue
        return resolved
    return None


def live_fill(host: str, rel: str) -> bytes | None:
    """Fetch a miss inside an allowlisted static asset dir from the live CDN."""
    prefix = FILL_RULES.get(host)
    rel = rel.strip("/")
    if prefix is None or not rel.startswith(prefix):
        return None
    base = (DUMP / host).resolve()
    dest = (base / rel)
    try:
        if not dest.resolve().is_relative_to(base):
            return None
    except OSError:
        return None
    url = "https://" + host + "/" + "/".join(
        urllib.parse.quote(seg) for seg in rel.split("/"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
        if "text/html" in ctype:  # SPA fallback, not a real asset
            print(f"[fill] {host}/{rel} rejected (live returned HTML)", flush=True)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"[fill] {host}/{rel} <- live ({len(data):,} bytes)", flush=True)
        return data
    except Exception as e:
        print(f"[fill] {host}/{rel} failed: {e}", flush=True)
        return None


def transform(target: Path, data: bytes) -> bytes:
    """Rewrite dumped-host absolute URLs to this origin; inject the shim."""
    ext = target.suffix.lower()
    if ext not in TEXT_EXT:
        return data
    text = data.decode("utf-8", "replace")
    if HOSTS:
        text = ABS_URL_RE.sub(lambda m: f"{LOCAL}/{m.group(1)}", text)
    if ext == ".html" and "<head" in text:
        text = re.sub(r"(<head[^>]*>)",
                      r'\1<script src="/local-shim.js"></script>',
                      text, count=1)
    data = text.encode("utf-8")
    # Engine JS gets the remap prelude inline: it also runs inside emscripten
    # workers (importScripts), where the page's injected shim never applies.
    try:
        rel = str(target.relative_to(DUMP))
    except ValueError:
        rel = ""
    if ext == ".js" and rel.startswith(ENGINE_DIR):
        # Keep a leading "use strict" directive as the directive prologue.
        m = re.match(rb"""^(\s*["']use strict["']\s*;\s*)""", data)
        head, rest = (m.group(1), data[m.end():]) if m else (b"", data)
        data = head + SHIM_JS.encode("utf-8") + rest
    return data


def upstream_allowed(host: str) -> bool:
    return host == "roblox.com" or host.endswith(".roblox.com") \
        or host.endswith(".rbxcdn.com")


# ---- Netblox accounts: password hashes + revocable persistent sessions ----
import hashlib
import hmac
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie

DB_PATH = Path(os.environ.get("NETBLOX_DB_PATH") or
               (Path(os.environ.get("NETBLOX_DATA_DIR", str(DUMP))) / "netblox.sqlite3"))
SESSION_DAYS = 30
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1").lower() not in ("0", "false", "no")

def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def init_auth_db():
    with db_connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
            username_key TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL, created_at INTEGER NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at)")
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))

def password_digest(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt_hex), 310000).hex()

def auth_json(handler, status, payload, extra=None):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache",
               "X-Content-Type-Options": "nosniff"}
    headers.update(extra or {})
    handler._send(status, body, "application/json; charset=utf-8", headers)

def request_json(handler):
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("Send JSON.")
    length = int(handler.headers.get("Content-Length") or 0)
    if length < 1 or length > 8192:
        raise ValueError("Invalid request size.")
    value = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid request.")
    return value

def current_user(handler):
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
        morsel = cookie.get("netblox_session")
        token = morsel.value if morsel else ""
    except Exception:
        token = ""
    if not token or len(token) > 200:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_connect() as db:
        row = db.execute("""SELECT users.id, users.username FROM sessions
            JOIN users ON users.id=sessions.user_id
            WHERE sessions.token_hash=? AND sessions.expires_at>?""",
            (token_hash, int(time.time()))).fetchone()
    return {"id": row["id"], "username": row["username"]} if row else None

def issue_session(handler, user_id):
    token = secrets.token_urlsafe(48)
    now = int(time.time())
    expires = now + SESSION_DAYS * 86400
    with db_connect() as db:
        db.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, expires, now))
    secure = "; Secure" if COOKIE_SECURE else ""
    return {"Set-Cookie": f"netblox_session={token}; Path=/; Max-Age={SESSION_DAYS*86400}; HttpOnly; SameSite=Lax{secure}"}

def auth_route(handler, path):
    if path == "/api/auth/me" and handler.command == "GET":
        user = current_user(handler)
        auth_json(handler, 200, {"authenticated": bool(user), "user": user})
        return True
    if path == "/api/auth/register" and handler.command == "POST":
        try:
            data = request_json(handler)
            username, password = data.get("username"), data.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("Enter a username and password.")
            username = username.strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
                raise ValueError("Username must be 3–20 characters (letters, numbers or _).")
            if len(password) < 10 or len(password) > 128:
                raise ValueError("Password must be 10–128 characters.")
            salt = secrets.token_hex(16)
            digest = password_digest(password, salt)
            now = int(time.time())
            with db_connect() as db:
                cur = db.execute("""INSERT INTO users(username,username_key,password_hash,password_salt,created_at)
                    VALUES(?,?,?,?,?)""", (username, username.casefold(), digest, salt, now))
                user_id = cur.lastrowid
            auth_json(handler, 201, {"authenticated": True, "user": {"id": user_id, "username": username}},
                      issue_session(handler, user_id))
        except sqlite3.IntegrityError:
            auth_json(handler, 409, {"error": "That username is already taken."})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            auth_json(handler, 400, {"error": str(e) or "Invalid request."})
        return True
    if path == "/api/auth/login" and handler.command == "POST":
        try:
            data = request_json(handler)
            username, password = data.get("username"), data.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("Enter your username and password.")
            if len(username) > 20 or len(password) > 128:
                raise ValueError("Incorrect username or password.")
            with db_connect() as db:
                row = db.execute("SELECT id,username,password_hash,password_salt FROM users WHERE username_key=?",
                                 (username.strip().casefold(),)).fetchone()
            if not row or not hmac.compare_digest(password_digest(password, row["password_salt"]), row["password_hash"]):
                auth_json(handler, 401, {"error": "Incorrect username or password."})
                return True
            auth_json(handler, 200, {"authenticated": True, "user": {"id": row["id"], "username": row["username"]}},
                      issue_session(handler, row["id"]))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            auth_json(handler, 400, {"error": str(e) or "Invalid request."})
        return True
    if path == "/api/auth/logout" and handler.command == "POST":
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
            morsel = cookie.get("netblox_session")
            token = morsel.value if morsel else ""
        except Exception:
            token = ""
        if token:
            with db_connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?",
                           (hashlib.sha256(token.encode()).hexdigest(),))
        secure = "; Secure" if COOKIE_SECURE else ""
        auth_json(handler, 200, {"authenticated": False},
                  {"Set-Cookie": f"netblox_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"})
        return True
    return False



class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "play-roblox-local"

    # ---- response helpers -------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # play.roblox.com is cross-origin isolated: the WebClient needs
        # SharedArrayBuffer (pthreads) -> COOP + COEP on every response.
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        print(f"{code} {len(body):>10}  {self.command}  {self.path}", flush=True)

    def _redirect(self, loc: str) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        print(f"302 {self.command}  {self.path} -> {loc}", flush=True)

    # ---- live bridge (/__up/<host>/<path>) --------------------------------
    def _bridge(self, host: str, rel: str, query: str) -> None:
        """Forward to the real host server-side: from the browser this is a
        same-origin request, so no CORS/preflight applies; upstream only ever
        sees live-like headers (Origin/Referer = play.roblox.com)."""
        import urllib.error

        url = "https://" + host + "/" + "/".join(
            urllib.parse.quote(seg) for seg in rel.split("/"))
        if query:
            url += "?" + query

        drop = {"host", "accept-encoding", "connection", "content-length",
                "cookie", "origin", "referer", "transfer-encoding"}
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in drop}
        headers["Accept-Encoding"] = "identity"
        headers["Origin"] = "https://play.roblox.com"
        headers["Referer"] = "https://play.roblox.com/"
        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        req = urllib.request.Request(
            url, data=body if self.command in ("POST", "PUT") else None,
            headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, rheaders, data = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, rheaders, data = e.code, e.headers, (e.read() or b"")
        except Exception as e:
            msg = f"bridge error {host}/{rel}: {e}\n".encode()
            self._send(502, msg, "text/plain")
            return

        enc = (rheaders.get("Content-Encoding") or "").lower()
        if enc in ("gzip", "deflate"):
            import gzip, zlib
            data = gzip.decompress(data) if enc == "gzip" else zlib.decompress(data)
        ctype = rheaders.get("Content-Type", "application/octet-stream")
        print(f"[bridge] {self.command} {host}/{rel} -> {status}"
              f" ({len(data):,} bytes)", flush=True)
        self._send(status, data, ctype)

    # ---- routes -----------------------------------------------------------
    def _route(self) -> None:
        raw = urlsplit(self.path)
        path = unquote(raw.path)

        if path.startswith("/api/auth/") and auth_route(self, path):
            return
        if path == "/local-shim.js":
            body = SHIM_PATH.read_text().replace("__HOSTS__", json.dumps(HOSTS))
            self._send(200, body.encode(), "application/javascript",
                       {"Cache-Control": "no-store"})
            return
        if path == "/":
            launcher = DUMP / "netblox" / "index.html"
            self._send(200, launcher.read_bytes(), "text/html",
                       {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            return

        if path.startswith("/netblox/"):
            base = (DUMP / "netblox").resolve()
            target = (DUMP / path.lstrip("/")).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                self._send(404, b"not found\\n", "text/plain")
                return
            data = target.read_bytes()
            self._send(200, data, sniff_type(target, data),
                       {"X-Content-Type-Options": "nosniff"})
            return

        parts = path.lstrip("/").split("/", 1)
        if parts[0] == "__up":
            seg = path.lstrip("/").split("/", 2)
            uhost = seg[1] if len(seg) > 1 else ""
            urel = seg[2] if len(seg) > 2 else ""
            if not upstream_allowed(uhost):
                self._send(403, b"bridge host not allowed\n", "text/plain")
                return
            self._bridge(uhost, urel, raw.query)
            return
        if parts[0] in HOSTS:
            host, rel = parts[0], parts[1] if len(parts) > 1 else ""
        else:
            host, rel = DEFAULT_HOST, path.lstrip("/")

        target = find_file(host, rel)
        if target is None:
            filled = live_fill(host, rel)
            if filled is not None:
                body = transform(DUMP / host / rel, filled)
                ctype = sniff_type(DUMP / host / rel, body)
                self._send(200, body, ctype)
                return
            body = f"404 not in dump: {host}/{rel}\n".encode()
            self._send(404, body, "text/plain")
            return

        data = transform(target, target.read_bytes())
        ctype = sniff_type(target, data)
        headers = {"Cache-Control": "no-store"} if ctype == "text/html" else None
        self._send(200, data, ctype, headers)

    do_GET = _route
    do_HEAD = _route
    do_POST = _route
    do_PUT = _route

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:  # default access log is ours
        pass


if __name__ == "__main__":
    init_auth_db()
    print(f"hosts: {', '.join(HOSTS)}")
    print(f"serving Netblox + dump {DUMP} on 0.0.0.0:{PORT}  (default host: {DEFAULT_HOST})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
