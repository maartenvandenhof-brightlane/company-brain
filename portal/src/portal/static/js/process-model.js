// Import page: each model menu is hidden without JS (a <noscript> list offers the other
// models); with JS, picking a model points that block's LibreChat button at it.
for (const processModel of document.querySelectorAll("select[data-process]")) {
  const processLink = processModel.closest(".process")?.querySelector("[data-process-link]");
  if (!processLink) continue;
  const label = processLink.querySelector("[data-process-label]");
  processModel.hidden = false;
  processModel.addEventListener("change", () => {
    processLink.href = processModel.value;
    if (label) label.textContent = processModel.selectedOptions[0].textContent;
  });
}
