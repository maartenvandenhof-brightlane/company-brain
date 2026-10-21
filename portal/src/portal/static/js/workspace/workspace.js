// The wiki workspace: tabs in one or two panes side by side, loaded as fragments
// (/wiki/api/fragment/{id}, /wiki/api/view?path=) so following a link does not reload the
// page. The server renders every fragment with the same templates as the full page and
// sanitises it (render.py); nothing here renders markdown.
//
// Links to wiki pages open in the current tab; Ctrl/Cmd-click or a middle click opens a new
// tab, Shift-click opens the other pane. Anything outside /wiki (import, settings, docs…) is a
// normal page load. The address bar follows the focused pane (history.pushState), so back,
// forward and deep links work: a deep link renders on the server and this takes it over.
import { getJSON } from "./api.js";
import { emit, on } from "./bus.js";
import { el, icon } from "./icons.js";
import { isWikiPath, load, save } from "./store.js";

export const state = load();
const narrow = window.matchMedia("(max-width: 859px)");
const main = document.getElementById("content");
const siteTitle = document.title.includes(" – ") ? document.title.split(" – ").pop() : document.title;
const panes = []; // {index, el, tablist, view, busy}
const views = new Map(); // tab id -> {url, data, el}, the rendered views (see remember())
const fragments = new Map(); // url -> fragment JSON, most recent last
const MAX_VIEWS = 12;
const MAX_FRAGMENTS = 40;
let counter = 0;

const newId = () => `t${Date.now().toString(36)}${(counter++).toString(36)}`;
const persist = () => save(state);

export function single() {
  return narrow.matches || !state.split;
}

export function focusedPane() {
  return single() ? 0 : state.focused;
}

export function activeTab(index = focusedPane()) {
  const pane = state.panes[index];
  return pane.tabs.find((t) => t.id === pane.active) || null;
}

export function activeView(index = focusedPane()) {
  const tab = activeTab(index);
  return tab ? views.get(tab.id) || null : null;
}

// ------------------------------------------------------------------ fragments

function fragmentUrl(url) {
  const u = new URL(url, window.location.origin);
  if (u.pathname.startsWith("/wiki/page/")) return `/wiki/api/fragment/${u.pathname.slice(11)}`;
  return `/wiki/api/view?path=${encodeURIComponent(u.pathname + u.search)}`;
}

async function fetchFragment(url, { fresh = false } = {}) {
  const key = url.split("#")[0];
  if (!fresh && fragments.has(key)) {
    const data = fragments.get(key);
    fragments.delete(key);
    fragments.set(key, data);
    return data;
  }
  let data = await getJSON(fragmentUrl(key));
  for (let hops = 0; data.redirect && hops < 3; hops++) data = await getJSON(fragmentUrl(data.redirect));
  if (data.redirect) throw new Error("Too many redirects.");
  fragments.set(key, data);
  if (key !== data.url) fragments.set(data.url, data);
  while (fragments.size > MAX_FRAGMENTS) fragments.delete(fragments.keys().next().value);
  return data;
}

// Forget cached fragments (the wiki changed): the next visit of a page fetches it again.
export function forgetFragments() {
  fragments.clear();
}

function buildView(data) {
  const template = document.createElement("template");
  template.innerHTML = data.html; // server-rendered, sanitised page HTML (see render.py)
  const wrap = el("div", "ws-content");
  wrap.append(template.content);
  return wrap;
}

function remember(tab, entry) {
  views.delete(tab.id);
  views.set(tab.id, entry);
  const shown = new Set(state.panes.map((p) => p.active));
  for (const [id, view] of views) {
    if (views.size <= MAX_VIEWS) break;
    if (!shown.has(id)) {
      views.delete(id);
      emit("ws:dispose", { el: view.el });
    }
  }
}

// ------------------------------------------------------------------ panes and tabs

function makePane(index) {
  const pane = el("section", "ws-pane");
  pane.dataset.pane = String(index);
  pane.setAttribute("aria-label", index === 0 ? "Left pane" : "Right pane");
  const bar = el("div", "ws-tabbar");
  const tablist = el("div", "ws-tabs");
  tablist.setAttribute("role", "tablist");
  tablist.setAttribute("aria-label", index === 0 ? "Open pages" : "Open pages, right pane");
  const view = el("div", "ws-view");
  view.id = `ws-view-${index}`;
  view.setAttribute("role", "tabpanel");
  view.tabIndex = -1;
  bar.append(tablist);
  const tools = el("div", "ws-tools");
  const split = el("button", "ws-tool");
  split.type = "button";
  split.dataset.action = "split";
  tools.append(split);
  bar.append(tools);
  pane.append(bar, view);
  pane.addEventListener("focusin", () => focusPane(index));
  pane.addEventListener("pointerdown", () => focusPane(index));
  let ticking = false;
  view.addEventListener(
    "scroll",
    () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        const tab = activeTab(index);
        if (tab && views.get(tab.id)?.el.parentNode === view) {
          tab.scroll = Math.round(view.scrollTop);
          persist();
        }
      });
    },
    { passive: true },
  );
  return { index, el: pane, bar, tablist, view, tools, split };
}

function renderTabs(index) {
  const pane = panes[index];
  const data = state.panes[index];
  pane.tablist.replaceChildren(
    ...data.tabs.map((tab) => {
      const wrap = el("div", "ws-tab");
      const button = el("button", "ws-tab-button");
      button.type = "button";
      button.id = `ws-tab-${tab.id}`;
      button.setAttribute("role", "tab");
      const selected = tab.id === data.active;
      button.setAttribute("aria-selected", String(selected));
      button.setAttribute("aria-controls", pane.view.id);
      button.tabIndex = selected ? 0 : -1;
      button.dataset.tab = tab.id;
      const swatch = el("span", "swatch");
      swatch.setAttribute("aria-hidden", "true");
      if (tab.type) wrap.dataset.type = tab.type;
      const title = el("span", "ws-tab-title", tab.title || "Loading…");
      button.title = tab.title || "";
      button.append(swatch, title);
      if (tab.draft) {
        const mark = el("span", "draft-mark");
        mark.append(el("span", "vh", " (draft to review)"));
        button.append(mark);
      }
      // closing by mouse; by keyboard it is Delete on the tab (or Alt+W), so the tab list
      // holds nothing but tabs
      const close = el("span", "ws-tab-close");
      close.dataset.close = tab.id;
      close.title = "Close tab";
      close.setAttribute("aria-hidden", "true");
      close.append(icon("close"));
      button.setAttribute("aria-keyshortcuts", "Delete");
      wrap.classList.toggle("is-active", selected);
      wrap.append(button, close);
      return wrap;
    }),
  );
  if (selectedButton(index)) pane.view.setAttribute("aria-labelledby", selectedButton(index).id);
  pane.split.replaceChildren(icon(state.split ? "unsplit" : "split"));
  pane.split.setAttribute("aria-label", state.split ? "Close the right pane" : "Split into two panes");
  pane.split.title = state.split ? "Close the right pane (\\)" : "Split into two panes (\\)";
  pane.split.hidden = index === 1 ? false : state.split;
}

const selectedButton = (index) => panes[index].tablist.querySelector('[aria-selected="true"]');

function paneLayout() {
  document.body.classList.toggle("ws-split", !single());
  panes[1].el.hidden = single();
  for (const pane of panes) pane.el.classList.toggle("is-focused", !single() && pane.index === state.focused);
  renderTabs(0);
  renderTabs(1);
  emit("ws:layout");
}

function focusPane(index) {
  if (single() || state.focused === index) return;
  state.focused = index;
  paneLayout();
  const tab = activeTab(index);
  const view = activeView(index);
  if (tab) setAddress(tab.url, { replace: true, title: tab.title });
  if (tab && view) emit("ws:active", { pane: index, tab, data: view.data, el: view.el });
  persist();
}

function setAddress(url, { replace = false, title = "" } = {}) {
  const here = window.location.pathname + window.location.search + window.location.hash;
  if (url !== here) {
    if (replace) window.history.replaceState({ ws: true }, "", url);
    else window.history.pushState({ ws: true }, "", url);
  }
  document.title = `${title || "Wiki"} – ${siteTitle}`;
}

async function show(index, tab, { push = false, fresh = false } = {}) {
  const pane = panes[index];
  let entry = views.get(tab.id);
  if (!entry || entry.url !== tab.url || fresh) {
    pane.el.setAttribute("aria-busy", "true");
    try {
      const data = await fetchFragment(tab.url, { fresh });
      entry = { url: tab.url, data, el: buildView(data) };
    } catch (error) {
      entry = { url: tab.url, data: { url: tab.url, title: "Not loaded", kind: "error", type: "", html: "" }, el: errorView(tab.url, error) };
    } finally {
      pane.el.removeAttribute("aria-busy");
    }
    remember(tab, entry);
  }
  if (state.panes[index].active !== tab.id) return; // another tab was chosen meanwhile
  const data = entry.data;
  if (data.kind !== "error") {
    tab.title = data.title;
    tab.type = data.type || "";
    tab.draft = Boolean(data.draft);
    if (data.url && data.url !== tab.url.split("#")[0]) tab.url = data.url;
  }
  pane.view.replaceChildren(entry.el);
  if (data.type) pane.el.dataset.type = data.type;
  else delete pane.el.dataset.type;
  renderTabs(index);
  const hash = tab.url.split("#")[1];
  if (hash) scrollToHash(pane.view, decodeURIComponent(hash));
  else pane.view.scrollTop = tab.scroll || 0;
  emit("ws:view", { pane: index, tab, data, el: entry.el });
  if (index === focusedPane()) {
    setAddress(tab.url, { replace: !push, title: tab.title });
    emit("ws:active", { pane: index, tab, data, el: entry.el });
  }
  persist();
}

function errorView(url, error) {
  const box = el("div", "page");
  const callout = el("div", "callout bad");
  callout.setAttribute("role", "alert");
  const body = el("div", "callout-body");
  body.append(el("p", "callout-title", "This page could not be loaded"));
  body.append(el("p", "", error?.message || "Something went wrong."));
  const retry = el("a", "", "Load it as a full page");
  retry.href = url;
  retry.dataset.full = "1";
  const p = el("p");
  p.append(retry);
  body.append(p);
  callout.append(body);
  box.append(callout);
  return box;
}

function scrollToHash(view, hash) {
  const want = hash.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const heading = [...view.querySelectorAll(".prose :is(h1,h2,h3,h4,h5,h6)")].find(
    (h) => h.textContent.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") === want,
  );
  if (heading) heading.scrollIntoView({ block: "start" });
}

function activate(index, id, { push = false } = {}) {
  const tab = state.panes[index].tabs.find((t) => t.id === id);
  if (!tab) return;
  const current = activeTab(index);
  if (current && current.id !== id) current.scroll = Math.round(panes[index].view.scrollTop);
  state.panes[index].active = id;
  if (!single()) state.focused = index;
  paneLayout();
  show(index, tab, { push });
}

// Open a wiki URL: in the current tab, a new tab, or the other pane.
export function open(url, { where = "current", pane = focusedPane() } = {}) {
  if (!isWikiPath(url)) {
    window.location.assign(url);
    return;
  }
  let index = pane;
  if (where === "other") {
    if (narrow.matches) where = "new";
    else {
      index = pane === 0 ? 1 : 0;
      if (!state.split) state.split = true;
      where = "new";
    }
  }
  const data = state.panes[index];
  let tab = where === "current" ? activeTab(index) : null;
  if (tab) {
    tab.scroll = tab.url === url ? tab.scroll : 0;
    if (tab.url !== url) views.delete(tab.id);
    tab.url = url;
  } else {
    tab = { id: newId(), url, title: "", type: "", draft: false, scroll: 0 };
    const at = data.tabs.findIndex((t) => t.id === data.active);
    data.tabs.splice(at + 1, 0, tab);
  }
  data.active = tab.id;
  state.focused = index;
  paneLayout();
  show(index, tab, { push: true });
}

export function closeTab(index, id) {
  const data = state.panes[index];
  const at = data.tabs.findIndex((t) => t.id === id);
  if (at < 0) return;
  const [gone] = data.tabs.splice(at, 1);
  const view = views.get(gone.id);
  views.delete(gone.id);
  if (view) emit("ws:dispose", { el: view.el });
  if (data.active === id) data.active = data.tabs[Math.min(at, data.tabs.length - 1)]?.id ?? null;
  if (!data.tabs.length) {
    if (index === 1 || state.split) {
      if (index === 0) state.panes = [state.panes[1], { tabs: [], active: null }];
      state.split = false;
      state.focused = 0;
      paneLayout();
      const tab = activeTab(0);
      if (tab) show(0, tab);
      return;
    }
    open("/wiki", { pane: 0 });
    return;
  }
  paneLayout();
  show(index, activeTab(index));
  if (!single() || index === 0) selectedButton(index)?.focus();
}

export function closeActiveTab() {
  const tab = activeTab();
  if (tab) closeTab(focusedPane(), tab.id);
}

export function toggleSplit() {
  if (narrow.matches) return;
  if (state.split) {
    const moved = state.panes[1].tabs;
    state.panes[0].tabs.push(...moved);
    state.panes[1] = { tabs: [], active: null };
    state.split = false;
    state.focused = 0;
    paneLayout();
    show(0, activeTab(0));
  } else {
    const tab = activeTab(0);
    const copy = { id: newId(), url: tab ? tab.url : "/wiki", title: tab?.title || "", type: tab?.type || "", draft: Boolean(tab?.draft), scroll: 0 };
    state.panes[1] = { tabs: [copy], active: copy.id };
    state.split = true;
    state.focused = 1;
    paneLayout();
    show(1, copy);
  }
  persist();
}

export function cycleTab(step) {
  const index = focusedPane();
  const data = state.panes[index];
  if (data.tabs.length < 2) return;
  const at = data.tabs.findIndex((t) => t.id === data.active);
  activate(index, data.tabs[(at + step + data.tabs.length) % data.tabs.length].id);
}

export function goToTab(n) {
  const data = state.panes[focusedPane()];
  const tab = n === 9 ? data.tabs.at(-1) : data.tabs[n - 1];
  if (tab) activate(focusedPane(), tab.id);
}

// Reload the fragment of every open tab that is showing (after an import or processing run).
export function refreshShown() {
  forgetFragments();
  for (const index of single() ? [0] : [0, 1]) {
    const tab = activeTab(index);
    if (tab) {
      const scroll = panes[index].view.scrollTop;
      tab.scroll = scroll;
      show(index, tab, { fresh: true });
    }
  }
}

// ------------------------------------------------------------------ links, forms, history

function paneOf(node) {
  const pane = node.closest?.(".ws-pane");
  return pane ? Number(pane.dataset.pane) : focusedPane();
}

function onLink(event) {
  if (event.defaultPrevented || event.button > 1) return;
  const link = event.target.closest?.("a[href]");
  if (!link || link.target === "_blank" || link.hasAttribute("download") || link.dataset.full) return;
  const url = new URL(link.href, window.location.href);
  if (url.origin !== window.location.origin || !isWikiPath(url.pathname)) return;
  const target = url.pathname + url.search + url.hash;
  const modifier = event.ctrlKey || event.metaKey || event.button === 1;
  if (!modifier && !event.shiftKey && url.hash && url.pathname + url.search === (activeTab(paneOf(link))?.url || "").split("#")[0]) {
    event.preventDefault();
    scrollToHash(panes[paneOf(link)].view, decodeURIComponent(url.hash.slice(1)));
    return;
  }
  event.preventDefault();
  closeDrawers();
  const where = modifier ? "new" : event.shiftKey ? "other" : "current";
  open(target, { where, pane: link.closest(".ws-pane") ? paneOf(link) : focusedPane() });
}

function onSubmit(event) {
  const form = event.target;
  if (event.defaultPrevented || (form.method || "get").toLowerCase() !== "get") return;
  const action = new URL(form.action, window.location.href);
  if (action.origin !== window.location.origin || !isWikiPath(action.pathname)) return;
  event.preventDefault();
  const query = new URLSearchParams(new FormData(form));
  for (const [key, value] of [...query]) if (!value) query.delete(key);
  const qs = query.toString();
  form.closest("details")?.removeAttribute("open");
  open(action.pathname + (qs ? `?${qs}` : ""), { pane: paneOf(form) });
}

function closeDrawers() {
  for (const box of document.querySelectorAll("input.drawer-toggle")) box.checked = false;
}

function onPopState() {
  const url = window.location.pathname + window.location.search + window.location.hash;
  if (!isWikiPath(url)) {
    window.location.reload();
    return;
  }
  const index = focusedPane();
  const tab = activeTab(index);
  if (tab && tab.url === url) return;
  if (tab) {
    tab.url = url;
    views.delete(tab.id);
    show(index, tab);
  } else open(url);
}

function onTabKey(event) {
  const button = event.target.closest?.('[role="tab"]');
  if (!button) return;
  const index = paneOf(button);
  const buttons = [...panes[index].tablist.querySelectorAll('[role="tab"]')];
  const at = buttons.indexOf(button);
  let next = null;
  if (event.key === "ArrowRight") next = buttons[(at + 1) % buttons.length];
  else if (event.key === "ArrowLeft") next = buttons[(at - 1 + buttons.length) % buttons.length];
  else if (event.key === "Home") next = buttons[0];
  else if (event.key === "End") next = buttons.at(-1);
  else if (event.key === "Delete") {
    event.preventDefault();
    closeTab(index, button.dataset.tab);
    return;
  }
  if (next) {
    event.preventDefault();
    for (const b of buttons) b.tabIndex = b === next ? 0 : -1;
    next.focus();
  }
}

function onTabClick(event) {
  const close = event.target.closest?.("[data-close]");
  if (close) {
    closeTab(paneOf(close), close.dataset.close);
    return;
  }
  const button = event.target.closest?.('[role="tab"]');
  if (button) {
    const index = paneOf(button);
    if (event.button === 1) closeTab(index, button.dataset.tab);
    else activate(index, button.dataset.tab);
    return;
  }
  const tool = event.target.closest?.("[data-action=split]");
  if (tool) toggleSplit();
}

// ------------------------------------------------------------------ start

export function boot(adoptServerInfo = () => {}) {
  document.body.classList.add("ws");
  main.removeAttribute("data-type");
  const holder = el("div", "ws-panes");
  panes.push(makePane(0), makePane(1));
  holder.append(panes[0].el, panes[1].el);
  // The server-rendered page becomes the current tab's view.
  const server = el("div", "ws-content");
  const keep = new Set([...main.querySelectorAll(":scope > .wiki-bar, :scope > .notices")]);
  for (const child of [...main.children]) if (!keep.has(child)) server.append(child);
  main.append(holder);

  const url = window.location.pathname + window.location.search + window.location.hash;
  const index = focusedPane();
  let tab = state.panes[index].tabs.find((t) => t.url === url);
  if (!tab) {
    tab = activeTab(index);
    if (tab) {
      tab.url = url;
      tab.scroll = 0;
    } else {
      tab = { id: newId(), url, title: "", type: "", draft: false, scroll: 0 };
      state.panes[index].tabs.push(tab);
    }
  }
  state.panes[index].active = tab.id;
  const titleNode = document.title.includes(" – ") ? document.title.split(" – ").slice(0, -1).join(" – ") : "Wiki";
  const serverType = server.querySelector(".page > .type-tab")?.dataset.type || "";
  const data = {
    url: url.split("#")[0],
    kind: url.startsWith("/wiki/page/") ? "page" : "view",
    title: titleNode,
    type: serverType,
    draft: Boolean(server.querySelector(".page[data-draft]")),
    server: true, // the info pane is already in the page (see context.js)
  };
  adoptServerInfo(data);
  views.set(tab.id, { url: tab.url, data, el: server });
  tab.title = data.title;
  tab.type = data.type;
  tab.draft = data.draft;

  document.addEventListener("click", onLink);
  document.addEventListener("auxclick", (event) => {
    if (event.button === 1) onLink(event);
  });
  document.addEventListener("submit", onSubmit);
  window.addEventListener("popstate", onPopState);
  for (const pane of panes) {
    pane.tablist.addEventListener("keydown", onTabKey);
    pane.bar.addEventListener("click", onTabClick);
    pane.tablist.addEventListener("auxclick", (event) => {
      if (event.button === 1) {
        event.preventDefault();
        onTabClick(event);
      }
    });
  }
  narrow.addEventListener("change", () => {
    paneLayout();
    for (const i of single() ? [0] : [0, 1]) {
      const t = activeTab(i);
      if (t && views.get(t.id)?.el.parentNode !== panes[i].view) show(i, t);
    }
  });
  window.history.replaceState({ ws: true }, "", url);
  paneLayout();
  show(index, tab);
  if (!single()) {
    const other = index === 0 ? 1 : 0;
    const t = activeTab(other);
    if (t) show(other, t);
  }
}

on("ws:refresh", refreshShown);
