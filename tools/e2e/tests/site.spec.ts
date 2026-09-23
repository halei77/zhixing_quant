import { test, expect } from "@playwright/test";

// 冒烟：站点加载、关键元素在、口令鉴权流程走通、截图留档。
// 行情数据的正确性由 Python 侧对照测试保证（tests/test_site_prompt.py），这里只测壳。

test("页面加载且关键 UI 元素在位", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/知行/);
  await expect(page.locator("#search")).toBeVisible();
  await expect(page.locator(".seal")).toContainText("知行");
  // 提示词区在选票前隐藏（2026-09-22 重设计的 UX）：存在但不可见
  await expect(page.locator("#prompt-zone")).toBeAttached();
  await expect(page.locator("#templates")).toBeAttached();
});

test("无口令时明确提示而不是静默空转", async ({ page }) => {
  await page.goto("/");
  await page.locator("#search").fill("600519");
  // 无口令：候选拉取 401，状态条说清原因
  await expect(page.locator("#status")).toContainText("口令不对", { timeout: 5_000 });
});

test("带正确口令后搜索出候选并可选", async ({ page }) => {
  const token = process.env.ZX_E2E_TOKEN || "e2e-local-token";
  await page.goto("/");
  await page.locator("#token").fill(token);
  // 候选与模板在口令接上后自动加载
  await expect(page.locator("#auth-state")).toHaveClass(/on/, { timeout: 10_000 });
  await page.locator("#search").fill("600519");
  await expect(page.locator("#suggest button").first()).toBeVisible({ timeout: 10_000 });
  await page.screenshot({ path: "test-results/search-suggest.png", fullPage: false });
});

test("截图留档：整页", async ({ page }) => {
  await page.goto("/");
  await page.screenshot({ path: "test-results/page.png", fullPage: true });
});
