// Keyboard shortcuts, and their help (?). None of them fires while you type in a field.
import { COMMANDS, workspace } from "./commands.js";
import { openPalette } from "./palette.js";
import { el } from "./workspace/icons.js";

const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const MOD = mac ? "⌘" : "Ctrl";
let help;
let pendingG = 0;

const byId = Object.fromEntries(COMMANDS.map((c) => [c.id, c]));
const G_KEYS = { g: "graph", i: "index", r: "recent", l: "log", p: "proposals", d: "drafts", h: "home" };

function typing(target) {
  if (!(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  if (target.closest("dialog[open]") && !target.closest("dialog.shortcuts")) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  return !["checkbox", "radio", "button", "submit", "range", "reset", "file", "color"].includes(target.type);
}

const SECTIONS = [
  ["Anywhere in the portal", [
    [`${MOD}+K`, "Go to a page or run a command"],
    [`${MOD}+O`, "Go to a page"],
    ["?", "Show these shortcuts"],
    ["g then g / i / r / l / p / d / h", "Graph, index, recent changes, log, proposals, drafts to review, home"],
  ]],
  ["In the wiki", [
    ["[  /  ]", "Hide or show the file explorer / the page info"],
    ["\\", "Split into two panes, or close the right one"],
    ["Alt+W", "Close the tab"],
    ["Alt+1 … Alt+9", "Go to tab 1 … the last tab"],
    ["Alt+,  /  Alt+.", "Previous / next tab"],
    ["/", "Filter files by name"],
    ["Click, Ctrl/⌘-click, Shift-click", "Open a link here, in a new tab, in the other pane"],
  ]],
  ["File explorer and tabs", [
    ["↑ ↓ ← →", "Move; open or close a folder"],
    ["Enter", "Open the page (Ctrl/⌘+Enter: new tab, Shift+Enter: other pane)"],
    ["Delete", "Close the focused tab"],
  ]],
  ["Graph", [
    ["Arrows, + −, 0", "Pan, zoom, fit"],
    ["Click a node", "Open the page; on a group of chats, show them"],
  ]],
];

function showHelp() {
  if (!help) {
    help = el("dialog", "shortcuts");
    help.setAttribute("aria-labelledby", "shortcuts-h");
    const box = el("div", "shortcuts-box");
    const head = el("div", "shortcuts-head");
    const title = el("h2", "", "Keyboard shortcuts");
    title.id = "shortcuts-h";
    const close = el("button", "button small quiet", "Close");
    close.type = "button";
    close.addEventListener("click", () => help.close());
    head.append(title, close);
    box.append(head);
    for (const [label, rows] of SECTIONS) {
      box.append(el("h3", "", label));
      const dl = el("dl", "shortcut-list");
      for (const [keys, what] of rows) {
        const row = el("div");
        const dt = el("dt");
        dt.append(el("kbd", "", keys));
        row.append(dt, el("dd", "", what));
        dl.append(row);
      }
      box.append(dl);
    }
    help.append(box);
    help.addEventListener("click", (event) => {
      if (event.target === help) help.close();
    });
    document.body.append(help);
  }
  help.showModal();
}

document.addEventListener("keydown", (event) => {
  const mod = event.ctrlKey || event.metaKey;
  const key = event.key;
  // the palette's own field (also just after Escape closed it) does not count as typing
  const inPalette = event.target instanceof Element && event.target.closest("dialog.palette");
  if (event.defaultPrevented || event.isComposing || (typing(event.target) && !(inPalette && mod))) return;
  // Ctrl/Cmd+K and Ctrl/Cmd+O
  if (mod && !event.altKey && !event.shiftKey && (key === "k" || key === "K" || key === "o" || key === "O")) {
    event.preventDefault();
    openPalette(key.toLowerCase() === "o" ? "pages" : "all");
    return;
  }
  if (mod) return;
  const run = (id) => {
    const command = byId[id];
    if (command && (!command.when || command.when())) {
      event.preventDefault();
      command.run();
    }
  };
  if (event.altKey) {
    const ws = workspace();
    if (!ws) return;
    if (event.code === "KeyW") run("close-tab");
    else if (/^Digit[1-9]$/.test(event.code)) {
      event.preventDefault();
      ws.goToTab(Number(event.code.slice(5)));
    } else if (event.code === "Comma" || event.code === "Period") {
      event.preventDefault();
      ws.cycleTab(event.code === "Comma" ? -1 : 1);
    }
    return;
  }
  if (pendingG && Date.now() - pendingG < 1200) {
    pendingG = 0;
    if (G_KEYS[key]) run(G_KEYS[key]);
    return;
  }
  pendingG = 0;
  if (key === "g") pendingG = Date.now();
  else if (key === "?") {
    event.preventDefault();
    showHelp();
  } else if (key === "[") run("toggle-left");
  else if (key === "]") run("toggle-right");
  else if (key === "\\") run("split");
  else if (key === "/") run("filter");
});
