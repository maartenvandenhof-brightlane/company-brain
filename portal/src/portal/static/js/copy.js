// Copy buttons (dashboard, Patterns): <button class="copy" data-copy="…">. Falls back to a
// prompt. "Copied" shows on the button and is said in a status line for screen readers.
function announce(text) {
  let line = document.getElementById("copy-status");
  if (!line) {
    line = document.createElement("p");
    line.id = "copy-status";
    line.className = "vh";
    line.setAttribute("role", "status");
    document.body.append(line);
  }
  // Emptied first, so copying again is said again.
  line.textContent = "";
  setTimeout(() => { line.textContent = text; }, 50);
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button.copy[data-copy]");
  if (!button) return;
  event.preventDefault();
  const label = button.querySelector("[data-copy-label]");
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    button.setAttribute("data-copied", "");
    if (label) label.textContent = "Copied";
    announce("Copied to the clipboard.");
    setTimeout(() => {
      button.removeAttribute("data-copied");
      if (label) label.textContent = "Copy";
    }, 1500);
  } catch {
    window.prompt("Copy this:", button.dataset.copy);
  }
});
