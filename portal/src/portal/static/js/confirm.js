// Forms with data-confirm="question" ask before they are sent (removing a key).
document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
