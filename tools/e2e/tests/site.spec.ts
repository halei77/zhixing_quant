import { test, expect } from "@playwright/test";

// 冒烟：站点加载、关键元素在、无口令即可用、截图留档（口令鉴权已按 ADR-0019 撤销）。
// 行情数据的正确性由 Python 侧对照测试保证（tests/test_site_prompt.py），这里只测壳。

test("页面加载且关键 UI 元素在位", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/知行/);
  await expect(page.locator("#search")).toBeVisible();
  await expect(page.locator(".seal")).toContainText("知行");
  // 口令框不许再出现（ADR-0019）
  await expect(page.locator("#token")).toHaveCount(0);
  // 提示词区在选票前隐藏（2026-09-22 重设计的 UX）：存在但不可见
  await expect(page.locator("#prompt-zone")).toBeAttached();
  await expect(page.locator("#templates")).toBeAttached();
});

test("不填任何口令就能搜出候选", async ({ page }) => {
  await page.goto("/");
  // 页面加载即 boot（无口令可填）：连接点转为已连接
  await expect(page.locator("#auth-state")).toHaveClass(/on/, { timeout: 10_000 });
  await page.locator("#search").fill("600519");
  await expect(page.locator("#suggest button").first()).toBeVisible({ timeout: 10_000 });
  // 状态条不许出现鉴权话术
  await expect(page.locator("#status")).not.toContainText("口令");
});

test("搜出候选并可选、模板下拉有货", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#auth-state")).toHaveClass(/on/, { timeout: 10_000 });
  await page.locator("#search").fill("600519");
  await expect(page.locator("#suggest button").first()).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#templates option").first()).toBeAttached({ timeout: 10_000 });
  await page.screenshot({ path: "test-results/search-suggest.png", fullPage: false });
});

test("截图留档：整页", async ({ page }) => {
  await page.goto("/");
  await page.screenshot({ path: "test-results/page.png", fullPage: true });
});
