const keywords = document.querySelector("#keywords");
const template = document.querySelector("#keyword-template");
const addKeyword = document.querySelector("#add-keyword");

function applyHintType(row) {
  const brand = row.querySelector("[data-brand]");
  const needsBrand = row.querySelector("select").value === "brand";
  brand.hidden = !needsBrand;
  brand.querySelector("input").required = needsBrand;
}

if (keywords) {
  for (const row of keywords.querySelectorAll("[data-keyword]")) {
    applyHintType(row);
  }
  const maxKeywords = 100;
  addKeyword.disabled = keywords.children.length >= maxKeywords;

  keywords.addEventListener("change", (event) => {
    if (event.target.name === "hint_type") {
      applyHintType(event.target.closest("[data-keyword]"));
    }
  });

  keywords.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove]");
    if (remove) {
      const row = remove.closest("[data-keyword]");
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      addKeyword.disabled = false;
      (next?.querySelector("input") || addKeyword).focus();
    }
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

const chart = document.querySelector("#visibility-chart");
if (chart) {
  const modes = document.querySelector("[data-chart-modes]");
  const context = chart.getContext("2d");
  const colors = Array.from(document.querySelectorAll(".chart-legend span"), (legend) =>
    getComputedStyle(legend).getPropertyValue("--tier-chart").trim()
  );
  const points = Array.from(document.querySelectorAll("#visibility-data [data-day]"), (row) => ({
    day: row.dataset.day,
    total: Number(row.cells[1].textContent),
    counts: Array.from(row.querySelectorAll("[data-count]"), (cell) => Number(cell.dataset.count)),
  }));
  let mode = "percent";

  function drawVisibility() {
    const width = chart.clientWidth;
    const height = chart.clientHeight;
    const scale = window.devicePixelRatio || 1;
    chart.width = width * scale;
    chart.height = height * scale;
    context.scale(scale, scale);
    const left = 44;
    const top = 14;
    const bottom = height - 27;
    const right = width - 12;
    const step = (right - left) / points.length;
    const barWidth = Math.min(48, Math.max(4, step * .68));
    const percent = mode === "percent";
    const maximum = percent ? 100 : Math.ceil(Math.max(...points.map((point) => point.total)) / 4) * 4;
    context.font = "12px system-ui";
    context.textBaseline = "middle";
    for (let index = 0; index <= 4; index++) {
      const y = bottom - (bottom - top) * index / 4;
      const value = maximum * index / 4;
      context.fillStyle = "#dbe3ee";
      context.fillRect(left, Math.round(y), right - left, 1);
      context.textAlign = "right";
      context.fillStyle = "#53637a";
      context.fillText(percent ? `${value}%` : String(value), left - 8, y);
    }
    const labelInterval = Math.ceil(39 / step);
    points.forEach((point, index) => {
      const x = left + step * (index + .5);
      let y = bottom;
      for (const [tier, count] of point.counts.entries()) {
        const value = percent && point.total ? count * 100 / point.total : count;
        const barHeight = (bottom - top) * value / maximum;
        context.fillStyle = colors[tier];
        context.fillRect(x - barWidth / 2, y - barHeight, barWidth, barHeight);
        y -= barHeight;
      }
      if (index % labelInterval === 0) {
        context.textAlign = "center";
        context.fillStyle = "#53637a";
        context.fillText(point.day, x, bottom + 16);
      }
    });
  }

  modes.hidden = false;
  modes.addEventListener("click", (event) => {
    const button = event.target.closest("[data-chart-mode]");
    if (!button) return;
    mode = button.dataset.chartMode;
    for (const option of modes.querySelectorAll("button")) {
      option.setAttribute("aria-pressed", String(option === button));
    }
    drawVisibility();
  });
  new ResizeObserver(drawVisibility).observe(chart);
  drawVisibility();
}
