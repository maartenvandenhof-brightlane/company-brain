// A force-directed graph on a canvas (d3-force, d3-zoom, d3-drag, d3-quadtree; vendored,
// loaded on first use). Used by the graph view (graph-page.js) and the local graph in the
// context pane (local-graph.js).
//
// Drawing is batched per colour and culled to the viewport, labels are drawn in screen space
// and only when they fit (by zoom, size, hover or search), so a few thousand nodes pan at
// full frame rate. Colours are the page-type tokens of css/tokens.css, read with
// getComputedStyle and read again when the theme changes. Identity never rests on colour
// alone: the tooltip names the type, the legend doubles as a filter, and the list view is
// one click away. Marks: a draft carries an ink ring, an unprocessed chat is hollow, a
// missing page is a dashed outline, a cluster of chats shows its count.
//
// The simulation stops by itself when it settles, pauses while the graph is off screen or the
// browser tab is hidden, and under prefers-reduced-motion is run ahead in small chunks and
// drawn settled instead of animated.
import { on } from "./bus.js";
import { el } from "./icons.js";

let d3promise = null;
export const loadD3 = () => (d3promise ||= import("/static/vendor/d3/d3.js").then((m) => m.default));

const TYPES = ["person", "organization", "project", "concept", "decision", "meeting", "howto", "owner", "reference", "source", "guide", "maintenance-report", "proposal"];
export const TYPE_LABELS = {
  person: "People", organization: "Organizations", project: "Projects", concept: "Concepts",
  decision: "Decisions", meeting: "Meetings", howto: "How-tos", owner: "Owner", reference: "References",
  source: "Raw sources", guide: "Guides", "maintenance-report": "Maintenance reports", proposal: "Proposals", unknown: "Other",
};
export const DEFAULT_SETTINGS = { linkDistance: 60, repel: 140, center: 0.06, nodeSize: 1 };
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const darkScheme = window.matchMedia("(prefers-color-scheme: dark)");

export function readColors() {
  const css = getComputedStyle(document.documentElement);
  const get = (name) => css.getPropertyValue(name).trim();
  const types = {};
  for (const t of TYPES) types[t] = get(`--type-${t}`);
  types.unknown = get("--type-unknown");
  return {
    types, sheet: get("--sheet"), ink: get("--ink"), ink2: get("--ink-2"), ink3: get("--ink-3"),
    link: get("--rule-strong"), accent: get("--accent"), broken: get("--link-broken"), mark: get("--mark"),
  };
}

export async function createGraph(host, { compact = false, onPick = () => {}, describe = () => "" } = {}) {
  const d3 = await loadD3();
  const canvas = el("canvas", "graph-canvas");
  canvas.tabIndex = 0;
  canvas.setAttribute("role", "img");
  const tip = el("div", "graph-tip");
  tip.hidden = true;
  tip.setAttribute("aria-hidden", "true");
  host.append(canvas, tip);
  const ctx = canvas.getContext("2d");
  let colors = readColors();
  let width = 300;
  let height = 200;
  let dpr = 1;
  let nodes = [];
  let links = [];
  let byId = new Map();
  let adjacent = new Map();
  let transform = d3.zoomIdentity;
  let hover = null;
  let matches = null; // Set of ids highlighted by the search
  let focus = null; // the page the local graph is about
  let tree = null;
  let treeDirty = true;
  let frame = 0;
  let visible = true;
  let touched = false; // the viewer panned or zoomed: stop fitting
  let settling = 0;
  let startedAt = 0;
  const settings = { ...DEFAULT_SETTINGS };
  const stats = { settleMs: 0, frames: 0 };

  const radius = (n) => {
    const s = settings.nodeSize;
    if (n.cluster) return s * (4 + Math.sqrt(n.count) * 1.7);
    if (n.ghost) return s * 2.6;
    // the skipped-conversations page is a hub of the chats it cites, even with its links hidden
    const weight = Math.max(n.backlink_count || 0, (n.hubCount || 0) / 4);
    return s * Math.min(26, (compact ? 3.2 : 3.4) + Math.sqrt(weight) * 1.5);
  };
  const colorOf = (n) => colors.types[n.type] || colors.types.unknown;

  const sim = d3
    .forceSimulation()
    .alphaDecay(compact ? 0.06 : 0.045)
    .velocityDecay(0.38)
    .force("link", d3.forceLink().id((d) => d.id))
    .force("charge", d3.forceManyBody().theta(0.95).distanceMax(compact ? 260 : 700))
    .force("x", d3.forceX(0))
    .force("y", d3.forceY(0))
    .force("collide", d3.forceCollide().radius((d) => radius(d) + 1.5).iterations(1))
    .stop();
  sim.on("tick", () => {
    treeDirty = true;
    if (!touched && compact) fit(false);
    redraw();
  });
  sim.on("end", () => {
    if (startedAt) stats.settleMs = Math.round(performance.now() - startedAt);
    startedAt = 0;
    host.dataset.settled = "1";
    if (!touched) fit(false);
  });

  function applySettings() {
    sim.force("link").distance(settings.linkDistance * (compact ? 0.8 : 1));
    sim.force("charge").strength(-settings.repel * (compact ? 0.7 : 1));
    sim.force("x").strength(settings.center);
    sim.force("y").strength(settings.center);
    sim.force("collide").radius((d) => radius(d) + 1.5);
  }

  // ---------------------------------------------------------------- drawing

  function redraw() {
    if (!frame && visible) frame = requestAnimationFrame(draw);
  }

  function neighbours(id) {
    const set = new Set(adjacent.get(id) || []);
    set.add(id);
    return set;
  }

  function draw() {
    frame = 0;
    stats.frames += 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const k = transform.k;
    const lit = hover ? neighbours(hover.id) : null;
    const dimAll = Boolean(lit || matches);
    const [x0, y0] = transform.invert([0, 0]);
    const [x1, y1] = transform.invert([width, height]);
    const pad = 30 / k;
    const inView = (n) => n.x > x0 - pad && n.x < x1 + pad && n.y > y0 - pad && n.y < y1 + pad;
    ctx.save();
    ctx.translate(transform.x, transform.y);
    ctx.scale(k, k);

    // links: one path per style
    const plain = new Path2D();
    const broken = new Path2D();
    const strong = new Path2D();
    for (const l of links) {
      const s = l.source;
      const t = l.target;
      if (!inView(s) && !inView(t)) continue;
      const path = lit && (lit.has(s.id) && lit.has(t.id)) && (s.id === hover.id || t.id === hover.id) ? strong : l.broken ? broken : plain;
      path.moveTo(s.x, s.y);
      path.lineTo(t.x, t.y);
    }
    ctx.lineWidth = Math.max(0.6, 1 / k);
    ctx.globalAlpha = dimAll ? 0.18 : compact ? 0.8 : 0.55;
    ctx.strokeStyle = colors.link;
    ctx.stroke(plain);
    ctx.setLineDash([3 / k, 3 / k]);
    ctx.strokeStyle = colors.broken;
    ctx.stroke(broken);
    ctx.setLineDash([]);
    if (lit) {
      ctx.globalAlpha = 0.95;
      ctx.strokeStyle = colors.accent;
      ctx.lineWidth = 1.6 / k;
      ctx.stroke(strong);
    }

    // nodes: batched per colour and dimming
    const fills = new Map();
    const hollow = [];
    const rings = [];
    const ghosts = [];
    for (const n of nodes) {
      if (!inView(n)) continue;
      const dim = (lit && !lit.has(n.id)) || (matches && !matches.has(n.id) && !(lit && lit.has(n.id)));
      if (n.ghost) {
        ghosts.push([n, dim]);
        continue;
      }
      if (n.unprocessed || n.cluster) hollow.push([n, dim]);
      else {
        const key = `${colorOf(n)}|${dim ? 1 : 0}`;
        let path = fills.get(key);
        if (!path) fills.set(key, (path = new Path2D()));
        const r = radius(n);
        path.moveTo(n.x + r, n.y);
        path.arc(n.x, n.y, r, 0, Math.PI * 2);
      }
      if (n.draft || n.id === focus) rings.push([n, dim]);
    }
    for (const [key, path] of fills) {
      const [color, dim] = key.split("|");
      ctx.globalAlpha = dim === "1" ? 0.16 : 1;
      ctx.fillStyle = color;
      ctx.fill(path);
    }
    ctx.lineWidth = Math.max(1.4, 1.8 / k);
    for (const [n, dim] of hollow) {
      const r = radius(n);
      ctx.globalAlpha = dim ? 0.2 : 1;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      ctx.fillStyle = n.cluster ? colors.sheet : colors.sheet;
      ctx.fill();
      ctx.strokeStyle = colorOf(n);
      if (n.skipped && n.cluster) ctx.setLineDash([2 / k, 2 / k]);
      ctx.stroke();
      ctx.setLineDash([]);
      if (n.cluster && !n.unprocessed && !n.skipped) {
        ctx.globalAlpha *= 0.35;
        ctx.fillStyle = colorOf(n);
        ctx.fill();
      }
    }
    ctx.lineWidth = Math.max(1, 1.2 / k);
    ctx.setLineDash([2 / k, 2 / k]);
    for (const [n, dim] of ghosts) {
      ctx.globalAlpha = dim ? 0.2 : 0.9;
      ctx.beginPath();
      ctx.arc(n.x, n.y, radius(n), 0, Math.PI * 2);
      ctx.strokeStyle = colors.broken;
      ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const [n, dim] of rings) {
      ctx.globalAlpha = dim ? 0.2 : 1;
      ctx.beginPath();
      ctx.arc(n.x, n.y, radius(n) + 2.6 / Math.min(k, 2), 0, Math.PI * 2);
      ctx.strokeStyle = n.id === focus ? colors.accent : colors.ink2;
      ctx.lineWidth = (n.id === focus ? 2.4 : 1.5) / Math.min(k, 2);
      ctx.stroke();
    }
    if (hover) {
      ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.arc(hover.x, hover.y, radius(hover) + 3 / k, 0, Math.PI * 2);
      ctx.strokeStyle = colors.accent;
      ctx.lineWidth = 2 / k;
      ctx.stroke();
    }
    ctx.restore();
    ctx.globalAlpha = 1;
    labels(k, lit, inView);
  }

  function labels(k, lit, inView) {
    const picked = [];
    const few = nodes.length <= (compact ? 60 : 30);
    for (const n of nodes) {
      if (!inView(n)) continue;
      const screenR = radius(n) * k;
      const important = (hover && n.id === hover.id) || (lit && lit.has(n.id)) || (matches && matches.has(n.id)) || n.id === focus;
      if (important || few || n.cluster || n.hubCount || screenR >= 9 || k >= 2.2) picked.push([important ? 0 : n.cluster ? 1 : 2, -screenR, n]);
    }
    picked.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    ctx.font = `${compact ? 11 : 12}px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineJoin = "round";
    const boxes = [];
    let drawn = 0;
    for (const [, , n] of picked) {
      if (drawn >= (compact ? 80 : 220)) break;
      const [sx, sy] = transform.apply([n.x, n.y]);
      const r = radius(n) * k;
      let text = String(n.title || n.id);
      if (text.length > 34) text = `${text.slice(0, 32)}…`;
      if (n.hubCount) text += ` (+${n.hubCount} skipped)`;
      const w = ctx.measureText(text).width;
      const box = [sx - w / 2 - 2, sy + r + 2, sx + w / 2 + 2, sy + r + 16];
      const important = (hover && n.id === hover.id) || n.id === focus;
      if (!important && boxes.some((b) => b[0] < box[2] && box[0] < b[2] && b[1] < box[3] && box[1] < b[3])) continue;
      boxes.push(box);
      const dim = (lit && !lit.has(n.id)) || (matches && !matches.has(n.id));
      ctx.globalAlpha = dim ? 0.35 : 1;
      ctx.lineWidth = 3;
      ctx.strokeStyle = colors.sheet;
      ctx.strokeText(text, sx, sy + r + 3);
      ctx.fillStyle = important || (matches && matches.has(n.id)) ? colors.ink : colors.ink2;
      ctx.fillText(text, sx, sy + r + 3);
      if (n.cluster && r >= 9) {
        ctx.textBaseline = "middle";
        ctx.fillStyle = colors.ink2;
        ctx.fillText(String(n.count), sx, sy);
        ctx.textBaseline = "top";
      }
      drawn += 1;
    }
    ctx.globalAlpha = 1;
  }

  // ---------------------------------------------------------------- size, view, visibility

  function resize() {
    const box = host.getBoundingClientRect();
    width = Math.max(120, Math.round(box.width));
    height = Math.max(120, Math.round(box.height));
    dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    redraw();
  }

  const zoom = d3
    .zoom()
    .scaleExtent([0.03, 8])
    .on("zoom", (event) => {
      transform = event.transform;
      if (event.sourceEvent) touched = true;
      hideTip();
      redraw();
    });

  function fit(user = true) {
    if (!nodes.length) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of nodes) {
      if (!Number.isFinite(n.x)) continue;
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x);
      maxY = Math.max(maxY, n.y);
    }
    if (!Number.isFinite(minX)) return;
    const margin = compact ? 24 : 48;
    const k = Math.min(compact ? 2 : 3, (width - margin * 2) / Math.max(1, maxX - minX), (height - margin * 2) / Math.max(1, maxY - minY));
    const t = d3.zoomIdentity.translate(width / 2 - (k * (minX + maxX)) / 2, height / 2 - (k * (minY + maxY)) / 2).scale(k);
    d3.select(canvas).call(zoom.transform, t);
    if (user) touched = false;
  }

  function running() {
    return sim.alpha() > sim.alphaMin();
  }

  function pause() {
    visible = false;
    sim.stop();
  }

  function resume() {
    if (visible) return;
    visible = true;
    if (running() && !reduceMotion.matches) sim.restart();
    redraw();
  }

  // Run the layout ahead without animating (reduced motion): small chunks, so the page stays
  // responsive, then one settled drawing.
  function settle() {
    const mine = ++settling;
    host.setAttribute("aria-busy", "true");
    const step = () => {
      if (mine !== settling) return;
      const until = performance.now() + 12;
      while (running() && performance.now() < until) sim.tick();
      if (running()) setTimeout(step, 0);
      else {
        host.removeAttribute("aria-busy");
        stats.settleMs = Math.round(performance.now() - startedAt);
        host.dataset.settled = "1";
        treeDirty = true;
        fit(false);
        redraw();
      }
    };
    step();
  }

  function start(alpha) {
    startedAt = performance.now();
    delete host.dataset.settled;
    sim.alpha(alpha);
    if (reduceMotion.matches) {
      sim.stop();
      settle();
    } else if (visible) sim.restart();
  }

  // ---------------------------------------------------------------- data

  function setData(model, { focusId = null } = {}) {
    const old = byId;
    focus = focusId;
    const placed = [];
    nodes = model.nodes.map((n) => {
      const was = old.get(n.id);
      if (was) {
        n.x = was.x;
        n.y = was.y;
        placed.push(n);
      }
      return n;
    });
    byId = new Map(nodes.map((n) => [n.id, n]));
    adjacent = new Map();
    for (const l of model.links) {
      if (!adjacent.has(l.source)) adjacent.set(l.source, []);
      if (!adjacent.has(l.target)) adjacent.set(l.target, []);
      adjacent.get(l.source).push(l.target);
      adjacent.get(l.target).push(l.source);
    }
    // new nodes start next to a placed neighbour, so a filter change does not scatter them
    for (const n of nodes) {
      if (Number.isFinite(n.x)) continue;
      const near = (adjacent.get(n.id) || []).map((id) => byId.get(id)).find((m) => m && Number.isFinite(m.x));
      if (near) {
        n.x = near.x + (Math.random() - 0.5) * 20;
        n.y = near.y + (Math.random() - 0.5) * 20;
      }
    }
    if (focusId && byId.has(focusId)) {
      const f = byId.get(focusId);
      f.fx = 0;
      f.fy = 0;
    }
    links = model.links.map((l) => ({ ...l }));
    applySettings();
    sim.nodes(nodes);
    sim.force("link").links(links);
    if (hover && !byId.has(hover.id)) hover = null;
    treeDirty = true;
    canvas.setAttribute("aria-label", describe(model));
    start(placed.length > nodes.length / 2 ? 0.35 : 1);
    if (!placed.length) touched = false;
    redraw();
  }

  // ---------------------------------------------------------------- pointer: hover, click, drag

  // slack: how far outside a node (in screen pixels) still counts as on it
  function hit(px, py, slack = 6) {
    if (treeDirty) {
      tree = d3.quadtree(nodes.filter((n) => Number.isFinite(n.x)), (d) => d.x, (d) => d.y);
      treeDirty = false;
    }
    const [x, y] = transform.invert([px, py]);
    const n = tree.find(x, y, 40 / transform.k);
    if (!n) return null;
    const reach = radius(n) + slack / transform.k;
    return Math.hypot(n.x - x, n.y - y) <= reach ? n : null;
  }

  function point(event) {
    const box = canvas.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  function showTip(n, px, py) {
    tip.replaceChildren();
    tip.append(el("strong", "", n.title || n.id));
    const kind = n.ghost ? "Missing page" : n.cluster ? `${n.count} chats: click to show them` : TYPE_LABELS[n.type] || n.type || "Page";
    const facts = [kind];
    if (!n.ghost && !n.cluster) facts.push(`${n.backlink_count || 0} backlink${n.backlink_count === 1 ? "" : "s"}`);
    if (n.parts > 1) facts.push(`${n.parts} files`);
    if (n.draft) facts.push("draft to review");
    if (n.unprocessed && !n.cluster) facts.push("not processed yet");
    if (n.skipped && !n.cluster) facts.push("skipped");
    if (n.hubCount) facts.push(`links to ${n.hubCount} skipped chats (hidden)`);
    tip.append(el("span", "", facts.join(" · ")));
    tip.hidden = false;
    const x = Math.min(px + 14, width - tip.offsetWidth - 4);
    const y = py + 14 + tip.offsetHeight > height ? py - tip.offsetHeight - 10 : py + 14;
    tip.style.translate = `${Math.max(4, x)}px ${Math.max(4, y)}px`;
  }

  function hideTip() {
    tip.hidden = true;
  }

  canvas.addEventListener("pointermove", (event) => {
    if (event.buttons) return;
    const [px, py] = point(event);
    const n = hit(px, py);
    if (n !== hover) {
      hover = n;
      canvas.classList.toggle("is-over", Boolean(n));
      redraw();
    }
    if (n) showTip(n, px, py);
    else hideTip();
  });
  canvas.addEventListener("pointerleave", () => {
    hover = null;
    canvas.classList.remove("is-over");
    hideTip();
    redraw();
  });
  canvas.addEventListener("click", (event) => {
    const [px, py] = point(event);
    const n = hit(px, py);
    if (n) onPick(n, event);
  });
  canvas.addEventListener("auxclick", (event) => {
    if (event.button !== 1) return;
    const [px, py] = point(event);
    const n = hit(px, py);
    if (n) {
      event.preventDefault();
      onPick(n, event);
    }
  });

  const drag = d3
    .drag()
    .container(canvas)
    // touch pans and pinches (d3-zoom); nodes are dragged with a mouse or pen
    .filter((event) => !event.ctrlKey && !event.button && !event.touches)
    .subject((event) => {
      const n = hit(event.x, event.y);
      return n ? { node: n, x: event.x, y: event.y } : null;
    })
    .on("start", (event) => {
      const n = event.subject.node;
      n.fx = n.x;
      n.fy = n.y;
      if (!reduceMotion.matches) sim.alphaTarget(0.25).restart();
    })
    .on("drag", (event) => {
      const n = event.subject.node;
      const [x, y] = transform.invert([event.x, event.y]);
      n.fx = x;
      n.fy = y;
      if (reduceMotion.matches) {
        n.x = x;
        n.y = y;
        treeDirty = true;
        redraw();
      }
    })
    .on("end", (event) => {
      const n = event.subject.node;
      if (!reduceMotion.matches) sim.alphaTarget(0);
      if (n.id !== focus) {
        n.fx = null;
        n.fy = null;
      }
    });
  d3.select(canvas).call(drag).call(zoom);

  canvas.addEventListener("keydown", (event) => {
    const sel = d3.select(canvas);
    const moves = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] };
    if (moves[event.key]) zoom.translateBy(sel, moves[event.key][0] / transform.k, moves[event.key][1] / transform.k);
    else if (event.key === "+" || event.key === "=") zoom.scaleBy(sel, 1.3);
    else if (event.key === "-") zoom.scaleBy(sel, 1 / 1.3);
    else if (event.key === "0") fit();
    else return;
    touched = true;
    event.preventDefault();
  });

  // ---------------------------------------------------------------- lifecycle

  const sizes = new ResizeObserver(resize);
  sizes.observe(host);
  const seen = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting && !document.hidden) resume();
      else pause();
    }
  });
  seen.observe(canvas);
  const onVisibility = () => (document.hidden ? pause() : canvas.isConnected && resume());
  document.addEventListener("visibilitychange", onVisibility);
  let alive = true;
  const retheme = () => {
    if (!alive) return;
    colors = readColors();
    redraw();
  };
  on("ws:theme", retheme);
  darkScheme.addEventListener("change", retheme);
  resize();

  return {
    canvas,
    stats,
    setData,
    fit,
    setSettings(next) {
      Object.assign(settings, next);
      applySettings();
      treeDirty = true;
      if (nodes.length) start(0.5); // before the data arrives there is nothing to lay out
      redraw();
    },
    setMatches(set) {
      matches = set && set.size ? set : null;
      redraw();
    },
    highlightType(type) {
      matches = type ? new Set(nodes.filter((n) => n.type === type && !n.ghost).map((n) => n.id)) : null;
      redraw();
    },
    centerOn(id) {
      const n = byId.get(id);
      if (!n) return;
      touched = true;
      d3.select(canvas).call(zoom.translateTo, n.x, n.y);
    },
    // for measuring: pan by (dx, dy) screen pixels
    panBy(dx, dy) {
      zoom.translateBy(d3.select(canvas), dx / transform.k, dy / transform.k);
    },
    running,
    destroy() {
      alive = false;
      settling += 1;
      sim.stop();
      sizes.disconnect();
      seen.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      darkScheme.removeEventListener("change", retheme);
      canvas.remove();
      tip.remove();
    },
  };
}
