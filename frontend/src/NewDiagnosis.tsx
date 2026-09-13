import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { findGuides, getGuideCategories } from './api'
import { GuideDetail } from './GuideDetail'
import { lookupAtEntry } from './guideEntry'
import type { DiagnosisGuide, EnvironmentSummary, Evidence, GuideCategories, GuideLookup, TargetSpec } from './types'

type Props = { profiles: string[]; environments: EnvironmentSummary[]; onClose: () => void; onSubmit: (question: string, context: Record<string, string>, evidence: Evidence[], profile: string, target: TargetSpec) => Promise<void> }
type EntryDraft = { question: string; context: Record<string, string>; evidence: Evidence[]; profile: string; target: TargetSpec }

export function NewDiagnosis({ profiles, environments, onClose, onSubmit }: Props) {
  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<'explicit' | 'infer'>('infer')
  const [environment, setEnvironment] = useState(environments[0]?.environment_id ?? '')
  const [environmentHint, setEnvironmentHint] = useState('')
  const [service, setService] = useState('')
  const [log, setLog] = useState('')
  const [profile, setProfile] = useState(profiles[0] ?? 'default')
  const [autoExplore, setAutoExplore] = useState(false)
  const [lookup, setLookup] = useState<GuideLookup | null>(null)
  const [draft, setDraft] = useState<EntryDraft | null>(null)
  const [selected, setSelected] = useState<DiagnosisGuide | null>(null)
  const [categories, setCategories] = useState<GuideCategories>({ items: [] })
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('先查找相似问题，有合适指南时可直接查看。')
  const [error, setError] = useState('')
  const root = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  const isBusy = useRef(busy)
  close.current = onClose
  isBusy.current = busy

  useEffect(() => {
    getGuideCategories().then(setCategories).catch(() => undefined)
    const previous = document.activeElement as HTMLElement | null
    root.current?.querySelector<HTMLElement>('textarea')?.focus()
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !isBusy.current) close.current()
      if (event.key === 'Tab') {
        const elements = [...(root.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled)') ?? [])]
        const first = elements[0], last = elements.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', keydown)
    return () => { document.removeEventListener('keydown', keydown); previous?.focus() }
  }, [])

  useEffect(() => { if (!environment && environments[0]) setEnvironment(environments[0].environment_id) }, [environment, environments])
  useEffect(() => { if (lookup || selected) root.current?.querySelector<HTMLElement>('h2')?.focus() }, [lookup, selected])

  function buildDraft(): EntryDraft {
    const hint = mode === 'infer' ? environmentHint.trim() : environment
    return {
      question: question.trim(),
      context: { ...(hint ? { environment: hint } : {}), ...(service.trim() ? { service: service.trim() } : {}), ...(autoExplore ? { auto_explore: 'true' } : {}) },
      evidence: log.trim() ? [{ source: 'log', content: log.trim() }] : [],
      profile,
      target: { mode, environment_id: hint || null, primary_service_id: service.trim() || null },
    }
  }

  async function beginDiagnosis(value: EntryDraft) {
    setStatus('正在提交诊断…')
    await onSubmit(value.question, value.context, value.evidence, value.profile, value.target)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !question.trim() || (mode === 'explicit' && !environment)) return
    const value = buildDraft()
    setDraft(value); setBusy(true); setError(''); setStatus('正在查找相似问题…')
    try {
      const result = await lookupAtEntry({ question: value.question, context: value.context, evidence: value.evidence, target: value.target }, findGuides, () => beginDiagnosis(value))
      if (result.next_action === 'confirm_similarity') {
        setLookup(result)
        setStatus('请选择现象相似的问题；指南不代表本次根因已确认。')
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '查找失败，请重试。'); setStatus('未能完成提交，问题草稿已保留。') }
    finally { setBusy(false) }
  }

  async function rejectMatches() {
    if (busy || !draft) return
    setBusy(true); setError('')
    try { await beginDiagnosis(draft) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '诊断提交失败，请重试。'); setStatus('诊断未能提交，请重试。') }
    finally { setBusy(false) }
  }

  const label = (guide: DiagnosisGuide) => categories.items.find((item) => item.id === guide.category)?.display_name ?? guide.category
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && !busy && onClose()}>
    <div className="new-diagnosis-modal guide-entry-modal" ref={root} role="dialog" aria-modal="true" aria-labelledby="entry-title">
      <div className="modal-heading"><div><span className="eyebrow">{selected ? '问题定位指南' : lookup ? '相似问题' : '提交问题'}</span><h2 id="entry-title" tabIndex={-1}>{selected ? selected.title : lookup ? '有没有与你相似的问题？' : '先看看有没有合适的定位指南'}</h2></div><button type="button" className="close-button" aria-label="关闭" onClick={onClose} disabled={busy}>×</button></div>
      <p className="guide-entry-status" aria-live="polite">{status}</p>
      {error && <p className="guide-error" role="alert">{error}</p>}
      {selected ? <>
        <span className="guide-category">{label(selected)}</span><GuideDetail guide={selected} />
        <div className="guide-actions"><button className="quiet-button" onClick={() => { setSelected(null); setStatus('请选择现象相似的问题；指南不代表本次根因已确认。') }} disabled={busy}>查看其他相似问题</button><button className="quiet-button" onClick={rejectMatches} disabled={busy}>不相似，开始诊断</button><button className="primary-button" onClick={onClose} disabled={busy}>完成</button></div>
      </> : lookup ? <>
        <div className="guide-match-list">{lookup.matches.map((match) => <section className="guide-match-card" key={match.guide.guide_id}><div><span className="guide-category">{label(match.guide)}</span><span className="guide-provenance">{match.guide.origin === 'memory' ? '来自诊断经验' : '手工指南'}</span></div><h3>{match.guide.title}</h3><p>{match.guide.phenomenon}</p><small>环境：{match.guide.environment || '未限定'} · 服务：{match.guide.service || '未限定'} · 版本：{match.guide.version || '需核对'}</small><button type="button" className="primary-button" onClick={() => { setSelected(match.guide); setStatus('已选择相似问题，可直接按指南排查。') }} disabled={busy}>相似，查看指南</button></section>)}</div>
        <div className="guide-actions"><button className="quiet-button" onClick={() => { setLookup(null); setError(''); setStatus('先查找相似问题，有合适指南时可直接查看。') }} disabled={busy}>返回编辑问题</button><button className="primary-button" onClick={rejectMatches} disabled={busy}>{busy ? '正在提交…' : '都不相似，开始诊断'}</button></div>
      </> : <form onSubmit={submit}>
        <fieldset disabled={busy}><label>问题描述<span className="required">必填</span><textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={4} maxLength={12000} placeholder="例如：生产环境订单接口从 10:20 开始大量超时…" required /></label>
          <div className="form-row"><label>目标方式<select value={mode} onChange={(event) => setMode(event.target.value as 'explicit' | 'infer')}><option value="infer">自动推断后确认</option><option value="explicit">明确选择环境</option></select></label>{mode === 'explicit' ? <label>环境<select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option value="" disabled>请选择已配置环境</option>{environments.map((item) => <option key={item.environment_id} value={item.environment_id}>{item.display_name} · {item.environment_id}</option>)}</select></label> : <label>环境/别名提示<input value={environmentHint} onChange={(event) => setEnvironmentHint(event.target.value)} placeholder="production、prod 或线上" /></label>}</div>
          <label>主服务提示<span className="optional">可选</span><input value={service} onChange={(event) => setService(event.target.value)} placeholder="order-api" /></label>
          <label>已有日志或证据<span className="optional">可选</span><textarea value={log} onChange={(event) => setLog(event.target.value)} rows={3} maxLength={12000} placeholder="粘贴一小段与问题直接相关的日志、指标或变更记录" /></label>
          <div className="form-row profile-row"><label>策略 Profile<select value={profile} onChange={(event) => setProfile(event.target.value)}>{profiles.map((name) => <option key={name}>{name}</option>)}</select></label><label className="auto-explore-toggle"><input type="checkbox" checked={autoExplore} onChange={(event) => setAutoExplore(event.target.checked)} /><span><strong>自主探索模式</strong><small>进入诊断后自动跳过澄清</small></span></label></div>
        </fieldset>
        <div className="modal-footer"><span>没有相似问题时自动开始诊断</span><button type="submit" className="primary-button" disabled={busy || !question.trim() || (mode === 'explicit' && !environment)}>{busy ? '正在处理…' : '查找相似问题 →'}</button></div>
      </form>}
    </div>
  </div>
}
