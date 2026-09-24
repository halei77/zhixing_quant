import { test, expect } from "@playwright/test";

// 新前端（Vue 3 + Vite，ADR-0018）的冒烟。旧的 site.spec.ts 绑的是 site/static 那套 DOM id
// （#search/#suggest/#prompt-zone/…），新前端除挂载点外一个 id 都不用，所以两套并行：
// 旧件退役（05 Q1-② 批准删除）时把 site.spec.ts 一起删。
//
// 定位一律走 data-testid：class 是视觉的，改样式不该弄红测试。
// 行情与提示词的数据正确性由 Python 侧对照测试保证（tests/test_site_prompt.py），这里只测壳。
// 口令鉴权已按 ADR-0019 撤销：没有口令框、没有口令可填，页面加载即 boot。

test("页面加载且关键控件在位、没有口令框", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle(/知行/);
  await expect(page.locator("#app")).toBeAttached();
  await expect(page.locator('[data-testid="search"]')).toBeVisible();
  // 口令框不许再出现（ADR-0019）
  await expect(page.locator('[data-testid="token"]')).toHaveCount(0);
  await expect(page.locator(".seal")).toContainText("知行");
});

test("不填任何口令直接搜出候选", async ({ page }) => {
  await page.goto("/");
  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[data-testid="suggest"] button').first()).toBeVisible({
    timeout: 10_000,
  });
  // 状态条不许出现鉴权话术
  await expect(page.locator('[role="status"]')).not.toContainText("口令");
});

test("页面加载完成即为已连接（无口令可填）", async ({ page }) => {
  await page.goto("/");
  // boot 不再等口令：模板与留痕加载成功，绿点亮起
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
});

test("搜出候选、可选、生成出提示词", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });

  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[data-testid="suggest"] button').first()).toBeVisible({
    timeout: 10_000,
  });
  await page.screenshot({ path: "test-results/vue-search-suggest.png" });

  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
  // 面板那一行由服务端 /api/kline 的 quote 渲染（docs/11 §六-2：壳不自己推涨跌幅）
  await expect(page.locator('[data-testid="quote"]')).toBeVisible({ timeout: 10_000 });
  await expect(page.locator('[data-testid="quote"]')).toContainText("%");
  await expect(page.locator('[data-testid="templates"]')).toBeVisible();
  await page.screenshot({ path: "test-results/vue-prompt.png", fullPage: true });
});

test("选过票之后留痕出现在页面上（06 §四）", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });

  await page.locator('[data-testid="search"]').fill("");
  await expect(page.getByText("最近搜过")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /茅台/ }).first()).toBeVisible();
});

// ── v2 交互（任务 #55）：键盘流、as_of 回溯、吸底操作条、模板组合展示 ──

test("键盘 Enter 选中第一条候选并生成提示词", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });

  await page.locator('[data-testid="search"]').fill("600519");
  await expect(page.locator('[data-testid="suggest"] [role="option"]')).toHaveCount(1, {
    timeout: 10_000,
  });
  // 首候选即默认高亮（aria-selected），Enter 直接选中，不必碰鼠标
  await expect(page.locator('[data-testid="suggest"] [aria-selected="true"]')).toHaveCount(1);
  await page.locator('[data-testid="search"]').press("Enter");
  await expect(page.locator('[data-testid="quote"]')).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
});

test("方向键在候选间移动高亮", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });

  await page.locator('[data-testid="search"]').fill("银行");
  await expect(page.locator('[data-testid="suggest"] [role="option"]').first()).toBeVisible({
    timeout: 10_000,
  });
  const box = page.locator('[data-testid="suggest"] [aria-selected="true"]');
  await expect(box).toHaveAttribute("id", "hit-0");
  await page.locator('[data-testid="search"]').press("ArrowDown");
  await expect(box).toHaveAttribute("id", "hit-1");
  await page.locator('[data-testid="search"]').press("ArrowUp");
  await expect(box).toHaveAttribute("id", "hit-0");
  // Esc 关掉候选，不选中任何东西
  await page.locator('[data-testid="search"]').press("Escape");
  await expect(page.locator('[data-testid="suggest"]')).toHaveCount(0);
});

test("吸底操作条在，生成后复制可用、空态禁用", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  // 未选票：操作条不渲染（没有可操作的上下文）
  await expect(page.locator('[data-testid="actions"]')).toHaveCount(0);

  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  const bar = page.locator('[data-testid="actions"]');
  await expect(bar).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
  // 生成出正文后复制才可用
  await expect(page.locator('[data-testid="copy"]')).toBeEnabled();
  // 吸底：操作条在长正文滚过视口后仍钉在视口内（sticky）
  await page.locator("pre").evaluate((el) => el.scrollTop = el.scrollHeight);
  const box = await bar.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeGreaterThan(300);
});

test("as_of 回溯：提示词数据末行退到截至日的最后交易日", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });

  // 选一个明显过去的交易日，重新生成后正文里出现的最大日期不应超过它
  await page.locator('[data-testid="asof"]').fill("2026-09-10");
  await page.locator('[data-testid="generate"]').click();
  await expect(page.locator("pre")).toContainText("2026-09-10", { timeout: 15_000 });
  await expect(page.locator("pre")).not.toContainText("2026-09-11");
  // 「回到今天」一键复位
  await page.locator('button[title="回到今天"]').click();
  await expect(page.locator('[data-testid="asof"]')).toHaveValue("");
});

test("模板的数据组合展示给读者（选模板不靠盲猜）", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
  // 默认模板的组合行：来自 /api/templates 的 data，壳只做中文标注。
  // 作用域钉在含模板选择器的那一节——页面有三节，裸 locator('section') 会撞 strict mode。
  const panel = page.locator("section", { has: page.locator('[data-testid="templates"]') });
  await expect(panel).toContainText("数据组合：");
  await expect(panel).toContainText("日K × 120");
});
