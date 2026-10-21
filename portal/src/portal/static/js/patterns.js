// The Patterns page. Without JS every form posts and comes back, and while a run goes the page
// reloads itself (<noscript> refresh). With it:
// - answers on a pattern are sent in place: the page's patterns are swapped for the fresh ones
//   the answer's redirect returns, and the page's status line says what was saved;
// - while chats are observed or patterns reassessed, GET /patterns/status is followed every
//   3 s, and when the run ends the patterns and the panel are swapped for fresh ones (no
//   reload: text typed in a note and folds opened stay).
const keep = document.getElementById("keep");
if (keep) {
  const SAVED = {
    agree: "Saved: you agree.",
    unsure: "Saved: you are not sure.",
    dispute: "Saved: you dispute this. It is now under Disputed by you.",
    correct: "Saved your wording.",
  };
  // The parts of the page that change with a run or an answer.
  const PARTS = ["coverage", "patterns", "keep"];

  function announce(text) {
    let line = document.getElementById("patterns-status");
    if (!line) {
      line = document.createElement("p");
      line.id = "patterns-status";
      line.className = "vh";
      line.setAttribute("role", "status");
      document.body.append(line);
    }
    line.textContent = text;
  }

  // Swap in the fresh page's parts, keeping what the owner has typed and opened.
  function swap(fresh, { clear = null } = {}) {
    const typed = new Map();
    document.querySelectorAll(".pattern-answer textarea").forEach((t) => {
      if (t.value && t !== clear) typed.set(t.id, t.value);
    });
    const open = new Set(
      [...document.querySelectorAll(".pattern details[open]")].map(
        (d) => `${d.closest(".pattern").id} ${d.className}`,
      ),
    );
    if (document.getElementById("retired")?.open) open.add("retired");
    for (const id of PARTS) {
      const old = document.getElementById(id);
      const next = fresh.getElementById(id);
      if (old && next) old.replaceWith(document.adoptNode(next));
    }
    typed.forEach((value, id) => {
      const t = document.getElementById(id);
      if (t) {
        t.value = value;
        t.closest("details").open = true;
      }
    });
    document.querySelectorAll(".pattern details").forEach((d) => {
      if (open.has(`${d.closest(".pattern").id} ${d.className}`)) d.open = true;
    });
    const retired = document.getElementById("retired");
    if (retired && open.has("retired")) retired.open = true;
    follow();
  }

  async function freshPage(response) {
    return new DOMParser().parseFromString(await response.text(), "text/html");
  }

  // Answers, in place.
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form.pattern-answer");
    const button = event.submitter;
    if (!form || !button || button.name !== "verdict" || form.dataset.plain) return;
    const note = form.querySelector("textarea");
    if (button.value === "correct" && note.value.trim().length < 3) {
      event.preventDefault();
      note.setCustomValidity("Write the pattern in your own words first.");
      note.reportValidity();
      note.addEventListener("input", () => note.setCustomValidity(""), { once: true });
      return;
    }
    event.preventDefault();
    const card = form.closest(".pattern");
    const buttons = form.querySelectorAll("button");
    // Read the form first: a disabled button is left out of it, and this one carries the answer.
    const body = new URLSearchParams(new FormData(form, button));
    buttons.forEach((b) => { b.disabled = true; });
    let fresh;
    try {
      const response = await fetch(form.action, { method: "POST", body, credentials: "same-origin" });
      if (!response.ok || !response.redirected) throw new Error(String(response.status));
      fresh = await freshPage(response);
      if (!fresh.getElementById("patterns")) throw new Error("not the Patterns page");
    } catch {
      // Let the server say what went wrong (an expired form, a lost session): send it the
      // plain way, with the button that was pressed.
      form.dataset.plain = "1";
      buttons.forEach((b) => { b.disabled = false; });
      form.requestSubmit(button);
      return;
    }
    const refused = fresh.querySelector(".notices .callout.bad");
    if (refused) {
      buttons.forEach((b) => { b.disabled = false; });
      announce(refused.textContent.trim());
      const old = document.querySelector(".notices");
      const notices = document.adoptNode(refused.closest(".notices"));
      if (old) old.replaceWith(notices);
      else document.getElementById("coverage").after(notices);
      return;
    }
    swap(fresh, { clear: note });
    announce(SAVED[button.value] || "Your answer is saved.");
    // Back to the same answer on the card, wherever the card is now.
    const again = document.getElementById(card.id);
    const target = again?.querySelector(`button[name="verdict"][value="${button.value}"]`);
    if (target) target.focus();
  });

  // A run: follow it, then swap.
  let following = false;

  function showBackfill(job) {
    const block = document.querySelector("[data-backfill]");
    if (!block || !job) return;
    const bar = block.querySelector("progress");
    bar.max = job.total || 1;
    bar.value = job.handled;
    block.querySelector("[data-handled]").textContent = job.handled;
    block.querySelector("[data-total]").textContent = job.total;
    block.querySelector("[data-cost]").textContent = `€${job.cost_eur.toFixed(3)}`;
  }

  function showReassess(dream) {
    const panel = document.querySelector("[data-reassess]");
    if (!panel || !dream || dream.steps[0] !== "Patterns") return;
    panel.querySelectorAll(".stepper li").forEach((li, i) => {
      const n = i + 1;
      li.classList.toggle("done", n < dream.step || (dream.finished && !dream.error && n === dream.step));
      li.classList.toggle("failed", Boolean(dream.finished && dream.error && n === dream.step));
      if (n === dream.step) li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    });
    panel.querySelector("[data-progress-text]").textContent = dream.text;
    panel.querySelector("[data-progress-time]").textContent = `${dream.seconds} s`;
  }

  async function poll() {
    const panel = document.getElementById("keep");
    try {
      const response = await fetch(panel.dataset.status, { credentials: "same-origin" });
      if (!response.ok) throw new Error(String(response.status));
      const status = await response.json();
      showBackfill(status.backfill);
      showReassess(status.dream);
      const busy = (status.backfill && status.backfill.running) || status.dreaming;
      if (!busy) {
        const page = await fetch(window.location.pathname, { credentials: "same-origin" });
        if (page.ok) {
          following = false;
          swap(await freshPage(page));
          announce("The run has ended. The page shows the latest.");
          return;
        }
      }
    } catch {
      // A hiccup (a restart, a lost connection): try again a little later.
    }
    setTimeout(poll, 3000);
  }

  function follow() {
    const panel = document.getElementById("keep");
    if (following || !panel || !panel.hasAttribute("data-running")) return;
    following = true;
    setTimeout(poll, 3000);
  }

  follow();
}
