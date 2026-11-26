/**
 * 解析质量审计的窗口域逻辑：打开/恢复样本窗口、补报迟到样本、发布与重试发布、
 * 按回执核对采集量指标口径。与后端 app/services/parse_audit.py 的窗口约定保持一致。
 */

import { request } from '@/api/client'

export type WindowState =
  | '暂无样本'
  | '确有样本但未命中'
  | '部分命中'
  | '全部命中'
  | '—'

export type PublishState = '待发布' | '确认中' | '补报等待确认' | '已发布' | ''

export type AuditReceipt = {
  回执号: string
  版本: number
  站点名称: string
  解析模板: string
  样本窗口: string
  分母: number
  分子: number
  命中率: string
  样本判定: string
  失败类别: string
  发布状态: string
  口径: string
  指标记录id: number | null
  指标状态: string
  发布时间: string
  备注: string
}

export type WindowMetricsCaliber = {
  指标记录id: number | null
  分母: string
  分子: string
  命中率: string
  失败类别: string
  指标状态: string
  审计回执号: string
} | null

export type AuditWindow = {
  审计明细id: number
  站点名称: string
  解析模板: string
  样本窗口: string
  样本总数: number
  命中数: number
  未命中数: number
  字段命中率: string
  样本判定: string
  发布状态: PublishState
  版本: number
  失败类别: string
  失败原因分布: string
  status: string
  当前回执: AuditReceipt | null
  回执历史: AuditReceipt[]
  指标口径: WindowMetricsCaliber
}

export type WindowMetric = {
  id: number
  站点名称: string
  解析模板: string
  样本窗口: string
  分母: number
  分子: number
  命中率: string
  失败类别: string
  status: string
  审计回执号: string
  统计时间: string
}

export type WindowKey = {
  site: string
  template: string
  window: string
}

export type LateSample = {
  样本指纹: string
  命中结果: '命中' | '未命中'
  失败原因: string
}

export type RequestResult<T> = {
  ok: boolean
  message: string
  data: T | null
}

async function send<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as Record<string, unknown>).detail)
        : `接口返回 ${response.status}`
    throw new Error(detail)
  }
  return payload as T
}

function body(key: WindowKey, extra: Record<string, unknown> = {}) {
  return JSON.stringify({ site: key.site, template: key.template, window: key.window, ...extra })
}

function query(key: WindowKey, extra: Record<string, string> = {}) {
  return new URLSearchParams({ site: key.site, template: key.template, window: key.window, ...extra })
}

export async function listWindows(): Promise<AuditWindow[]> {
  const payload = await send<{ windows: AuditWindow[] }>('/api/parse_audit/windows')
  return payload.windows ?? []
}

export async function openWindow(key: WindowKey): Promise<RequestResult<AuditWindow>> {
  const payload = await send<{ ok: boolean; message: string; window: AuditWindow }>(
    '/api/parse_audit/windows/open',
    { method: 'POST', body: body(key) },
  )
  return { ok: payload.ok, message: payload.message, data: payload.window ?? null }
}

export async function readProgress(key: WindowKey): Promise<AuditWindow | null> {
  const payload = await send<{ ok: boolean; window?: AuditWindow }>(
    `/api/parse_audit/windows/progress?${query(key)}`,
  )
  return payload.window ?? null
}

export type BackfillResult = {
  accepted: number
  duplicates: number
  rejected: { index: number; reason: string }[]
  window: AuditWindow | null
}

export async function backfillWindow(
  key: WindowKey,
  samples: LateSample[],
): Promise<RequestResult<BackfillResult>> {
  const payload = await send<{ ok: boolean; message: string } & BackfillResult>(
    '/api/parse_audit/windows/backfill',
    { method: 'POST', body: body(key, { samples }) },
  )
  return {
    ok: payload.ok,
    message: payload.message,
    data: {
      accepted: payload.accepted,
      duplicates: payload.duplicates,
      rejected: payload.rejected,
      window: payload.window ?? null,
    },
  }
}

export type PublishResult = {
  published: boolean
  version: number
  receipt: AuditReceipt
  window: AuditWindow | null
}

export async function publishWindow(
  key: WindowKey,
  retry = false,
): Promise<RequestResult<PublishResult>> {
  const path = retry
    ? '/api/parse_audit/windows/retry-publish'
    : '/api/parse_audit/windows/publish'
  const payload = await send<{ ok: boolean; message: string } & PublishResult>(path, {
    method: 'POST',
    body: body(key),
  })
  return {
    ok: payload.ok,
    message: payload.message,
    data: {
      published: payload.published,
      version: payload.version,
      receipt: payload.receipt,
      window: payload.window ?? null,
    },
  }
}

export async function readReceipts(key: WindowKey): Promise<AuditReceipt[]> {
  const payload = await send<{ ok: boolean; receipts?: AuditReceipt[]; message?: string }>(
    `/api/parse_audit/windows/receipt?${query(key)}`,
  )
  return payload.receipts ?? []
}

export type VerifyResult = {
  consistent: boolean
  differences: string[]
  receipt: AuditReceipt
  metrics: Record<string, string | number> | null
}

export async function verifyCaliber(key: WindowKey): Promise<RequestResult<VerifyResult>> {
  const payload = await send<{ ok: boolean; message: string } & VerifyResult>(
    `/api/parse_audit/windows/verify?${query(key)}`,
  )
  return {
    ok: payload.ok,
    message: payload.message,
    data: {
      consistent: payload.consistent,
      differences: payload.differences ?? [],
      receipt: payload.receipt,
      metrics: payload.metrics ?? null,
    },
  }
}

export async function listWindowMetrics(): Promise<WindowMetric[]> {
  const payload = await send<{ metrics: WindowMetric[] }>('/api/collect_metrics/windows')
  return payload.metrics ?? []
}

/** 状态徽标：把空样本、全未命中、补报等待确认明确区分开。 */
export function stateTone(state: string): 'empty' | 'miss' | 'backfill' | 'ok' | 'pending' {
  if (state === '暂无样本') return 'empty'
  if (state === '确有样本但未命中') return 'miss'
  if (state === '补报等待确认') return 'backfill'
  if (state === '已发布' || state === '全部命中' || state === '正常') return 'ok'
  return 'pending'
}

/** 发布状态文案：空报告确认中与补报等待确认不能混为一谈。 */
export function publishLabel(state: PublishState, total: number): string {
  if (state === '已发布') return '已发布'
  if (state === '补报等待确认') return '补报等待确认'
  if (state === '确认中') return total === 0 ? '空报告确认中' : '确认中'
  return '待发布'
}
