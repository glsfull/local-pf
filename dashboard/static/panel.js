"use strict";

// Dropdowns (<details data-dropdown>): one open at a time, close on outside click and Escape.
const dropdowns = document.querySelectorAll("[data-dropdown]");
document.addEventListener("click", (event) => {
  for (const dropdown of dropdowns) {
    if (dropdown.open && !dropdown.contains(event.target)) dropdown.open = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const dropdown of dropdowns) {
    if (dropdown.open) {
      dropdown.open = false;
      dropdown.querySelector("summary").focus();
    }
  }
});
for (const dropdown of dropdowns) {
  dropdown.addEventListener("toggle", () => {
    if (!dropdown.open) return;
    for (const other of dropdowns) {
      if (other !== dropdown && other.open && !other.contains(dropdown)) other.open = false;
    }
  });
}

// Filter forms submit on change.
for (const form of document.querySelectorAll("form[data-autosubmit]")) {
  form.addEventListener("change", (event) => {
    if (event.target.matches("input, select")) form.requestSubmit();
  });
}

// Check-all checkboxes.
for (const all of document.querySelectorAll("[data-check-all]")) {
  const table = all.closest("table");
  const boxes = () => table.querySelectorAll("[data-check]");
  all.addEventListener("change", () => {
    for (const box of boxes()) box.checked = all.checked;
  });
  table.addEventListener("change", (event) => {
    if (!event.target.matches("[data-check]")) return;
    const list = Array.from(boxes());
    all.checked = list.every((box) => box.checked);
    all.indeterminate = !all.checked && list.some((box) => box.checked);
  });
}

// Keyword column visibility, remembered in localStorage.
for (const menu of document.querySelectorAll("[data-column-menu]")) {
  const table = document.getElementById(menu.dataset.columnMenu);
  const storageKey = `columns:${menu.dataset.columnMenu}`;
  let hidden = [];
  try {
    hidden = JSON.parse(localStorage.getItem(storageKey) || "[]");
  } catch {
    hidden = [];
  }
  const apply = () => {
    for (const toggle of menu.querySelectorAll("[data-column-toggle]")) {
      const key = toggle.dataset.columnToggle;
      toggle.checked = !hidden.includes(key);
      for (const cell of table.querySelectorAll(`[data-column="${key}"]`)) {
        cell.classList.toggle("column-hidden", !toggle.checked);
      }
    }
  };
  menu.addEventListener("change", (event) => {
    const key = event.target.dataset.columnToggle;
    if (!key) return;
    hidden = event.target.checked ? hidden.filter((item) => item !== key) : [...hidden, key];
    try {
      localStorage.setItem(storageKey, JSON.stringify(hidden));
    } catch {
      // Storage may be disabled; the choice then lasts until reload.
    }
    apply();
  });
  if (table) apply();
}

// Cluster tree: collapsed by default once JS is available.
for (const toggle of document.querySelectorAll("[data-tree-toggle]")) {
  const children = document.getElementById(toggle.getAttribute("aria-controls"));
  if (!children) continue;
  const set = (expanded) => {
    toggle.setAttribute("aria-expanded", String(expanded));
    children.hidden = !expanded;
  };
  set(false);
  toggle.addEventListener("click", () => set(toggle.getAttribute("aria-expanded") !== "true"));
}

// Guest link: copy the current address.
for (const button of document.querySelectorAll("[data-copy-link]")) {
  button.addEventListener("click", async () => {
    const label = button.lastChild;
    const original = label.textContent;
    try {
      await navigator.clipboard.writeText(window.location.href);
      label.textContent = " Ссылка скопирована";
    } catch {
      label.textContent = ` ${window.location.href}`;
    }
    window.setTimeout(() => { label.textContent = original; }, 2500);
  });
}

// Schedule: the 24×7 grid is only needed for custom modes.
for (const select of document.querySelectorAll("[data-schedule-mode]")) {
  const grid = select.closest(".field").querySelector("[data-schedule-grid]");
  const update = () => { grid.hidden = select.value === "uniform_day"; };
  select.addEventListener("change", update);
  update();
  for (const button of grid.querySelectorAll("[data-schedule-all]")) {
    button.addEventListener("click", () => {
      for (const box of grid.querySelectorAll("input[type=checkbox]")) box.checked = button.dataset.scheduleAll === "1";
    });
  }
}

// Region select: city hint and custom region fields.
const regionSelect = document.querySelector("[data-region-select]");
if (regionSelect) {
  const hint = document.querySelector("[data-region-hint]");
  const custom = document.querySelector("[data-custom-region]");
  const update = () => {
    const option = regionSelect.selectedOptions[0];
    const isCustom = regionSelect.value === "custom";
    custom.hidden = !isCustom;
    for (const input of custom.querySelectorAll("input")) input.required = isCustom;
    hint.textContent = isCustom
      ? "Позиции будут собираться по коду региона, указанному ниже."
      : `Позиции будут собираться из города: ${option.dataset.city}.`;
  };
  regionSelect.addEventListener("change", update);
  update();
}

// «Добавить домен без .zone» follows the site address.
const siteUrl = document.querySelector("[data-site-url]");
const zone = document.querySelector("[data-zone]");
if (siteUrl && zone) {
  siteUrl.addEventListener("input", () => {
    const host = siteUrl.value.trim().replace(/^[a-z]+:\/\//i, "").split(/[/?#]/)[0];
    const parts = host.split(".").filter(Boolean);
    zone.textContent = `.${parts.length > 1 ? parts[parts.length - 1].toLowerCase() : "ru"}`;
  });
}

const notFound = document.querySelector("[data-not-found]");
const brandQueries = document.querySelector("[data-brand-queries]");
if (notFound && brandQueries) {
  notFound.addEventListener("change", (event) => {
    brandQueries.hidden = event.target.value !== "brand";
  });
}

const budgetToggle = document.querySelector("[data-budget-toggle]");
const budgetField = document.querySelector("[data-budget-field]");
if (budgetToggle && budgetField) {
  const update = () => { budgetField.hidden = !budgetToggle.checked; };
  budgetToggle.addEventListener("change", update);
  update();
}

// Keyword rows in the project form.
const keywords = document.querySelector("#keywords");
const template = document.querySelector("#keyword-template");
const addKeyword = document.querySelector("#add-keyword");

function applyHintType(row) {
  const brand = row.querySelector("[data-brand]");
  const needsBrand = row.querySelector("select[name=hint_type]").value === "brand";
  brand.hidden = !needsBrand;
  brand.querySelector("input").required = needsBrand;
}

if (keywords && template && addKeyword) {
  for (const row of keywords.querySelectorAll("[data-keyword]")) applyHintType(row);
  const maxKeywords = 100;
  addKeyword.disabled = keywords.children.length >= maxKeywords;

  keywords.addEventListener("change", (event) => {
    if (event.target.name === "hint_type") applyHintType(event.target.closest("[data-keyword]"));
  });

  keywords.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove]");
    if (!remove) return;
    const row = remove.closest("[data-keyword]");
    const next = row.nextElementSibling || row.previousElementSibling;
    row.remove();
    addKeyword.disabled = false;
    (next?.querySelector("input") || addKeyword).focus();
  });

  addKeyword.addEventListener("click", () => {
    const fragment = template.content.cloneNode(true);
    const row = fragment.querySelector("[data-keyword]");
    applyHintType(row);
    keywords.append(fragment);
    row.querySelector("input").focus();
    addKeyword.disabled = keywords.children.length >= maxKeywords;
  });
}

if (document.querySelector("[data-poll]")) {
  window.setTimeout(() => window.location.reload(), 3000);
}

// Visibility chart: Chart.js stacked bars per position tier plus «Топ 1-10» and «Видимость» lines.
const chartCanvas = document.querySelector("#visibility-chart");
if (chartCanvas && window.Chart) {
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const tiers = Array.from(document.querySelectorAll(".chart-legend .legend-box"), (box) => ({
    label: box.parentElement.textContent.trim(),
    color: getComputedStyle(box).backgroundColor,
  }));
  // Rows are ordered oldest first, so the chart reads left to right in time.
  const rows = Array.from(document.querySelectorAll("#visibility-data [data-day]"));
  const number = (value) => (value === "" || value === undefined ? null : Number(value));
  const bars = tiers.map((tier, index) => ({
    type: "bar",
    label: tier.label,
    backgroundColor: tier.color,
    stack: "positions",
    order: 2,
    data: rows.map((row) => {
      const counts = Array.from(row.querySelectorAll("[data-count]"), (cell) => Number(cell.dataset.count));
      const total = counts.reduce((sum, value) => sum + value, 0);
      return total ? (counts[index] * 100) / total : 0;
    }),
  }));
  const line = (label, key, lineColor) => ({
    type: "line",
    label,
    data: rows.map((row) => number(row.dataset[key])),
    borderColor: lineColor,
    backgroundColor: lineColor,
    borderWidth: 2,
    pointRadius: 3,
    tension: 0.25,
    spanGaps: true,
    stack: key,
    order: 1,
  });
  new window.Chart(chartCanvas, {
    data: {
      labels: rows.map((row) => row.dataset.day),
      datasets: [line("Видимость", "visibility", color("--tier-visibility")), line("Топ 1-10", "top10", color("--tier-top10")), ...bars],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: (item) => `${item.dataset.label}: ${item.parsed.y === null ? "—" : `${Math.round(item.parsed.y)}%`}` },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, min: 0, max: 100, ticks: { stepSize: 25, callback: (value) => `${value}%` } },
      },
    },
  });
}
