// Idea buttons (home and the Ideas tab): answer in place instead of reloading the page. The
// form still works without JS (POST, then back to the page it came from). Useful toggles; the
// other answers take the card away and say so in the page's status line.
const GONE = {
  not_useful: "Marked not useful.",
  dismissed: "Dismissed.",
  done: "Marked done.",
  already_doing: "Marked as already doing.",
  "snooze:7": "Snoozed for 7 days.",
  "snooze:30": "Snoozed for 30 days.",
};

function announce(text) {
  let line = document.getElementById("idea-status");
  if (!line) {
    line = document.createElement("p");
    line.id = "idea-status";
    line.className = "vh";
    line.setAttribute("role", "status");
    document.body.append(line);
  }
  line.textContent = text;
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form.idea-actions");
  const button = event.submitter;
  if (!form || !button || !["state", "snooze"].includes(button.name) || form.dataset.plain) return;
  event.preventDefault();
  const card = form.closest(".idea");
  const buttons = form.querySelectorAll("button");
  // Read the form first: a disabled button is left out of it, and this one carries the answer.
  const body = new URLSearchParams(new FormData(form, button));
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const response = await fetch(form.action, { method: "POST", body, credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
  } catch {
    // Let the server say what went wrong (an expired form, a lost session): send it the
    // plain way, with the button that was pressed.
    form.dataset.plain = "1";
    buttons.forEach((b) => { b.disabled = false; });
    form.requestSubmit(button);
    return;
  }
  const state = button.name === "snooze" ? `snooze:${button.value}` : button.value;
  if (GONE[state]) {
    card.classList.add("is-leaving");
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setTimeout(() => card.remove(), reduce ? 0 : 240);
    announce(GONE[state]);
    return;
  }
  const on = state === "useful";
  button.value = on ? "new" : "useful";
  button.setAttribute("aria-pressed", String(on));
  button.classList.toggle("is-on", on);
  card.dataset.state = on ? "useful" : "new";
  buttons.forEach((b) => { b.disabled = false; });
  announce(on ? "Marked useful." : "No longer marked useful.");
});

// Ideating (the Ideas tab): follow the run through GET /ideas/status and, when it ends, swap
// in the new ideas and runs from a fresh copy of the page (no reload, so text typed in a form
// stays). Without JS the page reloads itself (<noscript> refresh).
const panel = document.getElementById("ideas-progress");

function show(status) {
  const steps = panel.querySelectorAll(".stepper li");
  steps.forEach((li, i) => {
    const n = i + 1;
    const failed = status.finished && status.error && n === status.step;
    li.classList.toggle("done", n < status.step || (status.finished && !status.error && n === status.step));
    li.classList.toggle("failed", Boolean(failed));
    if (n === status.step) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  });
  const bar = panel.querySelector("progress");
  if (bar) {
    if (status.finished) bar.remove();
    else if (status.step === 1 && status.total) {
      bar.max = status.total;
      bar.value = status.done;
    } else bar.removeAttribute("value");
  }
  panel.querySelector("[data-progress-text]").textContent = status.text;
  panel.querySelector("[data-progress-time]").textContent = `${status.seconds} s`;
  panel.classList.toggle("failed", Boolean(status.error));
}

async function refreshIdeas() {
  const response = await fetch(window.location.pathname, { credentials: "same-origin" });
  if (!response.ok) return;
  const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
  for (const id of ["ideas", "follow-ups", "runs"]) {
    const old = document.getElementById(id);
    const next = fresh.getElementById(id);
    if (old && next) old.replaceWith(next);
  }
  // The ideas section came back with its own (finished) progress panel: nothing to follow.
}

async function follow() {
  try {
    const response = await fetch(panel.dataset.status, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    const status = await response.json();
    if (status.step) show(status);
    if (status.finished) {
      await refreshIdeas();
      return;
    }
  } catch {
    // A hiccup (a restart, a lost connection): try again a little later.
  }
  setTimeout(follow, 1500);
}

if (panel && panel.hasAttribute("data-running")) setTimeout(follow, 800);
