// The quick switcher and command palette (Ctrl/Cmd+K; Ctrl/Cmd+O for pages only): one input
// over a list, the ARIA combobox pattern. Pages match instantly and fuzzily on their title and
// id, from the graph's node list; imported chats come after wiki pages; the last entry searches
// the full text. Starting with ">" lists commands only.
//
// Enter opens (Ctrl/Cmd+Enter in a new tab, Shift+Enter in the other pane), arrows move,
// Escape closes and gives focus back.
import { announce, available, go } from "./commands.js";
import { loadGraph } from "./workspace/graph-data.js";
import { el } from "./workspace/icons.js";

const LIMIT = 40;
let dialog;
let input;
let list;
let hint;
let mode = "all"; // "all" | "pages"
let items = []; // what is listed: {kind, id, title, sub, type, run}
let active = 0;
let pages = null; // [{id, title, type, draft, unprocessed, raw}]
let loading = null;
let back = null;
let seq = 0;

const pageHref = (id) => `/wiki/page/${id.split("/").map(encodeURIComponent).join("/")}`;

async function loadPages() {
  if (pages) return pages;
  loading ||= (async () => {
    // wiki pages first (small), then everything with raw sources
    const wiki = await loadGraph({});
    pages = wiki.nodes.filter((n) => !n.ghost).map((n) => ({ ...n, raw: false }));
    render();
    try {
      const all = await loadGraph({ raw: true });
      const chats = [];
      for (const n of all.nodes) {
        // one entry per conversation and per continuation: their first part
        if (!n.id.startsWith("raw/") || n.ghost || n.part) continue;
        chats.push({ ...n, raw: true, title: String(n.title || "").replace(/ \(part \d+ of \d+\)/, "") });
      }
      pages = [...pages, ...chats];
    } catch {
      // wiki pages alone still work
    }
    return pages;
  })();
  return loading;
}

const BOUNDARY = " /-_·.(";

// Score how well `query` matches `text`: the query in one piece ranks first (at the start of a
// word higher, at the start of the text highest), then its characters in order, rewarding
// runs and word starts. -1: no match.
export function score(query, text) {
  const t = text.toLowerCase();
  let best = -1;
  for (let i = t.indexOf(query); i >= 0; i = t.indexOf(query, i + 1)) {
    const s = 100 + (i === 0 ? 30 : BOUNDARY.includes(t[i - 1]) ? 20 : 0) - i * 0.2;
    best = Math.max(best, s);
  }
  if (best >= 0) return best - t.length * 0.02;
  let at = 0;
  let prev = -2;
  let total = 0;
  for (const c of query) {
    const i = t.indexOf(c, at);
    if (i < 0) return -1;
    total += i === prev + 1 ? 6 : 1;
    if (i === 0 || BOUNDARY.includes(t[i - 1])) total += 4;
    prev = i;
    at = i + 1;
  }
  return Math.min(total, 90) - t.length * 0.02;
}

function matchPages(query) {
  if (!pages) return [];
  if (!query) return pages.filter((p) => !p.raw).slice(0, 12);
  const scored = [];
  for (const p of pages) {
    const s = Math.max(score(query, p.title || ""), score(query, p.id) - 3);
    if (s >= 0) scored.push([s - (p.raw ? 25 : 0), p]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  return scored.slice(0, LIMIT).map(([, p]) => p);
}

function matchCommands(query) {
  const list = available();
  if (!query) return list;
  return list
    .map((c) => [score(query, c.title), c])
    .filter(([s]) => s >= 0)
    .sort((a, b) => b[0] - a[0])
    .map(([, c]) => c);
}

function option(item, index) {
  const li = el("li", "palette-option");
  li.id = `palette-opt-${index}`;
  li.setAttribute("role", "option");
  li.setAttribute("aria-selected", String(index === active));
  li.dataset.index = String(index);
  if (item.kind === "page") {
    li.dataset.type = item.page.type || "";
    const sw = el("span", `swatch${item.page.unprocessed ? " hollow" : ""}`);
    sw.setAttribute("aria-hidden", "true");
    li.append(sw);
  }
  const text = el("span", "palette-text");
  text.append(el("span", "palette-title", item.title));
  if (item.sub) text.append(el("small", "", item.sub));
  li.append(text);
  if (item.kind === "page" && item.page.draft) li.append(el("span", "draft-mark"), el("span", "vh", ", draft to review"));
  if (item.kind === "page" && item.page.unprocessed) li.append(el("span", "vh", ", not processed yet"));
  if (item.keys) li.append(el("kbd", "", item.keys));
  return li;
}

function group(label, entries, offset) {
  const g = el("li", "palette-group");
  g.setAttribute("role", "presentation");
  const heading = el("div", "palette-heading", label);
  heading.id = `palette-g-${offset}`;
  heading.setAttribute("aria-hidden", "true");
  const ul = el("ul");
  ul.setAttribute("role", "group");
  ul.setAttribute("aria-labelledby", heading.id);
  ul.append(...entries.map((item, i) => option(item, offset + i)));
  g.append(heading, ul);
  return g;
}

function render() {
  if (!dialog?.open) return;
  const raw = input.value.trim();
  const commandsOnly = raw.startsWith(">");
  const query = (commandsOnly ? raw.slice(1) : raw).trim().toLowerCase();
  const sections = [];
  const commands = mode === "all" ? matchCommands(query).map((c) => ({ kind: "command", title: c.title, keys: c.keys, run: c.run })) : [];
  if (!commandsOnly) {
    const found = matchPages(query);
    const wiki = found.filter((p) => !p.raw);
    const chats = found.filter((p) => p.raw);
    const asItem = (p) => ({
      kind: "page",
      page: p,
      title: p.title || p.id,
      sub: p.id + (p.unprocessed ? " · not processed yet" : ""),
    });
    if (wiki.length) sections.push(["Pages", wiki.map(asItem)]);
    if (commands.length && query) sections.push(["Commands", commands.slice(0, 8)]);
    if (chats.length) sections.push(["Imported chats and sources", chats.slice(0, 12).map(asItem)]);
    if (!query && commands.length) sections.push(["Commands", commands]);
    if (query) sections.push(["Full text", [{ kind: "search", title: `Search the full text for “${raw}”`, query: raw }]]);
  } else sections.push(["Commands", commands]);
  items = sections.flatMap(([, entries]) => entries);
  active = Math.min(active, Math.max(0, items.length - 1));
  let offset = 0;
  list.replaceChildren(
    ...sections.map(([label, entries]) => {
      const node = group(label, entries, offset);
      offset += entries.length;
      return node;
    }),
  );
  if (!items.length) list.append(el("li", "palette-empty", pages ? "Nothing matches." : "Loading pages…"));
  input.setAttribute("aria-activedescendant", items.length ? `palette-opt-${active}` : "");
  list.querySelector(`#palette-opt-${active}`)?.scrollIntoView({ block: "nearest" });
  hint.textContent = pages ? "" : "Loading pages…";
}

function move(step) {
  if (!items.length) return;
  active = (active + step + items.length) % items.length;
  for (const li of list.querySelectorAll('[role="option"]')) li.setAttribute("aria-selected", String(Number(li.dataset.index) === active));
  input.setAttribute("aria-activedescendant", `palette-opt-${active}`);
  list.querySelector(`#palette-opt-${active}`)?.scrollIntoView({ block: "nearest" });
}

function choose(index, event) {
  const item = items[index];
  if (!item) return;
  const where = event && (event.ctrlKey || event.metaKey) ? "new" : event?.shiftKey ? "other" : "current";
  back = null; // focus goes where the choice leads
  dialog.close();
  if (item.kind === "page") go(pageHref(item.page.id), where);
  else if (item.kind === "search") go(`/wiki/search?q=${encodeURIComponent(item.query)}`, where);
  else item.run();
}

function build() {
  dialog = el("dialog", "palette");
  dialog.setAttribute("aria-label", "Go to a page or run a command");
  const box = el("div", "palette-box");
  input = el("input", "palette-input");
  input.type = "text";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-controls", "palette-list");
  input.setAttribute("aria-autocomplete", "list");
  input.autocomplete = "off";
  input.spellcheck = false;
  list = el("ul", "palette-list");
  list.id = "palette-list";
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Pages and commands");
  hint = el("p", "palette-hint");
  hint.setAttribute("role", "status");
  const foot = el("p", "palette-foot");
  for (const [k, t] of [["↑↓", "move"], ["Enter", "open"], ["Ctrl+Enter", "new tab"], ["Shift+Enter", "other pane"], [">", "commands"]]) {
    foot.append(el("kbd", "", k), document.createTextNode(` ${t} `));
  }
  box.append(input, hint, list, foot);
  dialog.append(box);
  document.body.append(dialog);
  input.addEventListener("input", () => {
    active = 0;
    render();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") move(1);
    else if (event.key === "ArrowUp") move(-1);
    else if (event.key === "Home" && event.ctrlKey) move(-active);
    else if (event.key === "Enter") choose(active, event);
    else return;
    event.preventDefault();
  });
  list.addEventListener("click", (event) => {
    const li = event.target.closest('[role="option"]');
    if (li) choose(Number(li.dataset.index), event);
  });
  list.addEventListener("pointermove", (event) => {
    const li = event.target.closest('[role="option"]');
    if (li && Number(li.dataset.index) !== active) move(Number(li.dataset.index) - active);
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close(); // a click on the backdrop
  });
  dialog.addEventListener("close", () => {
    seq += 1;
    back?.focus?.();
  });
}

export function openPalette(which = "all") {
  if (!dialog) build();
  mode = which;
  back = document.activeElement;
  input.value = "";
  input.placeholder = which === "pages" ? "Go to a page…" : "Go to a page, or type > for commands…";
  active = 0;
  if (!dialog.open) dialog.showModal();
  input.focus();
  render();
  const mine = ++seq;
  loadPages().then(() => {
    if (mine === seq) render();
  }, (error) => announce(error.message));
}
