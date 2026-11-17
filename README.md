# play.roblox.com — local mirror

Netblox adds a custom browser launcher in front of the Localblox play.roblox.com site dump. The launcher is served at /; the game client remains available at /games/start. Both run from a single Python-stdlib server. No proxy, no CA
cert, no `/etc/hosts` edit, no sudo. The site **shell + WebGL engine boot
fully offline**; only *game traffic* (live API/asset/join calls) is forwarded
to the real Roblox hosts, and only through an explicit same-origin bridge.

Dump target build: `version-75f1039cb4964863-RobloxWebClient`
(`0.745.0.7451145`). Source zip: `~/Downloads/play.roblox.com.zip`.

## Run / teardown

```fish
# start (from this directory)
PORT=8080 python3 server.py     # listens on 0.0.0.0; defaults to 8080

# stop   (bracket trick so pkill doesn't match its own shell and suicide)
pkill -f '[s]erver\.py 8080'
```

Open http://localhost:8080/ for Netblox. To open the underlying game client directly, visit http://localhost:8080/games/start?placeId=<id>.

## Architecture

One handler (`server.py`) routes by first path segment:

| Path                                   | Served from                                     |
|----------------------------------------|-------------------------------------------------|
| `/`                                    | Netblox launcher                                 |
| `/<dumped-host>/<path>`                | virtual host off the dump (e.g. `/games.roblox.com/...`) |
| `/__up/<host>/<path>`                  | **live bridge** — forwarded server-side to the real `*.roblox.com` / `*.rbxcdn.com` host |
| `/<anything else>`                     | default host `play.roblox.com` (root-relative page assets) |

Three runtime layers make absolute/runtime-built URLs resolve to localhost:

1. **Text rewrite** — `https://<dumped-host>/…` inside served text bodies is
   rewritten to `http://localhost:8080/<host>/…`.
2. **`local-shim.js`** — injected as the first `<script>` in served HTML, and
   **prepended as a prelude** to the engine JS (`WebClient.js`,
   `WasmPlayer.js`, `audio-processor-fast.js`) because the engine's XHR/fetch
   run inside emscripten workers where page-level patches never apply. It
   monkey-patches `fetch` + `XMLHttpRequest.open` with a two-tier remap:
   - dumped hosts → `/<host>/…` (served from the dump),
   - other `*.roblox.com` / `*.rbxcdn.com` → `/__up/<host>/…` (bridged live),
   - everything else untouched (normal internet).
3. **Live bridge** (`/__up/`) — the browser request is same-origin, so no
   CORS/preflight applies; the *server* forwards to real Roblox with live-like
   headers (`Origin`/`Referer` = `https://play.roblox.com`,
   `Accept-Encoding: identity`). Only `roblox.com`/`rbxcdn.com` hosts are
   allowed (see `upstream_allowed()`).

### COOP / COEP
Every response carries `Cross-Origin-Opener-Policy: same-origin` and
`Cross-Origin-Embedder-Policy: require-corp`. These are **required** — without
them `SharedArrayBuffer`/pthreads are unavailable and the engine panics
(`RSL - panic: … PAL.Emscripten`).

### Live-fill (build assets missing from the dump)
A miss inside `setup.rbxcdn.com/webclient/<build>/` or
`play.roblox.com/<page-build>/` is fetched once from the live CDN, persisted
into the dump dir, then served locally thereafter. Logged as `[fill] … <- live`.

### Content-type / lookup quirks the dump introduced
The dumper stored API responses as `<path>.html`, dropped query strings, and
lost content types. The server therefore falls back to `+.html` / `+.png`
lookup ignoring the query, and re-derives content types (JSON-sniffing where
needed).

## Authentication and account safety

Netblox has its own username/password account system. Passwords are stored as PBKDF2-HMAC-SHA256 hashes with unique random salts (never plaintext). Successful registration/sign-in creates a random 30-day session token; only its SHA-256 hash is stored in SQLite. The browser receives the token only in an HttpOnly, SameSite=Lax cookie. Sign-out revokes the server-side session. No passwords or tokens are stored in localStorage/sessionStorage.

Configure `NETBLOX_DB_PATH` (or `NETBLOX_DATA_DIR`) to point at persistent storage. On Enzonic, mount persistent storage and set e.g. `NETBLOX_DB_PATH=/data/netblox.sqlite3`; otherwise accounts may be lost when an ephemeral container is replaced. `COOKIE_SECURE=1` is the production default for HTTPS; set `COOKIE_SECURE=0` only for local plain-HTTP development.

Netblox accounts are separate from Roblox accounts. Roblox OAuth 2.0 can authenticate a Roblox identity and authorize supported Open Cloud scopes, but its tokens are not a general Roblox gameplay session. The launcher does not collect Roblox passwords or .ROBLOSECURITY cookies, and the old Localblox cookie-file workflow remains disabled. Live Roblox game joins may still be rejected when Roblox requires an official gameplay session.

## Netblox launcher

- / serves the Netblox launcher.
- /netblox/ contains its CSS and JavaScript assets.
- /games/start?placeId=<id> opens the Localblox browser client.
- The launcher includes account registration/sign-in, persistent 30-day server-side sessions, and client-side starter-catalogue search/filtering. Live Roblox trending discovery is not yet connected.

The server accepts the platform PORT environment variable and binds to 0.0.0.0 for hosting behind a reverse proxy.

## Debugging

- Server logs every request (`<status> <bytes> <verb> <path>`), plus
  `[fill] …` and `[bridge] …` lines.

## Files

- `server.py` — the server: Netblox launcher routing, live-fill, anonymous bridge, COOP/COEP, transforms,
  engine prelude.
- `netblox/` — Netblox launcher HTML, CSS and JavaScript.
- `local-shim.js` — two-tier runtime URL remap (`__HOSTS__` placeholder is
  substituted at serve time).
- `<host-dirs>/…` — the extracted dump (also accumulates live-filled assets).

## History

An earlier mitmproxy-based stack in `~/Projects/brave-cache-recovery/` (and the
`fitzypopper/WasmPlayer.js` GitHub repo for the older `f4a4dba57f874ad0`
build) is **superseded** by this plain-local-server approach.
