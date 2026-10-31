# play.roblox.com — local mirror

Runs the user's full `play.roblox.com` site dump entirely on
`http://localhost:8080` from a single Python-stdlib server. No proxy, no CA
cert, no `/etc/hosts` edit, no sudo. The site **shell + WebGL engine boot
fully offline**; only *game traffic* (live API/asset/join calls) is forwarded
to the real Roblox hosts, and only through an explicit same-origin bridge.

Dump target build: `version-75f1039cb4964863-RobloxWebClient`
(`0.745.0.7451145`). Source zip: `~/Downloads/play.roblox.com.zip`.

## Run / teardown

```fish
# start (from this directory)
python3 server.py 8080          # default port 8080

# stop   (bracket trick so pkill doesn't match its own shell and suicide)
pkill -f '[s]erver\.py 8080'
```

Then open `http://localhost:8080/` (redirects to `/games/start`). To join a
specific place: `http://localhost:8080/games/start?placeId=<id>`.

## Architecture

One handler (`server.py`) routes by first path segment:

| Path                                   | Served from                                     |
|----------------------------------------|-------------------------------------------------|
| `/`                                    | 302 → `/games/start`                             |
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

## Playing a live game (auth)

Joining a live server requires a logged-in session. Anonymous join is
rejected by Roblox: `gamejoin …/v1/join-game` returns
`{"status":12,"message":"Unable to join Game 212"}` (engine logs
`[LIFECYCLE] joinGameFailed state=12`).

To authenticate, create `cookie.txt` in this directory containing your
`.ROBLOSECURITY` cookie value (the file content is sent verbatim as the
`Cookie` header on bridged requests; **never commit it**):

```fish
# value only, no "Cookie:" prefix, no quotes
echo -n '<your .ROBLOSECURITY value>' > cookie.txt
chmod 600 cookie.txt
```

If `cookie.txt` is absent, the bridge falls back to forwarding the browser's
own `Cookie` header (which is empty on a localhost origin). Note the cookie
value is a live credential — treat it as exposed and rotate it if concerned.

## What works today

- Full page + Next.js shell, CSS, fonts, favicons — all local.
- WebGL/WebAssembly engine (`WasmPlayer.js` + `WebClient.js` + 100 MB
  `WebClient.wasm`) boots, registers 2862 asset metadata entries, runs the
  render loop, and renders the authentic "Joining experience" loading screen.
- ClientSettings, flags (22937), metadata, shaders, sky/plastic textures,
  fonts — served locally (dump + live-fill).
- Live game traffic via the bridge: `gamejoin`, `assetdelivery`, `ecsv2`,
  `ephemeralcounters`, `fts.rbxcdn.com` (real place geometry), etc.
- The place's Lua runs and geometry loads; the last hop (authenticated join
  to a live server) is the only step gated behind `cookie.txt`.

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

- `server.py` — the server: routing, live-fill, bridge, COOP/COEP, transforms,
  engine prelude.
- `local-shim.js` — two-tier runtime URL remap (`__HOSTS__` placeholder is
  substituted at serve time).
- `<host-dirs>/…` — the extracted dump (also accumulates live-filled assets).
- `cookie.txt` — optional, user-created, holds `.ROBLOSECURITY` for the bridge.

## History

An earlier mitmproxy-based stack in `~/Projects/brave-cache-recovery/` (and the
`fitzypopper/WasmPlayer.js` GitHub repo for the older `f4a4dba57f874ad0`
build) is **superseded** by this plain-local-server approach.
