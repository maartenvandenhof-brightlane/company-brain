// The workspace's state in localStorage: open tabs per pane, the split, side pane widths and
// collapse, explorer folders, graph settings. Every access is guarded (private windows, blocked
// storage, quota): the workspace then works from defaults in memory.
const KEY = "sb.workspace.v1";

export const DEFAULTS = {
  panes: [{ tabs: [], active: null }, { tabs: [], active: null }],
  focused: 0,
  split: false,
  left: { collapsed: false, width: 272 },
  right: { collapsed: false, width: 304 },
  explorer: { open: ["wiki"], filter: "" },
  graph: {},
  local: { depth: 1 },
};

const isWikiPath = (url) => typeof url === "string" && /^\/wiki(\/|$|\?)/.test(url) && !url.startsWith("/wiki/api/");

function clean(tab) {
  if (!tab || !isWikiPath(tab.url) || typeof tab.id !== "string") return null;
  return {
    id: tab.id.slice(0, 40),
    url: tab.url.slice(0, 2000),
    title: String(tab.title || "").slice(0, 300),
    type: String(tab.type || "").slice(0, 40),
    draft: Boolean(tab.draft),
    scroll: Number.isFinite(tab.scroll) ? Math.max(0, tab.scroll) : 0,
  };
}

function merge(saved) {
  const state = structuredClone(DEFAULTS);
  if (!saved || typeof saved !== "object") return state;
  if (Array.isArray(saved.panes)) {
    state.panes = [0, 1].map((i) => {
      const pane = saved.panes[i] || {};
      const tabs = (Array.isArray(pane.tabs) ? pane.tabs : []).map(clean).filter(Boolean).slice(0, 30);
      const active = tabs.some((t) => t.id === pane.active) ? pane.active : tabs[0]?.id ?? null;
      return { tabs, active };
    });
  }
  state.focused = saved.focused === 1 ? 1 : 0;
  state.split = Boolean(saved.split) && state.panes[1].tabs.length > 0;
  for (const side of ["left", "right"]) {
    const s = saved[side] || {};
    state[side].collapsed = Boolean(s.collapsed);
    if (Number.isFinite(s.width)) state[side].width = s.width;
  }
  if (saved.explorer && Array.isArray(saved.explorer.open)) {
    state.explorer.open = saved.explorer.open.filter((p) => typeof p === "string").slice(0, 200);
  }
  if (saved.graph && typeof saved.graph === "object") state.graph = saved.graph;
  if (saved.local && [1, 2, 3].includes(saved.local.depth)) state.local.depth = saved.local.depth;
  return state;
}

export function load() {
  try {
    const raw = window.localStorage.getItem(KEY);
    return merge(raw ? JSON.parse(raw) : null);
  } catch {
    return structuredClone(DEFAULTS);
  }
}

let timer = 0;
export function save(state) {
  clearTimeout(timer);
  timer = setTimeout(() => {
    try {
      window.localStorage.setItem(KEY, JSON.stringify(state));
    } catch {
      // storage unavailable or full: the workspace keeps working, it just forgets on reload
    }
  }, 250);
}

// Small per-viewer preferences outside the workspace state (theme, graph options).
export function getPref(name, fallback) {
  try {
    const value = window.localStorage.getItem(`sb.${name}`);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

export function setPref(name, value) {
  try {
    window.localStorage.setItem(`sb.${name}`, JSON.stringify(value));
  } catch {
    // ignored: see save()
  }
}

export { isWikiPath };
