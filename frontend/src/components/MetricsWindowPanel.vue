<template>
  <section class="window-panel" data-testid="metrics-window-panel">
    <header class="window-head">
      <h3>解析质量口径（按窗口）</h3>
      <p class="window-desc">
        消费解析质量审计发布的分子分母与失败类别；旁边时段的窗口各自独立成行，重复提交不改分母。
      </p>
    </header>

    <button class="btn" data-testid="metrics-refresh" type="button" :disabled="busy" @click="load">
      刷新指标窗口
    </button>
    <p v-if="error" class="error-text" data-testid="metrics-error">{{ error }}</p>

    <table class="data-table" data-testid="metrics-window-table">
      <thead>
        <tr>
          <th>站点</th><th>解析模板</th><th>样本窗口</th>
          <th>分母</th><th>分子</th><th>命中率</th><th>失败类别</th><th>指标状态</th><th>审计回执号</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="`${row.站点名称}-${row.解析模板}-${row.样本窗口}`" :data-testid="`metrics-row-${row.样本窗口}`">
          <td>{{ row.站点名称 }}</td>
          <td>{{ row.解析模板 }}</td>
          <td>{{ row.样本窗口 }}</td>
          <td>{{ row.分母 }}</td>
          <td>{{ row.分子 }}</td>
          <td>{{ row.命中率 }}</td>
          <td>{{ row.失败类别 }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row.审计回执号 }}</td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="9" class="empty-state">暂无窗口指标，先到解析质量审计发布窗口报告</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listWindowMetrics, type WindowMetric } from '@/domain/parse_audit'

const rows = ref<WindowMetric[]>([])
const error = ref('')
const busy = ref(false)

async function load() {
  busy.value = true
  error.value = ''
  try {
    rows.value = await listWindowMetrics()
  } catch (next) {
    error.value = next instanceof Error ? next.message : '指标窗口读取失败'
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>
