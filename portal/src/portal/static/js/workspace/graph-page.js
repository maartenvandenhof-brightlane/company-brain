// The graph view (/wiki/graph, in a tab): the server renders a list of pages and their links;
// this draws the graph over it, with a panel of filters and forces in the spirit of
// Obsidian's graph settings. "View as list" switches back to the list at any time.
//
// Filters: page types (the legend: click to hide a type, hover to pick it out), tag, folder,
// a search that highlights matches; toggles for raw sources (imported chats, grouped as
// graph-data.js explains), links of skipped chats, missing pages, orphans, drafts only and
// proposals. Forces: link distance, repel strength, centre force, node size.
import { on } from "./bus.js";
import { el, icon } from "./icons.js";
import { DEFAULT_OPTIONS, buildModel, loadGraph } from "./graph-data.js";
import { DEFAULT_SETTINGS, TYPE_LABELS, createGraph } from "./graph-view.js";
import { save } from "./store.js";
import { open, state } from "./workspace.js";

const number = new Intl.NumberFormat();
const pageHref = (id) => `/wiki/page/${id.split("/").map(encodeURIComponent).join("/")}`;
const mounted = new WeakMap(); // page element -> controller

function prefs() {
  state.graph ||= {};
  state.graph.options = { ...DEFAULT_OPTIONS, ...(state.graph.options || {}) };
  state.graph.settings = { ...DEFAULT_SETTINGS, ...(state.graph.settings || {}) };
  return state.graph;
}

function whereOf(event) {
  if (event.ctrlKey || event.metaKey || event.button === 1) return "new";
  return event.shiftKey ? "other" : "current";
}

function describe(model) {
  const s = model.stats;
  const parts = [`Graph of ${number.format(s.pages)} pages and ${number.format(s.links)} links`];
  if (s.chats) parts.push(`${number.format(s.chats)} imported chats${s.clusters ? `, ${s.clusters} groups of them` : ""}`);
  if (s.ghosts) parts.push(`${number.format(s.ghosts)} missing pages`);
  return `${parts.join(", ")}. Use “View as list” for the pages and their links as a list.`;
}

function toggle(label, checked, onChange, hint = "") {
  const row = el("label", "check graph-check");
  const box = el("input");
  box.type = "checkbox";
  box.checked = checked;
  box.addEventListener("change", () => onChange(box.checked));
  row.append(box, el("span", "", label));
  if (hint) row.title = hint;
  return { row, box };
}

function slider(label, key, min, max, step, settings, onInput) {
  const id = `graph-${key}-${Math.random().toString(36).slice(2, 7)}`;
  const row = el("div", "graph-slider");
  const name = el("label", "", label);
  name.htmlFor = id;
  const out = el("output", "", String(settings[key]));
  out.htmlFor = id;
  const input = el("input");
  input.type = "range";
  input.id = id;
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = String(settings[key]);
  input.addEventListener("input", () => {
    out.textContent = input.value;
    onInput(key, Number(input.value));
  });
  row.append(name, out, input);
  return { row, input, out };
}

async function mount(page) {
  if (mounted.has(page)) return;
  const host = page.querySelector("[data-graph-mount]");
  const list = page.querySelector("#graph-list");
  if (!host || !list) return;
  mounted.set(page, true);
  page.classList.add("is-graph");
  host.hidden = false;
  const p = prefs();
  const expanded = new Set();
  let data = null;
  let model = null;
  let query = "";

  // ---- the stage, its tools and panel
  const stage = el("div", "graph-stage");
  const canvasHost = el("div", "graph-canvas-host");
  const tools = el("div", "graph-tools");
  const find = el("input", "graph-find");
  find.type = "search";
  find.placeholder = "Find in graph";
  find.setAttribute("aria-label", "Find pages in the graph");
  const fitButton = el("button", "ws-tool");
  fitButton.type = "button";
  fitButton.setAttribute("aria-label", "Fit the graph to the view (0)");
  fitButton.title = "Fit to view (0)";
  fitButton.append(icon("fit"));
  const listButton = el("button", "button small graph-to-list");
  listButton.type = "button";
  listButton.append(icon("list"), document.createTextNode("View as list"));
  tools.append(find, fitButton, listButton);

  const panel = el("details", "graph-panel");
  panel.open = false;
  const summary = el("summary");
  summary.append(icon("settings"), el("span", "", "Filters and forces"));
  const panelBody = el("div", "graph-panel-body");
  panel.append(summary, panelBody);

  const status = el("p", "graph-status");
  status.setAttribute("role", "status");
  const legend = el("div", "graph-marks");
  legend.setAttribute("aria-label", "What the marks mean");
  for (const [cls, text] of [
    ["mk-ring", "draft to review"],
    ["mk-hollow", "chat not processed yet"],
    ["mk-dash", "missing page"],
    ["mk-group", "group of chats (click)"],
  ]) {
    const item = el("span", "graph-mark");
    item.append(el("span", `mk ${cls}`), document.createTextNode(text));
    legend.append(item);
  }
  stage.append(canvasHost, tools, panel, status, legend);
  host.replaceChildren(stage);

  // "View as list" and back
  const back = el("button", "button small graph-to-graph");
  back.type = "button";
  back.append(icon("graph"), document.createTextNode("View as graph"));
  list.querySelector("h2").after(back);
  const showList = (asList) => {
    host.hidden = asList;
    back.hidden = !asList;
    page.classList.toggle("is-graph", !asList);
    if (asList) list.querySelector("h2").focus?.();
  };
  list.querySelector("h2").tabIndex = -1;
  listButton.addEventListener("click", () => showList(true));
  back.addEventListener("click", () => {
    showList(false);
    graph.canvas.focus();
  });
  showList(false);

  const graph = await createGraph(canvasHost, {
    describe,
    onPick(n, event) {
      if (n.cluster) {
        expanded.add(n.id);
        rebuild();
        status.textContent = `Showing the ${n.count} chats of ${n.title}.`;
        return;
      }
      if (n.ghost) {
        status.textContent = `${n.id} does not exist yet.`;
        return;
      }
      const id = n.files ? n.files[0] : n.id;
      open(pageHref(id), { where: whereOf(event) });
    },
  });
  mounted.set(page, graph);
  graph.setSettings(p.settings);

  // ---- panel contents
  const typeBox = el("div", "graph-types");
  typeBox.setAttribute("role", "group");
  typeBox.setAttribute("aria-label", "Page types (click to hide or show)");
  const tagSelect = el("select");
  tagSelect.setAttribute("aria-label", "Only pages with this tag");
  const folderSelect = el("select");
  folderSelect.setAttribute("aria-label", "Only pages in this folder");
  const set = (key, value) => {
    p.options[key] = value;
    save(state);
  };
  const toggles = el("div", "graph-toggles");
  const rawToggle = toggle("Raw sources (imported chats)", p.options.raw, async (v) => {
    set("raw", v);
    groupToggle.row.hidden = hubToggle.row.hidden = !v;
    await reload();
  });
  const groupToggle = toggle("Group unlinked chats by month", p.options.groupChats, (v) => {
    set("groupChats", v);
    expanded.clear();
    rebuild();
  }, "Chats no page cites, or only the skipped-conversations page, as one node per month");
  const hubToggle = toggle("Links of skipped chats", p.options.hubLinks, (v) => {
    set("hubLinks", v);
    rebuild();
  }, "The skipped-conversations page links to every chat with nothing to file");
  groupToggle.row.hidden = hubToggle.row.hidden = !p.options.raw;
  const more = [
    toggle("Missing pages", p.options.ghosts, (v) => { set("ghosts", v); rebuild(); }, "Links to pages that do not exist yet"),
    toggle("Orphans", p.options.orphans, (v) => { set("orphans", v); rebuild(); }, "Pages without links"),
    toggle("Drafts to review only", p.options.draftsOnly, (v) => { set("draftsOnly", v); rebuild(); }),
    toggle("Proposals", p.options.proposals, async (v) => { set("proposals", v); await reload(); }),
  ];
  toggles.append(rawToggle.row, groupToggle.row, hubToggle.row, ...more.map((t) => t.row));
  tagSelect.addEventListener("change", () => { set("tag", tagSelect.value); rebuild(); });
  folderSelect.addEventListener("change", () => { set("folder", folderSelect.value); rebuild(); });

  const forces = el("div", "graph-forces");
  const onForce = (key, value) => {
    p.settings[key] = value;
    save(state);
    graph.setSettings({ [key]: value });
  };
  const sliders = [
    slider("Link distance", "linkDistance", 10, 300, 5, p.settings, onForce),
    slider("Repel strength", "repel", 0, 800, 10, p.settings, onForce),
    slider("Centre force", "center", 0, 0.5, 0.01, p.settings, onForce),
    slider("Node size", "nodeSize", 0.4, 3, 0.1, p.settings, onForce),
  ];
  forces.append(...sliders.map((s) => s.row));
  const reset = el("button", "button small quiet");
  reset.type = "button";
  reset.textContent = "Reset forces";
  reset.addEventListener("click", () => {
    Object.assign(p.settings, DEFAULT_SETTINGS);
    save(state);
    for (const s of sliders) {
      const key = s.input.id.split("-")[1];
      s.input.value = String(p.settings[key]);
      s.out.textContent = s.input.value;
    }
    graph.setSettings(p.settings);
  });
  const heading = (text) => el("h2", "graph-panel-heading", text);
  const labelled = (text, control) => {
    const row = el("label", "graph-field");
    row.append(el("span", "", text), control);
    return row;
  };
  panelBody.append(
    heading("Types"), typeBox,
    heading("Filters"), labelled("Tag", tagSelect), labelled("Folder", folderSelect), toggles,
    heading("Forces"), forces, reset,
  );

  // ---- data -> model -> graph
  function fillSelect(select, values, current, all) {
    select.replaceChildren(el("option", "", all));
    select.firstChild.value = "";
    for (const [value, count] of values) {
      const option = el("option", "", `${value} (${number.format(count)})`);
      option.value = value;
      option.selected = value === current;
      select.append(option);
    }
  }

  function panelFromData() {
    const tags = new Map();
    const folders = new Map();
    const types = new Map();
    for (const n of data.nodes) {
      if (n.ghost) continue;
      types.set(n.type || "unknown", (types.get(n.type || "unknown") || 0) + 1);
      if (n.folder) folders.set(n.folder, (folders.get(n.folder) || 0) + 1);
      if (!n.id.startsWith("raw/")) for (const t of n.tags || []) tags.set(t, (tags.get(t) || 0) + 1);
    }
    fillSelect(tagSelect, [...tags].sort((a, b) => b[1] - a[1]).slice(0, 80), p.options.tag, "Any tag");
    fillSelect(folderSelect, [...folders].sort((a, b) => a[0].localeCompare(b[0])), p.options.folder, "Any folder");
    const hidden = new Set(p.options.hiddenTypes);
    typeBox.replaceChildren(
      ...[...types].sort((a, b) => b[1] - a[1]).map(([type, count]) => {
        const b = el("button", "graph-type");
        b.type = "button";
        b.dataset.type = type;
        b.setAttribute("aria-pressed", String(!hidden.has(type)));
        const sw = el("span", "swatch");
        sw.setAttribute("aria-hidden", "true");
        b.append(sw, el("span", "", TYPE_LABELS[type] || type), el("span", "count", number.format(count)));
        b.title = `${hidden.has(type) ? "Show" : "Hide"} ${TYPE_LABELS[type] || type}; hover to pick them out`;
        b.addEventListener("click", () => {
          const now = new Set(p.options.hiddenTypes);
          if (now.has(type)) now.delete(type);
          else now.add(type);
          set("hiddenTypes", [...now]);
          b.setAttribute("aria-pressed", String(!now.has(type)));
          rebuild();
        });
        b.addEventListener("pointerenter", () => graph.highlightType(type));
        b.addEventListener("focus", () => graph.highlightType(type));
        const off = () => applySearch();
        b.addEventListener("pointerleave", off);
        b.addEventListener("blur", off);
        return b;
      }),
    );
  }

  function applySearch() {
    if (!query) {
      graph.setMatches(null);
      showStats();
      return;
    }
    const q = query.toLowerCase();
    const found = new Set(model.nodes.filter((n) => `${n.title}\n${n.id}${n.search || ""}`.toLowerCase().includes(q)).map((n) => n.id));
    graph.setMatches(found.size ? found : new Set(["\u0000"]));
    status.textContent = found.size ? `${number.format(found.size)} match${found.size === 1 ? "" : "es"} for “${query}”. Enter centres the first.` : `Nothing matches “${query}”.`;
  }

  function showStats() {
    const s = model.stats;
    const bits = [`${number.format(s.pages)} pages`, `${number.format(s.links)} links`];
    if (s.chats) bits.push(`${number.format(s.chats)} chats${s.clusters ? ` (${s.clusters} groups)` : ""}`);
    if (s.hubHidden) bits.push(`${number.format(s.hubHidden)} links of skipped chats hidden`);
    status.textContent = bits.join(" · ");
  }

  function rebuild() {
    model = buildModel(data, p.options, expanded);
    graph.setData(model);
    showStats();
    if (query) applySearch();
  }

  async function reload({ background = false } = {}) {
    status.textContent = "Loading the graph…";
    try {
      data = await loadGraph({ raw: p.options.raw, proposals: p.options.proposals }, { background });
    } catch (error) {
      status.textContent = error.message;
      return;
    }
    panelFromData();
    rebuild();
  }


  let debounce = 0;
  find.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      query = find.value.trim();
      applySearch();
    }, 120);
  });
  find.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !query) return;
    const q = query.toLowerCase();
    const first = model.nodes.find((n) => `${n.title}\n${n.id}${n.search || ""}`.toLowerCase().includes(q));
    if (first) graph.centerOn(first.id);
  });
  fitButton.addEventListener("click", () => graph.fit());
  host.graph = graph; // for measuring (see docs/portal.md)
  await reload();
  on("ws:wiki-changed", () => {
    if (page.isConnected) reload({ background: true });
  });
}

on("ws:view", ({ el: view }) => {
  const page = view.querySelector?.("[data-graph-page]");
  if (page) mount(page);
});
on("ws:dispose", ({ el: view }) => {
  const page = view.querySelector?.("[data-graph-page]");
  const graph = page && mounted.get(page);
  if (graph && graph.destroy) graph.destroy();
  if (page) mounted.delete(page);
});
