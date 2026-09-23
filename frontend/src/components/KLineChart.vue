<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { Bar } from '@/api'

const props = defineProps<{ bars: Bar[] }>()
const canvas = ref<HTMLCanvasElement | null>(null)

/** 画布上的颜色一律读自 design token（docs/11 §3.1）：写死 #ff453a/#30d158 就是把深色档的
 *  红绿钉在浅色主题上，K线与旁边那两个数字会对不上。 */
function tokens(el: HTMLElement) {
  const css = getComputedStyle(el)
  const pick = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  return {
    up: pick('--up', '#d70015'),
    down: pick('--down', '#1a8a4a'),
    grid: pick('--stroke', 'rgba(15,23,42,.10)'),
    label: pick('--text-2', '#5b6472'),
  }
}

function draw() {
  const el = canvas.value
  if (!el) return
  const ratio = window.devicePixelRatio || 1
  const width = el.clientWidth
  const height = 260
  el.width = width * ratio
  el.height = height * ratio
  const ctx = el.getContext('2d')
  if (!ctx) return
  ctx.scale(ratio, ratio)
  ctx.clearRect(0, 0, width, height)

  const view = props.bars
  const t = tokens(el)
  ctx.font = '12px -apple-system, "SF Pro Text", "PingFang SC", sans-serif'

  // 少于两根画不出一根完整的K线。说清楚是「数据不够」，别留一张空白图让人以为坏了。
  if (view.length < 2) {
    ctx.fillStyle = t.label
    ctx.fillText('盘上K线不足两根，画不出来', 12, height / 2)
    return
  }

  // 画**全部**传进来的K线：悄悄 slice(-90) 等于把「取了 120 天」和「画了 90 天」这两件事
  // 分开又不告诉人（ADR-0012 要求实际区间写清楚）。区间用首末日期标在图上。
  const pad = { top: 10, right: 6, bottom: 34, left: 6 }
  const highs = view.map((b) => b.high)
  const lows = view.map((b) => b.low)
  const max = Math.max(...highs)
  const min = Math.min(...lows)
  const span = max - min || 1
  const plotH = height - pad.top - pad.bottom
  const step = (width - pad.left - pad.right) / view.length
  const bodyW = Math.max(1.5, step * 0.62)
  const y = (price: number) => pad.top + (1 - (price - min) / span) * plotH

  ctx.strokeStyle = t.grid
  ctx.fillStyle = t.label
  for (let i = 0; i <= 3; i++) {
    const price = min + (span * i) / 3
    const yy = y(price)
    ctx.beginPath()
    ctx.moveTo(pad.left, yy)
    ctx.lineTo(width - pad.right, yy)
    ctx.stroke()
    ctx.fillText(price.toFixed(2), pad.left + 2, yy - 3)
  }

  for (let i = 0; i < view.length; i++) {
    const b = view[i]
    const x = pad.left + step * (i + 0.5)
    // 口径沿用基准实现（旧 app.js）：收 >= 开 为阳，空心；否则实心。
    const rising = b.close >= b.open
    const color = rising ? t.up : t.down
    ctx.strokeStyle = color
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.moveTo(x, y(b.high))
    ctx.lineTo(x, y(b.low))
    ctx.stroke()
    const top = y(Math.max(b.open, b.close))
    const bottom = y(Math.min(b.open, b.close))
    if (rising) {
      ctx.strokeRect(x - bodyW / 2, top, bodyW, Math.max(1, bottom - top))
    } else {
      ctx.fillRect(x - bodyW / 2, top, bodyW, Math.max(1, bottom - top))
    }
  }

  ctx.fillStyle = t.label
  const last = view[view.length - 1].date
  const first = view[0].date
  ctx.fillText(`${first} → ${last}`, pad.left, height - pad.bottom + 20)
}

let raf = 0
function schedule() {
  cancelAnimationFrame(raf)
  raf = requestAnimationFrame(draw)
}

let observer: MutationObserver | null = null

onMounted(() => {
  draw()
  window.addEventListener('resize', schedule)
  // 主题换了要跟着重画，否则深浅两套红绿会停在上一次算出的颜色上。
  observer = new MutationObserver(schedule)
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', schedule)
  observer?.disconnect()
})
watch(() => props.bars, schedule, { deep: true })
</script>

<template>
  <canvas ref="canvas" class="block w-full" style="height: 260px" aria-label="K线图" />
</template>
