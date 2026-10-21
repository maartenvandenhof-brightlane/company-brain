// The portal's stroke icons (templates/macros.html ICONS) for markup built by scripts.
const PATHS = {
  close: ["M6 6l12 12M18 6 6 18"],
  split: ["M4 5h16v14H4z", "M12 5v14"],
  unsplit: ["M4 5h16v14H4z"],
  "panel-left": ["M4 5h16v14H4z", "M9.5 5v14"],
  "panel-right": ["M4 5h16v14H4z", "M14.5 5v14"],
  chevron: ["m9.5 6 6 6-6 6"],
  folder: ["M3.5 6.5h6l2 2h9v10h-17z"],
  plus: ["M12 5v14M5 12h14"],
  process: ["M4 6.5h9M4 12h6M4 17.5h9", "m15 9 4.5 3-4.5 3z"],
  search: ["M16.4 16.4 20 20", "M18 11a7 7 0 1 1-14 0 7 7 0 0 1 14 0z"],
  graph: ["M8.4 7.6l7.7-1.3M7.6 9l5.6 6.8M17.7 8l-1.9 7.1", "M8.5 7a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0zM20 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM18 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z"],
  settings: ["M4 7h9M17 7h3M4 17h3M11 17h9", "M17 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM11 17a2 2 0 1 1-4 0 2 2 0 0 1 4 0z"],
  list: ["M9 6.5h11M9 12h11M9 17.5h11M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01"],
  fit: ["M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"],
  minus: ["M5 12h14"],
  refresh: ["M19.5 11A7.5 7.5 0 1 0 17.3 17.3", "M20 4.5V11h-6.5"],
};

const NS = "http://www.w3.org/2000/svg";

export function icon(name) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const d of PATHS[name] || []) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// A small element with a class and text, the building block of script-made markup.
export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
