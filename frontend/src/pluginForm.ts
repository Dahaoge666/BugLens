import type { PluginView } from './types'

export type ConfigRecord = Record<string, unknown>
export type TransportType = 'driver' | 'mcp' | 'cli' | 'ssh'

export function sourceDisplayName(kind: string): string {
  return ({ database: '数据库', logs: '日志', host: '主机', knowledge: '知识库', traffic: '流量', traces: '链路追踪' } as Record<string, string>)[kind] ?? kind
}

export function isObject(value: unknown): value is ConfigRecord {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

export function copyRecord(value: unknown): ConfigRecord {
  return isObject(value) ? structuredClone(value) : {}
}

export function catalogRecords(value: unknown): ConfigRecord[] {
  if (Array.isArray(value)) return value.filter(isObject)
  if (!isObject(value)) return []
  return Object.entries(value).flatMap(([id, item]) => isObject(item) ? [{ ...item, id: item.id ?? id }] : [])
}

export function generatedId(prefix: string): string {
  const bytes = crypto.getRandomValues(new Uint8Array(6))
  const suffix = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${prefix.replace(/[^A-Za-z0-9_.-]/g, '-').slice(0, 100)}-${suffix}`
}

export function listValues(text: string): string[] {
  return [...new Set(text.split(/\r?\n/).map((value) => value.trim()).filter(Boolean))]
}

export function updateConfig(config: ConfigRecord, key: string, value: unknown): ConfigRecord {
  const next = { ...config }
  if (value === '' || value === undefined) delete next[key]
  else next[key] = value
  return next
}

export function schemaDefaults(schema: ConfigRecord): ConfigRecord {
  if (!isObject(schema.properties)) return {}
  return Object.fromEntries(Object.entries(schema.properties).flatMap(([key, value]) =>
    isObject(value) && value.default !== undefined && !value.writeOnly && !value['x-buglens-secret']
      ? [[key, structuredClone(value.default)]] : [],
  ))
}

export function sourceKinds(plugin: Pick<PluginView, 'capabilities'>): string[] {
  const knownKinds = ['database', 'logs', 'host', 'knowledge', 'traffic']
  const kinds = [...new Set(plugin.capabilities.map((capability) => capability.split('.')[0]).filter((kind) => knownKinds.includes(kind)))]
  return kinds.length ? kinds : knownKinds
}

export const operationLabels: Record<string, string> = {
  describe_database: '查看数据库结构', query_database: '查询数据库', search_logs: '检索日志', inspect_host: '检查主机', search_knowledge: '检索知识库', search_traffic: '检索流量',
}

export function exampleRequest(operation: string): ConfigRecord {
  if (operation === 'query_database') return { sql: 'SELECT 1', parameters: {} }
  if (operation === 'inspect_host') return { check: 'system' }
  if (operation === 'search_knowledge') return { query: '连接超时', top_k: 10, filters: {} }
  if (operation === 'search_logs' || operation === 'search_traffic') return { start_time: '2026-09-13T10:00:00+08:00', end_time: '2026-09-13T10:15:00+08:00', text_query: 'timeout' }
  return {}
}

export function operationsForSources(sources: ConfigRecord[]): string[] {
  const byKind: Record<string, string[]> = { database: ['describe_database', 'query_database'], logs: ['search_logs'], host: ['inspect_host'], knowledge: ['search_knowledge'], traffic: ['search_traffic'] }
  return [...new Set(sources.flatMap((source) => byKind[String(source.kind)] ?? []))]
}

export function supportedTransports(plugin: PluginView): TransportType[] {
  return plugin.supported_transports ?? ['driver', 'mcp', 'cli', 'ssh']
}

export function transportForSave(transport: ConfigRecord, sources: ConfigRecord[]): ConfigRecord {
  const next = structuredClone(transport)
  if (next.type === 'mcp' && sources.length) {
    const operations = operationsForSources(sources)
    next.tool_map = Object.fromEntries(Object.entries(copyRecord(next.tool_map)).filter(([operation, name]) => operations.includes(operation) && typeof name === 'string' && !!name.trim()))
  }
  return next
}

export function newSource(plugin: PluginView, instanceId: string): ConfigRecord {
  return { id: generatedId(`${instanceId}-source`), kind: sourceKinds(plugin)[0], enabled: true, config: schemaDefaults(plugin.source_config_schema) }
}

const transportFields: Record<TransportType, string[]> = {
  driver: ['credentials_ref'],
  mcp: ['url', 'command', 'args', 'headers', 'tool_map', 'credentials_ref', 'timeout_seconds', 'max_output_bytes'],
  cli: ['command', 'args', 'credentials_ref', 'timeout_seconds', 'max_output_bytes'],
  ssh: ['host', 'user', 'port', 'ssh_command', 'known_hosts', 'identity_file', 'remote_command', 'credentials_ref', 'timeout_seconds', 'max_output_bytes'],
}

export function changeTransport(config: ConfigRecord, type: TransportType): ConfigRecord {
  const next: ConfigRecord = { type }
  for (const field of transportFields[type]) {
    if (config[field] !== undefined && config[field] !== null) next[field] = structuredClone(config[field])
  }
  return next
}

export function logPaths(config: ConfigRecord): string[] {
  const paths = Array.isArray(config.paths) ? config.paths.map(String) : []
  return [...new Set([...paths, ...[config.path, config.glob].filter((value): value is string => typeof value === 'string' && !!value)])]
}

export function setLogPaths(config: ConfigRecord, text: string): ConfigRecord {
  const next: ConfigRecord = { ...config, paths: listValues(text) }
  delete next.path
  delete next.glob
  return next
}

export function validateBasicConfig(pluginId: string, config: ConfigRecord, sources: ConfigRecord[], transport: ConfigRecord): string[] {
  const errors: string[] = []
  const present = (value: unknown) => typeof value === 'string' && !!value.trim()
  if (transport.type === 'driver' && ['file_logs', 'ssh_logs'].includes(pluginId) && !present(config.root_path)) errors.push('请填写日志所在的服务器目录。')
  if (transport.type === 'driver' && ['ssh', 'ssh_logs', 'postgresql'].includes(pluginId) && !present(config.host)) errors.push('请填写要连接的主机地址。')
  if (transport.type === 'driver' && pluginId === 'postgresql' && !present(config.database)) errors.push('请填写 PostgreSQL 数据库名称。')
  if (transport.type === 'driver' && pluginId === 'ssh_logs' && present(config.root_path) && !String(config.root_path).startsWith('/')) errors.push('远程日志目录需要使用绝对路径，例如 /var/log/orders。')
  sources.forEach((source, index) => {
    const values = copyRecord(source.config)
    if (transport.type === 'driver' && pluginId === 'sqlite' && !present(values.path)) errors.push(`数据源 ${index + 1}：请填写数据库文件路径。`)
    if (transport.type === 'driver' && ['file_logs', 'ssh_logs'].includes(pluginId)) {
      const paths = logPaths(values)
      if (!paths.length) errors.push(`数据源 ${index + 1}：请填写日志文件或匹配规则。`)
      else if (paths.some((path) => /^(?:[A-Za-z]:[\\/]|[\\/])/.test(path) || path.split(/[\\/]/).includes('..'))) errors.push(`数据源 ${index + 1}：日志路径应相对于上方目录填写，例如 order-api.log。`)
      else if (pluginId === 'ssh_logs' && paths.some((path) => path.includes('\\') || path.includes('**') || /[*?\[]/.test(path.slice(0, path.lastIndexOf('/'))))) errors.push(`数据源 ${index + 1}：远程日志使用 / 分隔目录，通配符仅用于文件名，例如 orders/*.log。`)
    }
    if (transport.type === 'driver' && pluginId === 'ssh' && Array.isArray(values.checks) && !values.checks.length) errors.push(`数据源 ${index + 1}：请至少选择一项主机检查。`)
  })
  if (transport.type === 'mcp') {
    if (!present(transport.url) && !present(transport.command)) errors.push('请填写 MCP 服务地址或启动程序。')
    if (!isObject(transport.tool_map) || !Object.values(transport.tool_map).some(present)) errors.push('请至少填写一项 MCP 工具映射。')
  }
  if (transport.type === 'cli' && !present(transport.command)) errors.push('请填写连接器程序路径。')
  if (transport.type === 'ssh') {
    if (!present(transport.host)) errors.push('请填写远程主机。')
    if (!present(transport.remote_command)) errors.push('请填写远程只读探针命令。')
  }
  return errors
}

export function prepareSources(sources: ConfigRecord[], instanceId: string, environmentId: string, enabled: boolean, serviceId?: string): ConfigRecord[] {
  return sources.map((source) => ({ ...structuredClone(source), plugin_instance_id: instanceId, environment_id: environmentId, ...(serviceId ? { service_ids: [...new Set([serviceId, ...(Array.isArray(source.service_ids) ? source.service_ids.map(String) : [])])] } : {}), enabled: enabled && source.enabled !== false }))
}

export function instanceBelongsToService(instance: ConfigRecord, sources: ConfigRecord[], serviceId: string): boolean {
  if (instance.service_id) return instance.service_id === serviceId
  return sources.some((source) => source.plugin_instance_id === instance.id && Array.isArray(source.service_ids) && source.service_ids.includes(serviceId))
}

export function connectionForSave(pluginId: string, config: ConfigRecord, sources: ConfigRecord[], transport: ConfigRecord): ConfigRecord {
  const next = structuredClone(config)
  if (pluginId === 'sqlite' && transport.type === 'driver' && !next.health_path) {
    const source = sources.find((item) => item.enabled !== false && isObject(item.config) && typeof item.config.path === 'string' && item.config.path.trim())
    if (source && isObject(source.config)) next.health_path = source.config.path
  }
  return next
}
