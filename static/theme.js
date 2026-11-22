/* NextInQueue theme picker: festive Indian color themes, kept simple and
 * sober — a theme only swaps the accent palette (CSS variables in
 * styles.css); layout and content never change. The choice persists in
 * localStorage, and a small inline snippet in each page's <head> applies a
 * saved theme before first paint.
 */
(() => {
  "use strict";

  const STORAGE_KEY = "smartqueue.theme";
  const THEMES = [
    { id: "classic", label: "Classic · क्लासिक" },
    { id: "brand", label: "Brand · ब्रांड" },
    { id: "marigold", label: "Marigold · गेंदा" },
    { id: "sindoor", label: "Sindoor · सिंदूर" },
    { id: "rani", label: "Rani Pink · रानी गुलाबी" },
    { id: "peacock", label: "Peacock · मोर पंख" },
  ];

  let current = "classic";
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (THEMES.some((theme) => theme.id === saved)) current = saved;
  } catch (error) {
    current = "classic";
  }

  function applyTheme(id) {
    current = THEMES.some((theme) => theme.id === id) ? id : "classic";
    if (current === "classic") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = current;
    try {
      window.localStorage.setItem(STORAGE_KEY, current);
    } catch (error) {
      /* storage unavailable; the picker still works for this page view */
    }
    const picker = document.querySelector(".theme-picker");
    if (picker) picker.value = current;
  }

  function injectPicker() {
    if (document.querySelector(".theme-picker")) return;
    const host = document.querySelector(".header-actions") || document.querySelector(".header-inner");
    if (!host) return;
    const wrap = document.createElement("span");
    wrap.className = "theme-picker-wrap";
    const select = document.createElement("select");
    select.className = "theme-picker";
    select.setAttribute("aria-label", "Theme / थीम चुनें");
    for (const theme of THEMES) {
      const option = document.createElement("option");
      option.value = theme.id;
      option.textContent = theme.label;
      select.append(option);
    }
    select.value = current;
    select.addEventListener("change", () => applyTheme(select.value));
    wrap.append(select);
    const languagePicker = host.querySelector(".language-picker-wrap");
    if (languagePicker) host.insertBefore(wrap, languagePicker);
    else host.append(wrap);
  }

  applyTheme(current);
  injectPicker();

  window.smartQueueTheme = {
    setTheme: applyTheme,
    getTheme: () => current,
  };
})();
