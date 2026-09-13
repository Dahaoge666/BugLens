import { useEffect, useId, useRef, useState } from 'react'
import { copyRecord, exampleRequest, isObject, listValues, operationLabels, updateConfig } from './pluginForm'
import type { ConfigRecord, TransportType } from './pluginForm'

const fieldCopy: Record<string, { label: string; help?: string; placeholder?: string }> = {
  root_path: { label: '服务器上的数据目录', help: '填写 BugLens 后端所在服务器的目录，不是当前电脑的目录。', placeholder: '例如 data/environments' },
  path: { label: '数据库文件', help: '可填写完整路径；设置数据目录后，也可填写相对路径。', placeholder: '例如 orders.sqlite' },
  health_path: { label: '连接检查使用的数据库', help: '留空时使用第一个启用的数据源。', placeholder: '例如 orders.sqlite' },
  allowed_tables: { label: '允许查询的表', help: '每行一张表；留空表示不额外限制。', placeholder: 'orders\norder_events' },
  denied_columns: { label: '禁止返回的字段', help: '每行一个字段，适合隐藏敏感业务信息。', placeholder: 'password\npayment_token' },
  table_pattern: { label: '表名匹配规则', placeholder: '例如 order_*' },
  encodings: { label: '日志编码', help: '通常无需修改，默认尝试 UTF-8 等常见编码。', placeholder: 'utf-8\nutf-8-sig\nlatin-1' },
  timestamp_fields: { label: '日志时间字段', help: '默认识别 timestamp、time 等常见字段；仅在自定义格式时填写。', placeholder: 'observed_at' },
  host: { label: '主机地址', placeholder: '例如 10.0.0.10 或 server.example.internal' },
  port: { label: '端口' },
  database: { label: '数据库名称' },
  schema: { label: '数据库 Schema', help: '通常使用 public，只有数据位于其他 Schema 时才修改。' },
  sslmode: { label: '数据库连接加密', help: '默认 require；需要验证服务器证书时选择 verify-full 并填写 CA 文件。' },
  sslrootcert: { label: '数据库 CA 证书文件', help: '后端服务器上的 CA 文件路径。' },
  url: { label: '服务地址' },
}

export function ListConfigField({ label, value, onChange, help, placeholder = '每行一项', required = false, rows = 3, preserveWhitespace = false }: {
  label: string
  value: string[]
  onChange: (value: string[]) => void
  help?: string
  placeholder?: string
  required?: boolean
  rows?: number
  preserveWhitespace?: boolean
}) {
  const [text, setText] = useState(value.join('\n'))
  const serialized = JSON.stringify(value)
  const lastValue = useRef(serialized)
  useEffect(() => {
    if (lastValue.current !== serialized) {
      lastValue.current = serialized
      setText((JSON.parse(serialized) as string[]).join('\n'))
    }
  }, [serialized])
  return <label className="config-field">{label}{required && <span className="required">必填</span>}<textarea rows={rows} value={text} required={required} placeholder={placeholder} onChange={(event) => {
    const raw = event.target.value
    const next = preserveWhitespace ? raw.split(/\r?\n/).filter((line) => line !== '') : listValues(raw)
    setText(raw)
    lastValue.current = JSON.stringify(next)
    onChange(next)
  }} />{help && <small>{help}</small>}</label>
}

export function JsonConfigField({ label, value, onChange, help }: { label: string; value: unknown; onChange: (value: unknown) => void; help?: string }) {
  const [text, setText] = useState(JSON.stringify(value ?? {}, null, 2))
  const [invalid, setInvalid] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const serialized = JSON.stringify(value ?? {})
  const lastValue = useRef(serialized)
  useEffect(() => {
    if (lastValue.current !== serialized) {
      lastValue.current = serialized
      setText(JSON.stringify(JSON.parse(serialized), null, 2))
      setInvalid(false)
      textareaRef.current?.setCustomValidity('')
    }
  }, [serialized])
  return <label className="config-field">{label}<textarea ref={textareaRef} value={text} rows={5} spellCheck={false} onChange={(event) => {
    setText(event.target.value)
    try { const next: unknown = JSON.parse(event.target.value); onChange(next); lastValue.current = JSON.stringify(next); setInvalid(false); event.target.setCustomValidity('') }
    catch { setInvalid(true); event.target.setCustomValidity('请填写有效的 JSON') }
  }} aria-invalid={invalid} />{invalid ? <small className="field-error">JSON 格式有误，请检查引号、逗号和括号。</small> : help && <small>{help}</small>}</label>
}

export function ConfigFields({ schema, value, onChange, only, omit = [], required = [] }: {
  schema: ConfigRecord
  value: ConfigRecord
  onChange: (value: ConfigRecord) => void
  only?: string[]
  omit?: string[]
  required?: string[]
}) {
  const prefix = useId()
  const properties = copyRecord(schema.properties)
  const requiredKeys = [...required, ...(Array.isArray(schema.required) ? schema.required.map(String) : [])]
  const fields = Object.entries(properties).filter(([key, field]) => isObject(field) && !field.writeOnly && !field['x-buglens-secret'] && !omit.includes(key) && (!only || only.includes(key)))
  return <div className="config-fields">{fields.map(([key, raw]) => {
    const field = raw as ConfigRecord
    const copy = fieldCopy[key]
    const label = String(field.title ?? copy?.label ?? key)
    const help = String(field.description ?? copy?.help ?? '')
    const placeholder = Array.isArray(field.examples) && typeof field.examples[0] === 'string' ? `例如 ${field.examples[0]}` : copy?.placeholder
    const id = `${prefix}-${key}`
    const needed = requiredKeys.includes(key)
    const current = value[key]
    const change = (next: unknown) => onChange(updateConfig(value, key, next))
    if (isObject(current) && 'is_set' in current) return <div className="config-field" key={key}><span>{label}</span><small>已配置的凭据保持不变，请通过凭据区域管理。</small></div>
    if (Array.isArray(field.enum)) return <label className="config-field" key={key} htmlFor={id}>{label}{needed && <span className="required">必填</span>}<select id={id} value={current == null ? '' : String(current)} required={needed} onChange={(event) => change(field.enum instanceof Array ? field.enum.find((option) => String(option) === event.target.value) ?? '' : '')}><option value="">请选择</option>{field.enum.map((option) => <option key={String(option)} value={String(option)}>{String(option)}</option>)}</select>{help && <small>{help}</small>}</label>
    if (field.type === 'boolean') return <label className="config-checkbox" key={key}><input type="checkbox" checked={current === true} onChange={(event) => change(event.target.checked)} />{label}{help && <small>{help}</small>}</label>
    if (field.type === 'array' && isObject(field.items) && Array.isArray(field.items.enum)) {
      const selected = Array.isArray(current) ? current.map(String) : []
      const optionLabels: Record<string, string> = { system: '系统信息', uptime: '运行时间与负载', disk: '磁盘使用', memory: '内存信息', processes: '进程概况' }
      return <div className="config-field" key={key}><span>{label}</span>{field.items.enum.map((option) => <label className="config-checkbox" key={String(option)}><input type="checkbox" checked={selected.includes(String(option))} onChange={(event) => change(event.target.checked ? [...selected, String(option)] : selected.filter((item) => item !== option))} />{optionLabels[String(option)] ?? String(option)}</label>)}{help && <small>{help}</small>}</div>
    }
    if (field.type === 'array' && isObject(field.items) && field.items.type === 'string') return <ListConfigField key={key} label={label} value={Array.isArray(current) ? current.map(String) : []} required={needed} placeholder={placeholder} onChange={change} help={help} />
    if (['string', 'number', 'integer'].includes(String(field.type))) {
      const numeric = field.type !== 'string'
      return <label className="config-field" key={key} htmlFor={id}>{label}{needed && <span className="required">必填</span>}<input id={id} type={numeric ? 'number' : 'text'} value={current == null ? '' : String(current)} required={needed} min={typeof field.minimum === 'number' ? field.minimum : undefined} max={typeof field.maximum === 'number' ? field.maximum : undefined} minLength={typeof field.minLength === 'number' ? field.minLength : undefined} maxLength={typeof field.maxLength === 'number' ? field.maxLength : undefined} step={field.type === 'integer' ? 1 : numeric ? 'any' : undefined} placeholder={placeholder} onChange={(event) => change(numeric && event.target.value !== '' ? Number(event.target.value) : event.target.value)} />{help && <small>{help}</small>}</label>
    }
    return <JsonConfigField key={key} label={`${label}（扩展参数）`} value={current} onChange={change} help={help || '此字段使用复杂结构，请按插件要求填写。'} />
  })}</div>
}

export function TransportFields({ value, onChange, type, allowedOperations = Object.keys(operationLabels) }: { value: ConfigRecord; onChange: (value: ConfigRecord) => void; type: TransportType; allowedOperations?: string[] }) {
  const [localMcp, setLocalMcp] = useState(!!value.command && !value.url)
  const sampleOperation = allowedOperations[0] ?? 'search_knowledge'
  const update = (key: string, next: unknown) => onChange(updateConfig(value, key, next))
  const input = (key: string, label: string, placeholder: string, required = false) => <label className="config-field">{label}<input value={typeof value[key] === 'string' ? value[key] : ''} required={required} placeholder={placeholder} onChange={(event) => update(key, event.target.value)} /></label>
  if (type === 'driver') return null
  return <div className="transport-form">
    {type === 'mcp' && <><label className="config-checkbox"><input type="checkbox" checked={localMcp} onChange={(event) => { setLocalMcp(event.target.checked); const next = { ...value }; delete next.url; delete next.command; delete next.args; onChange(next) }} />通过服务器上的本地程序启动 MCP</label>{localMcp ? input('command', 'MCP 启动程序', '例如 /opt/buglens/bin/mcp-server', true) : input('url', 'MCP 服务地址', '例如 https://knowledge.example/mcp', true)}<div className="config-field"><span>工具映射</span><small>填入服务提供的工具名；留空的能力不会开放。MCP 服务需预先绑定数据位置，支持对应工具的请求参数。</small><div className="tool-map-fields">{Object.entries(operationLabels).filter(([operation]) => allowedOperations.includes(operation)).map(([operation, label]) => <label key={operation}>{label}<input value={String(copyRecord(value.tool_map)[operation] ?? '')} placeholder="MCP 工具名（不使用则留空）" onChange={(event) => update('tool_map', updateConfig(copyRecord(value.tool_map), operation, event.target.value))} /></label>)}</div></div></>}
    {type === 'cli' && <>{input('command', '连接器程序路径', '例如 /opt/buglens/bin/log-reader', true)}<p className="config-callout">程序需支持 buglens-tool/v1 JSON 协议：从标准输入读取一次请求，向标准输出返回一次 ToolResult JSON。普通 ssh、psql 等命令需要配套的只读协议适配器。</p></>}
    {type === 'ssh' && <><div className="config-fields">{input('host', '远程主机', '例如 probe-01.example', true)}{input('user', 'SSH 用户', '例如 buglens')}{input('remote_command', '远程只读探针命令', '例如 /opt/buglens/bin/traffic-probe', true)}</div><p className="field-hint">填写服务器上已部署的固定只读探针，保存配置不会执行修复操作。</p></>}
    <details className="config-advanced transport-usage"><summary>接入约定与示例</summary>{type === 'mcp' ? <><p>HTTP 使用 MCP streamable HTTP 地址；本地程序使用 MCP stdio。检查连接会列出工具，定位时按映射调用。工具参数为标准化 request，不包含连接地址或 source_config。</p><pre>{JSON.stringify({ tool_map: { [sampleOperation]: '替换为服务工具名' }, request: exampleRequest(sampleOperation) }, null, 2)}</pre><p>示例时间仅示范格式，请替换为故障时间。根据数据类型替换工具映射与请求参数；远程 Token 在下方认证区域填写，本地 MCP 凭据由服务自身管理。</p></> : <><p>后端启动已配置的固定程序，传入下列 JSON；stdout 只输出结果 JSON，调试信息写入 stderr。context 包含超时和结果大小上限，连接器应遵守只读权限与这些限制。</p><pre>{JSON.stringify({ protocol: 'buglens-tool/v1', operation: sampleOperation, source_config: {}, request: exampleRequest(sampleOperation), context: { execution_id: '由后端生成', run_id: '当前诊断', environment_snapshot_id: '固定快照', plugin_instance_id: '本次接入', deadline: '带时区的绝对时间', max_results: 200, max_bytes: 65536, max_scan_files: 100, max_scan_bytes: 16777216 } }, null, 2)}</pre><pre>{JSON.stringify({ status: 'succeeded', result: { items: [] }, source_references: [], cursor: null, truncated: false, warnings: [] }, null, 2)}</pre><p>程序由后端服务器执行，不读取当前浏览器电脑的文件。凭据由程序自己的安全配置管理，接入参数和使用说明不应包含密码。</p></>}</details>
    <details className="config-advanced"><summary>连接高级选项</summary><div className="config-fields">
      {(type === 'cli' || (type === 'mcp' && localMcp)) && <ListConfigField label="启动参数" value={Array.isArray(value.args) ? value.args.map(String) : []} placeholder="每行一个参数" onChange={(next) => update('args', next)} preserveWhitespace />}
      {type === 'ssh' && <>{input('known_hosts', '主机指纹文件', '例如 /etc/buglens/known_hosts')}{input('identity_file', 'SSH 私钥文件路径', '例如 /etc/buglens/probe_ed25519')}<label className="config-field">SSH 端口<input type="number" min={1} max={65535} value={typeof value.port === 'number' ? value.port : 22} onChange={(event) => update('port', event.target.value === '' ? undefined : Number(event.target.value))} /></label>{input('ssh_command', 'SSH 客户端程序', '默认使用 ssh')}</>}
      {type === 'mcp' && <JsonConfigField label="附加请求头（非敏感字段）" value={value.headers} onChange={(next) => update('headers', next)} help="密码和 Token 请在凭据区域填写。" />}
    </div></details>
  </div>
}
