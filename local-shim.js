// local-shim.js — served by server.py and injected as the first script in
// served HTML; also prepended (as a prelude) to the engine JS files so it
// covers worker/pthread contexts where window was never patched.
// Two-tier remap of request URLs built at runtime by Roblox code:
//   dumped hosts   -> this origin, /<host>/...            (served from the dump)
//   roblox/rbxcdn  -> this origin, /__up/<host>/...       (bridged live by server)
//   anything else  -> untouched (normal internet)
(() => {
  const g = globalThis;
  if (g.__rrm) return;
  g.__rrm = true;
  const ORIGIN = g.location.origin;
  const DUMPED = new Set(__HOSTS__);

  const remap = (u) => {
    if (typeof u !== "string") u = u.href || String(u);
    const m = /^(https?:)?\/\/([^/?#]+)([\/?#].*)?$/.exec(u);
    if (!m) return u;
    let authority = m[2];
    const at = authority.lastIndexOf("@");
    if (at >= 0) authority = authority.slice(at + 1);
    const host = authority.split(":")[0].toLowerCase();
    const path = m[3] || "";
    if (DUMPED.has(host)) return ORIGIN + "/" + host + path;
    if (host === "roblox.com" || host.endsWith(".roblox.com") ||
        host.endsWith(".rbxcdn.com")) {
      return ORIGIN + "/__up/" + host + path;
    }
    return u;
  };
  g.__rrmRemap = remap;

  const nativeFetch = g.fetch.bind(g);
  g.fetch = (input, init) => {
    if (typeof input === "string") {
      input = remap(input);
    } else if (input instanceof URL) {
      const r = remap(input.href);
      if (r !== input.href) input = r;
    } else if (typeof Request !== "undefined" && input instanceof Request) {
      const r = remap(input.url);
      if (r !== input.url) {
        try {
          input = new Request(r, input);
        } catch (e) {
          /* keep original on failure */
        }
      }
    }
    return nativeFetch(input, init);
  };

  const nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    return nativeOpen.call(this, method, remap(url), ...rest);
  };
})();
