// Entry of the wiki workspace (wiki pages only, loaded by ../portal.js). Without JS, or if
// anything here fails, the server-rendered page with its CSS drawers stays as it is.
import { registerWorkspace } from "../commands.js";
import { initContext } from "./context.js";
import { filterByName, initExplorer, showDrafts } from "./explorer.js";
import "./graph-page.js";
import { setupLayout, toggleSide } from "./layout.js";
import "./live.js";
import "./local-graph.js";
import { boot, closeActiveTab, cycleTab, goToTab, open, state, toggleSplit } from "./workspace.js";

setupLayout(state);
const adopt = initContext();
initExplorer();
boot(adopt);
registerWorkspace({ open, closeActiveTab, toggleSplit, toggleSide, cycleTab, goToTab, showDrafts, filterByName });
