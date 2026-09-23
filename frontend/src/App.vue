<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Moon, Sun } from 'lucide-vue-next'

import { ApiError, api, getToken, setToken, type Bar, type Hit, type Template } from '@/api'
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

const CUSTOM = '自定义'
const DATASETS: { key: string; label: string; days: number }[] = [
  { key: 'daily', label: '日K', days: 120 },
  { key: 'minute_60', label: '60 分K', days: 60 },
  { key: 'minute_30', label: '30 分K', days: 30 },
  { key: 'minute_5', label: '5 分K', days: 10 },
]

const token = ref(getToken())
const connected = ref(false)
const status = ref('')

const query = ref('')
const hits = ref<Hit[]>([])
let typing: number | undefined

const selected = ref<{ code: string; name: string; kind: 'stock' | 'index' } | null>(null)
const bars = ref<Bar[]>([])

const templates = ref<Template[]>([])
const templateName = ref('')
const custom = reactive<Record<string, boolean>>({ daily: true, minute_60: false, minute_30: false, minute_5: false })
const customDays = reactive<Record<string, number>>(
  Object.fromEntries(DATASETS.map((d) => [d.key, d.days])),
)

const text = ref('')
const tokens = ref<number | null>(null)
const warn = ref('')
const busy = ref(false)
let regenerating: number | undefined

const isCustom = computed(() => templateName.value === CUSTOM)
const showPrompt = computed(() => selected.value?.kind === 'stock')
const quote = computed(() => {
  const list = bars.value
  if (list.length === 0) return null
  const last = list[list.length - 1]
  const prev = list.length > 1 ? list[list.length - 2] : null
  const change = prev ? ((last.close - prev.close) / prev.close) * 100 : 0
  return { last, change }
})
const currentTemplate = computed(() => templates.value.find((t) => t.name === templateName.value))

function say(message: string) {
  status.value = message
}

function handleError(error: unknown) {
  if (error instanceof ApiError && error.status === 401) {
    connected.value = false
    say('口令不对，填好口令再操作')
    return
  }
  say(error instanceof Error ? error.message : String(error))
}

// ── 搜索 ──────────────────────────────────────────────
function onSearchInput() {
  window.clearTimeout(typing)
  typing = window.setTimeout(async () => {
    const q = query.value.trim()
    if (!q) {
      hits.value = []
      return
    }
    try {
      hits.value = (await api.search(q)).hits.slice(0, 12)
      connected.value = true
      if (hits.value.length === 0) say('没找到——试试代码、名称或拼音首字母')
      else say('')
    } catch (error) {
      handleError(error)
    }
  }, 160)
}

async function pick(hit: Hit) {
  hits.value = []
  query.value = `${hit.code} ${hit.name}`
  selected.value = { code: hit.code, name: hit.name, kind: hit.kind }
  text.value = ''
  tokens.value = null
  warn.value = ''
  if (hit.kind === 'stock') await generate()
  await loadQuote()
  api.remember(hit.code).catch(() => {})
}

// ── 行情 ──────────────────────────────────────────────
async function loadQuote() {
  const target = selected.value
  if (!target) return
  say('加载行情…')
  try {
    const body = await api.kline(target.code, target.kind, 120)
    bars.value = body.bars
    connected.value = true
    if (body.bars.length === 0) {
      say(`${target.name} 在盘上还没有日线数据——它在回填队列里，晚些再看`)
    } else {
      say('')
    }
  } catch (error) {
    handleError(error)
  }
}

// ── 模板与提示词 ──────────────────────────────────────
async function loadTemplates() {
  templates.value = (await api.templates()).templates
  templateName.value = templates.value[0]?.name ?? CUSTOM
}

function collectCustom() {
  return DATASETS.filter((d) => custom[d.key]).map((d) => ({
    dataset: d.key,
    days: Math.max(1, Math.min(750, Number(customDays[d.key]) || 0)),
  }))
}

function scheduleRegenerate() {
  if (!text.value) return
  window.clearTimeout(regenerating)
  regenerating = window.setTimeout(generate, 400)
}

async function generate() {
  if (busy.value || !selected.value) return
  if (selected.value.kind === 'index') {
    say('指数暂无提示词模板——看K线就好')
    return
  }
  busy.value = true
  say('生成中…')
  const body: Record<string, unknown> = { code: selected.value.code, template: templateName.value }
  if (isCustom.value) {
    body.custom = collectCustom()
    if ((body.custom as unknown[]).length === 0) {
      busy.value = false
      say('自定义组合至少勾一个K线类型')
      return
    }
  }
  try {
    const built = await api.prompt(body)
    text.value = built.text
    tokens.value = built.tokens
    warn.value = built.warn ?? ''
    connected.value = true
    say('')
  } catch (error) {
    handleError(error)
  } finally {
    busy.value = false
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
  if (!isCustom.value) scheduleRegenerate()
}

// ── 连接 ──────────────────────────────────────────────
async function boot() {
  if (!token.value.trim()) return
  try {
    await Promise.all([loadTemplates(), api.recent()])
    connected.value = true
    say('已连接')
  } catch (error) {
    handleError(error)
  }
}

function onTokenInput() {
  setToken(token.value)
  window.clearTimeout(typing)
  typing = window.setTimeout(boot, 500)
}

onMounted(() => {
  applyTheme()
  if (token.value.trim()) boot()
})
</script>

<template>
  <header class="glass-chrome sticky top-0 z-20">
    <div class="mx-auto flex max-w-[640px] items-center gap-3 px-4 py-2.5">
      <div class="seal" aria-hidden="true">知行</div>
      <div class="relative flex flex-1 items-center gap-2">
        <input
          v-model="token"
          type="password"
          class="field !h-10"
          placeholder="输入口令连接"
          autocomplete="off"
          @input="onTokenInput"
        />
        <span
          class="h-2.5 w-2.5 shrink-0 rounded-full"
          :style="{ background: connected ? 'var(--down)' : 'var(--text-3)' }"
          :title="connected ? '已连接' : '未连接'"
        />
      </div>
      <button
        type="button"
        class="icon-btn shrink-0"
        :title="theme === 'light' ? '切到深色' : '切到浅色'"
        @click="toggleTheme"
      >
        <component :is="theme === 'light' ? Moon : Sun" :size="18" />
      </button>
    </div>
  </header>

  <main class="mx-auto grid max-w-[640px] gap-3.5 px-4 pt-4 pb-12">
    <!-- 搜索 -->
    <section class="glass-float relative p-2.5">
      <input
        v-model="query"
        type="search"
        class="field"
        placeholder="搜代码 / 名称 / 拼音，如 600519、茅台、gzmt"
        autocomplete="off"
        enterkeyhint="search"
        @input="onSearchInput"
        @focus="onSearchInput"
        @keydown.escape="hits = []"
      />
      <Transition name="pop">
        <ul
          v-if="hits.length"
          class="glass-float absolute inset-x-0 top-[calc(100%+6px)] z-30 max-h-[44vh] overflow-auto p-1.5"
        >
          <li v-for="hit in hits" :key="hit.code">
            <button
              type="button"
              class="flex w-full items-center justify-between rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-[var(--glass-inset)]"
              @click="pick(hit)"
            >
              <span>{{ hit.code }} {{ hit.name }}</span>
              <span class="text-xs" :style="{ color: hit.kind === 'index' ? 'var(--seal)' : 'var(--text-2)' }">
                {{ hit.kind === 'index' ? '指数' : '个股' }}
              </span>
            </button>
          </li>
        </ul>
      </Transition>
      <p v-if="!hits.length" class="px-1 pt-2 text-[13px]" style="color: var(--text-2)">
        输入至少一个字开始搜索，指数直接搜名字
      </p>
    </section>

    <!-- 行情 -->
    <section v-if="selected" class="glass-float p-4">
      <div class="flex items-start justify-between">
        <div>
          <p class="text-[22px] font-semibold">{{ selected.name }}</p>
          <p class="num text-[13px]" style="color: var(--text-2)">{{ selected.code }}</p>
        </div>
        <div v-if="quote" class="text-right">
          <p
            class="num text-[30px] font-bold"
            :style="{ color: quote.change >= 0 ? 'var(--up)' : 'var(--down)' }"
          >
            {{ quote.last.close.toFixed(2) }}
          </p>
          <p
            class="num text-[15px]"
            :style="{ color: quote.change >= 0 ? 'var(--up)' : 'var(--down)' }"
          >
            {{ quote.change >= 0 ? '+' : '' }}{{ quote.change.toFixed(2) }}%
          </p>
        </div>
      </div>

      <div v-if="bars.length" class="surface-solid mt-3 p-2">
        <KLineChart :bars="bars" />
      </div>

      <dl v-if="quote" class="num mt-2.5 grid grid-cols-4 gap-2 text-center">
        <div>
          <dt class="text-xs" style="color: var(--text-2)">开</dt>
          <dd class="text-[15px]">{{ quote.last.open.toFixed(2) }}</dd>
        </div>
        <div>
          <dt class="text-xs" style="color: var(--text-2)">高</dt>
          <dd class="text-[15px]">{{ quote.last.high.toFixed(2) }}</dd>
        </div>
        <div>
          <dt class="text-xs" style="color: var(--text-2)">低</dt>
          <dd class="text-[15px]">{{ quote.last.low.toFixed(2) }}</dd>
        </div>
        <div>
          <dt class="text-xs" style="color: var(--text-2)">量</dt>
          <dd class="text-[15px]">
            {{ quote.last.volume == null ? '—' : Math.round(quote.last.volume).toLocaleString('zh-CN') }}
          </dd>
        </div>
      </dl>
    </section>

    <!-- 提示词 -->
    <section v-if="showPrompt" class="glass-float p-4">
      <div class="flex gap-2">
        <select v-model="templateName" class="field !h-[42px] flex-1" @change="onTemplateChange">
          <option v-for="t in templates" :key="t.name" :value="t.name">
            {{ t.status === 'ready' ? t.name : `${t.name}（未上线）` }}
          </option>
          <option :value="CUSTOM">{{ CUSTOM }}</option>
        </select>
        <button type="button" class="btn !h-[42px]" :disabled="busy" @click="generate">生成</button>
      </div>

      <p class="pt-2.5 text-sm">
        {{ isCustom ? '自己勾K线类型与天数，生成一条一次性组合' : (currentTemplate?.task ?? '') }}
      </p>
      <p v-if="!isCustom && currentTemplate && currentTemplate.status !== 'ready'" class="text-[13px]" style="color: var(--text-2)">
        还没上线：{{ currentTemplate.waiting_on }}
      </p>

      <div v-if="isCustom" class="glass-inset mt-2.5 grid gap-2 p-3">
        <p class="text-[13px]" style="color: var(--text-2)">勾选要进的K线类型，各自填天数（1–750 个交易日）</p>
        <label
          v-for="d in DATASETS"
          :key="d.key"
          class="flex items-center justify-between gap-2.5"
        >
          <span class="flex items-center gap-2 text-[15px]">
            <input v-model="custom[d.key]" type="checkbox" class="h-[18px] w-[18px]" style="accent-color: var(--ink)" />
            {{ d.label }}
          </span>
          <input
            v-model.number="customDays[d.key]"
            type="number"
            min="1"
            max="750"
            class="field num !h-9 !w-[90px] text-right"
          />
        </label>
      </div>

      <p class="num pt-2 text-[13px]" style="color: var(--text-2)">
        <span>{{ tokens == null ? '—' : tokens.toLocaleString('zh-CN') }}</span> token<span :style="{ color: 'var(--warn)' }">{{ warn ? `　⚠ ${warn}` : '' }}</span>
      </p>

      <pre
        class="surface-solid mt-2.5 max-h-[46vh] overflow-auto p-3.5 text-[13px] leading-relaxed whitespace-pre-wrap break-all"
        style="font-family: ui-monospace, 'SF Mono', Menlo, monospace"
      >{{ text || '选一只票，点生成。' }}</pre>
      <button type="button" class="btn mt-2.5 w-full" @click="copy">复制提示词</button>
    </section>
  </main>

  <p class="mx-auto min-h-6 max-w-[640px] px-4 pb-6 text-center text-[13px]" style="color: var(--text-2)">
    {{ status }}
  </p>
</template>

<style scoped>
.seal {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: var(--seal);
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
</style>
