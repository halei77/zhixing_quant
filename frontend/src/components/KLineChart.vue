<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { Bar } from '@/api'

const props = defineProps<{ bars: Bar[] }>()
const canvas = ref<HTMLCanvasElement | null>(null)

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

  const bars = props.bars
  if (bars.length < 2) return

  const view = bars.slice(-90)
  const pad = { top: 10, right: 6, bottom: 34, left: 6 }
  const highs = view.map((b) => b.high)
  const lows = view.map((b) => b.low)
  const max = Math.max(...highs)
  const min = Math.min(...lows)
  const span = max - min || 1
  const plotH = height - pad.top - pad.bottom
  const step = (width - pad.left - pad.right) / view.length
  const bodyW = Math.max(2, step * 0.62)
  const y = (price: number) => pad.top + (1 - (price - min) / span) * plotH

  const grid = 'rgba(128, 138, 155, 0.18)'
  const label = 'rgba(128, 138, 155, 0.75)'
  ctx.strokeStyle = grid
  ctx.fillStyle = label
  ctx.font = '10px -apple-system, sans-serif'
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
    const rising = b.close >= b.open
    const color = rising ? '#ff453a' : '#30d158' // A股：红涨绿跌
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

  ctx.fillStyle = label
  ctx.fillText(view[view.length - 1].date, width - pad.right - 64, height - pad.bottom + 16)
  ctx.fillText(view[0].date, pad.left, height - pad.bottom + 16)
}

let raf = 0
function schedule() {
  cancelAnimationFrame(raf)
  raf = requestAnimationFrame(draw)
}

onMounted(() => {
  draw()
  window.addEventListener('resize', schedule)
})
onBeforeUnmount(() => window.removeEventListener('resize', schedule))
watch(() => props.bars, schedule, { deep: true })
</script>

<template>
  <canvas ref="canvas" class="block w-full" style="height: 260px" aria-label="K线图" />
</template>
