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
]

// ── 连接状态（ADR-0019：无口令，点只反映“最近一次请求成功”）────
const connected = ref(false)
const status = ref('')

const query = ref('')
const hits = ref<Hit[]>([])
const recent = ref<Entry[]>([])
let searchTimer: number | undefined

const selected = ref<{ code: string; name: string; kind: 'stock' | 'index' } | null>(null)
const bars = ref<Bar[]>([])
// 面板那一行的展示值由服务端算好、格式化好（docs/11 §六-2）——壳不再自己推涨跌幅、不再自己 toFixed。
const quote = ref<Quote | null>(null)

const templates = ref<Template[]>([])
const templateName = ref('')
const custom = reactive<Record<string, boolean>>({ daily: true, minute_60: false, minute_30: false, minute_5: false })
const customDays = reactive<Record<string, number>>(
  Object.fromEntries(DATASETS.map((d) => [d.key, d.days])),
)
const dayError = ref('')

const text = ref('')
const tokens = ref<number | null>(null)
const warn = ref('')
const busy = ref(false)
const dirty = ref(false)
let regenerateTimer: number | undefined

const isCustom = computed(() => templateName.value === CUSTOM)
const showPrompt = computed(() => selected.value?.kind === 'stock')
const currentTemplate = computed(() => templates.value.find((t) => t.name === templateName.value))
const hint = computed(() => {
  if (hits.value.length) return ''
  return query.value.trim() ? '没找到——试试代码、名称或拼音首字母' : '输入至少一个字开始搜索，指数直接搜名字'
})

function say(message: string) {
  status.value = message
}

function handleError(error: unknown) {
  say(error instanceof Error ? error.message : String(error))
}

// ── 搜索 ──────────────────────────────────────────────
function onSearchInput() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(runSearch, 160)
}

async function runSearch() {
  const q = query.value.trim()
  if (!q) {
    hits.value = []
    return
  }
  try {
    hits.value = (await api.search(q)).hits.slice(0, 12)
    connected.value = true
    if (hits.value.length === 0) say('')
  } catch (error) {
    handleError(error)
  }
}

async function pick(hit: Hit) {
  hits.value = []
  query.value = `${hit.code} ${hit.name}`
  selected.value = { code: hit.code, name: hit.name, kind: hit.kind }
  bars.value = []
  quote.value = null
  text.value = ''
  tokens.value = null
  warn.value = ''
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
    connected.value = true
    say('')
  } catch (error) {
    // 失败就把正文清掉：留着上一个模板的提示词，而选择器已经是新的，是自相矛盾的页面
    text.value = ''
    tokens.value = null
    warn.value = ''
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
    dirty.value = false
    return
  }
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
    <div class="mx-auto flex max-w-[640px] items-center gap-3 px-4 py-2.5">
      <div class="seal" aria-hidden="true">知行</div>
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

  <main class="mx-auto grid max-w-[640px] gap-3.5 px-4 pt-4 pb-12">
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
        @input="onSearchInput"
        @focus="onSearchInput"
        @keydown.escape="hits = []"
      />
      <Transition name="pop">
        <ul
          v-if="hits.length"
          data-testid="suggest"
          class="surface-solid absolute inset-x-0 top-[calc(100%+6px)] z-30 max-h-[44vh] overflow-auto p-1.5"
        >
          <li v-for="hit in hits" :key="hit.code">
            <button
              type="button"
              class="hit flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-[var(--zx-glass-inset)]"
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

      <div v-if="!hits.length && recent.length" class="pt-2">
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

    <!-- 行情：整块是内容层（docs/11 §二「正文与数字一律实底」），不是玻璃 -->
    <section v-if="selected" class="surface-solid p-4">
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

    <!-- 提示词 -->
    <section v-if="showPrompt" class="surface-solid p-4">
      <div class="flex gap-2">
        <select
          v-model="templateName"
          data-testid="templates"
          class="field !h-11 flex-1"
          aria-label="模板"
          @change="onTemplateChange"
        >
          <option v-for="t in templates" :key="t.name" :value="t.name">
            {{ t.status === 'ready' ? t.name : `${t.name}（未上线）` }}
          </option>
          <option :value="CUSTOM">{{ CUSTOM }}</option>
        </select>
        <button type="button" class="btn !h-11" :disabled="busy" @click="generate">生成</button>
      </div>

      <p class="pt-2.5 text-sm">
        {{ isCustom ? '自己勾K线类型与天数，生成一条一次性组合' : (currentTemplate?.task ?? '') }}
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

      <p class="num pt-2 text-[13px]" style="color: var(--zx-text-2)">
        <span>{{ tokens == null ? '—' : tokens.toLocaleString('zh-CN') }}</span> token<span :style="{ color: 'var(--zx-warn)' }">{{ warn ? `　⚠ ${warn}` : '' }}</span>
      </p>

      <pre
        class="surface-solid mt-2.5 max-h-[46vh] overflow-auto p-3.5 text-[13px] leading-relaxed whitespace-pre-wrap break-words"
        style="font-family: ui-monospace, 'SF Mono', Menlo, monospace"
      >{{ text || '选一只票，点生成。' }}</pre>
      <button type="button" class="btn mt-2.5 w-full" @click="copy">复制提示词</button>
    </section>
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
</style>
