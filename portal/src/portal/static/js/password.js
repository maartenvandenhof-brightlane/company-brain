// "Show password" eye: hidden without JS; the field goes back to hidden before submit so the
// browser never stores the password as ordinary form history.
document.querySelectorAll("button[data-reveal]").forEach((button) => {
  const input = document.getElementById(button.dataset.reveal);
  if (!input) return;
  button.hidden = false;
  button.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.setAttribute("aria-pressed", String(show));
    button.setAttribute("aria-label", show ? "Hide password" : "Show password");
    input.focus();
  });
  input.form?.addEventListener("submit", () => { input.type = "password"; });
});
