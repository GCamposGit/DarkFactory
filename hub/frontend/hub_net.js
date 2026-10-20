// USR-207: the ONE place where the Hub frontend calls the network (raw fetch lives only here).
// Every module calls hubNetFetch(url, options) instead of fetch(): transient unavailability of the Hub
// (container being recreated during a deploy, 502/503/504 from the proxy, "Failed to fetch") is retried with
// backoff while a banner says the Hub is updating, so panels recover by themselves without raw errors.
const HUB_NET_BANNER_ID = "hub-net-banner";
const HUB_NET_BANNER_TEXT = "Hub atualizando, reconectando...";
const HUB_NET_RETRY_STATUS = [502, 503, 504];
// ~85 s in total: covers the roughly one minute the container needs to be recreated.
const HUB_NET_DELAYS_MS = [500, 1000, 2000, 3000, 5000, 8000, 10000, 10000, 10000, 10000, 10000, 10000];
const HUB_NET_SAFE_METHODS = ["GET", "HEAD", "OPTIONS"];
const hubNetState = { retrying: 0, watcher: false };

function hubNetSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function hubNetSetBanner(visible) {
  if (typeof document === "undefined" || !document || !document.body) return;
  let el = document.getElementById(HUB_NET_BANNER_ID);
  if (!visible) {
    if (el) el.hidden = true;
    return;
  }
  if (!el) {
    el = document.createElement("div");
    el.id = HUB_NET_BANNER_ID;
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.style.cssText =
      "position:fixed;top:0;left:0;right:0;z-index:99999;padding:8px 16px;text-align:center;" +
      "font:14px system-ui,sans-serif;background:#92400e;color:#fff;";
    el.textContent = HUB_NET_BANNER_TEXT;
    document.body.appendChild(el);
  }
  el.hidden = false;
}

function hubNetRetryable(method, error, response) {
  if (response) return HUB_NET_RETRY_STATUS.includes(response.status);
  // A network error on a non-idempotent request may or may not have been processed: never replay it.
  return Boolean(error) && HUB_NET_SAFE_METHODS.includes(method);
}

async function hubNetFetch(url, options = {}) {
  const method = String((options && options.method) || "GET").toUpperCase();
  let lastError = null;
  let lastResponse = null;
  let marked = false;
  for (let attempt = 0; attempt <= HUB_NET_DELAYS_MS.length; attempt += 1) {
    lastError = null;
    lastResponse = null;
    try {
      const response = await fetch(url, options);
      if (!HUB_NET_RETRY_STATUS.includes(response.status)) {
        if (marked) hubNetState.retrying = Math.max(0, hubNetState.retrying - 1);
        if (hubNetState.retrying === 0) hubNetSetBanner(false);
        return response;
      }
      lastResponse = response;
    } catch (error) {
      if (error && error.name === "AbortError") {
        if (marked) hubNetState.retrying = Math.max(0, hubNetState.retrying - 1);
        throw error;
      }
      lastError = error;
    }
    if (attempt === HUB_NET_DELAYS_MS.length || !hubNetRetryable(method, lastError, lastResponse)) break;
    if (!marked) {
      marked = true;
      hubNetState.retrying += 1;
    }
    hubNetSetBanner(true);
    await hubNetSleep(HUB_NET_DELAYS_MS[attempt]);
  }
  if (marked) hubNetState.retrying = Math.max(0, hubNetState.retrying - 1);
  if (lastResponse) {
    // Exhausted on a gateway status: hand the response to the caller (it already handles !ok).
    hubNetStartWatcher();
    return lastResponse;
  }
  hubNetStartWatcher();
  const friendly = new Error(HUB_NET_BANNER_TEXT);
  friendly.name = "HubUnavailableError";
  friendly.cause = lastError;
  throw friendly;
}

// After giving up, keep probing /health in the background; on recovery hide the banner and reload so every
// panel repaints with fresh data without the user doing anything.
function hubNetStartWatcher() {
  if (hubNetState.watcher) return;
  hubNetState.watcher = true;
  hubNetSetBanner(true);
  (async () => {
    let step = 0;
    for (;;) {
      await hubNetSleep(Math.min(15000, 2000 * (step += 1)));
      try {
        const response = await fetch("/health", { cache: "no-store" });
        if (response.ok) break;
      } catch (_) {}
    }
    hubNetState.watcher = false;
    hubNetSetBanner(false);
    try {
      if (typeof window !== "undefined" && window.location && window.location.reload) window.location.reload();
    } catch (_) {}
  })();
}

if (typeof window !== "undefined" && window) {
  window.hubNetFetch = hubNetFetch;
}
