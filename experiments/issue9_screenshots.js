// Historical: examples/dashboard_demo.py was removed in issue #14 (no demo data); see experiments/issue14_screenshots.js.
// Capture the TopPult-style dashboard screens from a running demo.
//   python -m examples.dashboard_demo --port 8091
//   NODE_PATH="$(npm root -g)" node experiments/issue9_screenshots.js http://127.0.0.1:8091 docs/screenshots
const { chromium } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:8091";
const out = process.argv[3] || "docs/screenshots";
const pages = [
  ["projects", "/"],
  ["create", "/projects/new", async (page) => {
    await page.fill("[data-site-url]", "pfult.ru");
    await page.click("details.accordion > summary");
    await page.selectOption("select[name=schedule_mode]", "uniform_hours");
  }],
  ["keywords", "/projects/2/keywords"],
  ["clusters", "/projects/2/clusters", async (page) => {
    for (const button of await page.$$("button[aria-controls][aria-expanded='false']")) await button.click();
  }],
  ["positions", "/projects/2/positions"],
  ["visits", "/projects/2/visits?deleted=1"],
  ["schedule", "/projects/2/schedule"],
  ["expenses", "/projects/2/expenses"],
  ["help", "/help"],
  ["dropdown", "/", async (page) => { await page.click("header details.dropdown > summary"); }],
];

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const watch = (page) => {
    page.on("console", (message) => message.type() === "error" && errors.push(`${page.url()}: ${message.text()}`));
    page.on("pageerror", (error) => errors.push(`${page.url()}: ${error.message}`));
  };
  const desktop = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "ru-RU" });
  watch(desktop);
  for (const [name, path, action] of pages) {
    await desktop.goto(base + path, { waitUntil: "networkidle" });
    if (action) await action(desktop);
    await desktop.waitForTimeout(500);
    await desktop.screenshot({ path: `${out}/issue9-${name}.jpg`, quality: 80, fullPage: name !== "dropdown" });
  }
  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, locale: "ru-RU" });
  watch(mobile);
  for (const [name, path] of [["mobile-projects", "/"], ["mobile-positions", "/projects/2/positions"]]) {
    await mobile.goto(base + path, { waitUntil: "networkidle" });
    await mobile.waitForTimeout(500);
    await mobile.screenshot({ path: `${out}/issue9-${name}.jpg`, quality: 80, fullPage: true });
  }
  await browser.close();
  console.log(errors.length ? errors.join("\n") : "no console errors");
})();
