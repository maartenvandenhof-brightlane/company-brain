// Second Brain portal: progressive enhancement only. Every page works without JS.
// Loaded as an ES module from /static (the CSP forbids inline scripts and handlers).
// Each module finds its own elements and does nothing when they are absent; add new
// modules next to these and import them here. The wiki workspace (tabs, explorer, graph) is
// loaded only on wiki pages.
import "./password.js";
import "./copy.js";
import "./confirm.js";
import "./menus.js";
import "./upload.js";
import "./process-model.js";
import "./ideas.js";
import "./patterns.js";
import "./dream-session.js";

// For the signed-in owner: the command palette (Ctrl/Cmd+K), shortcuts (?) and, on wiki pages,
// the workspace (tabs, explorer, graph).
if (document.querySelector(".topbar")) {
  import("./shortcuts.js");
  if (document.body.classList.contains("layout-wiki")) import("./workspace/index.js");
}
