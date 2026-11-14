<template>
  <section class="page" data-module="metric">
    <header class="page-head">
      <div>
        <h2>指标口径管理</h2>
        <p class="page-desc">
          口径按版本评审发布：<strong>草稿 → 口径评审中 → 已发布 → 已下线</strong>。发布新版本会冻结绑定旧版本的预警规则；
          下线后恢复必须回滚生成新的评审版本，不能直接复用已下线版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记指标定义</button>
        <button class="btn" type="button" @click="exportRows">导出指标口径清单</button>
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
        <input v-model="filters.keyword" placeholder="指标编号 / 名称 / 版本" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>序号</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>指标状态</th>
          <th>已发布版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['序号'] ?? '—' }}</td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['tag', row['可用'] ? 'tag-ok' : 'tag-warn']">{{ row['指标当前状态'] }}</span>
          </td>
          <td>
            {{ row['已发布版本'] ?? '—' }}
            <span v-if="row['评审中版本']" class="muted">（{{ row['评审中版本'] }} 评审中）</span>
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
          <td :colspan="columns.length + 4" class="empty-state">暂无指标口径数据，可先登记指标定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条指标口径记录 · 第 {{ page }} / {{ pageCount }} 页</span>
      <span class="pager">
        <button class="btn" type="button" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
        <button class="btn" type="button" :disabled="page >= pageCount" @click="goPage(page + 1)">下一页</button>
      </span>
    </footer>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <section v-if="detail" class="panel" data-role="metric-detail">
      <header class="panel-head">
        <h3>指标 {{ detail.metric['指标编号'] }} · {{ detail.metric['指标名称'] }}</h3>
        <button class="btn ghost" type="button" @click="detail = null">收起</button>
      </header>
      <p class="muted">
        当前状态 {{ detail.metric['指标当前状态'] }} · 发布状态 {{ detail.metric['发布状态'] }} ·
        已发布版本 {{ detail.metric['已发布版本'] ?? '无' }} · 可用 {{ detail.metric['可用'] ? '是' : '否' }}
      </p>
      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">关联规则</span><strong class="stat-value">{{ detail.summary['规则总数'] }}</strong></article>
        <article class="stat-card"><span class="stat-label">已生效</span><strong class="stat-value">{{ detail.summary['已生效'] }}</strong></article>
        <article class="stat-card"><span class="stat-label">已冻结</span><strong class="stat-value">{{ detail.summary['已冻结'] }}</strong></article>
        <article class="stat-card"><span class="stat-label">已关闭</span><strong class="stat-value">{{ detail.summary['已关闭'] }}</strong></article>
      </div>
      <h4>关联预警规则（状态与指标详情一致）</h4>
      <table class="data-table">
        <thead><tr><th>预警编号</th><th>规则状态</th><th>绑定版本</th><th>绑定版本状态</th><th>连续周期</th><th>阈值 / 比较符</th></tr></thead>
        <tbody>
          <tr v-for="rule in detail.alerts" :key="String(rule.id)">
            <td>{{ rule['预警编号'] }}</td>
            <td>{{ rule['status'] }}</td>
            <td>{{ rule['关联指标版本'] ?? '未绑定' }}</td>
            <td>{{ rule['绑定版本状态'] }}</td>
            <td>{{ rule['连续周期数'] }}</td>
            <td>{{ rule['阈值数值'] }} / {{ rule['比较运算符展示'] }}</td>
          </tr>
          <tr v-if="!detail.alerts.length"><td colspan="6" class="empty-state">该指标暂无预警规则</td></tr>
        </tbody>
      </table>
      <h4>口径版本追溯</h4>
      <table class="data-table">
        <thead><tr><th>口径版本</th><th>状态</th><th>提交时间</th><th>发布时间</th><th>下线时间</th><th>回滚自</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="version in detail.metric['版本追溯']" :key="version['口径版本']">
            <td>{{ version['口径版本'] }}</td>
            <td>{{ version['状态'] }}</td>
            <td>{{ version['提交时间'] ?? '—' }}</td>
            <td>{{ version['发布时间'] ?? '—' }}</td>
            <td>{{ version['下线时间'] ?? '—' }}</td>
            <td>{{ version['回滚自版本'] ?? '—' }}</td>
            <td>{{ version['备注'] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <h4>操作历史</h4>
      <ul class="timeline">
        <li v-for="item in detail.history" :key="String(item.id)">
          <span class="muted">{{ item['时间'] }}</span>
          <strong>{{ item['事件'] }}</strong>
          <span>{{ item['前状态'] }} → {{ item['后状态'] }}</span>
          <p class="muted">{{ item['说明'] }}</p>
        </li>
        <li v-if="!detail.history.length" class="muted">暂无历史</li>
      </ul>
    </section>

    <section v-if="showCreate" class="panel" data-role="metric-create">
      <header class="panel-head">
        <h3>登记指标定义</h3>
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

    <section v-if="rollingBack" class="panel" data-role="metric-rollback">
      <header class="panel-head">
        <h3>回滚指标 {{ rollingBack['指标编号'] }}</h3>
        <button class="btn ghost" type="button" @click="rollingBack = null">取消</button>
      </header>
      <form class="form-grid" @submit.prevent="submitRollback">
        <label class="filter-item">
          <span>目标版本（将复制成一个新的评审版本）</span>
          <select v-model="rollbackTarget">
            <option v-for="version in rollingBack['版本追溯']" :key="version['口径版本']" :value="version['口径版本']">
              {{ version['口径版本'] }}（{{ version['状态'] }}）
            </option>
          </select>
        </label>
        <button class="btn primary" type="submit">生成评审版本</button>
      </form>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/metric'
const columns = ['指标编号', '指标名称', '计算公式', '统计粒度', '时间窗口', '口径版本', '版本号']
const statuses = ['草稿', '口径评审中', '已发布', '已下线']

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const errorMessage = ref('')
const detail = ref<Row | null>(null)
const showCreate = ref(false)
const rollingBack = ref<Row | null>(null)
const rollbackTarget = ref('')
const stats = ref([
  { label: '指标定义总数', value: 0 },
  { label: '已发布指标', value: 0 },
  { label: '口径评审中', value: 0 },
])
const filters = reactive({ keyword: '', status: '' })
const createFields = [
  { key: '指标编号', label: '指标编号', placeholder: 'METR-0006' },
  { key: '指标名称', label: '指标名称', placeholder: '支付成功率' },
  { key: '计算公式', label: '计算公式', placeholder: '成功笔数 / 总笔数 * 100' },
  { key: '统计粒度', label: '统计粒度', placeholder: '自然日' },
  { key: '时间窗口', label: '时间窗口', placeholder: '最近 1 天' },
  { key: '筛选条件', label: '筛选条件', placeholder: '全部业务线' },
  { key: '精度位数', label: '精度位数', placeholder: '2' },
]
const createForm = reactive<Record<string, string>>({})

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

function actionsFor(row: Row) {
  switch (row['指标当前状态']) {
    case '草稿':
      return ['提交口径评审']
    case '口径评审中':
      return ['发布指标']
    case '已发布':
      return ['提交口径评审', '下线指标', '回滚版本']
    case '已下线':
      return ['回滚版本']
    default:
      return []
  }
}

async function loadStats() {
  const [all, published, reviewing] = await Promise.all([
    fetchJson<{ total: number }>(`${ENDPOINT}?size=1`).catch(() => ({ total: 0 })),
    fetchJson<{ total: number }>(`${ENDPOINT}?status=${encodeURIComponent('已发布')}&size=1`).catch(() => ({ total: 0 })),
    fetchJson<{ total: number }>(`${ENDPOINT}?status=${encodeURIComponent('口径评审中')}&size=1`).catch(() => ({ total: 0 })),
  ])
  stats.value = [
    { label: '指标定义总数', value: all.total ?? 0 },
    { label: '已发布指标', value: published.total ?? 0 },
    { label: '口径评审中', value: reviewing.total ?? 0 },
  ]
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams({ page: String(page.value), size: String(size.value) })
  if (filters.keyword) params.set('keyword', filters.keyword)
  if (filters.status) params.set('status', filters.status)
  try {
    const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${params.toString()}`)
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const maxPage = Math.max(1, Math.ceil(total.value / size.value))
    if (page.value > maxPage) {
      page.value = maxPage
      await reload()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标定义列表读取失败'
  }
}

async function applyFilters() {
  page.value = 1
  await reload()
}

async function resetFilters() {
  filters.keyword = ''
  filters.status = ''
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
    errorMessage.value = error instanceof Error ? error.message : '指标定义登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  if (action === '回滚版本') {
    rollingBack.value = row
    rollbackTarget.value = row['版本追溯']?.[0]?.['口径版本'] ?? ''
    return
  }
  try {
    const body: Record<string, any> = { action, expected_status: row['指标当前状态'] }
    if (action === '发布指标') body['评审人'] = '口径评审组'
    if (action === '下线指标') body['下线原因'] = prompt('请输入下线原因', '口径调整') ?? '口径调整'
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: body }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    await Promise.all([reload(), loadStats()])
    if (detail.value?.metric?.id === row.id) await openDetail(row, false)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标口径操作失败'
  }
}

async function submitRollback() {
  if (!rollingBack.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${rollingBack.value.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action: '回滚版本', 目标版本: rollbackTarget.value } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message)
    rollingBack.value = null
    await Promise.all([reload(), loadStats()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标回滚失败'
  }
}

async function openDetail(row: Row, scroll = true) {
  try {
    detail.value = await fetchJson<Row>(`${ENDPOINT}/${row.id}/detail`)
    if (scroll) window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标详情读取失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadStats()])
})
</script>
