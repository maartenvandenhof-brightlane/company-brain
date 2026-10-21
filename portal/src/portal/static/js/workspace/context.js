// The context pane (right column) for the focused tab:
//   - where the page stands: a draft to review, or an imported chat not processed yet;
//   - an outline of its headings, following the reading position (scrollspy);
//   - its neighbourhood as a small graph (local-graph.js fills .ctx-local);
//   - backlinks, links from it and its properties, as the server rendered them for the page
//     (the fragment's info_html), backlinks with a snippet of their context where the
//     search finds one;
//   - unlinked mentions: pages that name it without linking, fetched a moment after the page
//     is shown (one search for its title).
import { getJSON } from "./api.js";
import { emit, on } from "./bus.js";
import { el } from "./icons.js";
import { focusedPane } from "./workspace.js";

let body;
let spy = null; // {view, items: [{heading, link}]}
let mentionsTimer = 0;
let current = null;
const infos = new WeakMap(); // fragment data -> its info element
const mentions = new Map(); // page id -> response, until the wiki changes

// where processing lists chats with nothing worth filing (wiki-mcp's SKIPPED_PAGE)
const SKIPPED = "wiki/references/skipped-conversations";
const pageHref = (id) => `/wiki/page/${id.split("/").map(encodeURIComponent).join("/")}`;

function infoOf(data) {
  let node = infos.get(data);
  if (node) return node;
  node = el("div", "ctx-info");
  if (data.info_html) {
    const template = document.createElement("template");
    template.innerHTML = data.info_html; // server-rendered and sanitised, like the page
    const inner = template.content.querySelector(".side-right");
    if (inner) {
      inner.querySelector(".drawer-close")?.remove();
      node.append(...inner.childNodes);
    }
  }
  infos.set(data, node);
  return node;
}

function statusOf(data) {
  if (data.kind !== "page") return null;
  const line = el("p", "ctx-status");
  const tags = Array.isArray(data.frontmatter?.tags) ? data.frontmatter.tags.map(String) : [];
  if (data.draft) {
    line.append(el("span", "draft-mark"), el("span", "", "Draft to review"));
    return line;
  }
  if (data.raw && tags.includes("chat-import")) {
    const citers = (data.backlinks || []).filter((b) => b.type !== "source");
    const citing = citers.length;
    const skipped = citing > 0 && citers.every((b) => b.id === SKIPPED);
    const swatch = el("span", `swatch${citing ? "" : " hollow"}`);
    swatch.dataset.type = "source";
    swatch.setAttribute("aria-hidden", "true");
    line.append(swatch);
    const said = skipped
      ? "Processed: nothing worth filing (listed under Skipped conversations)."
      : citing
        ? `Processed: ${citing} page${citing === 1 ? " cites" : "s cite"} this chat.`
        : "Not processed yet: no page cites this chat. ";
    const text = el("span", "", said);
    if (!citing) {
      const link = el("a", "", "Process imported chats");
      link.href = "/import#process-title";
      text.append(link);
    }
    line.append(text);
    return line;
  }
  if (data.status && data.status !== "stable") {
    line.append(el("span", "pill plain", data.status));
    return line;
  }
  return null;
}

// ------------------------------------------------------------------ outline and scrollspy

function outlineOf(view) {
  const headings = [...view.querySelectorAll(".prose :is(h1,h2,h3,h4)")].filter((h) => h.getClientRects().length);
  if (headings.length < 2) return null;
  const section = el("details", "ctx-block ctx-outline");
  section.open = headings.length <= 60;
  const summary = el("summary", "nav-heading");
  summary.append(el("span", "", "Outline"), el("span", "count", String(headings.length)));
  const list = el("ol", "outline-list");
  const items = headings.slice(0, 400).map((heading) => {
    const li = el("li", `outline-${heading.tagName.toLowerCase()}`);
    const link = el("a", "", heading.textContent.trim() || "Untitled");
    link.href = "#";
    link.addEventListener("click", (event) => {
      event.preventDefault();
      heading.tabIndex = -1;
      heading.scrollIntoView({ block: "start" });
      heading.focus({ preventScroll: true });
    });
    li.append(link);
    list.append(li);
    return { heading, link };
  });
  section.append(summary, list);
  spy = { view: view.closest(".ws-view"), items, list };
  return section;
}

function onScroll() {
  if (!spy?.view) return;
  const top = spy.view.getBoundingClientRect().top + 96;
  let active = null;
  for (const item of spy.items) {
    if (item.heading.getBoundingClientRect().top <= top) active = item;
    else break;
  }
  for (const item of spy.items) {
    const on = item === active;
    if (on !== item.link.hasAttribute("aria-current")) {
      if (on) item.link.setAttribute("aria-current", "location");
      else item.link.removeAttribute("aria-current");
    }
  }
  // keep the current heading visible in a long outline, without moving the page
  if (active) {
    const list = spy.list;
    const row = active.link.offsetTop;
    if (row < list.scrollTop || row > list.scrollTop + list.clientHeight - 24) list.scrollTop = row - list.clientHeight / 2;
  }
}

let ticking = false;
function scrollHandler() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => {
    ticking = false;
    onScroll();
  });
}

// ------------------------------------------------------------------ mentions and backlink context

// Search snippets come as escaped text with <mark> (render.render_snippet); they are rebuilt
// from text nodes, so nothing else can come along.
function snippetOf(html) {
  if (!html) return null;
  const template = document.createElement("template");
  template.innerHTML = html;
  const out = el("small", "ctx-snippet");
  for (const node of template.content.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) out.append(node.textContent);
    else if (node.nodeName === "MARK") out.append(el("mark", "", node.textContent));
  }
  return out.textContent.trim() ? out : null;
}

function mentionsBlock() {
  const section = el("details", "ctx-block ctx-mentions");
  const summary = el("summary", "nav-heading");
  const count = el("span", "count", "…");
  summary.append(el("span", "", "Unlinked mentions"), count);
  const note = el("p", "none", "Looking for pages that name this one…");
  const list = el("ul", "link-list");
  section.append(summary, note, list);
  return { section, count, list, note };
}

function fillMentions(block, result, info) {
  const found = result.mentions;
  block.count.textContent = String(found.length);
  block.note.textContent = found.length ? "" : "No other page names it without linking to it.";
  block.note.hidden = found.length > 0;
  block.list.replaceChildren(
    ...found.map((m) => {
      const li = el("li");
      const a = el("a");
      a.href = pageHref(m.id);
      a.dataset.type = m.type || "";
      const swatch = el("span", "swatch");
      swatch.setAttribute("aria-hidden", "true");
      const text = el("span", "link-text", m.title);
      const snippet = snippetOf(m.snippet_html);
      if (snippet) text.append(snippet);
      a.append(swatch, text);
      li.append(a);
      return li;
    }),
  );
  // backlinks: the sentence they link from, where the search found it
  for (const a of info.querySelectorAll('section[aria-labelledby="backlinks-h"] .link-list a')) {
    const id = decodeURIComponent(new URL(a.href, window.location.origin).pathname.replace(/^\/wiki\/page\//, ""));
    const snippet = snippetOf(result.context[id]);
    const text = a.querySelector(".link-text");
    if (snippet && text && !text.querySelector(".ctx-snippet")) text.append(snippet);
  }
}

async function loadMentions(data, block, info) {
  try {
    let result = mentions.get(data.id);
    if (!result) {
      result = await getJSON(`/wiki/api/mentions/${data.id.split("/").map(encodeURIComponent).join("/")}`);
      mentions.set(data.id, result);
    }
    if (current === data) fillMentions(block, result, info);
  } catch (error) {
    block.note.textContent = error.message;
  }
}

// ------------------------------------------------------------------ the pane

function show({ pane, data, el: view }) {
  if (pane !== focusedPane()) return;
  current = data;
  clearTimeout(mentionsTimer);
  spy?.view?.removeEventListener("scroll", scrollHandler);
  spy = null;
  const info = infoOf(data);
  const parts = [];
  const status = statusOf(data);
  if (status) parts.push(status);
  const outline = outlineOf(view);
  if (outline) parts.push(outline);
  let local = null;
  if (data.kind === "page") {
    local = el("section", "ctx-block ctx-local");
    local.setAttribute("aria-label", "Local graph");
    parts.push(local);
  }
  parts.push(info);
  if (data.kind === "page" && data.id) {
    const block = mentionsBlock();
    parts.push(block.section);
    mentionsTimer = setTimeout(() => loadMentions(data, block, info), 450);
  }
  if (data.kind !== "page" && !info.childElementCount && !outline) {
    parts.push(el("p", "none ctx-empty", "Open a page to see its links and properties."));
  }
  body.replaceChildren(...parts);
  if (spy?.view) {
    spy.view.addEventListener("scroll", scrollHandler, { passive: true });
    onScroll();
  }
  emit("ws:context", { data, pane, local });
}

export function initContext() {
  const aside = document.querySelector(".side-right");
  body = el("div", "ctx");
  // the page the server rendered brings its own info: keep it for its tab
  const server = el("div", "ctx-info");
  for (const child of [...aside.childNodes]) {
    if (!(child instanceof Element && child.classList.contains("drawer-close"))) server.append(child);
  }
  aside.append(body);
  return (data) => {
    infos.set(data, server);
    if (data.kind !== "page") return;
    // what a fragment would say about the page, read from the server-rendered info
    data.id = decodeURIComponent(data.url.replace(/^\/wiki\/page\//, ""));
    data.raw = data.id.startsWith("raw/");
    const tags = [...server.querySelectorAll("dt")].find((dt) => dt.textContent.trim() === "tags");
    const list = tags?.nextElementSibling;
    data.frontmatter = { tags: list ? [...list.querySelectorAll("li")].map((li) => li.textContent.trim()) : [] };
    data.outlinks = [...server.querySelectorAll('section[aria-labelledby="outlinks-h"] a[href]')].map((a) => ({
      id: decodeURIComponent(new URL(a.href, window.location.origin).pathname.replace(/^\/wiki\/page\//, "")),
    }));
    data.backlinks = [...server.querySelectorAll('section[aria-labelledby="backlinks-h"] a[data-type]')].map((a) => ({
      id: decodeURIComponent(new URL(a.href, window.location.origin).pathname.replace(/^\/wiki\/page\//, "")),
      type: a.dataset.type,
    }));
  };
}

on("ws:active", show);
on("ws:wiki-changed", () => mentions.clear());
