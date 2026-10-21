// A tiny event bus between the workspace modules (tabs, explorer, context pane, graph).
//   ws:active   {pane, tab, data, el}  the focused pane shows another page or view
//   ws:view     {pane, tab, data, el}  a page or view was put into a pane (for its widgets)
//   ws:layout   {}                     panes were split, collapsed or resized
//   ws:theme    {}                     the colour theme changed
const bus = new EventTarget();

export function emit(type, detail = {}) {
  bus.dispatchEvent(new CustomEvent(type, { detail }));
}

export function on(type, handler) {
  bus.addEventListener(type, (event) => handler(event.detail));
}
