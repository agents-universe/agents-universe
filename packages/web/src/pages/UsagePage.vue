<template>
  <div class="usage-page">
    <div class="usage-page-inner">
      <header class="usage-header">
        <div class="usage-header-left">
          <span class="usage-header-icon"><BarChart3 :size="20" /></span>
          <div>
            <h1 class="usage-title">{{ t('usage.title') }}</h1>
            <p class="usage-sub">{{ t('usage.subtitle') }}</p>
          </div>
        </div>
        <div class="usage-range" role="group" :aria-label="t('usage.title')">
          <button
            v-for="opt in ranges"
            :key="opt.days"
            :class="days === opt.days ? 'btn-sm' : 'btn-sm secondary'"
            @click="days = opt.days"
          >
            {{ t(opt.labelKey) }}
          </button>
        </div>
      </header>

      <div v-if="loading && !data" class="usage-state">
        <Loader2 :size="18" class="usage-spin" />
        <span>{{ t('common.loading') }}</span>
      </div>

      <div v-else-if="error && !data" class="usage-message error">
        ✗ {{ error }}
        <button class="btn-sm secondary" @click="load">{{ t('usage.retry') }}</button>
      </div>

      <div v-else-if="data" class="usage-content" :class="{ refreshing }">
        <!-- Stale data stays on screen during a range switch; a failed
             refresh surfaces here without blanking the previous render. -->
        <div v-if="error" class="usage-message error">
          ✗ {{ error }}
          <button class="btn-sm secondary" @click="load">{{ t('usage.retry') }}</button>
        </div>

        <div v-if="data.summary.total_tokens === 0" class="empty-state">
          <span class="empty-state-icon"><BarChart3 :size="18" /></span>
          <p>{{ t('usage.empty') }}</p>
        </div>

        <template v-else>
          <section class="usage-summary" :aria-label="t('usage.title')">
            <div
              v-for="tile in tiles"
              :key="tile.labelKey"
              class="usage-tile"
              :title="tile.exact.toLocaleString()"
            >
              <span class="usage-tile-label">{{ t(tile.labelKey) }}</span>
              <span class="usage-tile-value">{{ fmtCompact(tile.value) }}</span>
            </div>
          </section>

          <section class="usage-card">
            <h2 class="usage-card-title">{{ t('usage.topUsers') }}</h2>
            <ol class="usage-bars">
              <li
                v-for="u in data.top_users"
                :key="u.user_id"
                class="usage-bar-row"
                :title="t('usage.barLabel', {
                  user: userTooltip(u),
                  tokens: u.tokens.toLocaleString(),
                  runs: u.run_count,
                })"
              >
                <span class="usage-rank" :class="{ podium: u.rank <= 3 }">{{ u.rank }}</span>
                <span class="usage-bar-user">
                  <span class="usage-user-id">{{ userLabel(u) }}</span>
                  <span class="usage-user-runs">{{ t('usage.runsUnit', { n: u.run_count }) }}</span>
                </span>
                <span class="usage-bar-track">
                  <span
                    class="usage-bar-fill"
                    :style="{ width: barPct(u.tokens) + '%' }"
                  />
                </span>
                <span class="usage-bar-value" :title="u.tokens.toLocaleString()">
                  {{ fmtCompact(u.tokens) }}
                </span>
              </li>
            </ol>
          </section>

          <section class="usage-card">
            <h2 class="usage-card-title">{{ t('usage.dailyTrend') }}</h2>
            <div class="usage-trend">
              <svg
                :viewBox="`0 0 ${CHART_W} ${CHART_H}`"
                class="usage-trend-svg"
                role="img"
                :aria-label="t('usage.dailyTrend')"
              >
                <line
                  :x1="0"
                  :y1="PLOT_H"
                  :x2="CHART_W"
                  :y2="PLOT_H"
                  class="usage-baseline"
                />
                <template v-for="(d, i) in trend.daily" :key="d.date">
                  <path
                    v-if="d.tokens > 0"
                    :d="barPath(i, d.tokens)"
                    class="usage-col"
                  />
                  <text
                    v-if="d.tokens > 0 && i === peakIndex"
                    class="usage-peak"
                    :x="i * trend.band + trend.band / 2"
                    :y="peakLabelY(d.tokens)"
                    text-anchor="middle"
                  >
                    {{ fmtCompact(d.tokens) }}
                  </text>
                  <!-- Full-band hit target: hover works anywhere in the
                       column's slot, not just on the mark itself. -->
                  <rect
                    class="usage-hit"
                    :x="i * trend.band"
                    y="0"
                    :width="trend.band"
                    :height="PLOT_H"
                  >
                    <title>{{ t('usage.dayLabel', {
                      date: d.date,
                      tokens: d.tokens.toLocaleString(),
                      runs: d.run_count,
                    }) }}</title>
                  </rect>
                  <text
                    v-if="i % labelStep === 0"
                    class="usage-xlabel"
                    :x="i * trend.band + trend.band / 2"
                    :y="PLOT_H + 18"
                    text-anchor="middle"
                  >
                    {{ d.date.slice(5) }}
                  </text>
                </template>
              </svg>
            </div>
            <!-- Table twin: the trend's values stay readable without hover. -->
            <details class="usage-table-details">
              <summary>{{ t('usage.viewTable') }}</summary>
              <table class="usage-table">
                <thead>
                  <tr>
                    <th>{{ t('usage.tableDate') }}</th>
                    <th>{{ t('usage.tableTokens') }}</th>
                    <th>{{ t('usage.tableRuns') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="d in data.daily" :key="d.date">
                    <td>{{ d.date }}</td>
                    <td>{{ d.tokens.toLocaleString() }}</td>
                    <td>{{ d.run_count }}</td>
                  </tr>
                </tbody>
              </table>
            </details>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { BarChart3, Loader2 } from 'lucide-vue-next'
import { usageApi, type UsageDashboard, type UsageTopUser } from '@/api/usage'
import { translateApiErrorWith } from '@/utils/apiError'

const { t } = useI18n()
const route = useRoute()

const projectId = computed(() => route.params.projectId as string)

const ranges = [
  { days: 7, labelKey: 'usage.range7' },
  { days: 30, labelKey: 'usage.range30' },
  { days: 90, labelKey: 'usage.range90' },
] as const

const days = ref(30)
const data = ref<UsageDashboard | null>(null)
const loading = ref(false)
/** A range switch with data already on screen: dim, don't blank. */
const refreshing = ref(false)
const error = ref<string | null>(null)

async function load() {
  const pid = projectId.value
  if (!pid) return
  if (data.value) refreshing.value = true
  else loading.value = true
  error.value = null
  try {
    const res = await usageApi.getUsage(pid, days.value)
    if (pid !== projectId.value) return // stale project switch
    data.value = res
  } catch (e) {
    error.value = translateApiErrorWith(e, t('usage.error'))
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

onMounted(load)
watch(days, load)

const tiles = computed(() => {
  const s = data.value?.summary
  if (!s) return []
  return [
    { labelKey: 'usage.totalTokens', value: s.total_tokens, exact: s.total_tokens },
    { labelKey: 'usage.runCount', value: s.run_count, exact: s.run_count },
    { labelKey: 'usage.userCount', value: s.user_count, exact: s.user_count },
    { labelKey: 'usage.conversationCount', value: s.conversation_count, exact: s.conversation_count },
  ]
})

/** Auto-compact per the stat-tile contract; exact value stays in the title. */
function fmtCompact(n: number): string {
  if (n < 1000) return String(n)
  if (n < 1_000_000) {
    const v = n / 1000
    return `${v < 100 ? v.toFixed(1).replace(/\.0$/, '') : Math.round(v)}K`
  }
  const v = n / 1_000_000
  return `${v < 100 ? v.toFixed(1).replace(/\.0$/, '') : Math.round(v)}M`
}

/* ── Top-10 bars ─────────────────────────────────────────────────────── */

/** Display name when the server knows one, else the raw id. */
function userLabel(u: UsageTopUser): string {
  return u.display_name || u.user_id
}

/** Tooltip keeps the id reachable even when the row shows a name. */
function userTooltip(u: UsageTopUser): string {
  return u.display_name && u.display_name !== u.user_id
    ? `${u.display_name} (${u.user_id})`
    : u.user_id
}

const maxTopTokens = computed(() =>
  Math.max(...(data.value?.top_users ?? []).map((u) => u.tokens), 1),
)

function barPct(tokens: number): number {
  // A non-zero token count never renders as an invisible sliver.
  const pct = (tokens / maxTopTokens.value) * 100
  return tokens > 0 ? Math.max(pct, 2) : 0
}

/* ── Daily trend geometry ────────────────────────────────────────────── */

const CHART_W = 720
const PLOT_H = 140
const AXIS_H = 30
const CHART_H = PLOT_H + AXIS_H
/** Headroom above the tallest column for its direct label. */
const HEADROOM = 18
/** Column thickness cap: never fill the band — leftover width is air. */
const BAR_MAX = 24

const trend = computed(() => {
  const daily = data.value?.daily ?? []
  const max = Math.max(...daily.map((d) => d.tokens), 1)
  const band = CHART_W / Math.max(daily.length, 1)
  const gap = 2 // surface gap between adjacent marks
  const barW = Math.max(Math.min(band - gap, BAR_MAX), 1)
  return { daily, max, band, barW }
})

const peakIndex = computed(() => {
  let idx = -1
  let max = -1
  trend.value.daily.forEach((d, i) => {
    if (d.tokens > max) {
      max = d.tokens
      idx = i
    }
  })
  return idx
})

const labelStep = computed(() => Math.ceil(trend.value.daily.length / 7) || 1)

function barHeight(tokens: number): number {
  return (tokens / trend.value.max) * (PLOT_H - HEADROOM)
}

function barPath(i: number, tokens: number): string {
  const { band, barW } = trend.value
  const w = barW
  const h = barHeight(tokens)
  const x = i * band + (band - w) / 2 // centered: band leftover reads as air
  const y = PLOT_H - h
  const r = Math.min(4, w / 2, h) // 4px rounded data-end, square at baseline
  return [
    `M${x},${y + h}`,
    `L${x},${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `L${x + w - r},${y}`,
    `Q${x + w},${y} ${x + w},${y + r}`,
    `L${x + w},${y + h}`,
    'Z',
  ].join(' ')
}

function peakLabelY(tokens: number): number {
  return PLOT_H - barHeight(tokens) - 6
}
</script>

<style scoped>
.usage-page {
  height: 100%;
  overflow-y: auto;
  background: var(--bg-primary);
}
.usage-page-inner {
  max-width: 980px;
  margin: 0 auto;
  padding: 28px 24px 48px;
}

.usage-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}
.usage-header-left {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
}
.usage-header-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: var(--radius-full);
  background: var(--accent-dim);
  color: var(--accent);
  flex: none;
}
.usage-title {
  font-size: var(--text-xl);
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}
.usage-sub {
  margin: 4px 0 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
}
.usage-range {
  display: flex;
  gap: 4px;
  align-items: center;
}
.usage-range .btn-sm {
  margin-right: 0;
}

.usage-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 12px;
  color: var(--text-secondary);
  font-size: var(--text-sm);
}
.usage-spin {
  animation: usage-spin 1s linear infinite;
}
@keyframes usage-spin {
  to { transform: rotate(360deg); }
}
.usage-message {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  margin-bottom: 16px;
}
.usage-message.error {
  background: rgba(208, 59, 59, 0.12);
  color: #f0a0a0;
  border: 1px solid rgba(208, 59, 59, 0.35);
}

.usage-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
  transition: opacity var(--dur-base);
}
.usage-content.refreshing {
  opacity: 0.55;
  pointer-events: none;
}

/* ── Stat tiles ──────────────────────────────────────────────────────── */

.usage-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.usage-tile {
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.usage-tile-label {
  font-size: var(--text-xs);
  color: var(--text-secondary);
}
.usage-tile-value {
  font-size: var(--text-2xl);
  font-weight: 600;
  color: var(--text-primary);
  /* Proportional figures: large standalone numbers keep default widths. */
}

/* ── Cards ───────────────────────────────────────────────────────────── */

.usage-card {
  background: var(--glass-bg);
  backdrop-filter: blur(var(--glass-blur, 12px));
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  padding: 18px 20px;
}
.usage-card-title {
  margin: 0 0 14px;
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--text-primary);
}

/* ── Top-10 ranking bars ─────────────────────────────────────────────── */

.usage-bars {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.usage-bar-row {
  display: grid;
  grid-template-columns: 26px minmax(120px, 190px) 1fr auto;
  align-items: center;
  gap: 12px;
  min-height: 24px;
}
.usage-rank {
  font-size: var(--text-sm);
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  text-align: right;
}
.usage-rank.podium {
  color: var(--text-primary);
  font-weight: 600;
}
.usage-bar-user {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 1px;
}
.usage-user-id {
  font-size: var(--text-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.usage-user-runs {
  font-size: var(--text-2xs);
  color: var(--text-muted);
}
.usage-bar-track {
  display: block;
  height: 16px;
  background: rgba(74, 142, 240, 0.14); /* lighter step of the series hue */
  border-radius: 0 4px 4px 0;
  min-width: 0;
  overflow: hidden;
}
.usage-bar-fill {
  display: block;
  height: 100%;
  /* Single series: one hue for every bar — length carries the value. */
  background: var(--usage-series, #4a8ef0);
  border-radius: 0 4px 4px 0; /* rounded data-end, square at baseline */
}
.usage-bar-value {
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums; /* value column aligns vertically */
  text-align: right;
  min-width: 56px;
}

/* ── Daily trend ─────────────────────────────────────────────────────── */

.usage-trend {
  width: 100%;
}
.usage-trend-svg {
  display: block;
  width: 100%;
  height: auto;
}
.usage-baseline {
  stroke: var(--text-muted);
  stroke-width: 1;
  opacity: 0.5;
}
.usage-col {
  fill: var(--usage-series, #4a8ef0);
}
.usage-hit {
  fill: transparent;
  pointer-events: all;
}
.usage-peak {
  font-size: 11px;
  fill: var(--text-secondary);
}
.usage-xlabel {
  font-size: 10px;
  fill: var(--text-muted);
}

/* ── Table twin ──────────────────────────────────────────────────────── */

.usage-table-details {
  margin-top: 12px;
  font-size: var(--text-sm);
}
.usage-table-details summary {
  cursor: pointer;
  color: var(--text-secondary);
  user-select: none;
}
.usage-table-details summary:hover {
  color: var(--text-primary);
}
.usage-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
}
.usage-table th,
.usage-table td {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 1px solid var(--glass-border);
  color: var(--text-secondary);
  font-weight: 400;
}
.usage-table th {
  color: var(--text-primary);
  font-weight: 600;
}
.usage-table td:nth-child(2),
.usage-table td:nth-child(3),
.usage-table th:nth-child(2),
.usage-table th:nth-child(3) {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 640px) {
  .usage-bar-row {
    grid-template-columns: 22px minmax(80px, 120px) 1fr auto;
    gap: 8px;
  }
}
</style>
