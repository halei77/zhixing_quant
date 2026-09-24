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

// ── 2026-09-24 测试发现的三处，各钉一条 ──

test("选中票之后搜索区不许误报「没找到」", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator('[data-testid="quote"]')).toBeVisible({ timeout: 10_000 });
  // pick() 把 query 写成「代码 名称」当选择标签——那不是一次没搜到的搜索。
  // 不钉这条，选完票的搜索区会一直挂着「没找到」，而右边正在生成它的提示词。
  const panel = page.locator("section", { has: page.locator('[data-testid="search"]') });
  await expect(panel).not.toContainText("没找到");
});

test("超阈值警告不许把整页撑出横向滚动条", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });

  // 拉满自定义组合把 token 顶过 10 万，让 warn 出现（06 §八-5 的那条警告）
  await page.locator('[data-testid="templates"]').selectOption({ label: "自定义" });
  for (const label of ["日K", "60 分K", "30 分K", "5 分K"]) {
    await page.locator(`input[aria-label="${label} 天数"]`).fill("750");
    const box = page.locator("label", { hasText: label }).locator('input[type="checkbox"]');
    if (!(await box.isChecked())) await box.check();
  }
  await page.locator('[data-testid="generate"]').click();
  await expect(page.locator('[data-testid="warn"]')).toBeVisible({ timeout: 30_000 });

  // 判据是几何不是观感：warn 那行用过 truncate（= nowrap），长中文句按整行宽度进最小内容
  // 尺寸，撑破 minmax(0,6fr) 网格列——2026-09-24 实测 scrollWidth 1650 vs 1280（溢出 370px）。
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  // 顺带钉住自定义面板的天数框没被推出屏幕（溢出时它在 x≈1633）
  const days = page.locator('input[aria-label="日K 天数"]');
  await expect(days).toBeVisible();
  const box = await days.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x + box!.width).toBeLessThanOrEqual(1280);
});

test("只采到日K的票：空组件出交代、不整条拒（300308 实测）", async ({ page }) => {
  // 中际旭创有 701 天日线、分钟一段没有（ADR-0009 决定 8 未裁决，分钟只跑小样）。
  // 此前任一组件空就整条拒，默认模板在全市场只有池内五票能用。
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("中际旭创");
  await page.locator('[data-testid="suggest"] button').first().click();
  const body = page.locator('[data-testid="prompt-body"]');
  await expect(body).toContainText("你是资深", { timeout: 20_000 });
  await expect(body).toContainText("### 日K");
  await expect(body).toContainText("本节无数据");
  // 空节不许带表格骨架（会被读成"那天没涨跌"）
  await expect(page.locator('[data-testid="warn"]')).toHaveCount(0);
});

test("生成失败时正文区给出原因，不是空白", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  // 建仓价分析是 pending 模板：服务端拒，理由写在 waiting_on
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 15_000 });
  await page.locator('[data-testid="templates"]').selectOption("建仓价分析（未上线）");
  const body = page.locator('[data-testid="prompt-body"]');
  await expect(body).toContainText("还没落地", { timeout: 15_000 });
  await expect(body).not.toContainText("选一只票，点生成");
});

// ── 任务 #59 数据面扩三块的自测（用户要求「从接口，页面都要进行实际调用的测试」）──
// 断言只钉「上去了就不该消失」的形状：基本面头、三张盘上参考表段、1 分K 自定义入口。
// Forward PE 段（C 刀）上线后这里不该翻——它加段不减段。

test("每条 ready 模板都带最新基本面头 + 三张盘上参考表段（接口实调）", async ({ page }) => {
  // 走 /api/prompt 真调，不走页面点击：这一条验的是组装层契约，页面只是搬运
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  const r = await page.evaluate(async () => {
    const out: Record<string, string[]> = {};
    const t = await (await fetch("/api/templates")).json() as {
      templates: { name: string; status: string }[];
    };
    for (const tpl of t.templates.filter((x) => x.status === "ready")) {
      const res = await fetch("/api/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: "600519", template: tpl.name }),
      });
      const d = (await res.json()) as { text?: string };
      out[tpl.name] = (d.text ?? "")
        .split("\n")
        .filter((x) => x.startsWith("###"))
        .map((x) => x.slice(0, 24));
    }
    return out;
  });
  for (const [name, heads] of Object.entries(r)) {
    // 用 startsWith 不用 toContain：heads 是截断到 24 字的标题，toContain 对数组是精确匹配
    expect(
      heads.some((h) => h.startsWith("### 最新基本面")),
      `${name} 必须有最新基本面头，实际段题=${heads.join(" | ")}`,
    ).toBe(true);
    // 三张盘上参考表（ADR-0015 已接、A 刀挂进模板）：估值 / 涨跌停 / 大盘
    expect(heads.some((h) => h.includes("估值")), `${name} 必须有估值段`).toBe(true);
    expect(heads.some((h) => h.includes("涨跌停")), `${name} 必须有涨跌停段`).toBe(true);
    // 大盘段只服务短期/波段（剥 Beta，设计合成 §一 第 11 条）——长期投资是估值视角，
    // 设计里就没给它挂 index_daily，这里不许一刀切。
    if (name === "短期投资" || name === "波段") {
      expect(heads.some((h) => h.includes("大盘")), `${name} 必须有大盘段`).toBe(true);
    }
  }
});

test("基本面头是键值两列表、含市值与 PE，且口径行在（06 §四 基本信息）", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  const body = page.locator('[data-testid="prompt-body"]');
  await expect(body).toContainText("### 最新基本面", { timeout: 20_000 });
  await expect(body).toContainText("贵州茅台");
  await expect(body).toContainText("600519");
  // 估值四件套（ADR-0016 的字段清单）+ 口径行自证单位
  await expect(body).toContainText("PE");
  await expect(body).toContainText("PB");
  // 口径行写明不复权收盘与「—」的语义（ADR-0022 决定 1 / ADR-0016 决定 2、3）
  await expect(body).toContainText("不复权");
});

test("自定义组合能勾 1 分K，且天数封在 30 以内（B 刀接线）", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".dot-on")).toBeVisible({ timeout: 10_000 });
  await page.locator('[data-testid="search"]').fill("600519");
  await page.locator('[data-testid="suggest"] button').first().click();
  await expect(page.locator("pre")).toContainText("你是资深", { timeout: 20_000 });

  await page.locator('[data-testid="templates"]').selectOption({ label: "自定义" });
  // 1 分K 在自定义清单里（B 刀加的 DATASETS 项）
  const box = page.locator("label", { hasText: "1 分K" }).locator('input[type="checkbox"]');
  await expect(box).toBeVisible();
  await box.check();
  await page.locator('input[aria-label="1 分K 天数"]').fill("10");
  await page.locator('[data-testid="generate"]').click();
  await expect(page.locator('[data-testid="prompt-body"]')).toContainText("1 分K", { timeout: 20_000 });

  // 31 天必须被服务端拒（site/api 的 cap=30），壳不静默改写
  await page.locator('input[aria-label="1 分K 天数"]').fill("31");
  await page.locator('[data-testid="generate"]').click();
  await expect(page.locator('[data-testid="prompt-body"]')).toContainText(/31|1–30|越界/, {
    timeout: 15_000,
  });
});
