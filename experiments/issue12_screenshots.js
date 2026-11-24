// Capture the issue #12 screens from a running demo and walk the agent flow end to end.
//   python -m examples.dashboard_demo --port 8091 --balance 1500
//   NODE_PATH="$(npm root -g)" node experiments/issue12_screenshots.js http://127.0.0.1:8091 docs/screenshots
const { chromium } = require("playwright");

const base = process.argv[2] || "http://127.0.0.1:8091";
const out = process.argv[3] || "docs/screenshots";

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: "ru-RU" });
  page.on("console", (message) => message.type() === "error" && errors.push(`${page.url()}: ${message.text()}`));
  page.on("pageerror", (error) => errors.push(`${page.url()}: ${error.message}`));
  const shot = (name, fullPage = true) => page.screenshot({ path: `${out}/issue12-${name}.jpg`, quality: 80, fullPage });

  // Settings: YANDEX_API_KEY / YANDEX_FOLDER_ID, the key is masked after saving.
  await page.goto(base + "/settings", { waitUntil: "networkidle" });
  await page.fill("input[name=yandex_api_key]", "AQVNdemo0000000000key1234");
  await page.fill("input[name=yandex_folder_id]", "b1gdemofolder");
  await Promise.all([page.waitForNavigation(), page.click("form button.primary")]);
  const settings = await page.content();
  if (!settings.includes("••••1234") || settings.includes("AQVNdemo0000000000key1234")) throw new Error("key is not masked");
  await shot("settings");

  // Positions tab: «📸 Снять позиции» and «🚀 Запустить агента».
  await page.goto(base + "/projects/2/positions", { waitUntil: "networkidle" });
  await shot("positions-toolbar", false);

  // Tools menu with «Агент» and «Окно агента (noVNC)».
  await page.click("nav.topnav details.dropdown > summary");
  await page.waitForTimeout(300);
  await shot("dropdown", false);

  // Agent page: start for all phrases, wait for the captcha, press «Продолжить», wait for the end.
  await page.goto(base + "/agent", { waitUntil: "networkidle" });
  await page.selectOption("#agent-target", "2:");
  await Promise.all([page.waitForNavigation(), page.click("form[action$='/agent/start'] button")]);
  await page.waitForSelector(".agent-state.status-CAPTCHA", { timeout: 20000 });
  await page.waitForSelector("[data-agent-resume]:not([hidden])");
  await page.waitForTimeout(300);
  await shot("agent-captcha");
  await page.click("[data-agent-resume] button");
  await page.waitForSelector(".agent-state.status-DONE", { timeout: 60000 });
  await page.waitForTimeout(500);
  await shot("agent-done");
  const rows = await page.$$eval("[data-agent-keywords] tr", (items) => items.map((row) => row.innerText));
  console.log(rows.join("\n"));

  await browser.close();
  console.log(errors.length ? errors.join("\n") : "no console errors");
})();
