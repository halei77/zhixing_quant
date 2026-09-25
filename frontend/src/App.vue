<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import { api, type Bar, type Entry, type Hit, type Quote, type Template } from '@/api'
import KLineChart from '@/components/KLineChart.vue'

const THEME_KEY = 'zx.site.theme'
const theme = ref<'light' | 'dark'>(
  localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light',
)
function applyTheme() {
  document.documentElement.dataset.theme = theme.value
}
function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  localStorage.setItem(THEME_KEY, theme.value)
  applyTheme()
}
function onStorage(event: StorageEvent) {
  // 多开标签页各自切主题时保持同步（同标签内的 setItem 不触发 storage，只有别的标签会）。
  if (event.key !== THEME_KEY) return
  theme.value = event.newValue === 'dark' ? 'dark' : 'light'
  applyTheme()
}

const CUSTOM = '自定义'
const DATASETS: { key: string; label: string; days: number }[] = [
  { key: 'daily', label: '日K', days: 120 },
  { key: 'minute_60', label: '60 分K', days: 60 },
  { key: 'minute_30', label: '30 分K', days: 30 },
  { key: 'minute_5', label: '5 分K', days: 10 },
  // 1 分K（niuguwang 源，minute_1）：token 贵，默认深度取 10 天（≈56.6k token，
  // 设计合成 §三 实测估算）；服务端把它的自定义天数封在 30 以内（site/api）。
  { key: 'minute_1', label: '1 分K', days: 10 },
]
function datasetLabel(key: string): string {
  return DATASETS.find((d) => d.key === key)?.label ?? key
}

// ── 连接状态（ADR-0019：无口令，点只反映“最近一次请求成功”）────
const connected = ref(false)
const status = ref('')

const query = ref('')
const hits = ref<Hit[]>([])
const recent = ref<Entry[]>([])
let searchTimer: number | undefined
// 键盘流的高亮位：随新结果归 0（第一条即默认候选），鼠标悬停跟随。
const activeHit = ref(-1)
// 搜索态（#67 前端优化，用户 2026-09-25 拍板「就地展开为独立结果面板」）：聚焦 = 左栏进
// 搜索模式——行情/K线让位，候选面板长在搜索框正下方的文档流里（不悬浮、不遮挡）；
// 选中 / 失焦 / Esc 即收起。面板高度贴合结果数，只留滚动上限，不再预留 44vh 的大空框。
const searchFocused = ref(false)
const searching = computed(() => searchFocused.value && hits.value.length > 0)

const selected = ref<{ code: string; name: string; kind: 'stock' | 'index' } | null>(null)
const bars = ref<Bar[]>([])
// 面板那一行的展示值由服务端算好、格式化好（docs/11 §六-2）——壳不再自己推涨跌幅、不再自己 toFixed。
const quote = ref<Quote | null>(null)

const templates = ref<Template[]>([])
const templateName = ref('')
const custom = reactive<Record<string, boolean>>({ daily: true, minute_60: false, minute_30: false, minute_5: false, minute_1: false })
const customDays = reactive<Record<string, number>>(
  Object.fromEntries(DATASETS.map((d) => [d.key, d.days])),
)
const dayError = ref('')
// as_of 回溯（ADR-0012 决定 2 的契约参数，服务端本就一直认）：空串 = 今天。
const asOf = ref('')
function localToday(): string {
  const now = new Date()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${mm}-${dd}`
}
const todayStr = localToday()

const text = ref('')
const tokens = ref<number | null>(null)
const warn = ref('')
// 生成失败的原因摆在正文区，而不是只在 toast 上闪一下就没了——用户要能看到"为什么没出提示词"。
const failure = ref('')
const busy = ref(false)
const dirty = ref(false)
let regenerateTimer: number | undefined

const isCustom = computed(() => templateName.value === CUSTOM)
const showPrompt = computed(() => selected.value?.kind === 'stock')
const currentTemplate = computed(() => templates.value.find((t) => t.name === templateName.value))
// 模板吃哪些数据，是模板配置里的事实（/api/templates 原样给），壳只做中文标注、不做推导。
const composition = computed(() => {
  const t = currentTemplate.value
  if (!t || isCustom.value) return ''
  return t.data.map((d) => `${datasetLabel(d.dataset)} × ${d.days}`).join(' · ')
})
const hint = computed(() => {
  if (hits.value.length) return ''
  const q = query.value.trim()
  if (!q) return '输入至少一个字开始搜索，指数直接搜名字'
  // 已选中的那只票：pick() 把 query 写成「代码 名称」当选择标签，这时不是"没找到"。
  // 不挡住它，选完票的搜索区会一直挂着「没找到——…」，而右边明明正在生成它的提示词。
  if (selected.value && q === `${selected.value.code} ${selected.value.name}`) return ''
  return '没找到——试试代码、名称或拼音首字母'
})

function say(message: string) {
  status.value = message
}

function handleError(error: unknown) {
  say(error instanceof Error ? error.message : String(error))
}

// ── 搜索 ──────────────────────────────────────────────
function onSearchInput() {
  // Enter 选中后焦点可能仍留在输入框：再次键入 = 重新进搜索模式（flag 归位，不等 focus 事件）
  searchFocused.value = true
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(runSearch, 160)
}

function onSearchFocus() {
  searchFocused.value = true
  onSearchInput()
}

function onSearchBlur(event: FocusEvent) {
  // 点候选时先触发输入框 blur——relatedTarget 落在搜索卡片内（候选按钮）不收面板，
  // 否则鼠标用户永远点不到；移向卡外才收起。
  const next = event.relatedTarget as HTMLElement | null
  if (next && (event.target as HTMLElement).closest("section")?.contains(next)) return
  searchFocused.value = false
}

async function runSearch() {
  const q = query.value.trim()
  if (!q) {
    hits.value = []
    activeHit.value = -1
    return
  }
  try {
    hits.value = (await api.search(q)).hits.slice(0, 12)
    activeHit.value = hits.value.length ? 0 : -1
    connected.value = true
    if (hits.value.length === 0) say('')
  } catch (error) {
    handleError(error)
  }
}

function onSearchKeydown(event: KeyboardEvent) {
  if (!hits.value.length) return
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    activeHit.value = (activeHit.value + 1) % hits.value.length
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    activeHit.value = (activeHit.value - 1 + hits.value.length) % hits.value.length
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const hit = hits.value[activeHit.value] ?? hits.value[0]
    if (hit) void pick(hit)
  } else if (event.key === 'Escape') {
    hits.value = []
    activeHit.value = -1
    searchFocused.value = false
    ;(event.target as HTMLInputElement).blur()
  }
}

async function pick(hit: Hit) {
  hits.value = []
  activeHit.value = -1
  searchFocused.value = false  // 选中即收起搜索模式：左栏回到行情+K线
  query.value = `${hit.code} ${hit.name}`
  selected.value = { code: hit.code, name: hit.name, kind: hit.kind }
  bars.value = []
  quote.value = null
  text.value = ''
  tokens.value = null
  warn.value = ''
  failure.value = ''
  dirty.value = false
  window.clearTimeout(regenerateTimer)
  if (hit.kind === 'stock') await generate()
  await loadQuote()
  await remember(hit.code)
}

async function remember(code: string) {
  try {
    recent.value = (await api.remember(code)).entries
    connected.value = true
  } catch {
    // 留痕写不进去不该打断看K线：它是旁路，不是主流程
  }
}

async function loadRecent() {
  recent.value = (await api.recent()).entries
}

async function pickRecent(entry: Entry) {
  await pick({ code: entry.code, name: entry.name, kind: 'stock' })
}

// ── 行情 ──────────────────────────────────────────────
async function loadQuote() {
  const target = selected.value
  if (!target) return
  say('加载行情…')
  try {
    const body = await api.kline(target.code, target.kind, 120)
    bars.value = body.bars
    quote.value = body.quote
    connected.value = true
    if (body.bars.length === 0) {
      say(`${target.name} 在盘上还没有日线数据——它在回填队列里，晚些再看`)
    } else {
      say('')
    }
  } catch (error) {
    bars.value = []
    quote.value = null
    handleError(error)
  }
}

// ── 模板与提示词 ──────────────────────────────────────
async function loadTemplates() {
  templates.value = (await api.templates()).templates
  templateName.value = templates.value[0]?.name ?? CUSTOM
}

/** 只校验、不改写：把 900 悄悄当 750 发出去，服务端那句「越界」就永远响不出来。 */
function collectCustom(): { dataset: string; days: number }[] | null {
  const picked = DATASETS.filter((d) => custom[d.key])
  if (picked.length === 0) {
    dayError.value = '自定义组合至少勾一个K线类型'
    return null
  }
  for (const d of picked) {
    const days = Number(customDays[d.key])
    if (!Number.isInteger(days) || days < 1 || days > 750) {
      dayError.value = `${d.label} 的天数 ${days} 越界（1–750 个交易日）`
      return null
    }
  }
  dayError.value = ''
  return picked.map((d) => ({ dataset: d.key, days: Number(customDays[d.key]) }))
}

function scheduleRegenerate() {
  if (!text.value && !dirty.value) return
  window.clearTimeout(regenerateTimer)
  regenerateTimer = window.setTimeout(() => void generate(), 400)
}

async function generate() {
  if (!selected.value) return
  if (busy.value) {
    // 上一次还没回来：记下脏标，回来以后重跑——静默丢掉会让正文停留在上一个模板
    dirty.value = true
    return
  }
  if (selected.value.kind === 'index') {
    say('指数暂无提示词模板——看K线就好')
    return
  }
  const body: Record<string, unknown> = { code: selected.value.code, template: templateName.value }
  if (asOf.value) body.as_of = asOf.value
  if (isCustom.value) {
    const picked = collectCustom()
    if (!picked) return
    body.custom = picked
  }
  busy.value = true
  say('生成中…')
  try {
    const built = await api.prompt(body)
    text.value = built.text
    tokens.value = built.tokens
    warn.value = built.warn ?? ''
    failure.value = ''
    connected.value = true
    say('')
  } catch (error) {
    // 失败就把正文清掉：留着上一个模板的提示词，而选择器已经是新的，是自相矛盾的页面。
    // 但原因要留在正文区——只在 toast 上闪一下，用户看到的是空白，读不到"为什么没出提示词"。
    text.value = ''
    tokens.value = null
    warn.value = ''
    failure.value = error instanceof Error ? error.message : String(error)
    handleError(error)
  } finally {
    busy.value = false
    if (dirty.value) {
      dirty.value = false
      window.clearTimeout(regenerateTimer)
      regenerateTimer = window.setTimeout(() => void generate(), 0)
    }
  }
}

async function copy() {
  if (!text.value) {
    say('还没有可复制的东西')
    return
  }
  try {
    await navigator.clipboard.writeText(text.value)
    say('已复制，去粘给大模型')
  } catch {
    say('浏览器不让直接复制，请手动全选')
  }
}

function onTemplateChange() {
  if (isCustom.value) {
    // 切到自定义：先把上一个模板的正文收掉，别让两套口径同时挂在页面上
    text.value = ''
    tokens.value = null
    warn.value = ''
    failure.value = ''
    dirty.value = false
    return
  }
  scheduleRegenerate()
}

function onAsOfChange() {
  scheduleRegenerate()
}

// ── 连接 ──────────────────────────────────────────────
async function boot() {
  try {
    await Promise.all([loadTemplates(), loadRecent()])
    connected.value = true
    say('已连接')
  } catch (error) {
    handleError(error)
  }
}

onMounted(() => {
  applyTheme()
  window.addEventListener('storage', onStorage)
  boot()
})
onUnmounted(() => {
  window.removeEventListener('storage', onStorage)
  window.clearTimeout(searchTimer)
  window.clearTimeout(regenerateTimer)
})
</script>

<template>
  <header class="glass-chrome sticky top-0 z-20">
    <div class="mx-auto flex max-w-[640px] items-center gap-3 px-4 py-2.5 lg:max-w-6xl">
      <div class="seal" aria-hidden="true">知行</div>
      <h1 class="text-[17px] font-semibold" style="color: var(--zx-text)">提示词站点</h1>
      <div class="relative flex flex-1 items-center gap-2">
        <span
          class="dot"
          :class="connected ? 'dot-on' : 'dot-off'"
          :title="connected ? '已连接' : '未连接'"
        />
        <span class="sr-only">{{ connected ? '已连接' : '未连接' }}</span>
      </div>
      <button
        type="button"
        class="icon-btn shrink-0"
        :title="theme === 'light' ? '切到深色' : '切到浅色'"
        :aria-label="theme === 'light' ? '切到深色' : '切到浅色'"
        @click="toggleTheme"
      >
        <svg v-if="theme === 'light'" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
        <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      </button>
    </div>
  </header>

  <main class="mx-auto grid max-w-[640px] gap-3.5 px-4 pt-4 pb-12 lg:max-w-6xl lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)] lg:items-start lg:gap-6">
    <!-- 左栏：搜索 + 行情/K线（桌面端）；移动端仍是自上而下的单列流 -->
    <div class="grid gap-3.5 lg:content-start">
      <!-- 搜索 -->
      <section class="glass-float relative p-2.5">
        <input
          v-model="query"
          data-testid="search"
          type="search"
          class="field"
          placeholder="搜代码 / 名称 / 拼音，如 600519、茅台、gzmt"
          autocomplete="off"
          enterkeyhint="search"
          aria-label="搜索股票或指数"
          role="combobox"
          :aria-expanded="hits.length > 0"
          :aria-activedescendant="activeHit >= 0 ? `hit-${activeHit}` : undefined"
          @input="onSearchInput"
          @focus="onSearchFocus"
          @blur="onSearchBlur"
          @keydown="onSearchKeydown"
        />
        <Transition name="pop">
          <ul
            v-if="searching"
            data-testid="suggest"
            class="surface-solid mt-2 max-h-[min(56vh,520px)] overflow-auto p-1.5"
            style="border-radius: var(--zx-r-lg)"
            role="listbox"
          >
            <li v-for="(hit, i) in hits" :key="hit.code" :id="`hit-${i}`" role="option" :aria-selected="i === activeHit">
              <button
                type="button"
                class="hit flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-left transition-colors"
                :class="{ 'bg-[var(--zx-glass-inset)]': i === activeHit }"
                @mouseenter="activeHit = i"
                @click="pick(hit)"
              >
                <span>{{ hit.code }} {{ hit.name }}</span>
                <span class="text-xs" :style="{ color: hit.kind === 'index' ? 'var(--zx-seal)' : 'var(--zx-text-2)' }">
                  {{ hit.kind === 'index' ? '指数' : '个股' }}
                </span>
              </button>
            </li>
          </ul>
        </Transition>

        <div v-if="searchFocused && !searching && recent.length" class="pt-2">
          <p class="px-1 text-[13px]" style="color: var(--zx-text-3)">最近搜过</p>
          <ul class="flex flex-wrap gap-1.5 px-1 pt-1.5">
            <li v-for="entry in recent" :key="entry.code">
              <button
                type="button"
                class="hit pill"
                :title="`${entry.name} · ${entry.at}`"
                @click="pickRecent(entry)"
              >
                {{ entry.name }}
              </button>
            </li>
          </ul>
        </div>

        <p v-if="hint" class="px-1 pt-2 text-[13px]" style="color: var(--zx-text-2)">{{ hint }}</p>
      </section>

      <!-- 行情：整块是内容层（docs/11 §二「正文与数字一律实底」），不是玻璃；
           搜索模式下让位给候选面板（#67 前端优化：就地展开为独立结果面板） -->
      <section v-if="selected && !searchFocused" class="surface-solid p-4">
        <div class="p-1">
          <div class="flex items-start justify-between">
            <div>
              <p class="text-[22px] font-semibold">{{ selected.name }}</p>
              <p class="num text-[13px]" style="color: var(--zx-text-2)">{{ selected.code }}</p>
            </div>
            <div v-if="quote" data-testid="quote" class="text-right">
              <p
                class="num text-[30px] font-bold"
                :style="{ color: quote.up ? 'var(--zx-up)' : 'var(--zx-down)' }"
              >
                {{ quote.close }}
              </p>
              <p
                class="num text-[15px]"
                :style="{ color: quote.up ? 'var(--zx-up)' : 'var(--zx-down)' }"
              >
                {{ quote.change_pct }}
              </p>
              <p v-if="quote.date" class="num text-xs" style="color: var(--zx-text-3)">截至 {{ quote.date }}</p>
            </div>
          </div>

          <div v-if="bars.length" class="mt-3">
            <KLineChart :bars="bars" />
          </div>

          <dl v-if="quote" class="num mt-2.5 grid grid-cols-4 gap-2 text-center">
            <div>
              <dt class="text-xs" style="color: var(--zx-text-2)">开</dt>
              <dd class="text-[15px]">{{ quote.open }}</dd>
            </div>
            <div>
              <dt class="text-xs" style="color: var(--zx-text-2)">高</dt>
              <dd class="text-[15px]">{{ quote.high }}</dd>
            </div>
            <div>
              <dt class="text-xs" style="color: var(--zx-text-2)">低</dt>
              <dd class="text-[15px]">{{ quote.low }}</dd>
            </div>
            <div>
              <dt class="text-xs" style="color: var(--zx-text-2)">量</dt>
              <dd class="text-[15px]">{{ quote.volume }}</dd>
            </div>
          </dl>
        </div>
      </section>
    </div>

    <!-- 右栏：提示词（主交付物）。操作条吸底，生成/复制不再跟着长文跑 -->
    <div class="grid gap-3.5 lg:content-start">
      <section v-if="showPrompt" class="surface-solid flex flex-col p-4">
        <div class="flex flex-wrap items-center gap-2">
          <select
            v-model="templateName"
            data-testid="templates"
            class="field !h-11 min-w-0 flex-1"
            aria-label="模板"
            @change="onTemplateChange"
          >
            <option v-for="t in templates" :key="t.name" :value="t.name">
              {{ t.status === 'ready' ? t.name : `${t.name}（未上线）` }}
            </option>
            <option :value="CUSTOM">{{ CUSTOM }}</option>
          </select>
          <label class="flex items-center gap-1.5">
            <span class="text-[13px]" style="color: var(--zx-text-2)">截至</span>
            <input
              v-model="asOf"
              data-testid="asof"
              type="date"
              :max="todayStr"
              :aria-label="'生成截至日期，默认今天'"
              class="field num !h-11 !w-[152px] !px-2 text-[13px]"
              @change="onAsOfChange"
            />
            <button
              v-if="asOf"
              type="button"
              class="icon-btn !h-11 !w-11"
              title="回到今天"
              aria-label="回到今天"
              @click="asOf = ''; onAsOfChange()"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
                <path d="M3 3v5h5" />
              </svg>
            </button>
          </label>
        </div>

        <p class="pt-2.5 text-sm">
          {{ isCustom ? '自己勾K线类型与天数，生成一条一次性组合' : (currentTemplate?.task ?? '') }}
        </p>
        <p v-if="composition" class="num pt-1 text-[13px]" style="color: var(--zx-text-2)">
          数据组合：{{ composition }}
        </p>
        <p v-if="!isCustom && currentTemplate && currentTemplate.status !== 'ready'" class="text-[13px]" style="color: var(--zx-text-2)">
          还没上线：{{ currentTemplate.waiting_on }}
        </p>

        <div v-if="isCustom" class="glass-inset mt-2.5 grid gap-2 p-3">
          <p class="text-[13px]" style="color: var(--zx-text-2)">勾选要进的K线类型，各自填天数（1–750 个交易日）</p>
          <label
            v-for="d in DATASETS"
            :key="d.key"
            class="flex items-center justify-between gap-2.5"
          >
            <span class="flex items-center gap-2 text-[15px]">
              <input v-model="custom[d.key]" type="checkbox" class="h-[18px] w-[18px]" style="accent-color: var(--zx-ink)" />
              {{ d.label }}
            </span>
            <input
              v-model.number="customDays[d.key]"
              type="number"
              min="1"
              max="750"
              :aria-label="`${d.label} 天数`"
              class="field num !h-11 !w-[90px] text-right"
            />
          </label>
          <p v-if="dayError" class="text-[13px]" style="color: var(--zx-warn)">{{ dayError }}</p>
        </div>

        <pre
          class="surface-solid mt-2.5 min-h-[120px] flex-1 overflow-auto p-3.5 text-[13px] leading-relaxed whitespace-pre-wrap break-words"
          :class="{ 'opacity-60': busy }"
          style="font-family: ui-monospace, 'SF Mono', Menlo, monospace; max-height: 46vh"
          aria-live="polite"
          data-testid="prompt-body"
        >{{ text || failure || '选一只票，点生成。' }}</pre>

        <!-- 吸底操作条：token 数、警告、生成、复制一条线，不再滚过整段正文去找。
             这一行不许 `truncate`（= nowrap）：warn 是服务端给的长中文句，nowrap 会让它按
             整行宽度算进最小内容尺寸，撑破 `minmax(0,6fr)` 网格列。2026-09-24 实测：超阈值
             警告一出现，整页横向溢出 370px、自定义面板的天数框被推出屏幕；去掉 truncate
             后溢出归 0。让它换行。 -->
        <div class="sticky bottom-3 z-10 -mx-1 mt-2.5 px-1 pb-1" data-testid="actions">
          <div class="glass-float flex items-center gap-2 p-2">
            <p class="num min-w-0 flex-1 pl-1 text-[13px]" style="color: var(--zx-text-2)">
              <span data-testid="tokens">{{ tokens == null ? '—' : tokens.toLocaleString('zh-CN') }}</span> token
              <span v-if="warn" data-testid="warn" class="pl-1 break-words" style="color: var(--zx-warn)">⚠ {{ warn }}</span>
            </p>
            <button type="button" data-testid="generate" class="btn !h-11 shrink-0" :disabled="busy" @click="generate">
              {{ busy ? '生成中…' : '生成' }}
            </button>
            <button type="button" data-testid="copy" class="btn btn-ghost !h-11 shrink-0" :disabled="busy || !text" @click="copy">
              复制
            </button>
          </div>
        </div>
      </section>

      <!-- 未选票时的右栏占位：让双栏在桌面端不塌，也提示下一步 -->
      <section v-else class="surface-solid grid place-items-center p-6 text-center lg:min-h-[320px]">
        <p class="text-[15px]" style="color: var(--zx-text-2)">搜一只个股，这里生成它的提示词</p>
      </section>
    </div>
  </main>

  <p class="toast" :class="{ 'toast-on': status }" role="status">{{ status }}</p>
</template>

<style scoped>
.seal {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--zx-seal);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 2px;
  writing-mode: vertical-rl;
  text-orientation: upright;
  box-shadow:
    inset 0 0 0 2px rgba(255, 255, 255, 0.22),
    0 4px 12px rgba(0, 0, 0, 0.3);
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.pill {
  min-height: 44px;
  padding: 0 16px;
  border-radius: var(--zx-r-pill);
  border: 1px solid var(--zx-stroke);
  background: var(--zx-glass-inset);
  cursor: pointer;
}
.hit {
  min-height: 44px;
  border: 1px solid transparent;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}
</style>
