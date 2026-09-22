// 知行 · 提示词站点前端（2026-09-22 重设计：typeahead / K线 canvas / 玻璃层）。
// 零依赖、无构建。页面上不算任何东西：token 数、口径句全来自服务器（ADR-0013 决定 5）。
// 这里只做：把输入变成请求、把响应变成 DOM、画K线、记口令、按复制。

const TOKEN_KEY = "zx.site.token";
const $ = (id) => document.getElementById(id);

const state = { code: null, name: null, kind: "stock", bars: [], text: "", busy: false };

function token() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

async function call(method, path, body) {
  const headers = { "X-Token": token() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? null : JSON.stringify(body),
  });
  if (response.status === 401) {
    setAuth("off");
    say("口令不对，填好口令再操作");
    throw new Error("401");
  }
  if (!response.ok) {
    const problem = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(problem.detail || String(response.status));
  }
  setAuth("on");
  return response.json();
}

function setAuth(stateName) {
  $("auth-state").className = `dot ${stateName}`;
  $("auth-state").title = stateName === "on" ? "已连接" : "未连接";
}

function say(text) {
  $("status").textContent = text;
}

// ── 搜索与候选（typeahead）─────────────────────────────

let typing = 0;
async function search() {
  clearTimeout(typing);
  typing = setTimeout(async () => {
    const query = $("search").value.trim();
    if (!query) return closeSuggest();
    try {
      const hits = (await call("GET", `/api/search?q=${encodeURIComponent(query)}`)).hits;
      renderSuggest(hits.slice(0, 12));
    } catch (error) {
      if (error.message !== "401") say(`搜索失败：${error.message}`);
    }
  }, 160);
}

function renderSuggest(hits) {
  const list = $("suggest");
  list.replaceChildren();
  if (!hits.length) {
    list.hidden = true;
    say("没找到——试试代码、名称或拼音首字母");
    return;
  }
  say("");
  for (const hit of hits) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    const label = document.createElement("span");
    label.textContent = `${hit.code} ${hit.name}`;
    const kind = document.createElement("span");
    kind.className = `kind${hit.kind === "index" ? " index" : ""}`;
    kind.textContent = hit.kind === "index" ? "指数" : "个股";
    button.append(label, kind);
    button.addEventListener("click", () => {
      closeSuggest();
      $("search").value = `${hit.code} ${hit.name}`;
      pick(hit);
    });
    li.append(button);
    list.append(li);
  }
  list.hidden = false;
}

function closeSuggest() {
  $("suggest").hidden = true;
}

function pick(hit) {
  state.code = hit.code;
  state.name = hit.name;
  state.kind = hit.kind;
  // 名字先落位再等数据：无数据的票也立刻看得见"选的是谁"，而不是上一只的名字
  $("quote-card").hidden = false;
  $("prompt-zone").hidden = state.kind !== "stock";
  $("q-name").textContent = hit.name;
  $("q-code").textContent = hit.code;
  loadQuote();
  call("POST", "/api/recent", { code: hit.code }).catch(() => {});
}

// ── 行情与K线（canvas 手绘，零依赖）────────────────────

async function loadQuote() {
  say("加载行情…");
  try {
    const body = await call("GET", `/api/kline?code=${encodeURIComponent(state.code)}&kind=${state.kind}&days=120`);
    state.bars = body.bars;
    if (!state.bars.length) {
      say(`${state.name} 在盘上还没有日线数据——它在回填队列里，晚些再看`);
      return;
    }
    renderQuote();
    drawKline(state.bars);
    say("");
  } catch (error) {
    if (error.message !== "401") say(`行情加载失败：${error.message}`);
  }
}

function renderQuote() {
  const bars = state.bars;
  if (!bars.length) return;
  const last = bars[bars.length - 1];
  const prev = bars.length > 1 ? bars[bars.length - 2] : null;
  const change = prev ? ((last.close - prev.close) / prev.close) * 100 : 0;
  const cls = change >= 0 ? "up" : "down";
  $("q-name").textContent = state.name;
  $("q-code").textContent = state.code;
  $("q-close").textContent = last.close.toFixed(2);
  $("q-close").className = `q-close ${cls}`;
  $("q-change").textContent = `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
  $("q-change").className = `q-change ${cls}`;
  $("q-open").textContent = last.open.toFixed(2);
  $("q-high").textContent = last.high.toFixed(2);
  $("q-low").textContent = last.low.toFixed(2);
  $("q-vol").textContent = last.volume == null ? "—" : Math.round(last.volume).toLocaleString("zh-CN");
}

function drawKline(bars) {
  const canvas = $("kline");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = 260;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  if (bars.length < 2) return;

  const view = bars.slice(-90); // 最多画 90 根：手机上再密就糊了
  const pad = { top: 10, right: 6, bottom: 34, left: 6 };
  const highs = view.map((b) => b.high);
  const lows = view.map((b) => b.low);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const span = max - min || 1;
  const plotH = height - pad.top - pad.bottom;
  const step = (width - pad.left - pad.right) / view.length;
  const bodyW = Math.max(2, step * 0.62);
  const y = (price) => pad.top + (1 - (price - min) / span) * plotH;

  // 分位线：三横一淡，够了
  ctx.strokeStyle = "rgba(128,138,155,0.18)";
  ctx.fillStyle = "rgba(128,138,155,0.75)";
  ctx.font = "10px -apple-system, sans-serif";
  for (let i = 0; i <= 3; i++) {
    const price = min + (span * i) / 3;
    const yy = y(price);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    ctx.fillText(price.toFixed(2), pad.left + 2, yy - 3);
  }

  view.forEach((b, i) => {
    const x = pad.left + step * (i + 0.5);
    const rising = b.close >= b.open;
    const color = rising ? "#e8463f" : "#1fa97a"; // A股：红涨绿跌
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    // 影线
    ctx.beginPath();
    ctx.moveTo(x, y(b.high));
    ctx.lineTo(x, y(b.low));
    ctx.stroke();
    // 实体
    const top = y(Math.max(b.open, b.close));
    const bottom = y(Math.min(b.open, b.close));
    if (rising) {
      ctx.strokeRect(x - bodyW / 2, top, bodyW, Math.max(1, bottom - top));
    } else {
      ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(1, bottom - top));
    }
  });

  // 末尾日期标注
  ctx.fillStyle = "rgba(128,138,155,0.75)";
  ctx.fillText(view[view.length - 1].date, width - pad.right - 64, height - pad.bottom + 16);
  ctx.fillText(view[0].date, pad.left, height - pad.bottom + 16);
}

// ── 提示词 ───────────────────────────────────────────

function note(template) {
  if (!template) return;
  $("template-note").textContent =
    template.status === "ready"
      ? `取 ${template.data.map((d) => `${d.dataset} ${d.days} 天`).join("、")}`
      : `还没上线：${template.waiting_on}`;
}

async function loadTemplates() {
  const templates = (await call("GET", "/api/templates")).templates;
  const select = $("templates");
  select.replaceChildren();
  for (const template of templates) {
    const option = document.createElement("option");
    option.value = template.name;
    option.textContent = template.status === "ready" ? template.name : `${template.name}（未上线）`;
    select.append(option);
  }
  note(templates[0]);
}

let regenerating = 0;
function scheduleRegenerate() {
  if (!state.text) return;
  clearTimeout(regenerating);
  regenerating = setTimeout(generate, 400);
}

async function generate() {
  if (state.busy) return;
  if (!state.code) return say("先在上面选一只票或指数");
  if (state.kind === "index") return say("指数暂无提示词模板——看K线就好");
  state.busy = true;
  $("generate").disabled = true;
  say("生成中…");
  const body = { code: state.code, template: $("templates").value };
  if ($("asof").value) body.as_of = $("asof").value;
  try {
    const built = await call("POST", "/api/prompt", body);
    state.text = built.text;
    $("preview").textContent = built.text;
    $("tokens").textContent = built.tokens.toLocaleString("zh-CN");
    $("warn").textContent = built.warn ? `　⚠ ${built.warn}` : "";
    say("");
  } catch (error) {
    say(`生成失败：${error.message}`);
  } finally {
    state.busy = false;
    $("generate").disabled = false;
  }
}

async function copy() {
  if (!state.text) return say("还没有可复制的东西");
  try {
    await navigator.clipboard.writeText(state.text);
    say("已复制，去粘给大模型");
  } catch {
    const range = document.createRange();
    range.selectNodeContents($("preview"));
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    say("浏览器不让直接复制，已替你选好，Ctrl/Cmd+C");
  }
}

// ── 接线 ─────────────────────────────────────────────

$("search").addEventListener("input", search);
$("search").addEventListener("focus", search);
$("search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSuggest();
});
$("templates").addEventListener("change", () => {
  note(state.templates.find((t) => t.name === $("templates").value));
  scheduleRegenerate();
});
$("asof").addEventListener("change", scheduleRegenerate);
$("generate").addEventListener("click", generate);
$("copy").addEventListener("click", copy);
$("token").addEventListener("input", () => {
  localStorage.setItem(TOKEN_KEY, $("token").value.trim());
  clearTimeout(typing);
  typing = setTimeout(boot, 500); // 停手半秒自动连接，不用点别处
});

async function boot() {
  if (!token()) return;
  try {
    await Promise.all([loadTemplates(), call("GET", "/api/recent")]);
    say("已连接");
  } catch {
    setAuth("off");
  }
}

$("token").value = token();
if (token()) boot();
