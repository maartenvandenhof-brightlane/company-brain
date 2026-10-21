// An assistant dream's page (/dreams/{id}): follow its session through GET
// /dreams/{id}/session while it runs, and reload the page once it has ended (the commits,
// proposals and the log then show). Without JS the page reloads itself (<noscript> refresh).
const panel = document.getElementById("dream-session");

function show(state) {
  const done = panel.querySelector("[data-session-done]");
  const todo = panel.querySelector("[data-session-todo]");
  if (done && state.passes_done) done.textContent = Object.keys(state.passes_done).join(", ") || "none yet";
  if (todo && state.passes_to_do) todo.textContent = state.passes_to_do.join(", ") || "none";
}

async function follow() {
  try {
    const response = await fetch(panel.dataset.status, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    const state = await response.json();
    if (!state.running) {
      window.location.reload();
      return;
    }
    if (state.status) show(state);
  } catch {
    // A hiccup (a restart, a lost connection): try again a little later.
  }
  setTimeout(follow, 5000);
}

if (panel && panel.hasAttribute("data-running")) setTimeout(follow, 5000);
