import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { getAdminEnvironmentConfig, PluginConfigError, saveAdminService } from './api'
import { generatedId, listValues } from './pluginForm'
import type { ConfigRecord } from './pluginForm'
import type { EnvironmentConfig } from './types'

export function ServiceEditor({ directory, environmentId, environmentName, service, onSaved, onRefresh, onClose, onBusyChange }: {
  directory: EnvironmentConfig
  environmentId: string
  environmentName: string
  service?: ConfigRecord
  onSaved: (config: EnvironmentConfig, serviceId: string) => void
  onRefresh: (config: EnvironmentConfig) => void
  onClose: () => void
  onBusyChange?: (busy: boolean) => void
}) {
  const section = useRef<HTMLElement>(null)
  const [id] = useState(() => String(service?.id ?? generatedId('service')))
  const [name, setName] = useState(String(service?.name ?? ''))
  const [version, setVersion] = useState(String(service?.version ?? ''))
  const [aliases, setAliases] = useState(Array.isArray(service?.aliases) ? service.aliases.join('\n') : '')
  const [revision, setRevision] = useState(directory.revision)
  const [latest, setLatest] = useState<EnvironmentConfig | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => { onBusyChange?.(busy); return () => onBusyChange?.(false) }, [busy, onBusyChange])
  useEffect(() => { section.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }); section.current?.querySelector('input')?.focus({ preventScroll: true }) }, [])
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim()) { setMessage('请填写子服务名称。'); return }
    setBusy(true)
    setMessage('')
    try {
      const next = await saveAdminService({ service: { ...service, id, name: name.trim(), environment_id: environmentId, aliases: listValues(aliases), version: version.trim() || null }, expected_revision: revision }, service ? id : undefined)
      onSaved(next, id)
    } catch (error) {
      if (error instanceof PluginConfigError && error.status === 409) {
        try { const next = await getAdminEnvironmentConfig(); onRefresh(next); setLatest(next); setMessage('配置版本已变化，填写内容已保留。核对最新配置后可再次保存。') }
        catch { setMessage('配置版本已变化，暂时无法刷新，填写内容已保留。') }
      } else setMessage(error instanceof Error ? error.message : '子服务保存失败。')
    } finally { setBusy(false) }
  }
  return <section className="panel plugin-config-editor" ref={section} aria-label="子服务配置"><div className="editor-heading"><div><span className="section-kicker">{environmentName} / 子服务</span><h2>{service ? '编辑子服务' : '新增子服务'}</h2><p>用业务名称区分服务，例如“订单 API”或“支付回调”，再为它添加所需插件。</p></div><button type="button" className="quiet-button" onClick={onClose} disabled={busy}>取消</button></div><form onSubmit={submit}><fieldset disabled={busy || !directory.writable}><label className="config-field">子服务名称<span className="required">必填</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 订单 API" required maxLength={256} /></label><details className="config-advanced"><summary>选填：简称与版本</summary><div className="config-fields"><label className="config-field">服务简称<textarea rows={3} value={aliases} onChange={(event) => setAliases(event.target.value)} placeholder={'每行一个简称，例如\norders\norder-api'} /></label><label className="config-field">服务版本<input value={version} onChange={(event) => setVersion(event.target.value)} maxLength={128} placeholder="例如 v2.1" /></label></div><p className="field-hint">所属环境固定为 {environmentName}，标识由系统自动生成。</p></details></fieldset>{message && <div className="form-message error" role="alert">{message}</div>}{latest && <button type="button" className="quiet-button" onClick={() => { setRevision(latest.revision); setLatest(null); setMessage('已采用最新版本，请再次保存。') }}>保留填写内容，采用最新版本</button>}<div className="settings-actions"><button type="submit" className="primary-button" disabled={busy || !directory.writable || !!latest}>{busy ? '正在保存…' : service ? '保存子服务' : '创建并选择子服务'}</button></div></form></section>
}
