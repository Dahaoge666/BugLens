import { FormEvent, useEffect, useState } from 'react'
import { applyAdminConfig, applyAdminEnvironmentConfig, createRunId, getAdminConfig, getAdminEnvironmentConfig, getAdminHealth, getAdminPlugins, getAdminRuns, getAdminSessions, getAdminVersion, getEnvironments, getRun, readCommandStream, readEvents, validateAdminConfig, validateAdminEnvironmentConfig } from './api'
import type { AdminConfig, AdminHealth, AdminRun, AdminSession, DomainEvent, EnvironmentConfig, EnvironmentList, EnvironmentSummary, Evidence, Hypothesis, InteractionRequest, PendingApproval, PendingTargetConfirmation, PluginList, Run, TargetSpec } from './types'

type WorkspacePage = 'dashboard' | 'tasks' | 'sessions' | 'settings' | 'environments' | 'system' | 'run'

const demoRuns: AdminRun[] = [
  { run_id: 'diag_7f2c9d1a', question: '生产环境 order-api 从 10:20 开始持续超时', lifecycle_status: 'completed', status: 'completed', outcome: 'confirmed', current_node: 'done', profile: 'default', config_version: 'default-v1', config_snapshot_id: 'cfg_7f2c9d1a', attempt: 1, clarification_round: 0, pending_input: false, created_at: '2026-09-07T10:20:00+08:00', updated_at: '2026-09-07T10:26:18+08:00', last_error: null },
  { run_id: 'diag_39ab20ce', question: '支付回调在 staging 偶发 502', lifecycle_status: 'waiting_user', status: 'waiting_user', outcome: null, current_node: 'investigate', profile: 'default', config_version: 'default-v1', config_snapshot_id: 'cfg_39ab20ce', attempt: 1, clarification_round: 1, pending_input: true, created_at: '2026-09-07T09:58:00+08:00', updated_at: '2026-09-07T10:11:43+08:00', last_error: null },
  { run_id: 'diag_18d2e04f', question: '订单服务发布后错误率升高', lifecycle_status: 'running', status: 'running', outcome: null, current_node: 'evaluate', profile: 'strict-prod', config_version: 'strict-v3', config_snapshot_id: 'cfg_18d2e04f', attempt: 2, clarification_round: 0, pending_input: false, created_at: '2026-09-07T09:42:00+08:00', updated_at: '2026-09-07T10:12:02+08:00', last_error: null },
  { run_id: 'diag_6b47e8d0', question: '数据同步任务延迟超过 SLA', lifecycle_status: 'completed', status: 'inconclusive', outcome: 'inconclusive', current_node: 'done', profile: 'default', config_version: 'default-v1', config_snapshot_id: 'cfg_6b47e8d0', attempt: 2, clarification_round: 2, pending_input: false, created_at: '2026-09-06T18:23:00+08:00', updated_at: '2026-09-06T18:35:20+08:00', last_error: null },
]

const demoSessions: AdminSession[] = [
  { session_id: 'diag_7f2c9d1a:analyze', run_id: 'diag_7f2c9d1a', node: 'analyze', status: 'completed', created_at: '10:20:02', updated_at: '10:20:14', message_count: 4, config_snapshot_id: 'cfg_7f2c9d1a' },
  { session_id: 'diag_7f2c9d1a:investigate', run_id: 'diag_7f2c9d1a', node: 'investigate', status: 'completed', created_at: '10:20:15', updated_at: '10:21:07', message_count: 8, config_snapshot_id: 'cfg_7f2c9d1a' },
  { session_id: 'diag_7f2c9d1a:evaluate', run_id: 'diag_7f2c9d1a', node: 'evaluate', status: 'completed', created_at: '10:21:08', updated_at: '10:21:19', message_count: 4, config_snapshot_id: 'cfg_7f2c9d1a' },
  { session_id: 'diag_39ab20ce:investigate', run_id: 'diag_39ab20ce', node: 'investigate', status: 'waiting', created_at: '10:00:10', updated_at: '10:11:43', message_count: 6, config_snapshot_id: 'cfg_39ab20ce' },
]

const demoRun: Run = {
  run_id: 'diag_7f2c9d1a',
  user_question: '生产环境 order-api 从 10:20 开始持续超时，P99 从 350ms 升到 8s。',
  user_context: { environment: 'production', service: 'order-api', time_window: '10:20–10:45 CST' },
  source_evidence: [
    { source: 'log', content: 'timeout waiting for database connection', reference: 'trace_id=tr_1001' },
    { source: 'metric', content: 'db_pool_active=100, db_pool_max=100, http_p99=8s at 10:31' },
    { source: 'change', content: 'order-api 2.4.1 deployed at 10:15; database pool max unchanged' },
  ],
  analysis: {
    category: 'performance', category_confidence: 0.96,
    summary: '订单接口在生产环境出现持续性延迟，症状与数据库连接池耗尽高度相关。',
    symptoms: ['POST /orders P99 从 350ms 升至 8s', '出现连接池等待超时', '问题集中发生在 10:20–10:45'],
    impact: '订单创建请求超时，可能造成用户重复提交。', time_window: '10:20–10:45 CST', environment: 'production', missing_information: [],
  },
  investigation: {
    investigation_summary: '已形成 2 个可验证假设，主假设得到日志、指标与变更记录的交叉支持。',
    hypotheses: [
      { rank: 1, cause: '数据库连接池耗尽导致请求排队', rationale: '连接池活跃数达到上限，同时出现等待连接超时；部署记录显示连接池配置未随流量变化。', supporting_evidence: ['log · timeout waiting for database connection', 'metric · active=100 / max=100'], contradicting_evidence: [], confidence: 0.88, verification_steps: ['检查 10:20–10:45 数据库连接获取耗时分布', '对比发布前后连接泄漏与慢查询指标'], remediation_direction: '先限制并发创建请求，再检查连接释放路径。' },
      { rank: 2, cause: '2.4.1 版本引入慢查询或连接未释放', rationale: '版本在异常开始前 5 分钟发布，时间上存在关联，但还缺少 SQL 与连接生命周期证据。', supporting_evidence: ['change · 2.4.1 deployed at 10:15'], contradicting_evidence: ['当前没有慢查询样本'], confidence: 0.57, verification_steps: ['回放 2.4.0 与 2.4.1 的关键订单路径', '检查连接池 checkout/checkin 计数是否平衡'], remediation_direction: null },
    ], evidence_gaps: ['缺少数据库慢查询样本', '缺少连接 checkout/checkin 指标'], next_data_to_collect: ['10:20–10:45 的 DB wait histogram', '2.4.1 版本连接池生命周期指标'], limitations: [],
  },
  evaluation: { passed: true, score: 84, criteria_scores: { problem_coverage: 18, evidence_traceability: 22, reasoning_consistency: 17, verification_executability: 16, uncertainty_expression: 11 }, strengths: ['证据引用可追溯', '验证步骤可以执行'], deficiencies: ['缺少数据库侧慢查询样本'], retry_guidance: [] },
  report: { executive_summary: '生产环境订单接口的主要风险是数据库连接池耗尽，已获得多类证据支持。建议先验证连接获取等待与连接释放计数，再决定是否回滚 2.4.1。', primary_conclusion: '数据库连接池耗尽导致请求排队（置信度 88%）', status_explanation: '评测通过，但仍应补充数据库侧证据后再执行高风险变更。', next_actions: ['查看连接获取等待分布', '核对 checkout/checkin 是否平衡', '必要时回滚 2.4.1'] },
  lifecycle_status: 'completed', outcome: 'confirmed', current_node: 'done', revision: 7, attempt: 1, clarification_round: 0, created_at: '2026-09-07T10:20:00+08:00', updated_at: '2026-09-07T10:26:18+08:00',
  target: { mode: 'explicit', environment_id: 'production' }, environment_snapshot_id: 'envsnap_demo', pending_target_confirmation: null,
}

const demoEvents: DomainEvent[] = [
  { event_id: 'e1', event_type: 'run_started', sequence: 1, revision: 0, occurred_at: '10:20:02', summary: '已创建诊断运行' },
  { event_id: 'e2', event_type: 'node_completed', sequence: 3, revision: 1, occurred_at: '10:20:14', node: 'analyze', next_node: 'investigate', summary: '完成问题理解' },
  { event_id: 'e3', event_type: 'node_completed', sequence: 5, revision: 2, occurred_at: '10:21:07', node: 'investigate', next_node: 'evaluate', summary: '形成 2 个原因假设' },
  { event_id: 'e4', event_type: 'node_completed', sequence: 7, revision: 3, occurred_at: '10:21:19', node: 'evaluate', next_node: 'summarize', summary: '评测通过 · 84/100' },
  { event_id: 'e5', event_type: 'run_completed', sequence: 9, revision: 7, occurred_at: '10:26:18', node: 'summarize', summary: '报告已生成', outcome: 'confirmed' },
]

const stageLabels = [
  ['analyze', '理解问题'], ['investigate', '定位原因'], ['evaluate', '检查结论'], ['summarize', '生成报告'],
] as const

function appendEvent(current: DomainEvent[], event: DomainEvent): DomainEvent[] {
  if (current.some((item) => item.event_id === event.event_id || item.sequence === event.sequence)) return current
  return [...current, event].sort((left, right) => left.sequence - right.sequence)
}

function runStatusLabel(run: { lifecycle_status: string; outcome?: string | null }): string {
  if (run.lifecycle_status === 'created') return '已创建'
  if (run.lifecycle_status === 'running') return '运行中'
  if (run.lifecycle_status === 'waiting_user') return '待补充'
  if (run.lifecycle_status === 'waiting_tool') return '等待数据'
  if (run.lifecycle_status === 'waiting_approval') return '待审批'
  if (run.lifecycle_status === 'waiting_for_target_confirmation') return '待确认环境'
  if (run.lifecycle_status === 'failed') return '执行失败'
  if (run.lifecycle_status === 'canceled') return '已取消'
  return run.outcome === 'inconclusive' ? '未决' : '已完成'
}

function sessionStatusLabel(status: string): string {
  return ({ active: '活动中', waiting: '等待输入', completed: '已完成', unknown: '未知' } as Record<string, string>)[status] ?? status
}

function App() {
  const [run, setRun] = useState<Run>(demoRun)
  const [events, setEvents] = useState<DomainEvent[]>(demoEvents)
  const [view, setView] = useState<'overview' | 'evidence' | 'events'>('overview')
  const [showNew, setShowNew] = useState(false)
  const [notice, setNotice] = useState('演示数据 · 可连接本地 BugLens API')
  const [adminRuns, setAdminRuns] = useState<AdminRun[]>(demoRuns)
  const [adminSessions, setAdminSessions] = useState<AdminSession[]>(demoSessions)
  const [adminConfig, setAdminConfig] = useState<AdminConfig | null>(null)
  const [environments, setEnvironments] = useState<EnvironmentList>({ revision: '', items: [] })
  const [environmentConfig, setEnvironmentConfig] = useState<EnvironmentConfig | null>(null)
  const [plugins, setPlugins] = useState<PluginList>({ items: [] })
  const [adminHealth, setAdminHealth] = useState<AdminHealth | null>(null)
  const [adminVersion, setAdminVersion] = useState('0.1.0')
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [page, setPage] = useState<WorkspacePage>(() => {
    const value = window.location.hash.replace(/^#\/?/, '') as WorkspacePage
    return ['dashboard', 'tasks', 'sessions', 'settings', 'environments', 'system', 'run'].includes(value)
      ? value
      : 'dashboard'
  })

  useEffect(() => {
    const onPopState = () => {
      const value = window.location.hash.replace(/^#\/?/, '') as WorkspacePage
      if (['dashboard', 'tasks', 'sessions', 'settings', 'environments', 'system', 'run'].includes(value)) setPage(value)
    }
    window.addEventListener('popstate', onPopState)
    window.addEventListener('hashchange', onPopState)
    return () => {
      window.removeEventListener('popstate', onPopState)
      window.removeEventListener('hashchange', onPopState)
    }
  }, [])

  useEffect(() => {
    getAdminHealth().then(setAdminHealth).catch(() => setAdminHealth(null))
    getAdminVersion().then((result) => setAdminVersion(result.version)).catch(() => undefined)
  }, [])

  useEffect(() => {
    let active = true
    const safe = <T,>(p: Promise<T>, fallback: T): Promise<T> => p.catch(() => fallback)
    const sync = () => Promise.all([
      safe(getAdminConfig(), null as AdminConfig | null),
      safe(getEnvironments(), { revision: '', items: [] } as EnvironmentList),
      safe(getAdminEnvironmentConfig(), null as EnvironmentConfig | null),
      safe(getAdminPlugins(), { items: [] } as PluginList),
      safe(getAdminRuns(), { items: [] as AdminRun[], total: 0 }),
      safe(getAdminSessions(), { items: [] as AdminSession[], total: 0 }),
    ]).then(([config, environmentList, nextEnvironmentConfig, pluginList, runs, sessions]) => {
      if (!active) return
      setAdminRuns(runs.items)
      setAdminSessions(sessions.items)
      setEnvironments(environmentList)
      setEnvironmentConfig(nextEnvironmentConfig)
      setPlugins(pluginList)
      if (config) setAdminConfig(config)
      setFetchError(config ? null : 'config 请求失败')
      setNotice(config ? '已连接 BugLens API · 数据实时同步' : '演示数据 · config 请求失败')
    })
    sync()
    const timer = window.setInterval(() => {
      Promise.all([getAdminRuns(), getAdminSessions(), getEnvironments()]).then(([runs, sessions, environmentList]) => {
        if (!active) return
        setAdminRuns(runs.items)
        setAdminSessions(sessions.items)
        setEnvironments(environmentList)
      }).catch(() => undefined)
    }, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  function navigate(next: WorkspacePage) {
    setPage(next)
    window.location.hash = next
  }

  const stageIndex = Math.max(0, stageLabels.findIndex(([node]) => node === run.current_node))
  const statusText = runStatusLabel(run)
  const latestEvent = events.at(-1)

  async function startDiagnosis(question: string, context: Record<string, string>, evidence: Evidence[], profile: string, target: TargetSpec) {
    const runId = createRunId()
    const command = { protocol_version: '2', command_id: crypto.randomUUID(), run_id: runId, expected_revision: null, submitted_at: new Date().toISOString(), command_type: 'start_diagnosis', question, context, evidence, profile, target }
    setShowNew(false)
    navigate('run')
    setEvents([])
    setNotice('正在提交诊断…')
    try {
      await readCommandStream('/v1/runs', command, (event) => setEvents((current) => appendEvent(current, event)))
      const nextRun = await getRun(runId)
      setRun(nextRun)
      getAdminRuns().then((result) => setAdminRuns(result.items)).catch(() => undefined)
      setNotice('已连接到 BugLens API，正在同步运行状态')
    } catch {
      setNotice('当前使用演示模式：已保留界面，API 尚未连接')
      setRun({ ...demoRun, run_id: runId, user_question: question, user_context: context, source_evidence: evidence, lifecycle_status: 'running', outcome: null, current_node: 'analyze', report: null, evaluation: null, investigation: null, analysis: null, revision: 0, available_actions: ['cancel'], target })
      setEvents([{ event_id: 'local-1', event_type: 'run_started', sequence: 1, revision: 0, occurred_at: new Date().toLocaleTimeString(), summary: '已在本地创建演示运行' }])
    }
  }

  async function openRun(runId: string) {
    navigate('run')
    setNotice('正在加载诊断运行…')
    try {
      setRun(await getRun(runId))
      setEvents([])
      await readEvents(runId, 0, (event) => setEvents((current) => [...current, event]))
      setNotice('已连接 BugLens API · 运行状态已同步')
    } catch {
      const item = adminRuns.find((candidate) => candidate.run_id === runId)
      setRun({
        ...demoRun,
        run_id: runId,
        user_question: item?.question ?? demoRun.user_question,
        lifecycle_status: (item?.lifecycle_status as Run['lifecycle_status']) ?? 'running',
        outcome: (item?.outcome as Run['outcome']) ?? null,
        current_node: (item?.current_node as Run['current_node']) ?? 'analyze',
        pending_interaction: item?.pending_input ? demoRun.pending_interaction : null,
        updated_at: item?.updated_at ?? demoRun.updated_at,
      })
      setEvents(demoEvents)
      setNotice('演示数据 · API 尚未连接')
    }
  }

  async function cancelDiagnosis() {
    const canCancel = run.available_actions?.includes('cancel') ?? (run.lifecycle_status === 'running' || run.lifecycle_status === 'waiting_user')
    if (!canCancel || run.cancel_requested_at) return
    const command = {
      protocol_version: '2', command_id: crypto.randomUUID(), run_id: run.run_id,
      expected_revision: run.revision, submitted_at: new Date().toISOString(),
      command_type: 'cancel_diagnosis', reason: '用户从前端取消运行',
    }
    setNotice('正在取消诊断…')
    try {
      await readCommandStream(`/v1/runs/${encodeURIComponent(run.run_id)}/commands`, command, (event) => setEvents((current) => appendEvent(current, event)))
      setRun(await getRun(run.run_id))
      setNotice('诊断已取消')
    } catch {
      setNotice('演示模式：取消操作未连接到后端')
      setRun((current) => ({ ...current, lifecycle_status: 'canceled', current_node: 'done', pending_interaction: null, available_actions: [], updated_at: new Date().toISOString() }))
    }
  }

  async function confirmTarget(environmentId: string, primaryServiceId?: string | null) {
    const pending = run.pending_target_confirmation
    if (!pending || !run.available_actions?.includes('confirm_target')) return
    const command = {
      protocol_version: '2', command_id: crypto.randomUUID(), run_id: run.run_id,
      expected_revision: run.revision, submitted_at: new Date().toISOString(),
      command_type: 'confirm_diagnosis_target', request_id: pending.request_id,
      environment_id: environmentId, ...(primaryServiceId ? { primary_service_id: primaryServiceId } : {}),
    }
    setNotice('正在确认诊断环境…')
    try {
      await readCommandStream(`/v1/runs/${encodeURIComponent(run.run_id)}/commands`, command, (event) => setEvents((current) => appendEvent(current, event)))
      setRun(await getRun(run.run_id))
      setNotice('环境已确认，诊断继续推进')
    } catch {
      setNotice('演示模式：环境确认未连接到后端')
    }
  }

  async function submitClarification(answers: Array<{ request_id: string; question_id: string; answer: string }>) {
    const command = {
      protocol_version: '2', command_id: crypto.randomUUID(), run_id: run.run_id,
      expected_revision: run.revision, submitted_at: new Date().toISOString(),
      command_type: 'submit_user_answers', request_id: answers[0]?.request_id ?? '', answers,
    }
    setNotice('正在提交补充信息…')
    try {
      await readCommandStream(`/v1/runs/${encodeURIComponent(run.run_id)}/commands`, command, (event) => setEvents((current) => appendEvent(current, event)))
      setRun(await getRun(run.run_id))
      setNotice('补充信息已提交，诊断继续推进')
    } catch {
      setNotice('演示模式：补充信息已记录在当前界面')
    }
  }

  async function skipClarification() {
    const request = run.pending_interaction
    if (!request || !run.available_actions?.includes('skip_input')) return
    const command = {
      protocol_version: '2', command_id: crypto.randomUUID(), run_id: run.run_id,
      expected_revision: run.revision, submitted_at: new Date().toISOString(),
      command_type: 'skip_user_interaction', request_id: request.request_id, reason: '用户暂时无法提供该信息',
    }
    setNotice('正在跳过本轮补充…')
    try {
      await readCommandStream(`/v1/runs/${encodeURIComponent(run.run_id)}/commands`, command, (event) => setEvents((current) => appendEvent(current, event)))
      setRun(await getRun(run.run_id))
      setNotice('已跳过本轮补充，诊断继续推进')
    } catch {
      setNotice('演示模式：跳过操作未连接到后端')
    }
  }

  async function resumeDiagnosis() {
    if (!run.available_actions?.includes('resume')) return
    const command = {
      protocol_version: '2', command_id: crypto.randomUUID(), run_id: run.run_id,
      expected_revision: run.revision, submitted_at: new Date().toISOString(), command_type: 'resume_diagnosis',
    }
    setNotice('正在恢复诊断…')
    try {
      await readCommandStream(`/v1/runs/${encodeURIComponent(run.run_id)}/commands`, command, (event) => setEvents((current) => appendEvent(current, event)))
      setRun(await getRun(run.run_id))
      setNotice('诊断已恢复')
    } catch {
      setNotice('演示模式：恢复操作未连接到后端')
    }
  }

  async function resolveToolApproval(decision: 'approve' | 'reject') {
    const request = run.pending_approval
    if (!request || !run.available_actions?.includes(decision)) return
    const command = {
      protocol_version: '2', command_id: crypto.randomUUID(), run_id: run.run_id,
      expected_revision: run.revision, submitted_at: new Date().toISOString(),
      command_type: decision === 'approve' ? 'approve_tool' : 'reject_tool',
      request_id: request.request_id,
      ...(decision === 'reject' ? { reason: '用户拒绝执行该只读工具调用' } : {}),
    }
    setNotice(decision === 'approve' ? '正在批准工具调用…' : '正在拒绝工具调用…')
    try {
      await readCommandStream(`/v1/runs/${encodeURIComponent(run.run_id)}/commands`, command, (event) => setEvents((current) => appendEvent(current, event)))
      setRun(await getRun(run.run_id))
      setNotice(decision === 'approve' ? '工具调用已批准，诊断继续推进' : '工具调用已拒绝，诊断继续推进')
    } catch {
      setNotice('演示模式：审批操作未连接到后端')
    }
  }

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">⌁</span><span>BUGLENS</span><span className="brand-divider" /><span className="brand-product">诊断工作台</span></div>
      <div className="top-actions"><span className="connection"><i />{notice}</span><button className="quiet-button" onClick={() => setShowNew(true)}>＋ 新建诊断</button><span className="avatar">DL</span></div>
    </header>
    <div className="app-body">
     <Sidebar page={page} health={adminHealth} version={adminVersion} runCount={adminRuns.length} navigate={navigate} />
    <main className={`workspace ${page === 'run' ? '' : 'management-workspace'}`}>
      {page !== 'run' ? <ManagementPage page={page} runs={adminRuns} sessions={adminSessions} config={adminConfig} environments={environments} environmentConfig={environmentConfig} plugins={plugins} health={adminHealth} version={adminVersion} onConfigChange={setAdminConfig} onEnvironmentConfigChange={setEnvironmentConfig} fetchError={fetchError} onNew={() => setShowNew(true)} onOpenRun={openRun} /> : <>
      <section className="run-heading">
        <div><div className="eyebrow">诊断运行 <span className="mono">/ {run.run_id}</span></div><h1>{run.user_question}</h1></div>
        <div className="run-meta"><StatusPill status={run.lifecycle_status} label={statusText} /><span>更新于 {run.updated_at.includes('T') ? run.updated_at.slice(11, 16) : run.updated_at}</span>{run.available_actions?.includes('resume') && <button className="quiet-button" onClick={resumeDiagnosis}>恢复诊断</button>}{run.available_actions?.includes('approve') && <button className="primary-button" onClick={() => resolveToolApproval('approve')}>批准工具</button>}{run.available_actions?.includes('reject') && <button className="quiet-button" onClick={() => resolveToolApproval('reject')}>拒绝工具</button>}{(run.available_actions?.includes('cancel') ?? (run.lifecycle_status === 'running' || run.lifecycle_status === 'waiting_user' || run.lifecycle_status === 'waiting_approval')) && <button className="danger-button" onClick={cancelDiagnosis} disabled={Boolean(run.cancel_requested_at)}>{run.cancel_requested_at ? '取消中…' : '取消运行'}</button>}</div>
      </section>
      <section className="stage-track" aria-label="诊断阶段">
        {stageLabels.map(([node, label], index) => <div className={`stage ${index < stageIndex || run.lifecycle_status === 'completed' ? 'done' : ''} ${node === run.current_node ? 'active' : ''}`} key={node}><span className="stage-number">{index < stageIndex || run.lifecycle_status === 'completed' ? '✓' : `0${index + 1}`}</span><span>{label}</span>{index < stageLabels.length - 1 && <span className="stage-line" />}</div>)}
      </section>
      <nav className="view-tabs" aria-label="诊断视图">{[['overview', '概览'], ['evidence', '证据与假设'], ['events', '事件记录']].map(([key, label]) => <button className={view === key ? 'selected' : ''} onClick={() => setView(key as typeof view)} key={key}>{label}</button>)}</nav>
      {view === 'events' ? <EventsPanel events={events} /> : view === 'evidence' ? <EvidenceView run={run} /> : <Overview run={run} latestEvent={latestEvent} onNew={() => setShowNew(true)} onClarification={submitClarification} onSkip={skipClarification} onApproval={resolveToolApproval} onConfirmTarget={confirmTarget} />}
      </>}
    </main>
    </div>
    {showNew && <NewDiagnosis profiles={Object.keys(adminConfig?.profiles ?? { default: {} })} environments={environments.items} onClose={() => setShowNew(false)} onSubmit={startDiagnosis} />}
  </div>
}

function Sidebar({ page, health, version, runCount, navigate }: { page: WorkspacePage; health: AdminHealth | null; version: string; runCount: number; navigate: (page: WorkspacePage) => void }) {
  const items: Array<[WorkspacePage, string, string]> = [
    ['dashboard', '⌂', '总览'],
    ['tasks', '▤', '诊断任务'],
    ['sessions', '◌', 'Agent Sessions'],
  ]
  return <aside className="sidebar">
    <div className="sidebar-label">工作区</div>
    <nav className="sidebar-nav" aria-label="工作区导航">
      {items.map(([target, icon, label]) => <button key={target} className={page === target ? 'active' : ''} onClick={() => navigate(target)}><span className="nav-icon">{icon}</span><span>{label}</span>{target === 'tasks' && runCount > 0 && <b>{runCount}</b>}</button>)}
    </nav>
    <div className="sidebar-label sidebar-label-spaced">管理</div>
    <nav className="sidebar-nav" aria-label="管理导航">
      <button className={page === 'settings' ? 'active' : ''} onClick={() => navigate('settings')}><span className="nav-icon">⚙</span><span>配置与初始化</span></button>
      <button className={page === 'environments' ? 'active' : ''} onClick={() => navigate('environments')}><span className="nav-icon">⌘</span><span>环境与插件</span></button>
      <button className={page === 'system' ? 'active' : ''} onClick={() => navigate('system')}><span className="nav-icon">◈</span><span>系统与更新</span></button>
    </nav>
    <div className="sidebar-bottom"><div className="backend-status"><i className={health?.status === 'error' ? 'error' : ''} /> <div><strong>{health?.status === 'error' ? '后端异常' : health?.status === 'degraded' ? '后端需关注' : '后端在线'}</strong><small>localhost:8000 · v{version}</small></div></div><button className="sidebar-help" onClick={() => navigate('system')}>？ 帮助与诊断</button></div>
  </aside>
}

function ManagementPage({ page, runs, sessions, config, environments, environmentConfig, plugins, health, version, onConfigChange, onEnvironmentConfigChange, fetchError, onNew, onOpenRun }: { page: Exclude<WorkspacePage, 'run'>; runs: AdminRun[]; sessions: AdminSession[]; config: AdminConfig | null; environments: EnvironmentList; environmentConfig: EnvironmentConfig | null; plugins: PluginList; health: AdminHealth | null; version: string; onConfigChange: (config: AdminConfig) => void; onEnvironmentConfigChange: (config: EnvironmentConfig | null) => void; fetchError: string | null; onNew: () => void; onOpenRun: (runId: string) => void }) {
  if (page === 'dashboard') return <DashboardPage runs={runs} sessions={sessions} health={health} onNew={onNew} onOpenRun={onOpenRun} />
  if (page === 'tasks') return <TasksPage runs={runs} onNew={onNew} onOpenRun={onOpenRun} />
  if (page === 'sessions') return <SessionsPage sessions={sessions} onOpenRun={onOpenRun} />
  if (page === 'settings') return <SettingsPage config={config} onConfigChange={onConfigChange} fetchError={fetchError} />
  if (page === 'environments') return <EnvironmentPage environments={environments} config={environmentConfig} plugins={plugins} onConfigChange={onEnvironmentConfigChange} />
  return <SystemPage health={health} version={version} />
}

function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}

function DashboardPage({ runs, sessions, health, onNew, onOpenRun }: { runs: AdminRun[]; sessions: AdminSession[]; health: AdminHealth | null; onNew: () => void; onOpenRun: (runId: string) => void }) {
  const waiting = runs.filter((run) => run.lifecycle_status === 'waiting_user' || run.lifecycle_status === 'waiting_approval' || run.lifecycle_status === 'waiting_for_target_confirmation').length
  const running = runs.filter((run) => run.lifecycle_status === 'running').length
  const completed = runs.filter((run) => run.lifecycle_status === 'completed').length
  const healthOk = health?.status === 'ok' || health === null
  const healthLabel = health ? (health.status === 'ok' ? '运行正常' : health.status === 'degraded' ? '需要关注' : '后端异常') : '演示状态'
  return <div className="management-content">
    <PageHeading eyebrow="控制台 / 总览" title="早上好，Dahaoge" description="这里是 BugLens 的运行概况与最近活动。" action={<button className="primary-button" onClick={onNew}>＋ 新建诊断</button>} />
    <div className="metric-grid"><MetricCard label="运行中" value={String(running)} detail="当前正在推进" tone="teal" /><MetricCard label="等待补充" value={String(waiting)} detail="需要用户输入" tone="amber" /><MetricCard label="已完成" value={String(completed + 14)} detail="过去 30 天" tone="blue" /><MetricCard label="平均评测分" value="84" detail="↑ 6% 对比上月" tone="violet" /></div>
    <div className="dashboard-grid"><section className={`panel health-panel ${healthOk ? '' : 'health-degraded'}`}><PanelHeader title="后端健康" meta={health ? '刚刚检查' : '演示'} /><div className="health-summary"><span className="health-ring">{healthOk ? '✓' : '!'}</span><div><strong>{healthLabel}</strong><p>{health ? '核心组件状态来自 Admin API' : '连接后显示真实健康状态'}</p></div><span className="health-latency">{health ? 'API' : '—'}</span></div>{[['checkpoint_store', '检查点存储'], ['configuration', '配置 profile'], ['model_credentials', '模型凭据']].map(([key, label]) => { const component = health?.components.find((item) => item.name === key); const status = component?.status ?? 'ok'; return <div className="health-row" key={key}><span className={`health-dot ${status}`} /><span>{label}</span><span>{status === 'ok' ? '正常' : status === 'degraded' ? '需关注' : '异常'}</span></div> })}<button className="text-button" onClick={() => window.location.hash = 'system'}>查看系统详情 →</button></section><section className="panel activity-chart"><PanelHeader title="诊断活动" meta="最近 7 天" /><div className="chart-placeholder"><div className="chart-bars">{[38, 52, 45, 72, 58, 84, 67].map((height, index) => <span key={index} style={{ height: `${height}%` }}><i /></span>)}</div><div className="chart-labels"><span>周一</span><span>周二</span><span>周三</span><span>周四</span><span>周五</span><span>周六</span><span>今天</span></div></div><div className="chart-legend"><span><i className="legend-teal" />完成 18</span><span><i className="legend-amber" />未决 4</span><span className="chart-total">22 次运行</span></div></section></div>
    <section className="panel recent-panel"><PanelHeader title="最近诊断" meta="查看全部 →" /><RunTable runs={runs.slice(0, 4)} onOpenRun={onOpenRun} /></section>
    <section className="panel quick-panel"><div><span className="section-kicker">快速开始</span><h2>从一个现象开始定位</h2><p>提交问题、环境和一小段证据，BugLens 会自动推进四阶段诊断。</p></div><button className="quiet-button" onClick={onNew}>创建任务 →</button></section>
  </div>
}

function MetricCard({ label, value, detail, tone }: { label: string; value: string; detail: string; tone: string }) {
  return <div className={`metric-card ${tone}`}><div className="metric-label">{label}<span className="metric-spark">↗</span></div><strong>{value}</strong><small>{detail}</small></div>
}

function TasksPage({ runs, onNew, onOpenRun }: { runs: AdminRun[]; onNew: () => void; onOpenRun: (runId: string) => void }) {
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const visible = runs.filter((run) => (filter === 'all' || run.lifecycle_status === filter || run.status === filter) && (!query.trim() || `${run.question} ${run.run_id}`.toLowerCase().includes(query.trim().toLowerCase())))
  return <div className="management-content"><PageHeading eyebrow="控制台 / 诊断任务" title="诊断任务" description="统一查看运行状态、评测结果和待处理输入。" action={<button className="primary-button" onClick={onNew}>＋ 新建诊断</button>} /><div className="toolbar"><div className="filter-tabs">{[['all', '全部'], ['running', '运行中'], ['waiting_user', '待补充'], ['waiting_for_target_confirmation', '待确认环境'], ['waiting_approval', '待审批'], ['completed', '已完成'], ['failed', '失败'], ['canceled', '已取消']].map(([value, label]) => <button className={filter === value ? 'active' : ''} onClick={() => setFilter(value)} key={value}>{label}</button>)}</div><div className="toolbar-actions"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="⌕ 搜索任务" /><button className="quiet-button" onClick={() => setQuery('')}>清除</button></div></div><section className="panel tasks-panel"><RunTable runs={visible} onOpenRun={onOpenRun} /></section></div>
}

function RunTable({ runs, onOpenRun }: { runs: AdminRun[]; onOpenRun: (runId: string) => void }) {
  return <div className="run-table"><div className="run-table-header"><span>任务</span><span>状态</span><span>阶段</span><span>Profile</span><span>更新时间</span><span /></div>{runs.map((run) => <button className="run-table-row" key={run.run_id} onClick={() => onOpenRun(run.run_id)}><span className="task-cell"><strong>{run.question}</strong><small className="mono">{run.run_id}</small></span><span><AdminStatusPill run={run} /></span><span className="node-cell"><span className="mini-node">{run.current_node === 'done' ? '✓' : '◌'}</span>{nodeLabel(run.current_node)}</span><span className="mono muted">{run.profile}</span><span className="muted">{formatTime(run.updated_at)}</span><span className="row-arrow">→</span></button>)}</div>
}

function SessionsPage({ sessions, onOpenRun }: { sessions: AdminSession[]; onOpenRun: (runId: string) => void }) {
  const active = sessions.filter((session) => session.status === 'active').length
  const waiting = sessions.filter((session) => session.status === 'waiting').length
  return <div className="management-content"><PageHeading eyebrow="控制台 / Agent Sessions" title="Session 管理" description="每个 run_id + node 使用独立 SDK SQLiteSession；这里只展示生命周期元数据。" /><div className="session-summary"><MetricCard label="活跃 Session" value={String(active)} detail="正在执行" tone="teal" /><MetricCard label="等待输入" value={String(waiting)} detail="可恢复" tone="amber" /><MetricCard label="历史 Session" value={String(sessions.length)} detail="仅保留元数据" tone="blue" /></div><section className="panel sessions-panel"><PanelHeader title="Session 列表" meta={`${sessions.length} 个可见记录`} /><div className="session-table"><div className="session-header"><span>Session</span><span>节点</span><span>状态</span><span>消息数</span><span>更新时间</span><span /></div>{sessions.map((session) => <div className="session-row" key={session.session_id}><span><strong className="mono">{session.session_id}</strong><small>run {session.run_id}</small></span><span className="node-badge">{nodeLabel(session.node ?? 'unknown')}</span><span><span className={`session-status ${session.status}`}>{sessionStatusLabel(session.status)}</span></span><span className="mono">{session.message_count}</span><span className="muted">{session.updated_at}</span><button className="row-link" onClick={() => onOpenRun(session.run_id)}>查看运行 →</button></div>)}</div><div className="session-note">模型消息由 Agents SDK SQLiteSession 管理，业务状态和 checkpoint 不会复制消息内容。</div></section></div>
}

function EnvironmentPage({ environments, config, plugins, onConfigChange }: { environments: EnvironmentList; config: EnvironmentConfig | null; plugins: PluginList; onConfigChange: (config: EnvironmentConfig | null) => void }) {
  const [jsonText, setJsonText] = useState('')
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({})
  const [message, setMessage] = useState('')
  useEffect(() => {
    if (config) setJsonText(JSON.stringify(config.config, null, 2))
  }, [config])
  const payload = (() => {
    try {
      const value: unknown = JSON.parse(jsonText)
      return isRecord(value) ? value : null
    } catch {
      return null
    }
  })()
  const rawInstances = payload && Array.isArray(payload.plugin_instances) ? payload.plugin_instances : []
  const instances = rawInstances.filter(isRecord)
  async function validate() {
    if (!config || !payload) { setMessage('请输入合法的 JSON 配置，或等待后端连接'); return }
    try {
      const result = await validateAdminEnvironmentConfig({ config: payload, expected_revision: config.revision, secret_updates: buildSecretUpdates() })
      setMessage(result.valid ? '环境目录校验通过，尚未落盘' : result.errors.join('；'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '校验失败，请重试')
    }
  }
  function buildSecretUpdates() {
    const updates: Record<string, Record<string, { action: 'set'; value: string }>> = {}
    for (const [key, value] of Object.entries(secretDrafts)) {
      if (!value.trim()) continue
      const separator = key.indexOf(':')
      const instanceId = key.slice(0, separator)
      const field = key.slice(separator + 1)
      if (!instanceId || !['username', 'password', 'token'].includes(field)) continue
      updates[instanceId] = { ...(updates[instanceId] ?? {}), [field]: { action: 'set', value } }
    }
    return updates
  }
  async function save() {
    if (!config || !payload) { setMessage('请输入合法的 JSON 配置，或等待后端连接'); return }
    try {
      const next = await applyAdminEnvironmentConfig({ config: payload, expected_revision: config.revision, secret_updates: buildSecretUpdates() })
      onConfigChange(next)
      setSecretDrafts({})
      setMessage('环境目录已原子保存；已有运行继续使用原快照')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败，请重试')
    }
  }
  return <div className="management-content">
    <PageHeading eyebrow="管理 / 环境与插件" title="环境目录与工具插件" description="配置先校验再原子替换；运行确认后固定环境快照，凭据只以写入或清除操作提交。" />
    <div className="dashboard-grid">
      <section className="panel"><PanelHeader title="可用环境" meta={`${environments.items.length} 个`} />{environments.items.length === 0 ? <div className="empty-state">尚未配置环境目录</div> : <div className="environment-list">{environments.items.map((item) => <div className="environment-row" key={item.environment_id}><span className="environment-mark">◈</span><div><strong>{item.display_name}</strong><small className="mono">{item.environment_id} · {item.level}{item.region ? ` · ${item.region}` : ''}</small></div><span className="muted">{item.aliases.join(' / ') || '无别名'}</span></div>)}</div>}</section>
      <section className="panel"><PanelHeader title="已发现插件" meta={`${plugins.items.length} 个`} />{plugins.items.length === 0 ? <div className="empty-state">没有已安装的 tool plugin；外部工具默认关闭</div> : <div className="plugin-list">{plugins.items.map((plugin) => <div className="plugin-row" key={plugin.plugin_id}><div><strong className="mono">{plugin.plugin_id}</strong><small>{plugin.capabilities.join(' · ')} · API {plugin.api_major}</small></div><span className="admin-status-pill"><i />{plugin.instance_ids.length} 个实例</span></div>)}</div>}</section>
    </div>
    <section className="panel environment-editor"><PanelHeader title="环境目录 JSON（非敏感字段）" meta={config ? `revision ${config.revision}` : '未连接'} /><p className="config-callout">密码、token、用户名不会从后端返回；保留字段中的 is_set 标记即可。需要轮换凭据时，在下方临时输入，提交后不会回显。</p><textarea value={jsonText} onChange={(event) => setJsonText(event.target.value)} rows={18} spellCheck={false} disabled={!config?.writable} /><div className="secret-editor">{instances.map((instance) => { const id = String(instance.id ?? ''); const pluginId = String(instance.plugin_id ?? 'unknown'); return <div className="secret-row" key={id}><strong>{id || '未命名实例'}</strong><small>{pluginId} · 仅本次提交使用</small><input type="password" value={secretDrafts[`${id}:password`] ?? ''} onChange={(event) => setSecretDrafts((current) => ({ ...current, [`${id}:password`]: event.target.value }))} placeholder="更新 password（可选）" autoComplete="new-password" /></div> })}</div><div className="settings-actions"><button className="quiet-button" onClick={validate} disabled={!config?.writable}>验证目录</button><button className="primary-button" onClick={save} disabled={!config?.writable}>保存环境配置</button></div>{message && <div className="form-message">{message}</div>}</section>
  </div>
}

type ModelEntry = { model: string; base_url: string; api_key: string; timeout: number; streaming: boolean | null }
function SettingsPage({ config, onConfigChange, fetchError }: { config: AdminConfig | null; onConfigChange: (config: AdminConfig) => void; fetchError: string | null }) {
  const [profile, setProfile] = useState(config?.active_profile ?? 'default')
  const [configVersion, setConfigVersion] = useState(String(config?.profiles[config?.active_profile ?? 'default']?.config_version ?? 'default-v1'))
  const [maxAttempts, setMaxAttempts] = useState(2)
  const [maxClarifications, setMaxClarifications] = useState(2)
  const [passingScore, setPassingScore] = useState(75)
  const nodeKeys = ['analyze', 'investigate', 'evaluate', 'summarize'] as const
  const [nodeModels, setNodeModels] = useState<Record<string, string>>(Object.fromEntries(nodeKeys.map((k) => [k, 'default'])))
  const [nodeTurns, setNodeTurns] = useState<Record<string, number>>(Object.fromEntries(nodeKeys.map((k) => [k, 6])))
  const [models, setModels] = useState<Record<string, ModelEntry>>({})
  const [saved, setSaved] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => {
    if (!config) return
    const raw = config.profiles[profile]
    if (!raw) {
      setConfigVersion(`${profile || 'new-profile'}-v1`)
      setMaxAttempts(2); setMaxClarifications(2); setPassingScore(75)
      setNodeModels(Object.fromEntries(nodeKeys.map((k) => [k, 'default'])))
      setNodeTurns(Object.fromEntries(nodeKeys.map((k) => [k, 6])))
      setModels({})
      return
    }
    setConfigVersion(String(raw.config_version ?? `${profile}-v1`))
    const graph = isRecord(raw.graph) ? raw.graph : {}
    const evaluation = isRecord(raw.evaluation) ? raw.evaluation : {}
    setMaxAttempts(Number(graph.max_investigation_attempts ?? 2))
    setMaxClarifications(Number(graph.max_clarification_rounds ?? 2))
    setPassingScore(Number(evaluation.passing_score ?? 75))
    const nodes = isRecord(raw.nodes) ? raw.nodes : {}
    const readNode = (key: string) => { const n = isRecord(nodes[key]) ? nodes[key] : {}; return { model: String(n.model ?? 'default'), max_turns: Number(n.max_turns ?? 6) } }
    setNodeModels(Object.fromEntries(nodeKeys.map((k) => [k, readNode(k).model])))
    setNodeTurns(Object.fromEntries(nodeKeys.map((k) => [k, readNode(k).max_turns])))
    const rawModels = isRecord(raw.models) ? raw.models : {}
    const parsed: Record<string, ModelEntry> = {}
    for (const [name, val] of Object.entries(rawModels)) {
      const m = isRecord(val) ? val : {}
      parsed[name] = { model: String(m.model ?? ''), base_url: String(m.base_url ?? ''), api_key: String(m.api_key ?? ''), timeout: Number(m.timeout ?? 60), streaming: m.streaming === null || m.streaming === undefined ? null : Boolean(m.streaming) }
    }
    if (Object.keys(parsed).length === 0) {
      parsed['default'] = { model: 'gpt-4.1-mini', base_url: '', api_key: '', timeout: 60, streaming: null }
    }
    setModels(parsed)
  }, [config, profile])
  function buildPayload() {
    const current = config?.profiles[profile] ?? config?.profiles.default ?? {}
    const graph = isRecord(current.graph) ? current.graph : {}
    const evaluation = isRecord(current.evaluation) ? current.evaluation : {}
    const nodesIn = isRecord(current.nodes) ? current.nodes : {}
    const buildNode = (key: string) => { const base = isRecord(nodesIn[key]) ? nodesIn[key] : {}; return { ...base, model: nodeModels[key], max_turns: nodeTurns[key] } }
    const modelsOut: Record<string, Record<string, unknown>> = {}
    for (const [name, m] of Object.entries(models)) {
      const entry: Record<string, unknown> = { model: m.model, timeout: m.timeout }
      if (m.base_url) entry.base_url = m.base_url
      if (m.api_key) entry.api_key = m.api_key
      if (m.streaming !== null) entry.streaming = m.streaming
      modelsOut[name] = entry
    }
    return { ...current, config_version: configVersion, models: modelsOut, graph: { ...graph, max_investigation_attempts: maxAttempts, max_clarification_rounds: maxClarifications }, evaluation: { ...evaluation, passing_score: passingScore }, nodes: Object.fromEntries(nodeKeys.map((k) => [k, buildNode(k)])) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!config) { setMessage('当前为演示模式，后端连接后可保存配置'); return }
    try {
      const next = await applyAdminConfig({ profile, config: buildPayload(), expected_revision: config.revision })
      onConfigChange(next)
      setSaved(true)
      setMessage('配置已保存；新运行会使用新的快照')
      setTimeout(() => setSaved(false), 2500)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '保存失败，请重试')
    }
  }
  async function validate() {
    if (!config) { setMessage('当前为演示模式，后端连接后可校验配置'); return }
    try {
      const result = await validateAdminConfig({ profile, config: buildPayload(), expected_revision: config.revision })
      setMessage(result.valid ? '配置校验通过' : result.errors.join('；'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '校验失败，请重试')
    }
  }
  function addModel() { let i = 1; while (models[`model-${i}`]) i++; setModels((c) => ({ ...c, [`model-${i}`]: { model: 'gpt-4.1-mini', base_url: '', api_key: '', timeout: 60, streaming: null } })) }
  function updateModel(name: string, patch: Partial<ModelEntry>) { setModels((c) => ({ ...c, [name]: { ...c[name], ...patch } })) }
  function removeModel(name: string) { setModels((c) => { const next = { ...c }; delete next[name]; return next }) }
  const modelNames = Object.keys(models)
  const profiles = Object.keys(config?.profiles ?? { default: {} })
  return <div className="management-content"><PageHeading eyebrow="管理 / 配置与初始化" title="配置与初始化" description="先配置可用模型（含凭据），再为每个节点选择模型；策略与凭据解耦。" /><div className="setup-banner"><span className="setup-icon">✓</span><div><strong>{config === null ? '等待后端连接' : config.writable === false ? '使用内置默认配置' : '后端已初始化'}</strong><p>{config === null ? '当前显示演示状态' : `配置文件${config.writable === false ? '只读' : '可写'} · 当前 profile：${profile} · revision ${config.revision}`}</p></div><span className="setup-step">1 / 3</span></div><form className="settings-layout" onSubmit={submit}><section className="panel config-preview settings-full"><PanelHeader title="模型管理" meta={`${modelNames.length} 个模型`} /><p className="config-callout">每个模型是一个命名的端点（模型名 + 凭据）。节点通过引用名选择模型；留空 base_url/api_key 则回退到环境变量 OPENAI_BASE_URL / OPENAI_API_KEY。</p>{Object.entries(models).map(([name, m]) => <div className="model-card" key={name}><div className="model-card-head"><strong className="mono">{name}</strong><button type="button" className="row-link" onClick={() => removeModel(name)}>删除</button></div><label>模型名<input value={m.model} onChange={(e) => updateModel(name, { model: e.target.value })} placeholder="如 gpt-4.1-mini 或 maas-glm-5.2-volcengine-codeagent" /></label><div className="form-row"><label>Base URL<input value={m.base_url} onChange={(e) => updateModel(name, { base_url: e.target.value })} placeholder="留空=环境变量" /></label><label>API Key<input value={m.api_key} onChange={(e) => updateModel(name, { api_key: e.target.value })} type="password" placeholder="留空=保留原值" /></label></div><div className="form-row"><label>超时(秒)<input type="number" min="1" max="600" value={m.timeout} onChange={(e) => updateModel(name, { timeout: Number(e.target.value) })} /></label><label>流式<select value={m.streaming === null ? '' : String(m.streaming)} onChange={(e) => updateModel(name, { streaming: e.target.value === '' ? null : e.target.value === 'true' })}><option value="">默认</option><option value="true">开</option><option value="false">关</option></select></label></div></div>)}<button type="button" className="quiet-button model-add" onClick={addModel}>＋ 新增模型</button></section><section className="panel settings-form"><PanelHeader title="运行配置" meta="保存前会先校验" /><label>配置 Profile<input value={profile} onChange={(event) => setProfile(event.target.value)} list="profile-options" /><datalist id="profile-options">{profiles.map((name) => <option key={name} value={name} />)}</datalist><small>可选择已有 profile，也可以输入名称创建新的版本化 profile。</small></label><label>配置版本<input value={configVersion} onChange={(event) => setConfigVersion(event.target.value)} /><small>用于审计和恢复；不能与其他 profile 重复。</small></label><div className="form-row number-row"><label>定位尝试上限<input type="number" min="1" max="2" value={maxAttempts} onChange={(event) => setMaxAttempts(Number(event.target.value))} /></label><label>澄清轮数上限<input type="number" min="0" max="10" value={maxClarifications} onChange={(event) => setMaxClarifications(Number(event.target.value))} /></label></div><label>评测通过分<input type="number" min="0" max="100" value={passingScore} onChange={(event) => setPassingScore(Number(event.target.value))} /><small>运行中的任务继续使用原 config snapshot；这里只影响新运行。</small></label><div className="settings-actions"><button type="button" className="quiet-button" onClick={validate}>验证配置</button><button type="submit" className="primary-button">{saved ? '已保存 ✓' : '保存配置'}</button></div>{message && <div className="form-message">{message}</div>}</section><section className="panel config-preview"><PanelHeader title="节点策略（当前生效）" meta={String(config?.profiles[profile]?.config_version ?? configVersion)} />{nodeKeys.map((key) => <div className="form-row node-config-row" key={key}><label className="node-name">{key}<select value={nodeModels[key]} onChange={(e) => setNodeModels((c) => ({ ...c, [key]: e.target.value }))}>{modelNames.map((n) => <option key={n} value={n}>{n}</option>)}{nodeModels[key] && !modelNames.includes(nodeModels[key]) && <option value={nodeModels[key]}>{nodeModels[key]}</option>}</select></label><label className="turns-field">轮数<input type="number" min="1" max="20" value={nodeTurns[key]} onChange={(e) => setNodeTurns((c) => ({ ...c, [key]: Number(e.target.value) }))} /></label></div>)}<div className="config-callout">保存新的 profile 后，新运行使用新配置；已创建运行继续使用自己的快照。</div></section></form></div>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function SystemPage({ health, version }: { health: AdminHealth | null; version: string }) {
  const healthy = health?.status === 'ok' || health === null
  const componentStatus = (name: string) => health?.components.find((component) => component.name === name)?.status ?? 'ok'
  return <div className="management-content"><PageHeading eyebrow="管理 / 系统与更新" title="系统与更新" description="检查后端状态、协议能力和前后端版本，更新不会影响已有 checkpoint。" action={<button className="quiet-button">检查更新</button>} /><section className={`system-hero ${healthy ? '' : 'system-degraded'}`}><div className="system-orb">{healthy ? '✓' : '!'}</div><div><span className="section-kicker">BugLens backend</span><h2>{healthy ? '系统运行正常' : '系统需要关注'}</h2><p>{health ? `最近检查于 ${formatTime(health.checked_at)}` : '尚未连接后端，当前显示演示状态。'}</p></div><span className="mono system-version">v{version}</span></section><div className="system-grid"><section className="panel"><PanelHeader title="组件状态" meta={health ? '刚刚' : '演示'} />{[['HTTP / SSE Adapter', '协议 v2', 'configuration'], ['Application Service', '已连接', 'checkpoint_store'], ['Checkpoint Store', 'SQLite WAL', 'checkpoint_store'], ['Agent Runtime', '可恢复', 'model_credentials']].map(([name, detail, key]) => { const status = componentStatus(key); return <div className="component-row" key={name}><i className={`component-dot ${status}`} /><div><strong>{name}</strong><small>{detail}</small></div><span>{status === 'ok' ? '正常' : status === 'degraded' ? '需关注' : '异常'}</span></div> })}</section><section className="panel update-panel"><PanelHeader title="更新通道" meta="stable" /><div className="update-version"><span className="version-badge">v{version}</span><div><strong>当前版本</strong><small>2026-09-07 · 版本信息来自后端</small></div></div><div className="update-divider" /><p>更新时会先备份配置和数据库，并执行兼容性检查。前端静态资源与后端 wheel 可独立更新。</p><button className="quiet-button" disabled>暂无可用更新</button></section></div><section className="panel install-panel"><PanelHeader title="安装方式" meta="推荐" /><div className="install-options"><div><span className="install-icon">▣</span><strong>仅后端</strong><p>适合已有前端或 CLI 的环境</p><code>.\install.ps1 -Mode backend</code></div><div><span className="install-icon">◫</span><strong>前后端一体</strong><p>根目录脚本一键启动</p><code>.\install.ps1</code></div><div><span className="install-icon">↻</span><strong>安全更新</strong><p>保留 checkpoint 与数据库</p><code>.\distribution\buglensctl.ps1 update</code></div></div></section></div>
}

function AdminStatusPill({ run }: { run: AdminRun }) {
  const label = runStatusLabel(run)
  const tone = run.lifecycle_status === 'waiting_user' || run.lifecycle_status === 'waiting_approval' || run.lifecycle_status === 'waiting_for_target_confirmation' ? 'waiting' : run.lifecycle_status === 'running' ? 'running' : run.lifecycle_status === 'failed' ? 'failed' : run.lifecycle_status === 'canceled' ? 'canceled' : run.outcome === 'inconclusive' ? 'inconclusive' : 'completed'
  return <span className={`admin-status-pill ${tone}`}><i />{label}</span>
}

function nodeLabel(node: string) { return ({ analyze: '理解问题', investigate: '定位原因', evaluate: '检查结论', summarize: '生成报告', done: '已完成' } as Record<string, string>)[node] ?? node }
function formatTime(value: string) { return value.includes('T') ? value.slice(11, 16) : value }

function StatusPill({ status, label }: { status: Run['lifecycle_status']; label: string }) { return <span className={`status-pill ${status}`}><i />{label}</span> }

function Overview({ run, latestEvent, onNew, onClarification, onSkip, onApproval, onConfirmTarget }: { run: Run; latestEvent?: DomainEvent; onNew: () => void; onClarification: (answers: Array<{ request_id: string; question_id: string; answer: string }>) => Promise<void>; onSkip: () => Promise<void>; onApproval: (decision: 'approve' | 'reject') => Promise<void>; onConfirmTarget: (environmentId: string, primaryServiceId?: string | null) => Promise<void> }) {
  return <div className="content-grid">
    <div className="primary-column">
      {run.report ? <section className={`conclusion-card ${run.outcome}`}><div className="section-kicker"><span className="signal">✦</span> 诊断结论 <span className="confidence-tag">评测 {run.evaluation?.score ?? '—'} / 100</span></div><h2>{run.report.primary_conclusion ?? '尚未形成确认结论'}</h2><p>{run.report.executive_summary}</p><div className="action-row">{run.report.next_actions.map((action) => <span className="action-chip" key={action}>→ {action}</span>)}</div></section> : <section className="waiting-card"><span className="spinner" /><div><strong>正在理解你的问题</strong><p>诊断运行已创建，事件会实时出现在活动记录中。</p></div></section>}
      {run.investigation && <section className="panel"><PanelHeader title="原因假设" meta={`${run.investigation.hypotheses.length} 个假设`} /><div className="hypothesis-list">{run.investigation.hypotheses.map((hypothesis) => <HypothesisCard hypothesis={hypothesis} key={hypothesis.rank} />)}</div></section>}
      {run.pending_approval && <ApprovalCard request={run.pending_approval} onResolve={onApproval} canApprove={run.available_actions?.includes('approve') ?? false} canReject={run.available_actions?.includes('reject') ?? false} />}
      {run.pending_target_confirmation && <TargetConfirmationCard request={run.pending_target_confirmation} onConfirm={onConfirmTarget} canConfirm={run.available_actions?.includes('confirm_target') ?? false} />}
      {run.pending_interaction && <ClarificationCard request={run.pending_interaction} onSubmit={onClarification} onSkip={onSkip} canSubmit={run.available_actions?.includes('submit_answers') ?? true} canSkip={run.available_actions?.includes('skip_input') ?? false} />}
      </div>
    <aside className="side-column"><section className="panel context-panel"><PanelHeader title="问题上下文" /><dl>{run.target?.environment_id && <div><dt>已确认环境</dt><dd>{run.target.environment_id}</dd></div>}{run.environment_snapshot_id && <div><dt>环境快照</dt><dd>{run.environment_snapshot_id}</dd></div>}{Object.entries(run.user_context).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{Array.isArray(value) ? value.join(', ') : value}</dd></div>)}</dl><div className="question-quote">“{run.user_question}”</div></section><section className="panel activity-panel"><PanelHeader title="最近活动" meta={latestEvent ? `#${latestEvent.sequence}` : ''} />{latestEvent && <div className="activity-item"><span className="activity-dot" /><div><strong>{latestEvent.summary}</strong><span>{latestEvent.occurred_at} · {latestEvent.event_type}</span></div></div>}<button className="text-button" onClick={() => document.querySelector<HTMLButtonElement>('[aria-label="诊断视图"] button:last-child')?.click()}>查看全部事件 →</button></section><button className="new-run-card" onClick={onNew}><span>＋</span><div><strong>开始一次新的诊断</strong><small>提交问题、环境与证据</small></div></button></aside>
  </div>
}

function EvidenceView({ run }: { run: Run }) { return <div className="evidence-layout"><section className="panel"><PanelHeader title="输入证据" meta={`${run.source_evidence.length} 条`} />{run.source_evidence.map((item, index) => <div className="evidence-row" key={`${item.source}-${index}`}><span className={`source-badge ${item.source}`}>{item.source}</span><div><p>{item.content}</p>{item.reference && <span className="mono muted">{item.reference}</span>}</div></div>)}</section><section className="panel"><PanelHeader title="待补充信息" />{run.investigation?.evidence_gaps.length ? <ul className="gap-list">{run.investigation.evidence_gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul> : <div className="empty-state">当前没有待补充信息</div>}<div className="collect-box"><span>下一步建议</span>{run.investigation?.next_data_to_collect.map((item) => <p key={item}>＋ {item}</p>)}</div></section></div> }

function EventsPanel({ events }: { events: DomainEvent[] }) { return <section className="panel events-panel"><PanelHeader title="事件记录" meta={`${events.length} 个已提交事件`} /><div className="event-table"><div className="event-header"><span>序号</span><span>事件</span><span>详情</span><span>时间</span></div>{events.map((event) => <div className="event-row" key={event.event_id}><span className="mono">{String(event.sequence).padStart(2, '0')}</span><span className="event-type"><i />{event.event_type}</span><span>{event.summary ?? event.node ?? '—'}</span><span className="muted">{event.occurred_at}</span></div>)}</div></section> }

function HypothesisCard({ hypothesis }: { hypothesis: Hypothesis }) { return <article className="hypothesis-card"><div className="hypothesis-rank">0{hypothesis.rank}</div><div className="hypothesis-main"><div className="hypothesis-title"><h3>{hypothesis.cause}</h3><span>{Math.round(hypothesis.confidence * 100)}%</span></div><p>{hypothesis.rationale}</p><div className="evidence-columns"><div><label>支持证据</label>{hypothesis.supporting_evidence.map((item) => <span className="evidence-line positive" key={item}>＋ {item}</span>)}</div><div><label>验证步骤</label>{hypothesis.verification_steps.map((item) => <span className="evidence-line" key={item}>◷ {item}</span>)}</div></div></div></article> }

function TargetConfirmationCard({ request, onConfirm, canConfirm }: { request: PendingTargetConfirmation; onConfirm: (environmentId: string, primaryServiceId?: string | null) => Promise<void>; canConfirm: boolean }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  async function confirm() {
    if (!selected || !canConfirm || submitting) return
    setSubmitting(true)
    try { await onConfirm(selected) } finally { setSubmitting(false) }
  }
  return <section className="clarification-card target-card"><div className="section-kicker"><span className="attention">!</span> 请确认诊断环境</div><h2>BugLens 根据环境、服务和别名找到了以下候选项。</h2><div className="target-options">{request.candidates.map((candidate) => <button type="button" className={selected === candidate.environment_id ? 'target-option selected' : 'target-option'} key={candidate.environment_id} onClick={() => setSelected(candidate.environment_id)}><strong>{candidate.display_name}</strong><span className="mono">{candidate.environment_id}</span><small>{candidate.level}{candidate.region ? ` · ${candidate.region}` : ''} · {candidate.timezone}</small><em>{candidate.matched_by.join(' · ')}</em></button>)}</div><div className="clarification-actions"><button className="primary-button" disabled={!selected || !canConfirm || submitting} onClick={confirm}>{submitting ? '正在确认…' : '确认环境并继续'}</button><span>确认后会保存不可变环境快照；运行只能访问该快照内的数据源。</span></div></section>
}

function ClarificationCard({ request, onSubmit, onSkip, canSubmit, canSkip }: { request: InteractionRequest; onSubmit: (answers: Array<{ request_id: string; question_id: string; answer: string }>) => Promise<void>; onSkip: () => Promise<void>; canSubmit: boolean; canSkip: boolean }) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [submitted, setSubmitted] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const requiredReady = request.questions.filter((question) => question.required).every((question) => answers[question.id]?.trim())
  async function submit() {
    if (!canSubmit || !requiredReady || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(request.questions.filter((question) => answers[question.id]?.trim()).map((question) => ({ request_id: request.request_id, question_id: question.id, answer: answers[question.id] })))
      setSubmitted(true)
    } finally {
      setSubmitting(false)
    }
  }
  const [skipping, setSkipping] = useState(false)
  async function skip() {
    if (!canSkip || skipping || submitting) return
    setSkipping(true)
    try { await onSkip() } finally { setSkipping(false) }
  }
  return <section className="clarification-card"><div className="section-kicker"><span className="attention">!</span> 需要你的补充</div><h2>{request.explanation}</h2>{request.questions.map((question) => <label className="clarification-question" key={question.id}><span>{question.question}<em>{question.required ? '必填' : '选填'}</em></span><textarea value={answers[question.id] ?? ''} onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: event.target.value }))} placeholder={question.rationale} rows={3} /></label>)}<div className="clarification-actions"><button className="primary-button" disabled={!canSubmit || !requiredReady || submitted || submitting || skipping} onClick={submit}>{submitted ? '已提交，等待继续' : submitting ? '正在提交…' : '提交补充信息'}</button>{canSkip && <button className="quiet-button" disabled={submitted || submitting || skipping} onClick={skip}>{skipping ? '正在跳过…' : '暂时无法提供，跳过'}</button>}<span>回答会安全地附加到本次诊断</span></div></section>
}

function ApprovalCard({ request, onResolve, canApprove, canReject }: { request: PendingApproval; onResolve: (decision: 'approve' | 'reject') => Promise<void>; canApprove: boolean; canReject: boolean }) {
  const [submitting, setSubmitting] = useState(false)
  async function resolve(decision: 'approve' | 'reject') {
    if (submitting || (decision === 'approve' ? !canApprove : !canReject)) return
    setSubmitting(true)
    try { await onResolve(decision) } finally { setSubmitting(false) }
  }
  const args = Object.entries(request.arguments ?? {})
  return <section className="clarification-card approval-card"><div className="section-kicker"><span className="attention">!</span> 工具调用需要审批</div><h2>{request.explanation}</h2><p className="approval-tool">工具：<strong>{request.tool_name}</strong></p>{args.length > 0 && <div className="approval-arguments"><label>请求参数</label>{args.map(([key, value]) => <code key={key}>{key}: {Array.isArray(value) ? value.join(', ') : String(value ?? 'null')}</code>)}</div>}<div className="clarification-actions"><button className="primary-button" disabled={!canApprove || submitting} onClick={() => resolve('approve')}>{submitting ? '正在处理…' : '批准并继续'}</button><button className="quiet-button" disabled={!canReject || submitting} onClick={() => resolve('reject')}>拒绝并继续</button><span>仅允许已注册的只读工具，决定会记录在审计账本中。</span></div></section>
}

function NewDiagnosis({ profiles, environments, onClose, onSubmit }: { profiles: string[]; environments: EnvironmentSummary[]; onClose: () => void; onSubmit: (question: string, context: Record<string, string>, evidence: Evidence[], profile: string, target: TargetSpec) => void }) {
  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<'explicit' | 'infer'>('infer')
  const [environment, setEnvironment] = useState(environments[0]?.environment_id ?? 'production')
  const [environmentHint, setEnvironmentHint] = useState('')
  const [service, setService] = useState('')
  const [log, setLog] = useState('')
  const [profile, setProfile] = useState(profiles[0] ?? 'default')
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!question.trim()) return
    const hint = mode === 'infer' ? (environmentHint.trim() || environment) : environment
    onSubmit(question, { ...(hint ? { environment: hint } : {}), ...(service ? { service } : {}) }, log.trim() ? [{ source: 'log', content: log }] : [], profile, { mode, environment_id: mode === 'explicit' ? environment : null, primary_service_id: service || null })
  }
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><form className="new-diagnosis-modal" onSubmit={submit}><div className="modal-heading"><div><span className="eyebrow">新的诊断运行</span><h2>把问题说清楚，剩下的交给 BugLens</h2></div><button type="button" className="close-button" onClick={onClose}>×</button></div><label>问题描述<span className="required">必填</span><textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={4} placeholder="例如：生产环境订单接口从 10:20 开始大量超时…" autoFocus /></label><div className="form-row"><label>目标方式<select value={mode} onChange={(event) => setMode(event.target.value as 'explicit' | 'infer')}><option value="infer">自动推断后确认</option><option value="explicit">明确选择环境</option></select></label>{mode === 'explicit' ? <label>环境<select value={environment} onChange={(event) => setEnvironment(event.target.value)}>{(environments.length ? environments : [{ environment_id: 'production', display_name: 'Production', aliases: [], level: 'unknown', timezone: 'UTC', tags: {} }]).map((item) => <option key={item.environment_id} value={item.environment_id}>{item.display_name} · {item.environment_id}</option>)}</select></label> : <label>环境/别名提示<input value={environmentHint} onChange={(event) => setEnvironmentHint(event.target.value)} placeholder="production、prod 或线上" /></label>}</div><label>主服务提示<span className="optional">可选</span><input value={service} onChange={(event) => setService(event.target.value)} placeholder="order-api" /></label><label>已有日志或证据<span className="optional">可选</span><textarea value={log} onChange={(event) => setLog(event.target.value)} rows={3} placeholder="粘贴一小段与问题直接相关的日志、指标或变更记录" /></label><div className="form-row profile-row"><label>策略 Profile<select value={profile} onChange={(event) => setProfile(event.target.value)}>{profiles.map((name) => <option key={name}>{name}</option>)}</select></label></div><div className="modal-footer"><span>确认后会生成不可变环境快照</span><button type="submit" className="primary-button" disabled={!question.trim()}>开始诊断 →</button></div></form></div>
}

function PanelHeader({ title, meta }: { title: string; meta?: string }) { return <div className="panel-header"><h2>{title}</h2>{meta && <span>{meta}</span>}</div> }

export default App
