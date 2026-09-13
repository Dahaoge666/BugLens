import { useEffect, useState } from 'react'
import { applyAdminEnvironmentConfig, checkAdminPluginInstance, validateAdminEnvironmentConfig } from './api'
import { EnvironmentEditor } from './EnvironmentEditor'
import { PluginInstanceEditor } from './PluginInstanceEditor'
import { ServiceEditor } from './ServiceEditor'
import { catalogRecords, copyRecord, instanceBelongsToService, isObject, logPaths } from './pluginForm'
import type { ConfigRecord } from './pluginForm'
import type { EnvironmentConfig, EnvironmentList, PluginCategory, PluginHealth, PluginList, PluginView } from './types'

type Panel = 'overview' | 'plugins' | 'editor'
const legacyService = '__legacy_shared__'
const fallbackCategories: PluginCategory[] = [
  { id: 'database', display_name: '数据库', description: '查看表结构、只读查询业务数据' },
  { id: 'logs', display_name: '日志', description: '检索本地或远程日志' },
  { id: 'host', display_name: '主机诊断', description: '读取系统、负载和资源状态' },
  { id: 'knowledge', display_name: '知识库', description: '检索文档和诊断知识' },
  { id: 'traffic', display_name: '流量', description: '查询已有流量证据' },
  { id: 'other', display_name: '其他连接器', description: 'MCP、CLI 等连接器' },
]
const icons: Record<string, string> = { database: '▦', logs: '≋', host: '▣', knowledge: '▤', traffic: '⌁', other: '◌' }
const healthLabels: Record<string, string> = { ok: '连接正常', degraded: '需要关注', error: '不可用', checking: '检查中…', disabled: '已停用', configured: '尚未检查' }
const healthDetails: Record<string, string> = {
  'SQLite plugin is loaded': '插件已加载，尚未检查具体数据库。',
  'SQLite database is readable': '数据库可以读取。',
  'SQLite database is unavailable': '数据库无法访问，请确认文件存在且后端有读取权限。',
  'PostgreSQL database is readable': '数据库可以读取。',
  'PostgreSQL database is unavailable': '数据库无法访问，请检查主机、账号、加密设置和访问权限。',
  'log directory is readable': '日志目录可以访问。',
  'log directory is unavailable': '日志目录无法访问，请确认目录存在且后端有读取权限。',
  'SSH host is reachable': 'SSH 主机可以连接。',
  'SSH host is unavailable; check credentials and known_hosts': 'SSH 无法连接，请检查账号、私钥或密码以及主机指纹文件。',
  'SSH log directory is readable': '远程日志目录可以读取。',
  'SSH log directory is unavailable; check credentials, known_hosts and directory permissions': '远程日志无法访问，请检查 SSH 认证、主机指纹和目录读取权限。',
}
function categoryOf(plugin: PluginView): string {
  return plugin.category && plugin.category !== 'other' ? plugin.category : fallbackCategories.find((category) => plugin.capabilities.some((capability) => capability === category.id || capability.startsWith(`${category.id}.`)))?.id ?? 'other'
}
function location(source: ConfigRecord): string {
  const values = copyRecord(source.config)
  return typeof values.path === 'string' ? values.path : logPaths(values).join('、') || (typeof values.schema === 'string' ? `Schema ${values.schema}` : '')
}
function integrationName(instance: ConfigRecord, sources: ConfigRecord[], plugin: PluginView): string {
  const config = copyRecord(instance.config)
  const transport = copyRecord(instance.transport)
  if (copyRecord(instance.usage).name && copyRecord(instance.usage).name !== plugin.display_name) return String(copyRecord(instance.usage).name)
  if (transport.type && transport.type !== 'driver') return String(transport.url ?? transport.host ?? transport.command ?? instance.id)
  if (instance.plugin_id === 'postgresql') return `${config.database ?? '数据库'} @ ${config.host ?? '主机'}`
  if (instance.plugin_id === 'ssh') return String(config.host ?? instance.id)
  if (instance.plugin_id === 'ssh_logs') return `${config.host ?? '主机'} · ${config.root_path ?? '远程日志'}`
  return sources.filter((source) => source.plugin_instance_id === instance.id).map(location).find(Boolean) || String(transport.url ?? transport.host ?? transport.command ?? config.root_path ?? instance.id)
}

export function EnvironmentPage({ environments, config, plugins, onConfigChange }: {
  environments: EnvironmentList
  config: EnvironmentConfig | null
  plugins: PluginList
  onConfigChange: (config: EnvironmentConfig | null) => void
}) {
  const [panel, setPanel] = useState<Panel>('overview')
  const [environmentId, setEnvironmentId] = useState('')
  const [serviceId, setServiceId] = useState('')
  const [adding, setAdding] = useState(false)
  const [category, setCategory] = useState('')
  const [editingEnvironment, setEditingEnvironment] = useState<{ record?: ConfigRecord } | null>(null)
  const [editingService, setEditingService] = useState<{ record?: ConfigRecord } | null>(null)
  const [editingPlugin, setEditingPlugin] = useState<{ plugin: PluginView; instance?: ConfigRecord } | null>(null)
  const [checks, setChecks] = useState<Record<string, { status: string; detail: string }>>({})
  const [json, setJson] = useState('')
  const [message, setMessage] = useState('')
  const [tone, setTone] = useState<'success' | 'error' | 'info'>('info')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (config) setJson(JSON.stringify(config.config, null, 2)) }, [config])
  const directory = config?.config ?? {}
  const envs = config ? catalogRecords(directory.environments) : environments.items.map((item) => ({ ...item, id: item.environment_id, enabled: true }))
  const services = catalogRecords(directory.services).filter((service) => service.environment_id === environmentId)
  const sources = catalogRecords(directory.sources)
  const instances = catalogRecords(directory.plugin_instances)
  const selectedEnvironment = envs.find((item) => item.id === environmentId)
  const selectedService = services.find((item) => item.id === serviceId)
  const categories = plugins.categories ?? fallbackCategories
  const envInstances = instances.filter((instance) => instance.environment_id === environmentId)
  const shared = envInstances.filter((instance) => !instance.service_id && !services.some((service) => instanceBelongsToService(instance, sources, String(service.id))))
  const scoped = serviceId === legacyService ? shared : envInstances.filter((instance) => instanceBelongsToService(instance, sources, serviceId))
  const canAdd = !!config?.writable && !!selectedService && selectedEnvironment?.enabled !== false
  const visiblePlugins = adding ? plugins.items.filter((plugin) => categoryOf(plugin) === category) : plugins.items.filter((plugin) => scoped.some((instance) => instance.plugin_id === plugin.plugin_id))
  const selectable = envs.filter((item) => item.enabled !== false).map((item) => ({ environment_id: String(item.id), display_name: String(item.display_name), aliases: Array.isArray(item.aliases) ? item.aliases.map(String) : [], level: String(item.level ?? 'unknown'), region: typeof item.region === 'string' ? item.region : null, timezone: String(item.timezone ?? 'UTC'), tags: {} }))

  function feedback(text: string, nextTone: typeof tone = 'info') { setMessage(text); setTone(nextTone) }
  function selectEnvironment(id: string) { setEnvironmentId(id); setServiceId(''); setPanel('overview'); setEditingEnvironment(null); setEditingService(null); setEditingPlugin(null); setAdding(false); setCategory(''); setMessage('') }
  function selectService(id: string) { setServiceId(id); setPanel('plugins'); setEditingService(null); setEditingPlugin(null); setAdding(false); setCategory(''); setMessage('') }
  function addPlugin() { setPanel('plugins'); setAdding(true); setCategory(''); setEditingPlugin(null); setMessage('') }
  async function check(id: string) {
    setChecks((current) => ({ ...current, [id]: { status: 'checking', detail: '正在检查连接…' } }))
    try { const health = await checkAdminPluginInstance(id); setChecks((current) => ({ ...current, [id]: { status: health.status, detail: healthDetails[health.detail] ?? health.detail } })) }
    catch (error) { setChecks((current) => ({ ...current, [id]: { status: 'error', detail: error instanceof Error ? error.message : '连接检查失败。' } })) }
  }
  async function saveDirectory(validate: boolean) {
    if (!config) return
    setBusy(true)
    try {
      const payload: unknown = JSON.parse(json)
      if (!isObject(payload)) throw new Error('目录配置需要是 JSON 对象。')
      if (validate) { const result = await validateAdminEnvironmentConfig({ config: payload, expected_revision: config.revision }); feedback(result.valid ? '目录校验通过，尚未保存。' : result.errors.join('；'), result.valid ? 'success' : 'error') }
      else { onConfigChange(await applyAdminEnvironmentConfig({ config: payload, expected_revision: config.revision })); feedback('目录已保存，进行中的诊断继续使用原快照。', 'success') }
    } catch (error) { feedback(error instanceof Error ? error.message : '操作失败。', 'error') }
    finally { setBusy(false) }
  }
  function savedIntegration(next: EnvironmentConfig, result: { id: string; health?: PluginHealth; removed?: boolean }) {
    onConfigChange(next); setEditingPlugin(null); setAdding(false)
    const health = result.health ? { ...result.health, detail: healthDetails[result.health.detail] ?? result.health.detail } : undefined
    setChecks((current) => { const updated = { ...current }; delete updated[result.id]; if (health) updated[result.id] = health; return updated })
    feedback(result.removed ? '此接入及其数据源已移除。' : health?.status === 'ok' ? '配置已保存，连接检查通过。' : health ? `配置已保存，连接需要关注：${health.detail}` : '此接入配置已保存。', health && health.status !== 'ok' ? 'info' : 'success')
  }

  return <div className="management-content">
    <div className="page-heading"><div><span className="eyebrow">管理 / 环境与插件</span><h1>按服务连接诊断数据</h1><p>选择环境与子服务，再按类别添加需要的插件。每项接入独立保存配置和凭据。</p></div><button className="primary-button" disabled={!config?.writable || busy} onClick={() => { setPanel('overview'); setEditingEnvironment({}); setEditingService(null); setEditingPlugin(null); setMessage('') }}>＋ 新增环境</button></div>
    <ol className="connection-flow" aria-label="添加插件流程"><li className={selectedEnvironment ? 'complete' : 'current'}><b>1</b>选择环境</li><li className={selectedService ? 'complete' : selectedEnvironment ? 'current' : ''}><b>2</b>选择子服务</li><li className={adding || editingPlugin ? 'complete' : selectedService ? 'current' : ''}><b>3</b>添加插件</li><li className={category || editingPlugin ? 'complete' : adding ? 'current' : ''}><b>4</b>选择类别</li><li className={editingPlugin ? 'current' : ''}><b>5</b>选择并配置</li></ol>
    <div className="environment-tabs" role="tablist" aria-label="环境管理视图"><button role="tab" aria-selected={panel === 'overview'} className={panel === 'overview' ? 'active' : ''} disabled={busy} onClick={() => { setPanel('overview'); setEditingPlugin(null); setAdding(false) }}>环境与子服务</button><button role="tab" aria-selected={panel === 'plugins'} className={panel === 'plugins' ? 'active' : ''} disabled={busy} onClick={() => { setPanel('plugins'); setEditingEnvironment(null); setEditingService(null) }}>服务插件 <span>{scoped.length}</span></button><button role="tab" aria-selected={panel === 'editor'} className={panel === 'editor' ? 'active' : ''} disabled={busy} onClick={() => { setPanel('editor'); setEditingPlugin(null); setEditingEnvironment(null); setEditingService(null) }}>高级目录</button></div>
    {!config && <div className="form-message info" role="status">暂时无法读取配置，请检查后端连接。</div>}{config && !config.writable && <div className="form-message info">当前目录只读，请管理员开启配置写入后再添加子服务或插件。</div>}
    {panel !== 'editor' && envs.length > 0 && <section className="panel service-context" aria-label="当前配置范围"><label>环境<select aria-label="选择环境" value={environmentId} disabled={busy} onChange={(event) => selectEnvironment(event.target.value)}><option value="">请选择环境</option>{envs.map((env) => <option key={String(env.id)} value={String(env.id)}>{String(env.display_name)}{env.enabled === false ? '（已停用）' : ''}</option>)}</select></label><span className="context-arrow">›</span><label>子服务<select aria-label="选择子服务" value={serviceId} disabled={!selectedEnvironment || busy} onChange={(event) => selectService(event.target.value)}><option value="">请选择子服务</option>{services.map((service) => <option key={String(service.id)} value={String(service.id)}>{String(service.name)}</option>)}{shared.length > 0 && <option value={legacyService}>环境级历史接入（{shared.length} 项）</option>}</select></label><button className="primary-button" disabled={!canAdd || busy} onClick={addPlugin}>＋ 添加插件</button></section>}
    {panel === 'overview' && <>
      {config && editingEnvironment && <EnvironmentEditor key={String(editingEnvironment.record?.id ?? 'new')} config={config} environment={editingEnvironment.record} onRefresh={onConfigChange} onBusyChange={setBusy} onClose={() => setEditingEnvironment(null)} onSaved={(next, id) => { onConfigChange(next); selectEnvironment(id); if (!editingEnvironment.record) { setEditingService({}); feedback('环境已创建，添加一个子服务后即可接入插件。', 'success') } else feedback('环境信息已保存。', 'success') }} />}
      {!selectedEnvironment && !editingEnvironment && <section className="panel"><div className="panel-header"><h2>{envs.length ? '第一步 · 选择环境' : '从一个环境、一个服务开始'}</h2><span>{envs.length} 个环境</span></div>{envs.length === 0 ? <div className="empty-state"><p>先创建环境，再为其中的子服务添加数据库、日志或主机诊断插件。</p><button className="primary-button" disabled={!config?.writable || busy} onClick={() => setEditingEnvironment({})}>创建第一个环境</button></div> : <div className="environment-card-list">{envs.map((env) => <article className="environment-card" key={String(env.id)}><div className="environment-card-head"><span className="environment-mark">◈</span><div><strong>{String(env.display_name)}</strong><small>{catalogRecords(directory.services).filter((service) => service.environment_id === env.id).length} 个子服务 · {instances.filter((instance) => instance.environment_id === env.id).length} 项接入</small></div><span className={`directory-badge ${env.enabled === false ? 'neutral' : 'ok'}`}>{env.enabled === false ? '已停用' : '已启用'}</span></div><div className="environment-card-actions"><button className="primary-button" disabled={busy} onClick={() => selectEnvironment(String(env.id))}>选择环境</button><button className="text-button" disabled={!config?.writable || busy} onClick={() => setEditingEnvironment({ record: env })}>编辑环境</button></div></article>)}</div>}</section>}
      {selectedEnvironment && <>
        <section className="panel"><div className="config-section-heading"><div><span className="section-kicker">第二步 · 选择子服务</span><h2>{String(selectedEnvironment.display_name)}</h2><p>每个子服务管理自己的插件接入，可按需要重复添加同一插件。</p></div><div className="environment-card-actions"><button className="quiet-button" disabled={!config?.writable || busy} onClick={() => { setEditingService(null); setEditingEnvironment({ record: selectedEnvironment }) }}>编辑环境</button><button className="primary-button" disabled={!config?.writable || selectedEnvironment.enabled === false || busy} onClick={() => { setEditingEnvironment(null); setEditingService({}) }}>＋ 新增子服务</button></div></div>{services.length === 0 ? <div className="empty-state"><p>此环境还没有子服务。创建一个业务服务后，再选择需要的插件。</p><button className="primary-button" disabled={!config?.writable || selectedEnvironment.enabled === false || busy} onClick={() => { setEditingEnvironment(null); setEditingService({}) }}>创建第一个子服务</button></div> : <div className="service-card-list">{services.map((service) => <article className={`service-card ${service.id === serviceId ? 'selected' : ''}`} key={String(service.id)}><div><strong>{String(service.name)}</strong><small>{envInstances.filter((instance) => instanceBelongsToService(instance, sources, String(service.id))).length} 项插件接入{service.version ? ` · ${service.version}` : ''}</small></div><div className="environment-card-actions"><button className="primary-button" disabled={busy} onClick={() => selectService(String(service.id))}>选择子服务</button><button className="text-button" disabled={!config?.writable || busy} onClick={() => { setEditingEnvironment(null); setEditingService({ record: service }) }}>编辑信息</button></div></article>)}</div>}{shared.length > 0 && <button className="text-button legacy-integrations" disabled={busy} onClick={() => selectService(legacyService)}>管理 {shared.length} 项环境级历史接入 →</button>}</section>
        {config && editingService && <ServiceEditor key={String(editingService.record?.id ?? 'new')} directory={config} environmentId={environmentId} environmentName={String(selectedEnvironment.display_name)} service={editingService.record} onRefresh={onConfigChange} onBusyChange={setBusy} onClose={() => setEditingService(null)} onSaved={(next, id) => { onConfigChange(next); selectService(id); feedback('子服务已保存，点击“添加插件”选择需要的类别。', 'success') }} />}
      </>}
    </>}
    {panel === 'plugins' && <>
      {!selectedService && serviceId !== legacyService && <section className="panel onboarding-card"><h2>先选择环境中的子服务</h2><p>选择上方环境和子服务，接入将自动归属该服务。还没有子服务时可以先创建。</p><button className="primary-button" disabled={!selectedEnvironment || !config?.writable || selectedEnvironment.enabled === false || busy} onClick={() => { setPanel('overview'); setEditingService({}) }}>新增子服务</button></section>}
      {(selectedService || serviceId === legacyService) && <>
        <section className="panel"><div className="config-section-heading"><div><span className="section-kicker">{String(selectedEnvironment?.display_name)} / {String(selectedService?.name ?? '环境级历史接入')}</span><h2>{adding ? '选择插件类别' : '此服务的插件'}</h2><p>{adding ? '选择你要读取的数据或检查对象，再选择连接器填写配置。' : serviceId === legacyService ? '保留旧接入的管理入口；新插件请添加到具体子服务。' : `${scoped.length} 项独立接入，配置和凭据分别管理。`}</p></div>{adding ? <button className="quiet-button" disabled={busy} onClick={() => { setAdding(false); setEditingPlugin(null); setCategory('') }}>返回服务插件</button> : <button className="primary-button" disabled={!canAdd || busy} onClick={addPlugin}>＋ 添加插件</button>}</div>
          {adding && <div className="plugin-category-grid" aria-label="插件类别">{categories.map((item) => <button type="button" key={item.id} className={`plugin-category-card ${category === item.id ? 'selected' : ''}`} disabled={busy} onClick={() => { setCategory(item.id); setEditingPlugin(null) }}><span className="plugin-category-icon">{icons[item.id] ?? '◌'}</span><strong>{item.display_name}</strong><small>{item.description}</small><span>{plugins.items.filter((plugin) => categoryOf(plugin) === item.id).length} 个连接器</span></button>)}</div>}
          {!adding && scoped.length === 0 && <div className="empty-state"><p>此服务尚未接入插件。添加数据库、日志或主机诊断插件，为诊断提供证据。</p><button className="primary-button" disabled={!canAdd || busy} onClick={addPlugin}>添加第一个插件</button></div>}
        </section>
        {config && editingPlugin && <PluginInstanceEditor key={`${editingPlugin.plugin.plugin_id}:${editingPlugin.instance?.id ?? 'new'}`} directory={config} plugin={editingPlugin.plugin} instance={editingPlugin.instance} sources={sources.filter((source) => source.plugin_instance_id === editingPlugin.instance?.id)} environments={[...selectable].sort((left, right) => Number(right.environment_id === environmentId) - Number(left.environment_id === environmentId))} selectedServiceId={editingPlugin.instance?.service_id ? String(editingPlugin.instance.service_id) : !editingPlugin.instance && selectedService ? serviceId : ''} onRefresh={onConfigChange} onBusyChange={setBusy} onClose={() => setEditingPlugin(null)} onSaved={savedIntegration} />}
        {adding && category && visiblePlugins.length === 0 && <section className="panel empty-state"><strong>该类别尚未安装连接器</strong><p>请管理员使用后端插件安装说明启用所需连接器，再回到这里配置。</p></section>}
        {visiblePlugins.map((plugin) => <section className="panel plugin-detail-card" key={plugin.plugin_id}><div className="plugin-detail-head"><span className="plugin-icon">{icons[categoryOf(plugin)] ?? '◌'}</span><div className="plugin-detail-title"><h2>{plugin.display_name ?? plugin.plugin_id}</h2><p>{plugin.description || categories.find((item) => item.id === categoryOf(plugin))?.description || '独立配置此连接器的数据位置与凭据。'}</p><small>{categories.find((item) => item.id === categoryOf(plugin))?.display_name ?? '其他连接器'} · {(plugin.supported_transports ?? ['driver', 'mcp', 'cli']).map((method) => method === 'driver' ? '内置驱动' : method.toUpperCase()).join(' / ')}</small></div><button className="primary-button" disabled={!canAdd || busy} onClick={() => { setEditingPlugin({ plugin }); setMessage('') }}>{adding ? '选择并配置' : '再添加一次'}</button></div><div className="plugin-instance-list">{scoped.filter((instance) => instance.plugin_id === plugin.plugin_id).map((instance) => { const id = String(instance.id); const result = checks[id]; const status = instance.enabled === false ? 'disabled' : result?.status ?? 'configured'; return <div className="plugin-instance-card" key={id}><div className="plugin-instance-copy"><span className={`instance-pulse ${status === 'ok' ? 'ok' : status === 'error' ? 'error' : 'neutral'}`} /><div><strong>{integrationName(instance, sources, plugin)}</strong><small>{sources.filter((source) => source.plugin_instance_id === id).length} 个数据源{result ? ` · ${result.detail}` : ''}</small></div></div><div className="plugin-instance-actions"><span className={`plugin-health ${status}`}><i />{healthLabels[status] ?? status}</span><button className="quiet-button" disabled={busy} onClick={() => { setEditingPlugin({ plugin, instance }); setMessage('') }}>管理配置</button><button className="quiet-button" disabled={busy || status === 'checking' || instance.enabled === false || !plugin.health_check} onClick={() => check(id)}>检查连接</button></div></div> })}</div><details className="schema-details"><summary>技术信息与配置规范</summary><p className="muted mono">{plugin.plugin_id} · v{plugin.implementation_version} · Plugin API {plugin.api_major}</p><div className="schema-grid"><div><span>连接配置规范</span><pre>{JSON.stringify(plugin.instance_config_schema, null, 2)}</pre></div><div><span>数据源配置规范</span><pre>{JSON.stringify(plugin.source_config_schema, null, 2)}</pre></div></div></details></section>)}
        {!adding && scoped.some((instance) => !plugins.items.some((plugin) => plugin.plugin_id === instance.plugin_id)) && <div className="form-message info">部分旧接入的连接器未安装，可通过高级目录维护或请管理员恢复连接器。</div>}
      </>}
    </>}
    {panel !== 'editor' && message && <div className={`form-message ${tone}`} role="status">{message}</div>}
    {panel === 'editor' && <section className="panel environment-editor"><div className="editor-heading"><div><span className="section-kicker">高级维护</span><h2>完整环境目录</h2><p>常用操作请按环境和子服务添加插件；JSON 仅用于批量维护与扩展元数据。</p></div><span className="mono muted">{config?.revision ?? '未连接'}</span></div><p className="config-callout">凭据只展示已配置状态，请进入具体插件接入的“管理配置”修改。</p><textarea aria-label="完整环境目录 JSON" rows={18} value={json} onChange={(event) => setJson(event.target.value)} disabled={!config?.writable || busy} spellCheck={false} /><div className="settings-actions"><button className="quiet-button" disabled={!config?.writable || busy} onClick={() => saveDirectory(true)}>校验目录</button><button className="primary-button" disabled={!config?.writable || busy} onClick={() => saveDirectory(false)}>保存完整目录</button></div>{message && <div className={`form-message ${tone}`} role="status">{message}</div>}</section>}
  </div>
}
