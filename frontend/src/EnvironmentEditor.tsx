import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { applyAdminEnvironmentConfig, getAdminEnvironmentConfig, PluginConfigError } from './api'
import { catalogRecords, generatedId, listValues } from './pluginForm'
import type { ConfigRecord } from './pluginForm'
import type { EnvironmentConfig } from './types'

const presets = [
  { id: 'testing', name: '测试环境', level: 'testing' },
  { id: 'staging', name: '预发布环境', level: 'staging' },
  { id: 'production', name: '生产环境', level: 'production' },
]
const levelLabels: Record<string, string> = { development: '开发', testing: '测试', staging: '预发布', production: '生产', 'non-production': '非生产', unknown: '其他' }

export function EnvironmentEditor({ config, environment, onSaved, onRefresh, onClose, onBusyChange }: {
  config: EnvironmentConfig
  environment?: ConfigRecord
  onSaved: (config: EnvironmentConfig, environmentId: string) => void
  onRefresh: (config: EnvironmentConfig) => void
  onClose: () => void
  onBusyChange?: (busy: boolean) => void
}) {
  const sectionRef = useRef<HTMLElement>(null)
  const [id, setId] = useState(() => String(environment?.id ?? generatedId('env')))
  const [name, setName] = useState(String(environment?.display_name ?? ''))
  const [level, setLevel] = useState(String(environment?.level ?? 'staging'))
  const [region, setRegion] = useState(String(environment?.region ?? ''))
  const [timezone, setTimezone] = useState(String(environment?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC'))
  const [aliases, setAliases] = useState(Array.isArray(environment?.aliases) ? environment.aliases.join('\n') : '')
  const [enabled, setEnabled] = useState(environment?.enabled !== false)
  const [revision, setRevision] = useState(config.revision)
  const [message, setMessage] = useState('')
  const [latest, setLatest] = useState<EnvironmentConfig | null>(null)
  const [busy, setBusy] = useState(false)
  const existing = catalogRecords(config.config.environments)
  useEffect(() => { onBusyChange?.(busy); return () => onBusyChange?.(false) }, [busy, onBusyChange])

  useEffect(() => { sectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }); sectionRef.current?.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true }) }, [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!name.trim()) { setMessage('请填写环境名称。'); return }
    if (!environment && existing.some((item) => item.id === id)) { setMessage('此环境标识已被使用，请在高级选项中换一个标识。'); return }
    if (environment && !existing.some((item) => item.id === id)) { setMessage('此环境已被移除，填写的内容已保留。请取消编辑并新增环境。'); return }
    setBusy(true)
    setMessage('')
    try {
      const record = { ...environment, id, display_name: name.trim(), level, region: region.trim() || null, timezone: timezone.trim() || 'UTC', aliases: listValues(aliases), enabled }
      const records = environment ? existing.map((item) => item.id === id ? record : item) : [...existing, record]
      const next = await applyAdminEnvironmentConfig({ config: { ...config.config, environments: records }, expected_revision: revision })
      onSaved(next, id)
    } catch (error) {
      if (error instanceof PluginConfigError && error.status === 409) {
        try {
          const refreshed = await getAdminEnvironmentConfig()
          onRefresh(refreshed)
          setLatest(refreshed)
          setMessage('环境配置已更新，你填写的内容已保留。请读取最新版本后再保存。')
        } catch { setMessage('配置版本已变化，暂时无法刷新；填写的内容已保留，请稍后重试。') }
      } else setMessage(error instanceof Error ? error.message : '环境保存失败，请稍后重试。')
    } finally { setBusy(false) }
  }

  return <section className="panel environment-form plugin-config-editor" ref={sectionRef} aria-label="环境配置">
    <div className="editor-heading"><div><span className="section-kicker">{environment ? '编辑环境' : '第一步 · 创建环境'}</span><h2>{environment ? '环境信息' : '先给诊断目标起个名字'}</h2><p>例如“订单预发布”或“华东生产”，下一步添加子服务，再为服务配置所需插件。</p></div><button type="button" className="quiet-button" onClick={onClose} disabled={busy}>取消</button></div>
    <form onSubmit={submit} onInvalid={(event) => { (event.target as HTMLElement).closest('details')?.setAttribute('open', '') }}><fieldset disabled={busy || !config.writable}>
      {!environment && <div className="environment-presets" aria-label="常用环境模板">{presets.map((preset) => <button type="button" className="quiet-button" key={preset.id} onClick={() => { setName(preset.name); setLevel(preset.level); setId(existing.some((item) => item.id === preset.id) ? generatedId(preset.id) : preset.id) }}>{preset.name}</button>)}</div>}
      <div className="config-fields"><label className="config-field">环境名称<span className="required">必填</span><input value={name} onChange={(event) => setName(event.target.value)} required maxLength={256} placeholder="例如 订单预发布" /></label><label className="config-field">环境类型<select value={level} onChange={(event) => setLevel(event.target.value)}>{[...new Set([...Object.keys(levelLabels), level])].map((value) => <option key={value} value={value}>{levelLabels[value] ?? value}</option>)}</select></label></div>
      <details className="config-advanced"><summary>选填：别名、区域与更多信息</summary><div className="config-fields"><label className="config-field">环境别名<textarea rows={3} value={aliases} onChange={(event) => setAliases(event.target.value)} placeholder={'例如 stage\n预发；每行一个别名'} /><small>描述问题时使用这些简称，也可以匹配到此环境。</small></label><label className="config-field">部署区域<input value={region} onChange={(event) => setRegion(event.target.value)} maxLength={128} placeholder="例如 cn-east-1" /></label><label className="config-field">时区<input value={timezone} onChange={(event) => setTimezone(event.target.value)} maxLength={128} placeholder="例如 Asia/Shanghai" /><small>已按当前浏览器时区预填，可按部署位置修改。</small></label><label className="config-field">环境标识<input value={id} onChange={(event) => setId(event.target.value)} disabled={!!environment} required pattern="[A-Za-z0-9_.-]+" maxLength={128} /><small>系统自动生成，通常无需修改。</small></label></div><label className="config-checkbox"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用此环境</label></details>
    </fieldset>{message && <div className="form-message error" role="alert">{message}</div>}{latest && <button type="button" className="quiet-button" onClick={() => { setRevision(latest.revision); setLatest(null); setMessage('已采用最新版本，请核对后再次保存。') }}>保留填写内容，采用最新版本</button>}<div className="settings-actions"><button type="submit" className="primary-button" disabled={busy || !config.writable || !!latest}>{busy ? '正在保存…' : environment ? '保存环境信息' : '创建环境并添加子服务'}</button></div></form>
  </section>
}
