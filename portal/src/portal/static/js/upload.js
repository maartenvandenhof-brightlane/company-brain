// Import upload (the form works as a plain file input without JS):
// - highlight the drop zone while a file is dragged over it;
// - show the chosen file's name and size, and warn before uploading a file over the limit;
// - on submit, show that the (possibly minute-long) parse is running.
const form = document.querySelector("form[data-upload]");
const input = form?.querySelector("input[type=file]");
const status = form?.querySelector("[data-file-status]");

function size(bytes) {
  return bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function tooBig() {
  const file = input.files?.[0];
  const max = Number(input.dataset.maxBytes || 0);
  return Boolean(file && max && file.size > max);
}

if (form && input && status) {
  for (const type of ["dragenter", "dragover"]) input.addEventListener(type, () => input.classList.add("is-over"));
  for (const type of ["dragleave", "drop"]) input.addEventListener(type, () => input.classList.remove("is-over"));

  input.addEventListener("change", () => {
    const file = input.files?.[0];
    status.classList.toggle("bad", tooBig());
    if (!file) status.textContent = "";
    else if (tooBig()) status.textContent = `${file.name} is ${size(file.size)}, over the limit. Upload the conversations.json inside the zip instead.`;
    else status.textContent = `${file.name}, ${size(file.size)}. Nothing is stored until you confirm the preview.`;
  });

  form.addEventListener("submit", (event) => {
    if (tooBig()) {
      event.preventDefault();
      input.focus();
      return;
    }
    const button = form.querySelector("button[type=submit]");
    if (!button) return;
    // after the submission has started, so the disabled button does not cancel it
    setTimeout(() => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = button.dataset.busyLabel || button.textContent;
    });
  });
}
