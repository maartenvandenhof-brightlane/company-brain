// d3 v7 modules for the wiki graph, vendored (see README.md): no CDN, no bundler. Each file is
// the module's official UMD build; imported for its side effect, it adds its functions to
// globalThis.d3 (dependencies first). Loaded on demand by js/workspace/graph-view.js.
import "./d3-dispatch.min.js";
import "./d3-timer.min.js";
import "./d3-quadtree.min.js";
import "./d3-force.min.js";
import "./d3-selection.min.js";
import "./d3-color.min.js";
import "./d3-interpolate.min.js";
import "./d3-ease.min.js";
import "./d3-transition.min.js";
import "./d3-drag.min.js";
import "./d3-zoom.min.js";

export default globalThis.d3;
