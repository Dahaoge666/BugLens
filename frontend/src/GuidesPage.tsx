import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { getAdminGuides, getGuideCategories, GuideRequestError, importAdminGuides, updateAdminGuide } from './api'
import { GuideDetail } from './GuideDetail'
import { guideContent, parseGuideImport } from './guideEntry'
import type { DiagnosisGuide, EnvironmentSummary, GuideCategories, GuideCategory, GuideContent, GuideList } from './types'

const initialContent: GuideContent = { title: '', category: 'performance', phenomenon: '', symptoms: [], applicability: '核对本次现象与适用条件，再按步骤采集证据。', steps: [], enabled: true }
const exampleContent: GuideContent = { ...initialContent, title: '数据库连接池等待超时', phenomenon: '请求日志包含 timeout waiting for database connection，接口延迟升高。', symptoms: ['timeout waiting for database connection', '接口延迟升高'], steps: [{ instruction: '检查故障时间窗口内的连接池使用率与等待时间。', expected_observation: '连接池使用率是否达到上限，等待时间是否同步上升。' }, { instruction: '核对同一时间窗口内的慢查询与依赖耗时。' }] }

export function GuidesPage({ environments }: { environments: EnvironmentSummary[] }) {
  const [categories, setCategories] = useState<GuideCategories>({ items: [] })
  const [list, setList] = useState<GuideList>({ items: [], total: 0 })
  const [category, setCategory] = useState('')
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<DiagnosisGuide | null>(null)
  const [edit, setEdit] = useState<DiagnosisGuide | null>(null)
  const [mode, setMode] = useState<'form' | 'json'>('form')
  const [content, setContent] = useState(initialContent)
  const [symptoms, setSymptoms] = useState('')
  const [steps, setSteps] = useState('')
  const [tenant, setTenant] = useState('')
  const [json, setJson] = useState(JSON.stringify({ guides: [exampleContent] }, null, 2))
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('现象明确且有证据支持的诊断会自动整理为指南。')
  const [error, setError] = useState('')

  useEffect(() => { getGuideCategories().then(setCategories).catch(() => setError('问题分类加载失败，请刷新页面重试。')) }, [])
  useEffect(() => {
    let active = true
    getAdminGuides(category, offset).then((value) => { if (active) setList(value) }).catch((cause: unknown) => { if (active) setError(cause instanceof Error ? cause.message : '指南列表加载失败') })
    return () => { active = false }
  }, [category, offset])

  async function refresh() {
    const value = await getAdminGuides(category, offset)
    setList(value)
    if (selected) setSelected(value.items.find((item) => item.guide_id === selected.guide_id) ?? null)
  }

  const label = (id: string) => categories.items.find((item) => item.id === id)?.display_name ?? id

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true); setError('')
    try {
      const payload = mode === 'json' ? parseGuideImport(json) : { tenant_id: tenant || null, guides: [{ ...content, symptoms: symptoms.split('\n').map((item) => item.trim()).filter(Boolean), steps: steps.split('\n').map((instruction) => instruction.trim()).filter(Boolean).map((instruction) => ({ instruction })) }] }
      if (edit) {
        const items = (payload as { guides?: GuideContent[] }).guides
        if (!Array.isArray(items) || items.length !== 1) throw new Error('编辑时请提供一份指南对象。')
        const updated = await updateAdminGuide(edit, items[0])
        setSelected(updated); setEdit(null); setNotice('指南已更新。')
      } else {
        const imported = await importAdminGuides(payload)
        setNotice(`已导入 ${imported.total} 份定位指南。`)
        setContent(initialContent); setSymptoms(''); setSteps('')
      }
      await refresh()
    } catch (cause) {
      setError(cause instanceof GuideRequestError && cause.status === 409 ? '指南已被其他人更新，请重新载入后再编辑。草稿已保留。' : cause instanceof Error ? cause.message : '指南保存失败')
      if (cause instanceof GuideRequestError && cause.status === 409) await refresh().catch(() => undefined)
    } finally { setBusy(false) }
  }

  async function toggle(guide: DiagnosisGuide) {
    if (busy) return
    setBusy(true); setError('')
    try {
      const updated = await updateAdminGuide(guide, { ...guideContent(guide), enabled: !guide.enabled })
      setNotice(updated.enabled ? '指南已启用，将参与问题入口匹配。' : '指南已停用。')
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '更新失败')
      await refresh().catch(() => undefined)
    } finally { setBusy(false) }
  }

  async function loadFile(file: File | undefined) {
    if (!file) return
    if (file.size > 1048576) { setError('请选择不超过 1 MiB 的 JSON 文件。'); return }
    try { setJson(await file.text()); setMode('json'); setEdit(null); setError('') }
    catch { setError('文件读取失败，请重试。') }
  }

  return <div className="management-content guides-page">
    <div className="page-heading"><div><div className="eyebrow">工作区 / 定位指南</div><h1>问题定位指南</h1><p>按现象和分类积累排查方法，提问时先找到合适的指南。</p></div><button className="quiet-button" disabled={busy} onClick={() => { setError(''); refresh().catch((cause: Error) => setError(cause.message)) }}>刷新指南</button></div>
    <p aria-live="polite" className="guide-entry-status">{notice}</p>{error && <p className="guide-error" role="alert">{error}</p>}
    <div className="guides-layout">
      <section className="panel guide-library"><div className="panel-header"><h2>指南库 · {list.total}</h2><label>问题分类<select value={category} onChange={(event) => { setCategory(event.target.value); setOffset(0); setSelected(null) }}><option value="">全部分类</option>{categories.items.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label></div>
        {!list.items.length && <p className="guide-empty">暂无指南。可手工导入，也可从后续完成的诊断中自动整理。</p>}
        {list.items.map((guide) => <article key={guide.guide_id} className="guide-library-card"><div><span className="guide-category">{label(guide.category)}</span><small>{guide.origin === 'memory' ? '来自 Memory' : '手工导入'} · {guide.enabled ? '已启用' : '已停用'}</small></div><h3>{guide.title}</h3><p>{guide.phenomenon}</p><small>{guide.environment || '未限定环境'} · {guide.service || '未限定服务'}</small><div className="guide-actions"><button className="quiet-button" onClick={() => setSelected(guide)}>查看指南</button><button className="text-button" disabled={busy} onClick={() => { setEdit(guide); setMode('json'); setJson(JSON.stringify(guideContent(guide), null, 2)); setError(''); setNotice(`正在编辑：${guide.title}`) }}>编辑</button><button className="text-button" disabled={busy} onClick={() => toggle(guide)}>{guide.enabled ? '停用' : '启用'}</button></div></article>)}
        <div className="guide-actions"><button className="quiet-button" disabled={offset === 0 || busy} onClick={() => setOffset(Math.max(0, offset - 100))}>上一页</button><span>{list.total ? offset + 1 : 0}–{Math.min(offset + 100, list.total)} / {list.total}</span><button className="quiet-button" disabled={offset + 100 >= list.total || busy} onClick={() => setOffset(offset + 100)}>下一页</button></div>
      </section>
      <section className="panel guide-import"><div className="panel-header"><h2>{edit ? '编辑定位指南' : '手工导入指南'}</h2></div><div className="filter-tabs"><button className={mode === 'form' ? 'active' : ''} disabled={Boolean(edit) || busy} onClick={() => setMode('form')}>填写表单</button><button className={mode === 'json' ? 'active' : ''} disabled={busy} onClick={() => setMode('json')}>JSON 导入</button></div>
        <form onSubmit={submit}><fieldset disabled={busy}>{mode === 'json' ? <><label>指南 JSON<textarea value={json} onChange={(event) => setJson(event.target.value)} rows={18} required spellCheck={false} /></label>{!edit && <label>选择 JSON 文件<input type="file" accept=".json,application/json" onChange={(event) => loadFile(event.target.files?.[0])} /></label>}<p className="muted">支持单份指南、指南数组或包含 guides 的对象；样例中的定位步骤可直接替换。</p></> : <>
          <label>指南标题<input value={content.title} onChange={(event) => setContent({ ...content, title: event.target.value })} maxLength={200} required /></label>
          <label>问题分类<select value={content.category} onChange={(event) => setContent({ ...content, category: event.target.value as GuideCategory })}>{categories.items.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>
          <label>明确的问题现象<textarea value={content.phenomenon} onChange={(event) => setContent({ ...content, phenomenon: event.target.value })} maxLength={2000} rows={3} required /></label>
          <label>症状或错误关键词（每行一条）<textarea value={symptoms} onChange={(event) => setSymptoms(event.target.value)} rows={3} required /></label>
          <div className="form-row"><label>适用环境<select value={content.environment || ''} onChange={(event) => setContent({ ...content, environment: event.target.value || null })}><option value="">通用指南，不限定环境</option>{environments.map((item) => <option key={item.environment_id} value={item.environment_id}>{item.display_name}</option>)}</select></label><label>适用服务<input value={content.service || ''} onChange={(event) => setContent({ ...content, service: event.target.value || null })} placeholder="可选，填写服务 ID" maxLength={256} /></label></div>
          <label>适用版本<input value={content.version || ''} onChange={(event) => setContent({ ...content, version: event.target.value || null })} placeholder="可选" maxLength={256} /></label>
          <label>适用条件<textarea value={content.applicability} onChange={(event) => setContent({ ...content, applicability: event.target.value })} maxLength={2000} rows={2} /></label>
          <label>定位步骤（每行一步）<textarea value={steps} onChange={(event) => setSteps(event.target.value)} rows={5} required /></label>
          <label>租户标签<input value={tenant} onChange={(event) => setTenant(event.target.value)} maxLength={128} placeholder="可选，留空用于当前默认租户" /></label>
        </>}</fieldset><div className="guide-actions">{edit && <button type="button" className="quiet-button" disabled={busy} onClick={() => { setEdit(null); setJson(JSON.stringify({ guides: [exampleContent] }, null, 2)) }}>取消编辑</button>}<button type="submit" className="primary-button" disabled={busy}>{busy ? '正在保存…' : edit ? '保存指南' : '导入指南'}</button></div></form>
      </section>
    </div>
    {selected && <section className="panel guide-selected"><div className="panel-header"><h2>{selected.title}</h2><button className="quiet-button" onClick={() => setSelected(null)}>收起</button></div><GuideDetail guide={selected} /></section>}
  </div>
}
