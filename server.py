#!/usr/bin/env python3
"""
Localhost server for the play.roblox.com site dump (see README.md).

Routing:
  /                     -> 302 /games/start
  /<dumped-host>/<path> -> virtual host (e.g. /games.roblox.com/v1/games)
  /__up/<host>/<path>   -> live bridge: forwarded server-side to the real
                           roblox/rbxcdn host (game traffic; browser stays
                           same-origin so CORS/cookies are a non-issue)
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
    print(f"hosts: {', '.join(HOSTS)}")
    print(f"serving Netblox + dump {DUMP} on 0.0.0.0:{PORT}  (default host: {DEFAULT_HOST})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
