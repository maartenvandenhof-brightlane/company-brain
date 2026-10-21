// Notices when the wiki changed (an import, each conversation a processing run files, an
// assistant's edit): a cheap versioned request every 15 s while the page is visible, and when
// it comes back into view. It is marked as a background request, so it does not keep an idle
// session alive. Listeners of ws:wiki-changed reload what they show.
import { getJSON } from "./api.js";
import { emit } from "./bus.js";

const EVERY_MS = 15000;
let version = "";
let timer = 0;
let busy = false;

export function knownVersion(v) {
  if (v && !version) version = v;
}

async function check() {
  if (busy || document.hidden) return;
  busy = true;
  try {
    const body = await getJSON(`/wiki/api/graph?v=${encodeURIComponent(version)}`, { background: true });
    if (!body.unchanged && body.version) {
      const changed = Boolean(version) && body.version !== version;
      version = body.version;
      if (changed) emit("ws:wiki-changed", { version, graph: body });
    }
  } catch {
    // offline or signed out: try again at the next tick
  } finally {
    busy = false;
  }
}

function schedule() {
  clearInterval(timer);
  if (!document.hidden) timer = setInterval(check, EVERY_MS);
}

document.addEventListener("visibilitychange", () => {
  schedule();
  if (!document.hidden) check();
});
window.addEventListener("focus", check);
schedule();
