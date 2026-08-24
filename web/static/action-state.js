(() => {
  "use strict";

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-api-action]");
    if (!form || form.dataset.submitting === "true") {
      if (form) event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    document.querySelectorAll("form[data-api-action] button[type='submit']").forEach((button) => {
      button.disabled = true;
      button.setAttribute("aria-disabled", "true");
    });
    const status = document.createElement("span");
    status.className = "action-wait";
    status.setAttribute("role", "status");
    status.innerHTML = '<span class="action-spinner" aria-hidden="true"></span><span></span>';
    status.lastElementChild.textContent = form.dataset.waitText || "Bitte warten …";
    form.appendChild(status);
  });
})();
