// Light or dark by choice instead of the device's setting (the command palette's "Switch
// theme" and "Use the device's theme"). The choice is kept in localStorage; theme-init.js
// applies it before the first paint. Listeners (the graph) redraw on ws:theme.
import { emit } from "./workspace/bus.js";
import { getPref, setPref } from "./workspace/store.js";

const dark = window.matchMedia("(prefers-color-scheme: dark)");

export function currentTheme() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "light" || chosen === "dark") return chosen;
  return dark.matches ? "dark" : "light";
}

export function setTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  setPref("theme", theme === "light" || theme === "dark" ? theme : null);
  emit("ws:theme");
}

export function switchTheme() {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
  return currentTheme();
}

export function chosenTheme() {
  return getPref("theme", null);
}
