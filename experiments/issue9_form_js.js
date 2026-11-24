// Check project-form behaviour that only exists in the browser.
//   NODE_PATH="$(npm root -g)" node experiments/issue9_form_js.js http://127.0.0.1:8091
const { chromium } = require("playwright");
const assert = require("assert");

const base = process.argv[2] || "http://127.0.0.1:8091";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(base + "/projects/new");
  const custom = page.locator("input[name=region_custom]");

  // A stale invalid custom code must not block submit after switching back to a known region.
  await page.selectOption("[data-region-select]", "custom");
  assert.strictEqual(await custom.isDisabled(), false);
  await custom.fill("abc");
  await page.selectOption("[data-region-select]", "213");
  assert.strictEqual(await custom.isDisabled(), true);
  const invalid = await page.evaluate(() => [...document.querySelectorAll("#project-form :invalid")].map((field) => field.name));
  assert.ok(!invalid.includes("region_custom"), `invalid fields: ${invalid}`);

  // The brand field is required only while no default brand query is set.
  const row = page.locator("[data-keyword]").first();
  await row.locator("select[name=hint_type]").selectOption("brand");
  const brand = row.locator("input[name=brand]");
  await brand.fill("");
  assert.strictEqual(await brand.evaluate((input) => input.required), true);
  await page.click("details.accordion > summary");
  await page.locator("input[name=not_found][value=brand]").check({ force: true });
  await page.fill("#brand-queries", "бобр");
  assert.strictEqual(await brand.evaluate((input) => input.required), false);

  await browser.close();
  console.log("form checks passed");
})().catch((error) => { console.error(error); process.exit(1); });
