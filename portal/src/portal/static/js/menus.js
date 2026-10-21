// The settings menu (<details class="menu">) and the wiki drawers (checkboxes) work without
// JS. This adds what only JS can: close them on Escape and the menu on an outside click.
const menus = [...document.querySelectorAll("details.menu")];
const drawers = [...document.querySelectorAll("input.drawer-toggle")];

document.addEventListener("click", (event) => {
  for (const menu of menus) {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const menu of menus) {
    if (menu.open) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    }
  }
  for (const drawer of drawers) drawer.checked = false;
});
