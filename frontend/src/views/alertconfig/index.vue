<template>
  <section class="page" data-module="alertconfig">
    <header class="page-head">
      <div>
        <h2>指标预警管理</h2>
        <p class="page-desc">
          预警规则绑定指标口径版本：草稿 → 规则评审中 → 已生效 → 已关闭（终点）。指标换版或下线会冻结规则，
          关闭后连续周期停在原值，不再累计也不再通知。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记预警规则</button>
        <button class="btn" type="button" @click="exportRows">导出指标预警清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="applyFilters">
      <label class="filter-item">
        <span>关键词</span>
        <input v-model="filters.keyword" placeholder="预警编号 / 指标 / 指标版本 / 接收人" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>指标版本</span>
        <input v-model="filters.metric_version" placeholder="例如 METR-0001-v1" />
      </label>
      <label class="filter-item">
        <span>排序</span>
        <select v-model="filters.sort_by">
          <option v-for="field in sortFields" :key="field" :value="field">{{ field }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>方向</span>
        <select v-model="filters.order">
          <option value="asc">升序</option>
          <option value="desc">降序</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>序号 / 位置</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>指标状态</th>
          <th>连续周期</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['序号'] ?? '—' }} / {{ row['序列位置'] ?? '—' }}</td>
          <td v-for="column in columns" :key="column">{{ displayValue(row, column) }}</td>
          <td>
            <span :class="['tag', row['指标可用'] ? 'tag-ok' : 'tag-warn']">{{ row['指标状态'] ?? '—' }}</span>
          </td>
          <td>
            {{ row['连续周期数'] ?? 0 }}
            <span v-if="row.status === '已关闭'" class="muted">（关闭前 {{ row['关闭前连续周期'] ?? 0 }}）</span>
            <span v-else-if="row.status === '已冻结'" class="muted">（冻结保留）</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actionsFor(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无指标预警数据，可先登记预警规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>
        共 {{ total }} 条指标预警记录 · 第 {{ page }} / {{ pageCount }} 页
        <span v-if="activeMetricVersion"> · 当前定位指标版本 {{ activeMetricVersion }}</span>
      </span>
      <span class="pager">
        <button class="btn" type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <button class="btn" type="button" :disabled="page >= pageCount" @click="goPage(page + 1)">下一页</button>
      </span>
    </footer>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <section v-if="detail" class="panel" data-role="alert-detail">
      <header class="panel-head">
        <h3>规则 {{ detail.rule['预警编号'] }} 追溯</h3>
        <button class="btn ghost" type="button" @click="detail = null">收起</button>
      </header>
      <dl class="kv">
        <dt>规则状态</dt><dd>{{ detail.rule.status }}</dd>
        <dt>绑定指标</dt><dd>{{ detail.rule['关联指标'] }} / {{ detail.rule['关联指标版本'] ?? '未绑定' }}</dd>
        <dt>绑定版本状态</dt><dd>{{ detail.binding['绑定版本状态'] }}</dd>
        <dt>指标当前状态</dt><dd>{{ detail.metric_brief['指标状态'] }}（{{ detail.metric_brief['发布状态'] ?? '未发布' }}）</dd>
        <dt>当前已发布版本</dt><dd>{{ detail.binding['当前已发布版本'] ?? '无' }}</dd>
        <dt>版本一致</dt><dd>{{ detail.binding['版本一致'] ? '是' : '否' }}</dd>
        <dt>阈值 / 比较符</dt><dd>{{ detail.rule['阈值数值'] }} / {{ detail.rule['比较运算符展示'] }}</dd>
        <dt>连续周期</dt><dd>{{ detail.rule['连续周期数'] }}</dd>
      </dl>
      <div class="panel-grid">
        <div>
          <h4>规则历史</h4>
          <ul class="timeline">
            <li v-for="item in detail.history" :key="String(item.id)">
              <span class="muted">{{ item['时间'] }}</span>
              <strong>{{ item['事件'] }}</strong>
              <span>{{ item['前状态'] }} → {{ item['后状态'] }}</span>
              <p class="muted">{{ item['说明'] }}</p>
            </li>
            <li v-if="!detail.history.length" class="muted">暂无历史</li>
          </ul>
        </div>
        <div>
          <h4>通知记录</h4>
          <ul class="timeline">
            <li v-for="item in detail.notifications" :key="String(item.id)">
              <span class="muted">{{ item['触发周期'] }}</span>
              <strong>{{ item['通知状态'] }}</strong>
              <span>取值 {{ item['取值'] }} / 阈值 {{ item['阈值'] }}</span>
              <p class="muted">{{ item['说明'] }}</p>
            </li>
            <li v-if="!detail.notifications.length" class="muted">暂无通知</li>
          </ul>
        </div>
      </div>
      <div v-if="detail.metric">
        <h4>指标口径版本</h4>
        <table class="data-table">
          <thead><tr><th>口径版本</th><th>状态</th><th>发布时间</th><th>下线时间</th><th>回滚自</th></tr></thead>
          <tbody>
            <tr v-for="version in detail.metric['版本追溯']" :key="version['口径版本']">
              <td>{{ version['口径版本'] }}</td>
              <td>{{ version['状态'] }}</td>
              <td>{{ version['发布时间'] ?? '—' }}</td>
              <td>{{ version['下线时间'] ?? '—' }}</td>
              <td>{{ version['回滚自版本'] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="showCreate" class="panel" data-role="alert-create">
      <header class="panel-head">
        <h3>登记预警规则</h3>
        <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
      </header>
      <form class="form-grid" @submit.prevent="submitCreate">
        <label v-for="field in createFields" :key="field.key" class="filter-item">
          <span>{{ field.label }}</span>
          <input v-model="createForm[field.key]" :placeholder="field.placeholder" />
        </label>
        <button class="btn primary" type="submit">登记为草稿</button>
      </form>
    </section>

    <section v-if="editing" class="panel" data-role="alert-edit">
      <header class="panel-head">
        <h3>修订规则 {{ editing.rule['预警编号'] }}（草稿）</h3>
        <button class="btn ghost" type="button" @click="editing = null">取消</button>
      </header>
      <form class="form-grid" @submit.prevent="submitUpdate">
        <label class="filter-item">
          <span>阈值数值</span>
          <input v-model="editing.threshold" />
        </label>
        <label class="filter-item">
          <span>比较运算符</span>
          <select v-model="editing.operator">
            <option v-for="(label, value) in operatorOptions" :key="value" :value="value">{{ value }}（{{ label }}）</option>
          </select>
        </label>
        <button class="btn primary" type="submit">保存（规则版本 {{ editing.version }}）</button>
      </form>
      <p class="muted">阈值与比较运算符只能在草稿中修改；已生效的规则先「发起修订」、已冻结的先「重新评审」，关闭后不能再改。</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/alertconfig'
const columns = ['预警编号', '关联指标', '关联指标版本', '比较运算符展示', '阈值数值', '持续周期数', '静默时段', '状态', '关闭时间']
const statuses = ['草稿', '规则评审中', '已生效', '已冻结', '已关闭']
const sortFields = ['id', '预警编号', '关联指标', '关联指标版本', '状态', '连续周期数', '阈值数值', '关闭时间']
const operatorOptions: Record<string, string> = {
  greater_than: '大于',
  greater_than_or_equal: '大于等于',
  less_than: '小于',
  less_than_or_equal: '小于等于',
  equal: '等于',
  not_equal: '不等于',
}

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const errorMessage = ref('')
const detail = ref<Row | null>(null)
const showCreate = ref(false)
const editing = ref<{ rule: Row; threshold: string; operator: string; version: number } | null>(null)
const stats = ref([
  { label: '预警规则总数', value: 0 },
  { label: '已生效规则', value: 0 },
  { label: '已冻结规则', value: 0 },
])
const filters = reactive({ keyword: '', status: '', metric_version: '', sort_by: 'id', order: 'asc' })
const activeMetricVersion = ref('')
const createFields = [
  { key: '预警编号', label: '预警编号', placeholder: 'ALER-2026-080' },
  { key: '关联指标', label: '关联指标', placeholder: 'METR-0001' },
  { key: '比较运算符', label: '比较运算符', placeholder: 'greater_than / > / 大于' },
  { key: '阈值数值', label: '阈值数值', placeholder: '99.5' },
  { key: '持续周期数', label: '持续周期数', placeholder: '3' },
  { key: '预警级别', label: '预警级别', placeholder: 'P1' },
  { key: '通知接收人', label: '通知接收人', placeholder: '支付值班组' },
  { key: '静默时段', label: '静默时段', placeholder: '00:00-06:00' },
]
const createForm = reactive<Record<string, string>>({})

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

function displayValue(row: Row, column: string) {
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : value
}

function actionsFor(row: Row) {
  switch (row.status) {
    case '草稿':
      return ['提交规则评审', '关闭规则']
    case '规则评审中':
      return ['生效规则', '撤回评审', '关闭规则']
    case '已生效':
      return ['关闭规则', '发起修订']
    case '已冻结':
      return ['重新评审', '关闭规则']
    default:
      return []
  }
}

async function loadStats() {
  const queries = ['已生效', '已冻结'].map((status) =>
    fetchJson<{ total: number }>(`${ENDPOINT}?status=${encodeURIComponent(status)}&size=1`).catch(() => ({ total: 0 })),
  )
  const [all, active, frozen] = await Promise.all([
    fetchJson<{ total: number }>(`${ENDPOINT}?size=1`).catch(() => ({ total: 0 })),
    ...queries,
  ])
  stats.value = [
    { label: '预警规则总数', value: all.total ?? 0 },
    { label: '已生效规则', value: active.total ?? 0 },
    { label: '已冻结规则', value: frozen.total ?? 0 },
  ]
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams({ page: String(page.value), size: String(size.value) })
  if (filters.keyword) params.set('keyword', filters.keyword)
  if (filters.status) params.set('status', filters.status)
  if (filters.metric_version) params.set('metric_version', filters.metric_version)
  params.set('sort_by', filters.sort_by)
  params.set('order', filters.order)
  try {
    const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${params.toString()}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    activeMetricVersion.value = filters.metric_version
    const maxPage = Math.max(1, Math.ceil(total.value / size.value))
    if (page.value > maxPage) {
      page.value = maxPage
      await reload()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标预警列表读取失败'
  }
}

async function applyFilters() {
  page.value = 1
  await reload()
}

async function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  filters.metric_version = ''
  filters.sort_by = 'id'
  filters.order = 'asc'
  page.value = 1
  await reload()
}

async function goPage(next: number) {
  if (next < 1 || next > pageCount.value) return
  page.value = next
  await reload()
}

function exportRows() {
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.metric_version) params.set('metric_version', filters.metric_version)
  window.open(`${ENDPOINT}/export?${params.toString()}`, '_blank')
}

function openCreate() {
  showCreate.value = true
  errorMessage.value = ''
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...createForm } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    showCreate.value = false
    for (const key of Object.keys(createForm)) delete createForm[key]
    page.value = 1
    await Promise.all([reload(), loadStats()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预警规则登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, expected_status: row.status } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    if (action === '发起修订' || action === '重新评审') {
      editing.value = {
        rule: payload.entry,
        threshold: String(payload.entry['阈值数值'] ?? ''),
        operator: String(payload.entry['比较运算符'] ?? 'greater_than'),
        version: Number(payload.entry['规则版本'] ?? 1),
      }
    }
    await Promise.all([reload(), loadStats()])
    if (detail.value?.rule?.id === row.id) await openDetail(row, false)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标预警操作失败'
  }
}

async function submitUpdate() {
  if (!editing.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${editing.value.rule.id}`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          阈值数值: editing.value.threshold,
          比较运算符: editing.value.operator,
          expected_rule_version: editing.value.version,
        },
      }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    editing.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预警规则修改失败'
  }
}

async function openDetail(row: Row, scroll = true) {
  try {
    detail.value = await fetchJson<Row>(`${ENDPOINT}/${row.id}/trace`)
    if (scroll) window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预警追溯读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadStats()])
})
</script>
