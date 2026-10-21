// The workspace's columns: [file explorer | panes | context pane]. Both side columns can be
// collapsed and resized (drag the edge, or focus it and use the arrow keys); below 1200 px the
// context pane, below 860 px both, are the CSS drawers of templates/wiki/base.html. Widths are
// custom properties set through the CSSOM (allowed under the CSP's style-src 'self').
import { emit, on } from "./bus.js";
import { el, icon } from "./icons.js";
import { save } from "./store.js";

const LIMITS = { left: [200, 520], right: [240, 600] };
const wideLeft = window.matchMedia("(min-width: 860px)");
const wideRight = window.matchMedia("(min-width: 1200px)");
let state;
const toggles = {};

function applyWidths() {
  const root = document.documentElement.style;
  for (const side of ["left", "right"]) {
    const [lo, hi] = LIMITS[side];
    state[side].width = Math.round(Math.min(hi, Math.max(lo, state[side].width)));
    root.setProperty(`--ws-${side}`, `${state[side].width}px`);
    document.body.classList.toggle(`ws-${side}-collapsed`, state[side].collapsed);
    const button = toggles[side];
    const shown = !state[side].collapsed;
    button.setAttribute("aria-expanded", String(shown));
    const what = side === "left" ? "file explorer" : "page info";
    button.setAttribute("aria-label", `${shown ? "Hide" : "Show"} the ${what}`);
    button.title = `${shown ? "Hide" : "Show"} the ${what} (${side === "left" ? "[" : "]"})`;
    const handle = document.querySelector(`.ws-resizer-${side}`);
    handle?.setAttribute("aria-valuenow", String(state[side].width));
    handle?.toggleAttribute("hidden", state[side].collapsed);
  }
}

export function toggleSide(side) {
  const drawer = side === "left" ? !wideLeft.matches : !wideRight.matches;
  if (drawer) {
    const box = document.getElementById(side === "left" ? "drawer-nav" : "drawer-info");
    if (box) box.checked = !box.checked;
    return;
  }
  state[side].collapsed = !state[side].collapsed;
  applyWidths();
  save(state);
  emit("ws:layout");
}

// Pages without page info (the index, recent changes…) have no right column on the server;
// the workspace always has one, for whichever tab is showing.
function ensureRight() {
  if (document.querySelector(".side-right")) return;
  const shell = document.querySelector(".shell");
  const box = el("input", "drawer-toggle");
  box.type = "checkbox";
  box.id = "drawer-info";
  box.setAttribute("aria-label", "Show links and properties of this page");
  shell.before(box);
  const scrim = el("label", "scrim scrim-info");
  scrim.htmlFor = "drawer-info";
  scrim.setAttribute("aria-hidden", "true");
  const aside = el("aside", "side side-right");
  aside.setAttribute("aria-label", "About this page");
  const close = el("label", "drawer-close");
  close.htmlFor = "drawer-info";
  close.setAttribute("aria-hidden", "true");
  close.append(icon("close"));
  aside.append(close);
  shell.append(scrim, aside);
  const bar = document.querySelector(".wiki-bar");
  if (bar && !bar.querySelector(".toggle-info")) {
    const label = el("label", "toggle-info");
    label.htmlFor = "drawer-info";
    label.setAttribute("aria-hidden", "true");
    label.append(icon("panel-right"), document.createTextNode("Page info"));
    bar.append(label);
  }
}

function resizer(side) {
  const handle = el("div", `ws-resizer ws-resizer-${side}`);
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-label", side === "left" ? "Width of the file explorer" : "Width of the page info");
  handle.setAttribute("aria-valuemin", String(LIMITS[side][0]));
  handle.setAttribute("aria-valuemax", String(LIMITS[side][1]));
  handle.tabIndex = 0;
  let start = 0;
  let width = 0;
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    start = event.clientX;
    width = state[side].width;
    document.body.classList.add("ws-resizing");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    const delta = event.clientX - start;
    state[side].width = side === "left" ? width + delta : width - delta;
    applyWidths();
  });
  const end = (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove("ws-resizing");
    save(state);
    emit("ws:layout");
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  handle.addEventListener("keydown", (event) => {
    const grow = side === "left" ? "ArrowRight" : "ArrowLeft";
    const shrink = side === "left" ? "ArrowLeft" : "ArrowRight";
    const step = event.shiftKey ? 64 : 16;
    if (event.key === grow) state[side].width += step;
    else if (event.key === shrink) state[side].width -= step;
    else if (event.key === "Home") state[side].width = LIMITS[side][0];
    else if (event.key === "End") state[side].width = LIMITS[side][1];
    else if (event.key === "Enter") {
      toggleSide(side);
      return;
    } else return;
    event.preventDefault();
    applyWidths();
    save(state);
    emit("ws:layout");
  });
  return handle;
}

function placeToggles() {
  const panes = [...document.querySelectorAll(".ws-pane:not([hidden])")];
  panes[0]?.querySelector(".ws-tabbar")?.prepend(toggles.left);
  panes.at(-1)?.querySelector(".ws-tools")?.append(toggles.right);
}

export function setupLayout(workspaceState) {
  state = workspaceState;
  ensureRight();
  document.body.classList.add("has-info");
  const shell = document.querySelector(".shell");
  const left = shell.querySelector(".side-left");
  const right = shell.querySelector(".side-right");
  left.id ||= "ws-left";
  right.id ||= "ws-right";
  left.after(resizer("left"));
  right.before(resizer("right"));
  for (const side of ["left", "right"]) {
    const button = el("button", `ws-tool ws-toggle-${side}`);
    button.type = "button";
    button.setAttribute("aria-controls", side === "left" ? left.id : right.id);
    button.append(icon(side === "left" ? "panel-left" : "panel-right"));
    button.addEventListener("click", () => toggleSide(side));
    toggles[side] = button;
  }
  applyWidths();
  on("ws:layout", placeToggles);
  for (const query of [wideLeft, wideRight]) query.addEventListener("change", () => emit("ws:layout"));
}
