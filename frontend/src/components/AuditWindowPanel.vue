<template>
  <section class="window-panel" data-testid="audit-window-panel">
    <header class="window-head">
      <h3>样本窗口审计</h3>
      <p class="window-desc">
        按「站点 + 模板 + 窗口」恢复待发布报告：暂无样本不伪造 0%，有样本全未命中记录真 0%。
      </p>
    </header>

    <form class="window-form" @submit.prevent="openOrResume">
      <label>
        <span>站点名称</span>
        <input v-model="form.site" data-testid="window-site" placeholder="如 站点E" />
      </label>
      <label>
        <span>解析模板</span>
        <input v-model="form.template" data-testid="window-template" placeholder="如 E站价格页模板" />
      </label>
      <label>
        <span>样本窗口</span>
        <input v-model="form.window" data-testid="window-window" placeholder="如 E18" />
      </label>
      <button class="btn primary" data-testid="window-open" type="submit" :disabled="busy">
        打开 / 恢复窗口
      </button>
    </form>

    <p v-if="message" class="window-message" data-testid="window-message">{{ message }}</p>
    <p v-if="error" class="error-text" data-testid="window-error">{{ error }}</p>

    <article v-if="current" class="window-card" data-testid="window-card">
      <div class="window-meta">
        <span>{{ current.站点名称 }} / {{ current.解析模板 }} / {{ current.样本窗口 }}</span>
        <span class="badge" :class="`tone-${stateTone(current.样本判定)}`" data-testid="window-state">
          {{ current.样本判定 }}
        </span>
        <span class="badge" :class="`tone-${stateTone(current.发布状态)}`" data-testid="window-publish-state">
          {{ publishLabel(current.发布状态, current.样本总数) }}
        </span>
      </div>

      <dl class="window-stats">
        <dt>样本总数（分母）</dt>
        <dd data-testid="window-total">{{ current.样本总数 }}</dd>
        <dt>命中数（分子）</dt>
        <dd data-testid="window-hit">{{ current.命中数 }}</dd>
        <dt>未命中数</dt>
        <dd data-testid="window-miss">{{ current.未命中数 }}</dd>
        <dt>字段命中率</dt>
        <dd data-testid="window-rate">{{ current.字段命中率 }}</dd>
        <dt>失败类别</dt>
        <dd data-testid="window-failure">{{ current.失败类别 }}</dd>
        <dt>发布版本</dt>
        <dd data-testid="window-version">{{ current.版本 }}</dd>
      </dl>

      <fieldset class="window-block">
        <legend>补报迟到样本（只进当前窗口，重复指纹不重复计入分母）</legend>
        <label class="window-line">
          <span>样本指纹（逗号分隔，可留空自动生成）</span>
          <input v-model="backfill.fingerprints" data-testid="backfill-fingerprints" placeholder="s1,s2" />
        </label>
        <div class="window-form">
          <label>
            <span>命中结果</span>
            <select v-model="backfill.result" data-testid="backfill-result">
              <option value="未命中">未命中</option>
              <option value="命中">命中</option>
            </select>
          </label>
          <label>
            <span>失败原因</span>
            <input v-model="backfill.reason" data-testid="backfill-reason" placeholder="如 价格节点缺失" />
          </label>
          <label>
            <span>补报条数（指纹为空时按此数量生成）</span>
            <input v-model.number="backfill.count" data-testid="backfill-count" type="number" min="1" max="50" />
          </label>
          <button class="btn" data-testid="backfill-submit" type="button" :disabled="busy" @click="submitBackfill">
            补报
          </button>
        </div>
      </fieldset>

      <div class="page-actions">
        <button class="btn primary" data-testid="window-publish" type="button" :disabled="busy" @click="publish(false)">
          发布窗口报告
        </button>
        <button class="btn" data-testid="window-retry-publish" type="button" :disabled="busy" @click="publish(true)">
          重试原发布
        </button>
        <button class="btn ghost" data-testid="window-verify" type="button" :disabled="busy" @click="verify">
          按回执核对指标口径
        </button>
      </div>

      <ul v-if="differences.length" class="window-diff" data-testid="window-diff">
        <li v-for="item in differences" :key="item">{{ item }}</li>
      </ul>

      <table v-if="current.回执历史.length" class="data-table" data-testid="window-receipts">
        <thead>
          <tr>
            <th>回执号</th><th>版本</th><th>分母</th><th>分子</th><th>命中率</th><th>失败类别</th><th>发布状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="receipt in current.回执历史" :key="receipt.回执号">
            <td>{{ receipt.回执号 }}</td>
            <td>{{ receipt.版本 }}</td>
            <td>{{ receipt.分母 }}</td>
            <td>{{ receipt.分子 }}</td>
            <td>{{ receipt.命中率 }}</td>
            <td>{{ receipt.失败类别 }}</td>
            <td>{{ receipt.发布状态 }}</td>
          </tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'

import {
  backfillWindow,
  openWindow,
  publishLabel,
  publishWindow,
  stateTone,
  verifyCaliber,
  type AuditWindow,
  type LateSample,
} from '@/domain/parse_audit'

const form = reactive({ site: '站点E', template: 'E站价格页模板', window: 'E18' })
const backfill = reactive({ fingerprints: 's1,s2', result: '未命中' as '命中' | '未命中', reason: '价格节点缺失', count: 2 })
const current = ref<AuditWindow | null>(null)
const message = ref('')
const error = ref('')
const differences = ref<string[]>([])
const busy = ref(false)

function key() {
  return { site: form.site.trim(), template: form.template.trim(), window: form.window.trim() }
}

async function guard(task: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try {
    await task()
  } catch (next) {
    error.value = next instanceof Error ? next.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function openOrResume() {
  return guard(async () => {
    const result = await openWindow(key())
    message.value = result.message
    current.value = result.data
    differences.value = []
  })
}

function submitBackfill() {
  return guard(async () => {
    const tokens = backfill.fingerprints
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    const count = Math.max(1, Math.min(50, Number(backfill.count) || 1))
    const samples: LateSample[] = (tokens.length ? tokens : Array.from({ length: count }, () => '')).map(
      (fingerprint) => ({
        样本指纹: fingerprint,
        命中结果: backfill.result,
        失败原因: backfill.result === '未命中' ? backfill.reason.trim() || '解析失败' : '',
      }),
    )
    const result = await backfillWindow(key(), samples)
    message.value = result.message
    if (result.data?.window) current.value = result.data.window
    differences.value = []
  })
}

function publish(retry: boolean) {
  return guard(async () => {
    const result = await publishWindow(key(), retry)
    message.value = result.message
    if (result.data?.window) current.value = result.data.window
    differences.value = []
  })
}

function verify() {
  return guard(async () => {
    const result = await verifyCaliber(key())
    message.value = result.message
    differences.value = result.data?.differences ?? []
  })
}
</script>
