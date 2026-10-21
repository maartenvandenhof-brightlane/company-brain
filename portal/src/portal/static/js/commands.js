// What the command palette and the keyboard shortcuts can do. On wiki pages the workspace
// registers its actions (tabs, panes, explorer); elsewhere pages open as normal page loads.
import { switchTheme, setTheme } from "./theme.js";

let ws = null;

export function registerWorkspace(actions) {
  ws = actions;
}

export function hasWorkspace() {
  return ws !== null;
}

export function workspace() {
  return ws;
}

// Open a portal URL: wiki pages in the workspace when there is one, anything else by loading it.
export function go(url, where = "current") {
  if (ws && /^\/wiki(\/|$|\?)/.test(url)) ws.open(url, { where });
  else window.location.assign(url);
}

export function announce(text) {
  let line = document.getElementById("ws-announce");
  if (!line) {
    line = document.createElement("p");
    line.id = "ws-announce";
    line.className = "vh";
    line.setAttribute("role", "status");
    document.body.append(line);
  }
  line.textContent = "";
  setTimeout(() => {
    line.textContent = text;
  }, 30);
}

// id, title, keys (for the help), what it does. `when` hides a command that cannot run here.
export const COMMANDS = [
  { id: "graph", title: "Open graph view", keys: "g g", run: () => go("/wiki/graph") },
  { id: "index", title: "Go to the wiki index", keys: "g i", run: () => go("/wiki") },
  { id: "recent", title: "Go to recent changes", keys: "g r", run: () => go("/wiki/recent") },
  { id: "log", title: "Go to the log", keys: "g l", run: () => go("/wiki/log") },
  { id: "proposals", title: "Go to proposals", keys: "g p", run: () => go("/wiki/proposals") },
  {
    id: "drafts",
    title: "Drafts to review",
    keys: "g d",
    run: () => {
      go("/wiki/drafts");
      ws?.showDrafts?.();
    },
  },
  { id: "search", title: "Search the wiki", keys: "", run: () => go("/wiki/search") },
  { id: "import", title: "Import chats", keys: "", run: () => window.location.assign("/import") },
  { id: "process", title: "Process imported chats", keys: "", run: () => window.location.assign("/import#process-title") },
  { id: "toggle-left", title: "Toggle the file explorer", keys: "[", when: () => ws, run: () => ws.toggleSide("left") },
  { id: "toggle-right", title: "Toggle the page info", keys: "]", when: () => ws, run: () => ws.toggleSide("right") },
  { id: "split", title: "Split into two panes, or close the right one", keys: "\\", when: () => ws, run: () => ws.toggleSplit() },
  { id: "close-tab", title: "Close tab", keys: "Alt+W", when: () => ws, run: () => ws.closeActiveTab() },
  { id: "filter", title: "Filter files by name", keys: "/", when: () => ws, run: () => ws.filterByName() },
  {
    id: "theme",
    title: "Switch theme",
    keys: "",
    run: () => announce(`${switchTheme() === "dark" ? "Dark" : "Light"} theme.`),
  },
  { id: "theme-device", title: "Follow the device's theme", keys: "", run: () => { setTheme(null); announce("The theme follows your device."); } },
  { id: "home", title: "Go to the wiki", keys: "g h", run: () => window.location.assign("/wiki") },
  { id: "apps", title: "Go to settings: connected apps", keys: "", run: () => window.location.assign("/apps") },
  { id: "docs", title: "Go to the docs", keys: "", run: () => window.location.assign("/docs") },
];

export function available() {
  return COMMANDS.filter((c) => !c.when || c.when());
}
