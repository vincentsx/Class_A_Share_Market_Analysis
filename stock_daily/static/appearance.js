/* Shared by the local workbench and self-contained public/offline reports. */
(() => {
  "use strict";
  const key = "guanshi-appearance";
  const valid = (value) => ["light", "dark", "system"].includes(value);
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try {
    const saved = localStorage.getItem(key);
    if (valid(saved)) preference = saved;
  } catch (_) { /* Restricted storage must not prevent changing appearance. */ }

  function apply() {
    const theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.appearance = preference;
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === preference));
    });
  }
  apply(); // Resolve before first paint, including on nonce-protected offline pages.
  document.addEventListener("DOMContentLoaded", apply, {once: true});
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-theme-choice]");
    if (!button || !valid(button.dataset.themeChoice)) return;
    preference = button.dataset.themeChoice;
    try { localStorage.setItem(key, preference); } catch (_) {}
    apply();
  });
  system.addEventListener("change", () => { if (preference === "system") apply(); });
  window.addEventListener("storage", (event) => {
    if (event.key !== key && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : "system";
    apply();
  });
})();
