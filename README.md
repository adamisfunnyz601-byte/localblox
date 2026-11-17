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

Netblox does **not** collect Roblox passwords or .ROBLOSECURITY cookies. The server bridge deliberately strips incoming Cookie headers and does not read a cookie file. The old Localblox cookie-file workflow is disabled in this fork.

Roblox OAuth 2.0 can authenticate a Roblox identity and authorize supported Open Cloud scopes, but its ID/access tokens are **not** a general Roblox gameplay session and cannot be used as a replacement for the browser's official game-join session. Netblox's sign-in UI therefore remains an integration placeholder until an approved OAuth app and a supported gameplay authentication path are available. Users are sent only to Roblox's official website for sign-in.

Live Roblox game joins may still be rejected when Roblox requires a gameplay session. Do not add cookie collection, cookie forwarding, login-page proxying, or credential capture to work around this restriction.

## Netblox launcher

- / serves the Netblox launcher.
- /netblox/ contains its CSS and JavaScript assets.
- /games/start?placeId=<id> opens the Localblox browser client.
- The current launcher includes a small starter catalogue and client-side search/filtering. It does not yet fetch live trending/search results from Roblox.

The server accepts the platform PORT environment variable and binds to 0.0.0.0 for hosting behind a reverse proxy.

## Debugging

- Server logs every request (`<status> <bytes> <verb> <path>`), plus
  `[fill] …` and `[bridge] …` lines.
- `gamejoin` request/response bodies are appended to
  `/tmp/opencode/join-debug.txt` (the `Cookie` header is redacted there).
- Headless smoke test (real time — **do not** add `--virtual-time-budget`, it
  warps Emscripten's clock):

  ```fish
  timeout 50 brave --headless=new --disable-gpu --enable-unsafe-swiftshader \
    --user-data-dir=/tmp/opencode/brave-local-profile \
    --enable-logging=stderr --v=0 --window-size=1280,900 \
    "http://localhost:8080/games/start?placeId=100495655211498" \
    | grep CONSOLE
  ```

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
