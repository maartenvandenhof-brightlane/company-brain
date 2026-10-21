// The local graph in the context pane: the focused page and the pages 1 to 3 links away (in
// either direction), drawn by the same graph-view.js as the graph view, small. On an imported
// chat it shows the pages that cite it, or says that none does yet.
import { on } from "./bus.js";
import { el, icon } from "./icons.js";
import { loadGraph, neighbourhood } from "./graph-data.js";
import { createGraph } from "./graph-view.js";
import { save } from "./store.js";
import { open, state } from "./workspace.js";

const pageHref = (id) => `/wiki/page/${id.split("/").map(encodeURIComponent).join("/")}`;
let graph = null;
let timer = 0;
let turn = 0;

function whereOf(event) {
  if (event.ctrlKey || event.metaKey || event.button === 1) return "new";
  return event.shiftKey ? "other" : "current";
}

async function draw(slot, data) {
  const mine = ++turn;
  graph?.destroy();
  graph = null;
  const id = data.id;
  const raw = id.startsWith("raw/");
  const details = el("details", "ctx-block");
  details.open = true;
  const summary = el("summary", "nav-heading");
  summary.append(el("span", "", "Local graph"));
  const head = el("div", "ctx-local-head");
  const depth = el("select");
  depth.setAttribute("aria-label", "How many links away");
  for (const d of [1, 2, 3]) {
    const option = el("option", "", d === 1 ? "1 link away" : `${d} links away`);
    option.value = String(d);
    option.selected = d === state.local.depth;
    depth.append(option);
  }
  const whole = el("a", "ctx-graph-link");
  whole.href = "/wiki/graph";
  whole.append(icon("graph"), document.createTextNode("Open graph"));
  head.append(depth, whole);
  const stage = el("div", "graph-stage");
  const note = el("p", "graph-note");
  details.append(summary, head, stage, note);
  slot.replaceChildren(details);

  let full;
  try {
    full = await loadGraph({ raw });
  } catch (error) {
    note.textContent = error.message;
    return;
  }
  if (mine !== turn || !slot.isConnected) return;
  const view = await createGraph(stage, {
    compact: true,
    describe: (model) => `The pages up to ${state.local.depth} link${state.local.depth === 1 ? "" : "s"} away from ${data.title}: ${model.nodes.length - 1} pages. The lists below name them.`,
    onPick(n, event) {
      if (n.ghost) {
        note.textContent = `${n.id} does not exist yet.`;
        return;
      }
      if (n.id !== id) open(pageHref(n.id), { where: whereOf(event) });
    },
  });
  if (mine !== turn || !slot.isConnected) {
    view.destroy();
    return;
  }
  graph = view;
  const render = () => {
    const model = neighbourhood(full, id, state.local.depth);
    if (!model.nodes.some((n) => n.id === id)) model.nodes.push({ id, title: data.title, type: data.type, backlink_count: 0 });
    graph.setData(model, { focusId: id });
    const others = model.nodes.length - 1;
    if (raw) {
      const citers = full.edges.filter((e) => e.target === id).map((e) => e.source);
      const citing = citers.length;
      if (citing && citers.every((c) => c === full.skipped_page)) note.textContent = "Processed: nothing worth filing.";
      else note.textContent = citing ? `${citing} page${citing === 1 ? "" : "s"} cite${citing === 1 ? "s" : ""} this chat.` : "Not processed yet: no wiki page cites this chat.";
    } else if (others) note.textContent = "";
    else {
      const chats = (data.outlinks || []).filter((o) => o.id.startsWith("raw/")).length;
      note.textContent = chats
        ? `It links only to imported chats (${chats}): the graph view shows them with “Raw sources” on.`
        : "No links to or from this page yet.";
    }
    note.hidden = !note.textContent;
  };
  depth.addEventListener("change", () => {
    state.local.depth = Number(depth.value);
    save(state);
    render();
  });
  render();
}

on("ws:context", ({ data, local }) => {
  clearTimeout(timer);
  if (!local || !data.id) {
    turn += 1;
    graph?.destroy();
    graph = null;
    return;
  }
  // wait a moment: flicking through tabs should not start a layout for each
  timer = setTimeout(() => draw(local, data), 120);
});
