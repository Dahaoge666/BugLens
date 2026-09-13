import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { checkAdminPluginInstance, deleteAdminPluginInstance, getAdminEnvironmentConfig, PluginConfigError, saveAdminPluginInstance } from './api'
import { ConfigFields, JsonConfigField, ListConfigField, TransportFields } from './PluginConfigFields'
import { ConnectorUsageFields } from './ConnectorUsageFields'
import { catalogRecords, changeTransport, connectionForSave, copyRecord, generatedId, isObject, logPaths, newSource, operationsForSources, prepareSources, schemaDefaults, setLogPaths, sourceDisplayName, sourceKinds, supportedTransports, transportForSave, validateBasicConfig } from './pluginForm'
import type { ConfigRecord, TransportType } from './pluginForm'
import type { EnvironmentConfig, EnvironmentSummary, PluginHealth, PluginView } from './types'

type SecretField = 'username' | 'password' | 'token'
export type IntegrationSaveResult = { id: string; health?: PluginHealth; removed?: boolean }
const secretFields: SecretField[] = ['username', 'password', 'token']
const secretLabels = { username: '用户名', password: '密码', token: 'Token' }
const typeLabels: Record<TransportType, string> = { driver: '已安装的插件', mcp: 'MCP 服务', cli: '命令行连接器', ssh: 'SSH 只读探针' }
const limitsSchema = { properties: {
  max_results: { title: '单次最多返回条数', type: 'integer', minimum: 1, maximum: 1000 },
  timeout_seconds: { title: '单次查询超时（秒）', type: 'integer', minimum: 1, maximum: 30 },
  max_bytes: { title: '单次结果大小上限（字节）', type: 'integer', minimum: 1024, maximum: 1048576 },
  max_scan_files: { title: '最多扫描日志文件数', type: 'integer', minimum: 1, maximum: 10000 },
  max_scan_bytes: { title: '最多扫描日志字节数', type: 'integer', minimum: 1024, maximum: 67108864 },
} }
const optionalInstanceFields = ['health_path', 'encodings', 'sslmode', 'sslrootcert', 'known_hosts']
const optionalSourceFields = ['allowed_tables', 'denied_columns', 'table_pattern', 'timestamp_fields', 'schema']

export function PluginInstanceEditor({ directory, plugin, instance, sources, environments, selectedServiceId = '', onSaved, onRefresh, onClose, onBusyChange }: {
  directory: EnvironmentConfig
  plugin: PluginView
  instance?: ConfigRecord
  sources: ConfigRecord[]
  environments: EnvironmentSummary[]
  selectedServiceId?: string
  onSaved: (config: EnvironmentConfig, result: IntegrationSaveResult) => void
  onRefresh: (config: EnvironmentConfig) => void
  onClose: () => void
  onBusyChange?: (busy: boolean) => void
}) {
  const editorRef = useRef<HTMLElement>(null)
  const formRef = useRef<HTMLFormElement>(null)
  const [id, setId] = useState(() => String(instance?.id ?? generatedId(plugin.plugin_id)))
  const [environmentId, setEnvironmentId] = useState(String(instance?.environment_id ?? environments[0]?.environment_id ?? ''))
  const [serviceId, setServiceId] = useState(String(instance?.service_id ?? selectedServiceId))
  const [enabled, setEnabled] = useState(instance?.enabled !== false)
  const [connection, setConnection] = useState(() => instance ? copyRecord(instance.config) : schemaDefaults(plugin.instance_config_schema))
  const [checkFirst, setCheckFirst] = useState(() => !copyRecord(instance?.config).health_path || copyRecord(instance?.config).health_path === copyRecord(sources.find((source) => source.enabled !== false)?.config).path)
  const [transport, setTransport] = useState<ConfigRecord>(() => instance ? copyRecord(instance.transport) : { type: plugin.default_transport ?? 'driver' })
  const [usage, setUsage] = useState<ConfigRecord>(() => copyRecord(copyRecord(instance?.usage).description ? instance?.usage : plugin.usage_template))
  const [limits, setLimits] = useState(() => instance ? copyRecord(instance.default_limits) : {})
  const [sourceDrafts, setSourceDrafts] = useState(() => instance ? sources.map((source) => structuredClone(source)) : [newSource(plugin, id)])
  const [secrets, setSecrets] = useState<Partial<Record<SecretField, string>>>({})
  const [clears, setClears] = useState<Partial<Record<SecretField, boolean>>>({})
  const [revision, setRevision] = useState(directory.revision)
  const [phase, setPhase] = useState<'saving' | 'checking' | null>(null)
  const [message, setMessage] = useState('')
  const [errorDetail, setErrorDetail] = useState('')
  const [conflict, setConflict] = useState<EnvironmentConfig | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const busy = phase !== null
  useEffect(() => { onBusyChange?.(busy); return () => onBusyChange?.(false) }, [busy, onBusyChange])
  const type = String(transport.type ?? 'driver') as TransportType
  const allowedOperations = operationsForSources(sourceDrafts)
  const enabledOperations = allowedOperations.filter((operation) => type !== 'mcp' || !!copyRecord(transport.tool_map)[operation])
  const fileLogs = ['file_logs', 'ssh_logs'].includes(plugin.plugin_id) && type === 'driver'
  const remoteLogs = plugin.plugin_id === 'ssh_logs' && type === 'driver'
  const sqlite = plugin.plugin_id === 'sqlite' && type === 'driver'
  const authenticated = ['postgresql', 'ssh', 'ssh_logs'].includes(plugin.plugin_id) && type === 'driver'
  const visibleSecrets = type === 'mcp' ? ['token' as SecretField] : type === 'cli' || type === 'ssh' ? [] : authenticated ? secretFields.filter((field) => field !== 'token') : secretFields
  const services = catalogRecords(directory.config.services).filter((record) => !record.environment_id || record.environment_id === environmentId)
  const nodes = catalogRecords(directory.config.nodes).filter((record) => !record.environment_id || record.environment_id === environmentId)

  useEffect(() => {
    editorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    formRef.current?.querySelector<HTMLSelectElement>('select')?.focus({ preventScroll: true })
  }, [])

  async function handleError(error: unknown) {
    if (error instanceof PluginConfigError && error.status === 409) {
      try {
        const latest = await getAdminEnvironmentConfig()
        onRefresh(latest)
        setConflict(latest)
        setMessage('配置已被其他操作更新，填写的内容已保留。请核对最新配置后选择如何继续。')
      } catch { setMessage('配置版本已变化，填写的内容已保留。暂时无法读取最新配置，请稍后重试。') }
    } else {
      const detail = error instanceof Error ? error.message : '未知错误'
      setErrorDetail(detail)
      setMessage(error instanceof PluginConfigError ? '保存未成功，请核对连接位置、接入说明和参数格式，或查看详细原因。' : detail)
    }
  }

  function updateSource(index: number, next: ConfigRecord) {
    setSourceDrafts((current) => current.map((source, position) => position === index ? next : source))
  }

  async function save(check: boolean) {
    setMessage('')
    setErrorDetail('')
    const savedTransport = transportForSave(transport, sourceDrafts)
    const issues = validateBasicConfig(plugin.plugin_id, connection, sourceDrafts, savedTransport)
    if (issues.length) { setMessage(issues.join(' ')); return }
    setPhase('saving')
    try {
      const secretUpdates: Record<string, { action: 'set' | 'clear'; value?: string }> = {}
      for (const field of secretFields) {
        if (clears[field]) secretUpdates[field] = { action: 'clear' }
        else if (secrets[field]) secretUpdates[field] = { action: 'set', value: secrets[field] }
      }
      const prepared = prepareSources(sourceDrafts, id, environmentId, enabled, serviceId || undefined)
      const configuredConnection = { ...connection }
      if (sqlite && checkFirst) delete configuredConnection.health_path
      const next = await saveAdminPluginInstance({
        instance: { ...instance, id, plugin_id: plugin.plugin_id, environment_id: environmentId, service_id: serviceId || null, enabled, config: type === 'driver' ? connectionForSave(plugin.plugin_id, configuredConnection, prepared, transport) : {}, transport: savedTransport, usage: { ...usage, examples: catalogRecords(usage.examples).filter((example) => enabledOperations.includes(String(example.operation))) }, default_limits: limits },
        sources: prepared,
        expected_revision: revision,
        secret_updates: secretUpdates,
      }, instance ? String(instance.id) : undefined)
      setSecrets({})
      setClears({})
      let health: PluginHealth | undefined
      if (check && enabled && plugin.health_check) {
        setPhase('checking')
        try { health = await checkAdminPluginInstance(id) }
        catch { health = { status: 'error', detail: '配置已保存，但连接检查请求失败，可稍后在接入卡片中重试。' } }
      }
      onSaved(next, { id, health })
    } catch (error) { await handleError(error) }
    finally { setPhase(null) }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void save((event.nativeEvent as SubmitEvent).submitter?.getAttribute('data-check') !== 'false')
  }

  async function remove() {
    if (!instance) return
    setPhase('saving')
    try { onSaved(await deleteAdminPluginInstance(String(instance.id), revision), { id, removed: true }) }
    catch (error) { await handleError(error) }
    finally { setPhase(null) }
  }

  const knownInstanceFields = isObject(plugin.instance_config_schema.properties) && Object.keys(plugin.instance_config_schema.properties).length > 0
  const knownSourceFields = isObject(plugin.source_config_schema.properties) && Object.keys(plugin.source_config_schema.properties).length > 0

  return <section className="panel plugin-config-editor" ref={editorRef} aria-label="插件接入配置">
    <div className="editor-heading"><div><span className="section-kicker">{instance ? '编辑接入' : '新增接入'}</span><h2>接入 {plugin.display_name ?? plugin.plugin_id}</h2><p>{type !== 'driver' ? '选择 MCP 服务或兼容 CLI 程序，并补充定位节点的使用说明。' : sqlite ? '填写数据库文件位置即可开始；可按需限制查询的表和字段。' : remoteLogs ? '填写远程主机、日志目录与文件名，直接通过 SFTP 只读检索。' : fileLogs ? '指定服务器上的日志目录，再填写要读取的日志文件。' : plugin.description || '填写访问位置和数据源，凭据按本次接入单独保存。'}</p></div><button className="quiet-button" type="button" onClick={onClose} disabled={busy}>取消</button></div>
    <div className="setup-progress" aria-label="配置流程"><span><b>1</b>确认归属</span><span><b>2</b>连接与数据</span><span><b>3</b>保存并检查</span></div>
    <form ref={formRef} onSubmit={submit} onInvalid={(event) => { (event.target as HTMLElement).closest('details')?.setAttribute('open', '') }}>
      <fieldset disabled={busy || !directory.writable}>
        <section className="config-section"><h3>所属环境与子服务</h3><div className="config-fields"><label className="config-field">所属环境<select value={environmentId} required onChange={(event) => { setEnvironmentId(event.target.value); setServiceId('') }} disabled={!!instance?.environment_id || !!selectedServiceId}><option value="" disabled>请选择环境</option>{environments.map((environment) => <option key={environment.environment_id} value={environment.environment_id}>{environment.display_name}</option>)}{!!instance?.environment_id && !environments.some((environment) => environment.environment_id === instance.environment_id) && <option value={String(instance.environment_id)}>{String(instance.environment_id)}（已停用）</option>}</select><small>位置和凭据仅用于此项接入。</small></label><label className="config-field">所属子服务<select value={serviceId} required={!instance} disabled={!!instance?.service_id || !!selectedServiceId} onChange={(event) => setServiceId(event.target.value)}><option value="">{instance ? '环境级历史接入' : '请选择子服务'}</option>{services.filter((service) => service.environment_id === environmentId).map((service) => <option value={String(service.id)} key={String(service.id)}>{String(service.name)}</option>)}</select><small>数据源自动关联此服务；迁移时新增接入。</small></label></div></section>
        <section className="config-section"><h3>连接位置</h3>
          <div className="transport-picker" role="group" aria-label="连接方式">{supportedTransports(plugin).map((method) => <button type="button" key={method} className={type === method ? 'selected' : ''} aria-pressed={type === method} onClick={() => setTransport(changeTransport(transport, method))}><strong>{method === 'driver' ? '内置驱动' : method.toUpperCase()}</strong><small>{typeLabels[method]}</small></button>)}</div>
          {type === 'driver' && <ConfigFields schema={plugin.instance_config_schema} value={connection} onChange={setConnection} omit={optionalInstanceFields} required={fileLogs ? ['root_path'] : []} />}
          {type !== 'driver' && <TransportFields key={type} type={type} value={transport} onChange={setTransport} allowedOperations={allowedOperations} />}
          {type === 'driver' && !knownInstanceFields && <p className="field-hint">此插件未声明额外连接字段；如需扩展参数，可在高级选项中填写。</p>}
        </section>
        <section className="config-section"><div className="config-section-heading"><div><h3>要读取的数据</h3><p>{fileLogs ? '文件名或匹配规则相对于上方日志目录填写，支持轮转日志。' : '可以添加多个数据源，自动归属当前环境与本次接入。'}</p></div><button type="button" className="quiet-button" onClick={() => setSourceDrafts((current) => [...current, newSource(plugin, id)])}>＋ 添加数据源</button></div>
          {sourceDrafts.length === 0 && <div className="empty-state"><span>暂未添加数据源，可先保存连接，稍后再补充。</span><button className="text-button" type="button" onClick={() => setSourceDrafts([newSource(plugin, id)])}>添加第一个数据源</button></div>}
          <div className="source-form-list">{sourceDrafts.map((source, index) => {
            const values = copyRecord(source.config)
            const kinds = [...new Set([...sourceKinds(plugin), String(source.kind)])]
            const changeValues = (next: ConfigRecord) => updateSource(index, { ...source, config: next })
            return <article className="source-form-card" key={String(source.id)}><div className="config-section-heading"><strong>数据源 {index + 1} · {sourceDisplayName(String(source.kind))}</strong><button className="text-button" type="button" onClick={() => setSourceDrafts((current) => current.filter((_, position) => position !== index))}>移除</button></div>
              {kinds.length > 1 && <label className="config-field">数据类型<select value={String(source.kind)} onChange={(event) => updateSource(index, { ...source, kind: event.target.value, capabilities: [], config: {} })}>{kinds.map((kind) => <option key={kind} value={kind}>{sourceDisplayName(kind)}</option>)}</select></label>}
              {type !== 'driver' ? <p className="config-callout">{type === 'mcp' ? '数据位置在 MCP 服务中预先绑定；此数据源负责环境、子服务和能力授权。' : '数据源选择器按连接器约定填写，可在下方选填区域设置 source_config JSON。'}</p> : fileLogs ? <ListConfigField label="日志文件或匹配规则" required value={logPaths(values)} placeholder={'例如 order-api.log\n或 orders/*.log；每行一项'} onChange={(next) => changeValues(setLogPaths(values, next.join('\n')))} help={remoteLogs ? '相对于远程日志目录；使用 / 分隔目录，通配符仅用于文件名。' : '例如目录为 data/logs 时，order-api.log 指向该目录内的文件。'} /> : <ConfigFields schema={plugin.source_config_schema} value={values} onChange={changeValues} omit={optionalSourceFields} required={sqlite ? ['path'] : []} />}
              {type === 'driver' && plugin.plugin_id === 'postgresql' && <p className="field-hint">默认读取 public Schema，可在选填区域调整 Schema 和表权限。</p>}
              <details className="config-advanced"><summary>{sqlite ? '选填：表权限、关联服务与更多选项' : '选填：关联服务与更多选项'}</summary>
                {type === 'driver' && <ConfigFields schema={plugin.source_config_schema} value={values} onChange={changeValues} only={optionalSourceFields} />}
                {(services.length > 0 || nodes.length > 0) && <div className="config-associations">{[{ title: '关联服务', field: 'service_ids', records: services, label: 'name' }, { title: '关联节点', field: 'node_ids', records: nodes, label: 'hostname' }].map((group) => group.records.length > 0 && <div key={group.field}><span>{group.title}</span>{group.records.map((record) => { const associated = source[group.field]; const selected = Array.isArray(associated) ? associated.map(String) : []; return <label className="config-checkbox" key={String(record.id)}><input type="checkbox" disabled={group.field === 'service_ids' && record.id === serviceId} checked={selected.includes(String(record.id)) || (group.field === 'service_ids' && record.id === serviceId)} onChange={(event) => updateSource(index, { ...source, [group.field]: event.target.checked ? [...selected, String(record.id)] : selected.filter((item) => item !== record.id) })} />{String(record[group.label] ?? record.id)}</label> })}</div>)}</div>}
                <label className="config-checkbox"><input type="checkbox" checked={source.enabled !== false} onChange={(event) => updateSource(index, { ...source, enabled: event.target.checked })} />启用此数据源</label>
                <label className="config-field">数据源标识<input value={String(source.id)} readOnly /><small>系统自动生成，无需手动维护。</small></label>
                {(type === 'cli' || type === 'ssh' || (type === 'driver' && !knownSourceFields)) && <JsonConfigField label={type === 'driver' ? '插件扩展参数' : '数据源选择器 source_config'} value={values} onChange={(next) => { if (!isObject(next)) throw new Error('扩展参数必须是对象'); changeValues(next) }} />}
                <ListConfigField label="限定能力（选填）" rows={2} value={Array.isArray(source.capabilities) ? source.capabilities.map(String) : []} placeholder="通常无需填写；每行一个能力标识" onChange={(next) => updateSource(index, { ...source, capabilities: next })} />
              </details>
            </article>
          })}</div>
        </section>
        <ConnectorUsageFields value={usage} onChange={setUsage} required={plugin.plugin_id === 'connector'} operations={enabledOperations} sourceIds={sourceDrafts.map((source) => String(source.id))} />
        <details className="config-advanced config-section" open={authenticated}><summary>访问凭据（仅在需要认证时填写）</summary><p className="field-hint">{type === 'driver' && plugin.plugin_id === 'postgresql' ? '填写数据库只读账号。已有凭据留空保持不变。' : authenticated ? '填写连接账号；SSH 可使用上方私钥或 SSH Agent，密码仅在需要时填写。已有凭据留空保持不变。' : '留空表示保持不变；只用于当前接入，保存后清空输入。'}</p><div className="plugin-secret-grid">{visibleSecrets.length === 0 && <p className="field-hint">此连接方式使用连接器自身的认证配置或私钥文件。旧接入的凭据会保留。</p>}{visibleSecrets.map((field) => { const presence = copyRecord(instance?.[field]); return <div className="credential-field" key={field}><label className="config-field">{secretLabels[field]} · {presence.is_set ? '已配置' : '未配置'}<input type={field === 'username' ? 'text' : 'password'} value={secrets[field] ?? ''} required={authenticated && field === 'username' && !presence.is_set && !instance} autoComplete="new-password" placeholder="留空表示不变" onChange={(event) => { setSecrets((current) => ({ ...current, [field]: event.target.value })); setClears((current) => ({ ...current, [field]: false })) }} /></label><label className="config-checkbox"><input type="checkbox" checked={clears[field] ?? false} onChange={(event) => { setClears((current) => ({ ...current, [field]: event.target.checked })); setSecrets((current) => ({ ...current, [field]: '' })) }} />清除此凭据</label></div> })}</div></details>
        <details className="config-advanced config-section"><summary>高级设置</summary><div className="config-fields"><label className="config-field">接入标识<input value={id} onChange={(event) => setId(event.target.value)} disabled={!!instance} required pattern="[A-Za-z0-9_.-]+" maxLength={128} /><small>自动生成；只有需要固定标识时才修改。</small></label></div>
          {sqlite && <label className="config-checkbox"><input type="checkbox" checked={checkFirst} onChange={(event) => setCheckFirst(event.target.checked)} />连接检查使用第一个启用的数据库</label>}
          {type === 'driver' && <ConfigFields schema={plugin.instance_config_schema} value={connection} onChange={setConnection} only={sqlite && checkFirst ? ['encodings'] : optionalInstanceFields} />}
          {type === 'driver' && !knownInstanceFields && <JsonConfigField label="插件扩展连接参数" value={connection} onChange={(next) => { if (!isObject(next)) throw new Error('扩展参数必须是对象'); setConnection(next) }} />}
          <p className="field-hint">查询上限通常无需修改，留空时使用后端默认值。</p><ConfigFields schema={limitsSchema} value={limits} onChange={setLimits} />
          <label className="config-checkbox"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用此接入</label><small className="field-hint">停用接入会同时停用其数据源，可在重新启用时选择要恢复的数据源。</small>
        </details>
      </fieldset>
      {message && <div className="form-message error" role="alert">{message}{errorDetail && <details><summary>查看详细原因</summary><p>{errorDetail}</p></details>}</div>}
      {conflict && <div className="config-conflict"><details><summary>查看服务器上的最新配置</summary><pre>{JSON.stringify({ instance: catalogRecords(conflict.config.plugin_instances).find((item) => item.id === id), sources: catalogRecords(conflict.config.sources).filter((item) => item.plugin_instance_id === id) }, null, 2)}</pre></details><button type="button" className="quiet-button" onClick={() => { setRevision(conflict.revision); setConflict(null); setMessage('已保留填写内容并采用最新版本，请再次保存。') }}>保留填写内容，使用最新版本</button><button type="button" className="quiet-button" onClick={onClose}>关闭后重新加载</button></div>}
      <div className="config-save-footer"><div><strong>{sourceDrafts.length} 个数据源</strong><small>{sqlite ? '连接检查会验证检查库或第一个启用的数据库。' : fileLogs ? '连接检查会验证日志目录是否可读。' : '配置保存后，检查连接器是否可访问。'}</small><small>进行中的诊断继续使用原有数据配置。</small></div><div className="settings-actions">{instance && <button type="button" className="text-button danger-text" onClick={() => setConfirmDelete(true)} disabled={busy || !directory.writable}>移除此接入</button>}<button type="submit" className="quiet-button" data-check="false" disabled={busy || !directory.writable || !!conflict}>仅保存</button><button type="submit" className="primary-button" disabled={busy || !directory.writable || !!conflict}>{phase === 'checking' ? '正在检查连接…' : phase === 'saving' ? '正在保存…' : enabled && plugin.health_check ? '保存并检查连接' : '保存配置'}</button></div></div>
      <div aria-live="polite" className="sr-only">{phase === 'checking' ? '配置已保存，正在检查连接' : phase === 'saving' ? '正在保存配置' : ''}</div>
    </form>
    {confirmDelete && <div className="plugin-delete-confirm" role="alert"><p>将移除此接入及全部 {sources.length} 个数据源。其他接入不受影响。</p><button type="button" className="quiet-button" onClick={() => setConfirmDelete(false)} disabled={busy}>保留接入</button><button type="button" className="primary-button" onClick={remove} disabled={busy || !!conflict}>确认移除</button></div>}
  </section>
}
