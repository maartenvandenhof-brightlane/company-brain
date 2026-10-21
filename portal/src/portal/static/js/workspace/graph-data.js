// The graph's data: loading /wiki/api/graph (per variant, revalidated by version) and turning
// it into what is drawn.
//
// Imported chats come in thousands and most link to nothing, so with raw sources shown:
//   - the -part-N files and -after-N continuations of one conversation are one node;
//   - conversations nothing links to (not processed yet) and those only the
//     skipped-conversations page cites are grouped per source, state and month into a
//     cluster node; clicking a cluster expands it;
//   - the skipped-conversations page's links to them are hidden and shown as a count on
//     it, unless "Links of skipped chats" is on (then one link per cluster).
import { getJSON } from "./api.js";
import { on } from "./bus.js";

const cache = new Map(); // "raw,proposals" -> response

export async function loadGraph({ raw = false, proposals = false } = {}, { background = false } = {}) {
  const key = `${raw ? 1 : 0}${proposals ? 1 : 0}`;
  const known = cache.get(key);
  const params = new URLSearchParams();
  if (raw) params.set("raw", "1");
  if (proposals) params.set("proposals", "1");
  if (known) params.set("v", known.version);
  const body = await getJSON(`/wiki/api/graph?${params}`, { background });
  if (body.unchanged && known) return known;
  cache.set(key, body);
  return body;
}

on("ws:wiki-changed", () => {
  // keep the data: the next load revalidates it by version
});

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const PART_TITLE = / \(continued\)| \(part \d+ of \d+\)/g;

function monthOf(date) {
  const m = /^(\d{4})-(\d{2})/.exec(date || "");
  return m ? { key: `${m[1]}-${m[2]}`, label: `${MONTHS[Number(m[2]) - 1]} ${m[1]}` } : { key: "undated", label: "undated" };
}

export const DEFAULT_OPTIONS = {
  raw: false,
  proposals: false,
  groupChats: true,
  hubLinks: false,
  ghosts: false,
  orphans: true,
  draftsOnly: false,
  hiddenTypes: [],
  tag: "",
  folder: "",
};

// Build the drawn graph: {nodes, links, hub, stats}. `expanded` holds cluster ids to show
// as their conversations. Node objects are new each time; the view keeps positions by id.
export function buildModel(data, options, expanded = new Set()) {
  const o = { ...DEFAULT_OPTIONS, ...options };
  const hubId = data.skipped_page || "";
  const byId = new Map();
  const alias = new Map(); // file id -> drawn id (conversations fold their files)

  for (const n of data.nodes) {
    if (n.conversation) {
      const key = n.conversation;
      alias.set(n.id, key);
      let c = byId.get(key);
      if (!c) {
        c = { ...n, id: key, files: [], backlink_count: 0, parts: 0, skipped: true, unprocessed: false, date: "" };
        c.title = String(n.title || "").replace(PART_TITLE, "");
        byId.set(key, c);
      }
      c.files.push(n.id);
      c.parts += 1;
      c.backlink_count += n.backlink_count || 0;
      c.unprocessed ||= Boolean(n.unprocessed);
      c.skipped &&= Boolean(n.skipped);
      if ((n.date || "") > c.date) c.date = n.date || "";
      if (!n.part && !n.after) c.title = String(n.title || c.title).replace(PART_TITLE, "");
    } else {
      byId.set(n.id, { ...n });
      alias.set(n.id, n.id);
    }
  }

  // links between drawn nodes, once each
  const seen = new Set();
  let links = [];
  for (const e of data.edges) {
    const s = alias.get(e.source) ?? e.source;
    const t = alias.get(e.target) ?? e.target;
    if (s === t || !byId.has(s) || !byId.has(t)) continue;
    const key = `${s}\u0000${t}`;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({ source: s, target: t, broken: Boolean(e.broken), hub: s === hubId && t.startsWith("raw/") });
  }

  // clusters of chats nothing but the skipped page links to
  const degree = new Map();
  for (const l of links) {
    if (l.hub) continue;
    degree.set(l.source, (degree.get(l.source) || 0) + 1);
    degree.set(l.target, (degree.get(l.target) || 0) + 1);
  }
  const clusterOf = new Map();
  if (o.raw && o.groupChats) {
    for (const n of byId.values()) {
      if (!n.conversation || degree.get(n.id)) continue;
      const source = n.folder?.split("/")[2] || "chats";
      const state = n.unprocessed ? "unprocessed" : n.skipped ? "skipped" : "other";
      const month = monthOf(n.date);
      const id = `cluster:${source}:${state}:${month.key}`;
      if (expanded.has(id)) continue;
      let c = byId.get(id);
      if (!c) {
        const who = source === "chatgpt" ? "ChatGPT" : source === "claude" ? "Claude" : source;
        const what = state === "unprocessed" ? "not processed" : state === "skipped" ? "skipped" : "chats";
        c = { id, cluster: true, type: "source", folder: n.folder, tags: [], title: `${who} · ${month.label} · ${what}`, count: 0, members: [], unprocessed: state === "unprocessed", skipped: state === "skipped", backlink_count: 0, date: month.key };
        byId.set(id, c);
      }
      c.count += 1;
      c.members.push(n.id);
      c.search = `${c.search || ""}\n${n.title}\n${n.id}`; // so a search finds a chat inside it
      clusterOf.set(n.id, id);
      byId.delete(n.id);
    }
  }
  let hubHidden = 0;
  const hubClusters = new Set();
  links = links.filter((l) => {
    if (clusterOf.has(l.target)) {
      if (l.hub && o.hubLinks) hubClusters.add(clusterOf.get(l.target));
      if (l.hub) hubHidden += 1;
      return false;
    }
    if (l.hub && !o.hubLinks) {
      hubHidden += 1;
      return false;
    }
    return true;
  });
  for (const id of hubClusters) links.push({ source: hubId, target: id, hub: true });
  if (byId.has(hubId) && !o.hubLinks) byId.get(hubId).hubCount = hubHidden;

  // filters
  const hidden = new Set(o.hiddenTypes);
  const keep = (n) => {
    if (n.ghost) return o.ghosts;
    if (hidden.has(n.type || "unknown")) return false;
    if (o.draftsOnly && !n.draft) return false;
    if (o.tag && !(n.tags || []).includes(o.tag)) return false;
    if (o.folder && !(n.folder === o.folder || (n.folder || "").startsWith(`${o.folder}/`))) return false;
    return true;
  };
  let nodes = [...byId.values()].filter(keep);
  let ids = new Set(nodes.map((n) => n.id));
  links = links.filter((l) => ids.has(l.source) && ids.has(l.target));
  if (!o.orphans) {
    const linked = new Set(links.flatMap((l) => [l.source, l.target]));
    nodes = nodes.filter((n) => linked.has(n.id) || n.cluster);
    ids = new Set(nodes.map((n) => n.id));
  }
  const stats = {
    pages: nodes.filter((n) => !n.ghost && !n.cluster && !String(n.id).startsWith("raw/")).length,
    chats: nodes.filter((n) => n.conversation).length + nodes.filter((n) => n.cluster).reduce((a, n) => a + n.count, 0),
    clusters: nodes.filter((n) => n.cluster).length,
    ghosts: nodes.filter((n) => n.ghost).length,
    links: links.length,
    hubHidden,
  };
  return { nodes, links, hub: hubId, stats };
}

// The neighbourhood of one page, `depth` links away (either direction), for the local graph.
export function neighbourhood(data, id, depth) {
  const adj = new Map();
  for (const e of data.edges) {
    if (!adj.has(e.source)) adj.set(e.source, new Set());
    if (!adj.has(e.target)) adj.set(e.target, new Set());
    adj.get(e.source).add(e.target);
    adj.get(e.target).add(e.source);
  }
  const keep = new Map([[id, 0]]);
  let frontier = [id];
  for (let d = 1; d <= depth && frontier.length; d++) {
    const next = [];
    for (const f of frontier) {
      for (const n of adj.get(f) || []) {
        if (!keep.has(n)) {
          keep.set(n, d);
          next.push(n);
        }
      }
    }
    frontier = next;
    if (keep.size > 400) break; // a hub's third ring is the whole wiki: stop growing
  }
  const nodes = data.nodes.filter((n) => keep.has(n.id)).map((n) => ({ ...n, ring: keep.get(n.id) }));
  const ids = new Set(nodes.map((n) => n.id));
  const links = data.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, broken: Boolean(e.broken) }));
  return { nodes, links, hub: data.skipped_page || "", stats: { pages: nodes.length, links: links.length } };
}
