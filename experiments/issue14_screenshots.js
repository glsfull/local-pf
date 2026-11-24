// Capture the issue #14 screens on an empty panel: no demo data, profiles and proxies, DeepSeek core collection.
//   python experiments/issue14_panel.py --port 8092     (DeepSeek answers are canned, the key is fake)
//   NODE_PATH="$(npm root -g)" node experiments/issue14_screenshots.js http://127.0.0.1:8092 docs/screenshots
const { chromium } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:8092";
const out = process.argv[3] || "docs/screenshots";

const submit = (page, selector) => Promise.all([page.waitForNavigation(), page.click(selector)]);

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "ru-RU" });
  page.on("console", (message) => message.type() === "error" && errors.push(`${page.url()}: ${message.text()}`));
  page.on("pageerror", (error) => errors.push(`${page.url()}: ${error.message}`));
  const shot = (name, fullPage = true) => page.screenshot({ path: `${out}/issue14-${name}.png`, fullPage });

  // A fresh panel has no projects, phrases, keys, profiles or proxies.
  await page.goto(base + "/", { waitUntil: "networkidle" });
  await shot("empty-panel", false);

  // Settings: bulk upload of profiles and proxies, DeepSeek key (masked after saving).
  await page.goto(base + "/settings", { waitUntil: "networkidle" });
  await page.fill("#profile-bulk", 'yandex-01\nyandex-02\nyandex-03;[{"name": "yandexuid", "value": "1234567890", "domain": ".yandex.ru"}]');
  await submit(page, "form[action$='/settings/profiles'] button[type=submit]");
  await page.fill("#proxy-bulk", "http://user:secret@203.0.113.10:8000\nsocks5://user:secret@203.0.113.11:1080\n203.0.113.12:3128");
  await submit(page, "form[action$='/settings/proxies'] button[type=submit]");
  await page.fill("#deepseek-api-key", "sk-not-a-real-key-0000abcd");
  await submit(page, "form.form-card button.primary");
  const settings = await page.content();
  if (settings.includes("secret") || settings.includes("sk-not-a-real-key-0000abcd")) throw new Error("secret is shown");
  if (!settings.includes("abcd")) throw new Error("masked key is missing");
  await shot("settings");

  // A project entered by the user, then «Сбор ядра (ИИ)».
  await page.goto(base + "/projects/new", { waitUntil: "networkidle" });
  await page.fill("#project-url", "https://potolki.example.ru/");
  await page.fill("#project-name", "Натяжные потолки");
  await page.fill("[data-keyword] input[name=phrase]", "натяжные потолки");
  await page.selectOption("[data-keyword] select[name=hint_type]", "domain");
  await submit(page, "#project-form button[type=submit]");
  const project = page.url().replace(/\/$/, "");
  await page.goto(project + "/semantic", { waitUntil: "networkidle" });
  await page.fill("#semantic-seeds", "натяжные потолки, монтаж, цены, потолки с подсветкой");
  await page.fill("#semantic-count", "10");
  await submit(page, "form[action$='/semantic'] button[type=submit]");
  await page.waitForSelector("input[name=pick]");
  await shot("semantic");

  // Add the chosen phrases to the project keywords.
  const picks = await page.$$("input[name=pick]:not([disabled])");
  for (const pick of picks.slice(0, 5)) await pick.check();
  await submit(page, "form[action*='/semantic/'] button[type=submit]");
  await page.goto(project + "/keywords", { waitUntil: "networkidle" });
  await shot("keywords-added", false);

  await browser.close();
  console.log(errors.length ? errors.join("\n") : "no console errors");
})();
