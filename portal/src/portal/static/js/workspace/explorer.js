// The file explorer (left column): the wiki's folders as an ARIA tree, loaded a level at a
// time from /wiki/api/tree. Big folders (raw/chats/claude with thousands of chats) come a page
// of 200 at a time, newest first, with the next page loaded as it scrolls into view; a
// chat's parts and continuations are grouped under it. Drafts to review carry a ring, chats
// no page cites yet a hollow swatch. A name filter and "Drafts to review" / "Unprocessed
// chats" switch the tree to a flat list across all folders.
//
// Keyboard (the ARIA tree pattern): Up/Down move, Right opens a folder or goes into it, Left
// closes it or goes to its parent, Home/End, Enter opens the page (Ctrl/Cmd+Enter in a new
// tab, Shift+Enter in the other pane), / focuses the filter.
import { getJSON } from "./api.js";
import { on } from "./bus.js";
import { el, icon } from "./icons.js";
import { knownVersion } from "./live.js";
import { save } from "./store.js";
import { open, state } from "./workspace.js";

const PAGE = 200;
const LABELS = { wiki: "Wiki pages", raw: "Raw sources", proposals: "Proposals" };
const number = new Intl.NumberFormat();
let tree;
let filterInput;
let status;
let chips;
let processLink;
let only = "";
let query = "";
let currentId = "";
let seq = 0;
const observer = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) if (entry.isIntersecting) entry.target.click();
  },
  { rootMargin: "200px" },
);

const pageHref = (id) => `/wiki/page/${id.split("/").map(encodeURIComponent).join("/")}`;
const openFolders = () => new Set(state.explorer.open);

function setOpen(path, isOpen) {
  const set = openFolders();
  if (isOpen) set.add(path);
  else set.delete(path);
  state.explorer.open = [...set];
  save(state);
}

function marks(row, item, own = false) {
  if (item.unprocessed) {
    row.classList.add("is-unprocessed");
    row.append(el("span", "vh", ", not processed yet"));
  }
  if (item.skipped) row.append(el("span", "vh", ", skipped: nothing to file"));
  if (item.draft) {
    const ring = el("span", "draft-mark");
    ring.title = "Draft to review";
    row.append(ring, el("span", "vh", ", draft to review"));
  }
  if (own && item.unprocessed) row.querySelector(".swatch")?.classList.add("own");
}

function treeitem(level) {
  const li = el("li", "tree-item");
  li.setAttribute("role", "treeitem");
  li.setAttribute("aria-level", String(level));
  li.tabIndex = -1;
  return li;
}

function fileItem(item, level) {
  const li = treeitem(level);
  li.dataset.id = item.id;
  if (item.type) li.dataset.type = item.type;
  const row = el("a", "tree-row");
  row.href = pageHref(item.id);
  row.tabIndex = -1;
  const swatch = el("span", "swatch");
  swatch.setAttribute("aria-hidden", "true");
  row.append(swatch, el("span", "tree-name", item.label || item.title || item.id));
  if (item.date && item.id.startsWith("raw/")) row.append(el("span", "tree-date", item.date.slice(0, 10)));
  marks(row, item);
  row.title = item.id;
  li.append(row);
  if (item.id === currentId) mark(li);
  return li;
}

function conversationItem(item, level) {
  const li = treeitem(level);
  li.dataset.id = item.parts[0].id;
  li.dataset.type = item.type || "source";
  li.setAttribute("aria-expanded", "false");
  const row = el("a", "tree-row");
  row.href = pageHref(item.parts[0].id);
  row.tabIndex = -1;
  const twisty = el("span", "twisty");
  twisty.append(icon("chevron"));
  twisty.setAttribute("aria-hidden", "true");
  const swatch = el("span", "swatch");
  swatch.setAttribute("aria-hidden", "true");
  row.append(twisty, swatch, el("span", "tree-name", item.title));
  row.append(el("span", "tree-count", `${item.parts.length}`));
  row.lastChild.title = `${item.parts.length} files: parts and continuations`;
  row.append(el("span", "vh", `, ${item.parts.length} files`));
  if (item.date) row.append(el("span", "tree-date", item.date.slice(0, 10)));
  marks(row, item);
  const group = el("ul", "tree-group");
  group.setAttribute("role", "group");
  group.hidden = true;
  group.append(...item.parts.map((part) => fileItem(part, level + 1)));
  li.append(row, group);
  twisty.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    toggle(li);
  });
  return li;
}

function folderItem(folder, level) {
  const li = treeitem(level);
  li.dataset.path = folder.path;
  li.setAttribute("aria-expanded", "false");
  const row = el("div", "tree-row tree-folder");
  const twisty = el("span", "twisty");
  twisty.append(icon("chevron"));
  twisty.setAttribute("aria-hidden", "true");
  const name = LABELS[folder.path] || folder.name;
  row.append(twisty, icon("folder"), el("span", "tree-name", name));
  if (folder.unprocessed) {
    const todo = el("span", "tree-todo", number.format(folder.unprocessed));
    todo.title = `${number.format(folder.unprocessed)} chats not processed yet`;
    row.append(todo, el("span", "vh", ` (${folder.unprocessed} not processed yet)`));
  }
  if (folder.drafts) {
    const ring = el("span", "tree-drafts");
    ring.append(el("span", "draft-mark"), document.createTextNode(number.format(folder.drafts)));
    ring.title = `${number.format(folder.drafts)} drafts to review`;
    row.append(ring, el("span", "vh", ` (${folder.drafts} drafts to review)`));
  }
  row.append(el("span", "tree-count", number.format(folder.count)));
  const group = el("ul", "tree-group");
  group.setAttribute("role", "group");
  group.hidden = true;
  li.append(row, group);
  row.addEventListener("click", () => toggle(li));
  if (openFolders().has(folder.path)) toggle(li, true);
  return li;
}

function moreItem(label, level, load) {
  const li = treeitem(level);
  li.classList.add("tree-more");
  const button = el("button", "tree-row tree-more-button", label);
  button.type = "button";
  button.tabIndex = -1;
  button.addEventListener("click", () => {
    observer.unobserve(button);
    button.disabled = true;
    button.textContent = "Loading…";
    load().finally(() => li.remove());
  });
  li.append(button);
  observer.observe(button);
  return li;
}

function entry(item, level) {
  if (item.kind === "folder") return folderItem(item, level);
  if (item.kind === "conversation") return conversationItem(item, level);
  return fileItem(item, level);
}

async function fill(group, params, level) {
  const url = `/wiki/api/tree?${new URLSearchParams({ limit: String(PAGE), ...params })}`;
  const mine = seq;
  const body = await getJSON(url);
  if (mine !== seq) return body; // the filter changed meanwhile
  knownVersion(body.version);
  const nodes = [...body.folders.map((f) => entry(f, level)), ...body.items.map((i) => entry(i, level))];
  group.append(...nodes);
  if (body.next_offset !== null) {
    const rest = body.total - body.next_offset;
    const label = `Show ${number.format(Math.min(PAGE, rest))} more of ${number.format(rest)}`;
    group.append(moreItem(label, level, () => fill(group, { ...params, offset: String(body.next_offset) }, level)));
  }
  return body;
}

async function toggle(li, force) {
  const expanded = li.getAttribute("aria-expanded") === "true";
  const want = force ?? !expanded;
  if (want === expanded) return;
  const group = li.querySelector(":scope > .tree-group");
  li.setAttribute("aria-expanded", String(want));
  group.hidden = !want;
  if (li.dataset.path !== undefined) {
    setOpen(li.dataset.path, want);
    if (want && !li.dataset.loaded) {
      li.dataset.loaded = "1";
      li.setAttribute("aria-busy", "true");
      try {
        await fill(group, { folder: li.dataset.path }, Number(li.getAttribute("aria-level")) + 1);
      } catch (error) {
        delete li.dataset.loaded;
        group.append(el("li", "tree-error", error.message));
      } finally {
        li.removeAttribute("aria-busy");
      }
    }
  }
}

function visibleItems() {
  return [...tree.querySelectorAll('[role="treeitem"]')].filter((li) => !li.closest(".tree-group[hidden]"));
}

function focusItem(li) {
  if (!li) return;
  for (const item of tree.querySelectorAll('[role="treeitem"][tabindex="0"]')) item.tabIndex = -1;
  li.tabIndex = 0;
  li.focus();
}

function onKey(event) {
  const li = event.target.closest('[role="treeitem"]');
  if (!li) return;
  const items = visibleItems();
  const at = items.indexOf(li);
  const expandable = li.hasAttribute("aria-expanded");
  const expanded = li.getAttribute("aria-expanded") === "true";
  switch (event.key) {
    case "ArrowDown":
      focusItem(items[at + 1]);
      break;
    case "ArrowUp":
      focusItem(items[at - 1]);
      break;
    case "Home":
      focusItem(items[0]);
      break;
    case "End":
      focusItem(items.at(-1));
      break;
    case "ArrowRight":
      if (expandable && !expanded) toggle(li, true);
      else if (expanded) focusItem(li.querySelector(':scope > .tree-group > [role="treeitem"]'));
      break;
    case "ArrowLeft":
      if (expanded) toggle(li, false);
      else focusItem(li.parentElement.closest('[role="treeitem"]'));
      break;
    case "Enter":
    case " ": {
      const link = li.querySelector(":scope > a.tree-row");
      const more = li.querySelector(":scope > .tree-more-button");
      if (more) more.click();
      else if (link) {
        const where = event.ctrlKey || event.metaKey ? "new" : event.shiftKey ? "other" : "current";
        open(new URL(link.href).pathname, { where });
      } else if (expandable) toggle(li);
      break;
    }
    default:
      return;
  }
  event.preventDefault();
}

function mark(li) {
  for (const old of tree.querySelectorAll(".is-current")) {
    old.classList.remove("is-current");
    old.querySelector(":scope > .tree-row")?.removeAttribute("aria-current");
  }
  if (!li) return;
  li.classList.add("is-current");
  li.querySelector(":scope > .tree-row")?.setAttribute("aria-current", "page");
}

function highlight({ data }) {
  currentId = data.kind === "page" ? data.id || decodeURIComponent((data.url || "").replace(/^\/wiki\/page\//, "")) : "";
  const li = currentId ? tree.querySelector(`[data-id="${CSS.escape(currentId)}"]`) : null;
  mark(li);
  if (li && !li.closest(".tree-group[hidden]")) li.scrollIntoView({ block: "nearest" });
}

async function load() {
  seq += 1;
  tree.replaceChildren();
  const flat = Boolean(query || only);
  tree.classList.toggle("is-flat", flat);
  status.textContent = "";
  try {
    const body = flat ? await fill(tree, { q: query, only }, 1) : await fill(tree, { folder: "" }, 1);
    if (flat) {
      if (only === "unprocessed" && body.total) {
        const n = number.format(body.transcripts);
        status.textContent = `${n} transcript${body.transcripts === 1 ? "" : "s"} not processed yet, in ${number.format(body.total)} conversations`;
      } else {
        const what = only === "drafts" ? "drafts to review" : only === "unprocessed" ? "chats to process" : "matches";
        status.textContent = body.total ? `${number.format(body.total)} ${what}` : `No ${what}${query ? ` for “${query}”` : ""}.`;
      }
    } else counts(body.folders);
    const first = tree.querySelector('[role="treeitem"]');
    if (first) first.tabIndex = 0;
  } catch (error) {
    status.textContent = error.message;
  }
}

function counts(folders) {
  const raw = folders.find((f) => f.path === "raw");
  const wiki = folders.find((f) => f.path === "wiki");
  chips.drafts.querySelector(".chip-count").textContent = wiki?.drafts ? number.format(wiki.drafts) : "";
  chips.unprocessed.querySelector(".chip-count").textContent = raw?.unprocessed ? number.format(raw.unprocessed) : "";
  processLink.querySelector(".chip-count").textContent = raw?.unprocessed ? number.format(raw.unprocessed) : "";
}

function chip(label, value) {
  const button = el("button", "chip");
  button.type = "button";
  button.setAttribute("aria-pressed", "false");
  button.append(el("span", "", label), el("span", "chip-count"));
  button.addEventListener("click", () => {
    only = only === value ? "" : value;
    for (const b of Object.values(chips)) b.setAttribute("aria-pressed", String(b === button && only === value));
    load();
  });
  return button;
}

function action(href, label, iconName) {
  const link = el("a", "explorer-action");
  link.href = href;
  link.dataset.full = "1"; // a full page load: importing and processing may ask for the password
  link.append(icon(iconName), el("span", "", label), el("span", "chip-count"));
  return link;
}

export function filterByName() {
  filterInput?.focus();
}

export function showDrafts() {
  if (only !== "drafts") chips.drafts.click();
  filterInput?.focus();
}

export function initExplorer() {
  const aside = document.querySelector(".side-left");
  if (!aside) return;
  aside.classList.add("has-explorer");
  const section = el("section", "explorer");
  section.setAttribute("aria-labelledby", "explorer-h");
  const head = el("div", "explorer-head");
  const heading = el("h2", "nav-heading", "Files");
  heading.id = "explorer-h";
  head.append(heading);
  const add = el("div", "explorer-add");
  add.setAttribute("aria-label", "Add to wiki");
  add.setAttribute("role", "group");
  processLink = action("/import#process-title", "Process chats", "process");
  processLink.title = "Process imported chats: turn them into wiki pages";
  add.append(action("/import", "Import chats", "plus"), processLink);
  filterInput = el("input", "explorer-filter");
  filterInput.type = "search";
  filterInput.placeholder = "Filter by name";
  filterInput.setAttribute("aria-label", "Filter files by name");
  filterInput.setAttribute("aria-controls", "explorer-tree");
  let debounce = 0;
  filterInput.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      query = filterInput.value.trim();
      load();
    }, 160);
  });
  filterInput.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusItem(visibleItems()[0]);
    }
  });
  chips = { drafts: chip("Drafts to review", "drafts"), unprocessed: chip("Unprocessed chats", "unprocessed") };
  const chipRow = el("div", "explorer-chips");
  chipRow.setAttribute("role", "group");
  chipRow.setAttribute("aria-label", "Show only");
  chipRow.append(chips.drafts, chips.unprocessed);
  tree = el("ul", "tree explorer-tree");
  tree.id = "explorer-tree";
  tree.setAttribute("role", "tree");
  tree.setAttribute("aria-labelledby", "explorer-h");
  tree.addEventListener("keydown", onKey);
  tree.addEventListener("click", (event) => {
    const li = event.target.closest('[role="treeitem"]');
    if (li) {
      for (const item of tree.querySelectorAll('[role="treeitem"][tabindex="0"]')) item.tabIndex = -1;
      li.tabIndex = 0;
    }
  });
  status = el("p", "explorer-status");
  status.setAttribute("role", "status");
  section.append(head, add, filterInput, chipRow, status, tree);
  aside.querySelector(".wiki-nav").after(section);
  on("ws:active", highlight);
  on("ws:wiki-changed", () => {
    const keep = document.activeElement?.closest?.("#explorer-tree") ? null : true;
    if (keep) load();
  });
  load();
}
