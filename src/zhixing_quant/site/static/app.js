// 知行 · 提示词站点前端（06 §四 一期、§八-1/5）。零依赖、无构建。
//
// 页面上不算任何东西：token 数、价格口径、缺口那句"盘上 N 天，模板要 M 天"全来自服务器
// （ADR-0013 决定 5 只允许一份口径，它在 Python 那侧）。这里只做四件事：把输入变成请求、
// 把响应变成 DOM、记住口令、把复制按钮按下去。

const TOKEN_KEY = "zx.site.token";
const $ = (id) => document.getElementById(id);

const state = { code: null, name: null, templates: [], text: "", busy: false };

function token() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
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
    say("口令不对——先在上面填口令");
    $("auth-state").textContent = "被拒";
    throw new Error("401");
  }
  if (!response.ok) {
    const problem = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(problem.detail || String(response.status));
  }
  $("auth-state").textContent = "已连接";
  return response.json();
}

function say(text) {
  $("status").textContent = text;
}

function renderList(id, rows, onPick) {
  const list = $(id);
  list.replaceChildren();
  for (const row of rows) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${row.code} ${row.name}`;
    button.addEventListener("click", () => onPick(row));
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = row.by || "";
    item.append(button, tag);
    list.append(item);
  }
}

async function pick(hit) {
  state.code = hit.code;
  state.name = hit.name;
  $("symbol").textContent = `${hit.code} ${hit.name}`;
  try {
    renderList("recent", (await call("POST", "/api/recent", { code: hit.code })).entries, pick);
  } catch (error) {
    if (error.message !== "401") say(`留痕没记上：${error.message}`);
  }
}

let typing = 0;
async function search() {
  clearTimeout(typing);
  typing = setTimeout(async () => {
    const query = $("search").value.trim();
    if (!query) return renderList("hits", [], pick);
    try {
      const hits = (await call("GET", `/api/search?q=${encodeURIComponent(query)}`)).hits;
      renderList("hits", hits, pick);
    } catch (error) {
      if (error.message !== "401") say(`搜索失败：${error.message}`);
    }
  }, 200);
}

function note(template) {
  if (!template) return;
  $("template-note").textContent =
    template.status === "ready"
      ? `取 ${template.data.map((d) => `${d.dataset} ${d.days} 天`).join("、")}｜${template.fields.length} 列`
      : `还没上线：${template.waiting_on}`;
}

async function loadTemplates() {
  state.templates = (await call("GET", "/api/templates")).templates;
  const select = $("templates");
  select.replaceChildren();
  for (const template of state.templates) {
    const option = document.createElement("option");
    option.value = template.name;
    option.textContent = template.status === "ready" ? template.name : `${template.name}（未上线）`;
    select.append(option);
  }
  note(state.templates[0]);
}

// §八-5 的"实时"：估算只在服务器算，所以已出结果之后改模板或改日期会自动重发一次
// （400ms 防抖）。没出结果之前不猜数字——页面上凭空长出一个 token 数是第二套口径。
let regenerating = 0;
function scheduleRegenerate() {
  if (!state.text) return;
  clearTimeout(regenerating);
  regenerating = setTimeout(generate, 400);
}

async function generate() {
  if (state.busy) return;
  if (!state.code) return say("先选一只票");
  state.busy = true;
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
  }
}

async function copy() {
  if (!state.text) return say("还没有可复制的东西");
  try {
    await navigator.clipboard.writeText(state.text);
    say("已复制，去粘给大模型");
  } catch {
    // 剪贴板在非安全上下文（http 公网直连、无证书）里会被浏览器拒——那是选文本，不是失败。
    const range = document.createRange();
    range.selectNodeContents($("preview"));
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    say("浏览器不让直接复制，已替你选好，Ctrl/Cmd+C");
  }
}

$("search").addEventListener("input", search);
$("templates").addEventListener("change", () => {
  note(state.templates.find((t) => t.name === $("templates").value));
  scheduleRegenerate();
});
$("asof").addEventListener("change", scheduleRegenerate);
$("generate").addEventListener("click", generate);
$("copy").addEventListener("click", copy);
$("token").addEventListener("change", async () => {
  sessionStorage.setItem(TOKEN_KEY, $("token").value.trim());
  try {
    await Promise.all([loadTemplates(), boot()]);
  } catch {
    $("auth-state").textContent = "被拒";
  }
});

async function boot() {
  renderList("recent", (await call("GET", "/api/recent")).entries, pick);
}

if (token()) {
  Promise.all([loadTemplates(), boot()]).catch(() => {});
} else {
  say("填口令后连接");
}
