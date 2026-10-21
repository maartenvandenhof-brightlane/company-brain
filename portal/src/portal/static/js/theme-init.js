// A classic script in <head> (not a module, so it runs before the first paint): applies the
// theme chosen in the portal, if any, so a dark choice does not flash light. Without a choice
// the device's setting applies (css/tokens.css). See js/theme.js.
try {
  const theme = JSON.parse(window.localStorage.getItem("sb.theme") || "null");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  // storage blocked: the device's setting applies
}
