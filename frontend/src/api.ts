// 站点 API 客户端。页面不产生任何口径：token 数、口径句、缺口句全来自服务器（ADR-0013 决定 5）。
// 这里只做「输入 → 请求 → 响应」，与旧 app.js 的调用一一对应。

const TOKEN_KEY = 'zx.site.token'

// ADR-0013 补充决定一：口令由用户填进页面、存在 sessionStorage（关页即清），逐个请求带上。
export const getToken = (): string => sessionStorage.getItem(TOKEN_KEY) || ''
export const setToken = (value: string): void => sessionStorage.setItem(TOKEN_KEY, value.trim())

export interface Hit {
  code: string
  name: string
  kind: 'stock' | 'index'
}

/** 留痕里的一条（ADR-0013 决定 4）。`at` 是写下的时刻。 */
export interface Entry {
  code: string
  name: string
  at: string
}

export interface Template {
  name: string
  status: string
  task: string
  waiting_on?: string
  data: { dataset: string; days: number }[]
}

export interface Bar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number | null
}

/** 行情面板那一行的展示值：服务端已算好口径、做好格式化，壳只搬不改（docs/11 §六-2）。 */
export interface Quote {
  date: string | null
  close: string
  change_pct: string
  up: boolean
  open: string
  high: string
  low: string
  volume: string
}

export interface PromptResult {
  text: string
  tokens: number
  warn: string | null
}

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'X-Token': getToken() }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? null : JSON.stringify(body),
  })
  if (!response.ok) {
    const problem = (await response.json().catch(() => ({ detail: response.statusText }))) as {
      detail?: string
    }
    throw new ApiError(problem.detail || String(response.status), response.status)
  }
  return (await response.json()) as T
}

export const api = {
  search: (q: string, limit = 20) =>
    call<{ hits: Hit[] }>('GET', `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  templates: () => call<{ templates: Template[] }>('GET', '/api/templates'),
  kline: (code: string, kind: string, days = 120) =>
    call<{ bars: Bar[]; quote: Quote | null }>(
      'GET',
      `/api/kline?code=${encodeURIComponent(code)}&kind=${kind}&days=${days}`,
    ),
  prompt: (body: unknown) => call<PromptResult>('POST', '/api/prompt', body),
  recent: () => call<{ entries: Entry[] }>('GET', '/api/recent'),
  remember: (code: string) => call<{ entries: Entry[] }>('POST', '/api/recent', { code }),
}
