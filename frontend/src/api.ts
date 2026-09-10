import type { AdminConfig, AdminHealth, AdminRun, AdminSession, AdminVersion, DomainEvent, EnvironmentConfig, EnvironmentList, NodeExecution, PluginHealth, PluginList, Run, ToolExecution } from './types'

const baseUrl = (import.meta.env.VITE_BUGLENS_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''
const adminToken = (import.meta.env.VITE_BUGLENS_ADMIN_TOKEN as string | undefined)?.trim()

export function createRunId() {
  return `web_${crypto.randomUUID().replaceAll('-', '')}`
}

export async function getRun(runId: string): Promise<Run> {
  const response = await fetch(`${baseUrl}/v1/runs/${encodeURIComponent(runId)}`)
  if (!response.ok) throw new Error(`获取诊断失败（${response.status}）`)
  return response.json() as Promise<Run>
}

export async function readCommandStream(
  path: string,
  command: unknown,
  onEvent: (event: DomainEvent) => void,
) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
    body: JSON.stringify(command),
  })
  if (!response.ok || !response.body) throw new Error(`命令提交失败（${response.status}）`)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      const data = frame.split('\n').find((line) => line.startsWith('data:'))?.slice(5).trim()
      if (data) onEvent(JSON.parse(data) as DomainEvent)
    }
    if (done) break
  }
}

export async function readEvents(runId: string, after: number, onEvent: (event: DomainEvent) => void) {
  const response = await fetch(`${baseUrl}/v1/runs/${encodeURIComponent(runId)}/events?after=${after}`)
  if (!response.ok || !response.body) throw new Error(`事件追赶失败（${response.status}）`)
  const text = await response.text()
  for (const frame of text.split('\n\n')) {
    const data = frame.split('\n').find((line) => line.startsWith('data:'))?.slice(5).trim()
    if (data) onEvent(JSON.parse(data) as DomainEvent)
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, { headers: { accept: 'application/json', ...authHeaders() } })
  if (!response.ok) throw new Error(`请求失败（${response.status}）`)
  return response.json() as Promise<T>
}

export function getAdminRuns() {
  return getJson<{ items: AdminRun[]; total: number }>('/v1/admin/runs?limit=100')
}

export function getAdminSessions() {
  return getJson<{ items: AdminSession[]; total: number }>('/v1/admin/sessions?limit=100')
}

export function getRunTools(runId: string) {
  return getJson<{ items: ToolExecution[]; total: number }>(
    `/v1/admin/runs/${encodeURIComponent(runId)}/tools?limit=100`,
  )
}

export function getNodeExecutions(runId: string) {
  return getJson<{ items: NodeExecution[]; total: number }>(
    `/v1/admin/runs/${encodeURIComponent(runId)}/executions?limit=100`,
  )
}

export function getAdminConfig() {
  return getJson<AdminConfig>('/v1/admin/config')
}

export function getAdminHealth() {
  return getJson<AdminHealth>('/v1/admin/health')
}

export function getAdminVersion() {
  return getJson<AdminVersion>('/v1/admin/version')
}

export function getEnvironments() {
  return getJson<EnvironmentList>('/v1/environments')
}

export function getAdminPlugins() {
  return getJson<PluginList>('/v1/admin/plugins')
}

export function getAdminEnvironmentConfig() {
  return getJson<EnvironmentConfig>('/v1/admin/environment-config')
}

export function checkAdminPluginInstance(instanceId: string) {
  return fetch(
    `${baseUrl}/v1/admin/plugin-instances/${encodeURIComponent(instanceId)}/check`,
    {
      method: 'POST',
      headers: { accept: 'application/json', ...authHeaders() },
    },
  ).then(async (response) => {
    if (!response.ok) throw new Error(`连接检查失败（${response.status}）`)
    return response.json() as Promise<PluginHealth>
  })
}

export function validateAdminConfig(payload: { profile: string; config: Record<string, unknown>; expected_revision: string }) {
  return fetch(`${baseUrl}/v1/admin/config/validate`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  }).then(async (response) => {
    if (!response.ok) throw new Error(`校验失败（${response.status}）`)
    return response.json() as Promise<{ valid: boolean; errors: string[]; warnings: string[]; revision: string }>
  })
}

export function applyAdminConfig(payload: { profile: string; config: Record<string, unknown>; expected_revision: string }) {
  return fetch(`${baseUrl}/v1/admin/config`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json', accept: 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  }).then(async (response) => {
    if (!response.ok) throw new Error(`保存失败（${response.status}）`)
    return response.json() as Promise<AdminConfig>
  })
}

export function validateAdminEnvironmentConfig(payload: { config: Record<string, unknown>; expected_revision: string; secret_updates?: Record<string, Record<string, { action: 'set' | 'clear'; value?: string }>> }) {
  return fetch(`${baseUrl}/v1/admin/environment-config/validate`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  }).then(async (response) => {
    if (!response.ok) throw new Error(`环境配置校验失败（${response.status}）`)
    return response.json() as Promise<{ valid: boolean; errors: string[]; warnings: string[]; revision: string }>
  })
}

export function applyAdminEnvironmentConfig(payload: { config: Record<string, unknown>; expected_revision: string; secret_updates?: Record<string, Record<string, { action: 'set' | 'clear'; value?: string }>> }) {
  return fetch(`${baseUrl}/v1/admin/environment-config`, {
    method: 'PUT',
    headers: { 'content-type': 'application/json', accept: 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
  }).then(async (response) => {
    if (!response.ok) throw new Error(`环境配置保存失败（${response.status}）`)
    return response.json() as Promise<EnvironmentConfig>
  })
}

function authHeaders(): Record<string, string> {
  return adminToken ? { authorization: `Bearer ${adminToken}` } : {}
}
