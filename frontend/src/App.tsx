import { FormEvent, useEffect, useState } from 'react'
import { applyAdminConfig, applyAdminEnvironmentConfig, checkAdminPluginInstance, createRunId, getAdminConfig, getAdminEnvironmentConfig, getAdminHealth, getAdminPlugins, getAdminRuns, getAdminSessions, getAdminVersion, getEnvironments, getNodeExecutions, getRun, getRunTools, readCommandStream, readEvents, validateAdminConfig, validateAdminEnvironmentConfig } from './api'
import type { AdminConfig, AdminHealth, AdminRun, AdminSession, DomainEvent, EnvironmentConfig, EnvironmentList, EnvironmentSummary, Evidence, Hypothesis, InteractionRequest, NodeExecution, PendingApproval, PendingTargetConfirmation, PluginList, Run, TargetSpec, ToolExecution } from './types'

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

const NODE_LABELS: Record<string, string> = {
  analyze: '理解问题',
  investigate: '定位原因',
  evaluate: '检查结论',
  summarize: '生成报告',
  done: '已完成',
}

// 后端健康组件：技术名 → 中文名 → 作用说明
const HEALTH_COMPONENTS: Array<[string, string, string]> = [
  ['checkpoint_store', '检查点存储', '持久化诊断状态与检查点，断线可恢复'],
  ['configuration', '配置文件', 'Profile 与节点策略可用且有效'],
  ['model_credentials', '模型凭据', '模型 API Key 与 Base URL 已配置'],
]

// 将后端英文 detail 翻译为中文
function translateHealthDetail(raw: string): string {
  if (!raw) return ''
  const map: Record<string, string> = {
    'SQLite checkpoint store is reachable': '检查点存储可正常读写',
    'SQLite checkpoint store is unavailable': '检查点存储不可用，诊断无法持久化',
    'default profile is valid': '默认配置文件校验通过',
    'default profile is invalid': '默认配置文件校验失败',
    'model credentials are configured': '模型凭据已配置',
    'no model credentials; set OPENAI_API_KEY/OPENAI_BASE_URL or define api_key/base_url on a model in the config profile': '未配置模型凭据：请设置 OPENAI_API_KEY/OPENAI_BASE_URL，或在配置 profile 的模型上定义 api_key/base_url',
  }
  return map[raw] ?? raw
}

// 诊断任务中文摘要：阶段 + 进度 + 尝试/澄清次数
function runSummaryText(run: { lifecycle_status: string; current_node: string; outcome?: string | null; attempt?: number; clarification_rounds?: Record<string, number> | null; last_error?: string | null }): string {
  const node = NODE_LABELS[run.current_node] ?? run.current_node
  const clarif = run.clarification_rounds ?? {}
  const clarifTotal = Object.values(clarif).reduce((a: number, b) => a + (b as number), 0)
  if (run.lifecycle_status === 'completed') return run.outcome === 'confirmed' ? '诊断完成 · 已确认根因' : '诊断完成 · 证据不足，未决'
  if (run.lifecycle_status === 'failed') return `诊断失败 · ${translateError(run.last_error)}`
  if (run.lifecycle_status === 'canceled') return '已取消'
  if (run.lifecycle_status === 'waiting_user') return `${node} · 等待补充信息${clarifTotal > 0 ? ` · 已澄清 ${clarifTotal} 次` : ''}`
  if (run.lifecycle_status === 'waiting_tool') return `${node} · 等待工具数据`
  if (run.lifecycle_status === 'waiting_approval') return `${node} · 等待工具审批`
  if (run.lifecycle_status === 'waiting_for_target_confirmation') return `${node} · 等待确认环境`
  if (run.current_node === 'investigate' && (run.attempt ?? 0) > 0) return `重新定位原因 · 第 ${(run.attempt ?? 0) + 1} 次尝试`
  if (run.lifecycle_status === 'running') return `${node} · 推进中`
  return node
}

function translateError(message?: string | null): string {
  if (!message) return '执行出错'
  const map: Record<string, string> = {
    'analyze: execution failed': 'analyze 节点执行失败',
    'investigate: execution failed': 'investigate 节点执行失败',
    'evaluate: execution failed': 'evaluate 节点执行失败',
    'summarize: execution failed': 'summarize 节点执行失败',
  }
  return map[message] ?? message
}

// 事件类型标签 / 图标 / 色调映射（用于执行时间线）
const EVENT_LABELS: Record<string, string> = {
  run_started: '诊断已启动',
  run_completed: '诊断已完成',
  run_failed: '诊断失败',
  run_canceled: '已取消',
  node_attempt_started: '节点开始执行',
  node_completed: '节点完成',
  node_failed: '节点失败',
  input_required: '需要补充',
  input_submitted: '已提交补充',
  input_skipped: '已跳过补充',
  tool_call_started: '工具调用开始',
  tool_call_completed: '工具调用完成',
  tool_call_failed: '工具调用失败',
  tool_approval_required: '工具待审批',
  tool_approval_resolved: '工具审批已处理',
  run_waiting: '运行等待',
  run_resumed: '已恢复运行',
  target_confirmation_required: '需要确认环境',
  target_confirmed: '环境已确认',
}
const EVENT_ICONS: Record<string, string> = {
  run_started: '▶',
  run_completed: '✓',
  run_failed: '✕',
  run_canceled: '⊘',
  node_attempt_started: '◐',
  node_completed: '●',
  node_failed: '✕',
  input_required: '?',
  input_submitted: '↵',
  input_skipped: '⤳',
  tool_call_started: '⚙',
  tool_call_completed: '✓',
  tool_call_failed: '✕',
  tool_approval_required: '!',
  tool_approval_resolved: '✓',
  run_waiting: '⏸',
  run_resumed: '▶',
  target_confirmation_required: '?',
  target_confirmed: '✓',
}
const EVENT_TONES: Record<string, string> = {
  run_started: 'blue',
  run_completed: 'green',
  run_failed: 'red',
  run_canceled: 'gray',
  node_attempt_started: 'blue',
  node_completed: 'green',
  node_failed: 'red',
  input_required: 'orange',
  input_submitted: 'blue',
  input_skipped: 'orange',
  tool_call_started: 'blue',
  tool_call_completed: 'green',
  tool_call_failed: 'red',
  tool_approval_required: 'orange',
  tool_approval_resolved: 'green',
  run_waiting: 'orange',
  run_resumed: 'blue',
  target_confirmation_required: 'orange',
  target_confirmed: 'green',
}

// 诊断 Graph 节点状态推导
function deriveNodeStates(run: { lifecycle_status: string; current_node: string; outcome?: string | null; attempt?: number }): Record<string, 'pending' | 'running' | 'waiting' | 'completed' | 'failed'> {
  const order = ['analyze', 'investigate', 'evaluate', 'summarize']
  const states: Record<string, 'pending' | 'running' | 'waiting' | 'completed' | 'failed'> = {}
  const cur = run.current_node
  const curIdx = order.indexOf(cur)
  const completed = run.lifecycle_status === 'completed'
  const failed = run.lifecycle_status === 'failed'
  const waiting = run.lifecycle_status === 'waiting_user' || run.lifecycle_status === 'waiting_approval' || run.lifecycle_status === 'waiting_tool' || run.lifecycle_status === 'waiting_for_target_confirmation'
  order.forEach((node, idx) => {
    if (completed || (curIdx >= 0 && idx < curIdx)) states[node] = 'completed'
    else if (failed && node === cur) states[node] = 'failed'
    else if (node === cur) states[node] = waiting ? 'waiting' : 'running'
    else states[node] = 'pending'
  })
  return states
}

function deriveFlowStatus(run: { lifecycle_status: string; current_node: string; attempt?: number }): 'idle' | 'forward' | 'retry' | 'completed' | 'failed' {
  if (run.lifecycle_status === 'completed') return 'completed'
  if (run.lifecycle_status === 'failed') return 'failed'
  if (run.current_node === 'investigate' && (run.attempt ?? 0) > 0) return 'retry'
  return 'forward'
}

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
  const [toolExecutions, setToolExecutions] = useState<ToolExecution[]>([])
  const [nodeExecutions, setNodeExecutions] = useState<NodeExecution[]>([])
  const [view, setView] = useState<'timeline' | 'overview' | 'evidence'>('timeline')
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
      getRunTools(runId).then((result) => setToolExecutions(result.items)).catch(() => setToolExecutions([]))
      getNodeExecutions(runId).then((result) => setNodeExecutions(result.items)).catch(() => setNodeExecutions([]))
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
      const [nextRun, tools, nodes] = await Promise.all([
        getRun(runId),
        getRunTools(runId).catch(() => ({ items: [] as ToolExecution[], total: 0 })),
        getNodeExecutions(runId).catch(() => ({ items: [] as NodeExecution[], total: 0 })),
      ])
      setRun(nextRun)
      setToolExecutions(tools.items)
      setNodeExecutions(nodes.items)
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
      setToolExecutions([])
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
      getRunTools(run.run_id).then((result) => setToolExecutions(result.items)).catch(() => undefined)
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
      <DiagnosisGraph run={run} />
      <nav className="view-tabs" aria-label="诊断视图">{[['timeline', '执行过程'], ['overview', '概览'], ['evidence', '证据与假设']].map(([key, label]) => <button className={view === key ? 'selected' : ''} onClick={() => setView(key as typeof view)} key={key}>{label}</button>)}</nav>
      {view === 'evidence' ? <EvidenceView run={run} toolExecutions={toolExecutions} /> : view === 'timeline' ? <ExecutionTimeline events={events} nodeExecutions={nodeExecutions} toolExecutions={toolExecutions} /> : <Overview run={run} latestEvent={latestEvent} onNew={() => setShowNew(true)} onClarification={submitClarification} onSkip={skipClarification} onApproval={resolveToolApproval} onConfirmTarget={confirmTarget} />}
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
  if (page === 'dashboard') return <DashboardPage runs={runs} health={health} onNew={onNew} onOpenRun={onOpenRun} />
  if (page === 'tasks') return <TasksPage runs={runs} onNew={onNew} onOpenRun={onOpenRun} />
  if (page === 'sessions') return <SessionsPage sessions={sessions} onOpenRun={onOpenRun} />
  if (page === 'settings') return <SettingsPage config={config} onConfigChange={onConfigChange} fetchError={fetchError} />
  if (page === 'environments') return <EnvironmentPage environments={environments} config={environmentConfig} plugins={plugins} onConfigChange={onEnvironmentConfigChange} />
  return <SystemPage health={health} version={version} />
}

function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}

function DashboardPage({ runs, health, onNew, onOpenRun }: { runs: AdminRun[]; health: AdminHealth | null; onNew: () => void; onOpenRun: (runId: string) => void }) {
  const waiting = runs.filter((run) => run.lifecycle_status === 'waiting_user' || run.lifecycle_status === 'waiting_approval' || run.lifecycle_status === 'waiting_for_target_confirmation').length
  const running = runs.filter((run) => run.lifecycle_status === 'running').length
  const completed = runs.filter((run) => run.lifecycle_status === 'completed').length
  const healthOk = health?.status === 'ok' || health === null
  const healthLabel = health ? (health.status === 'ok' ? '运行正常' : health.status === 'degraded' ? '需要关注' : '后端异常') : '演示状态'
  return <div className="management-content">
    <PageHeading eyebrow="控制台 / 总览" title="早上好，Dahaoge" description="这里是 BugLens 的运行概况与最近活动。" action={<button className="primary-button" onClick={onNew}>＋ 新建诊断</button>} />
    <div className="metric-grid"><MetricCard label="运行中" value={String(running)} detail="当前正在推进" tone="teal" /><MetricCard label="等待补充" value={String(waiting)} detail="需要用户输入" tone="amber" /><MetricCard label="已完成" value={String(completed + 14)} detail="过去 30 天" tone="blue" /><MetricCard label="平均评测分" value="84" detail="↑ 6% 对比上月" tone="violet" /></div>
    <div className="dashboard-grid"><section className={`panel health-panel ${healthOk ? '' : 'health-degraded'}`}><PanelHeader title="后端健康" meta={health ? '刚刚检查' : '演示'} /><div className="health-summary"><span className="health-ring">{healthOk ? '✓' : '!'}</span><div><strong>{healthLabel}</strong><p>{health ? '核心组件状态来自 Admin API' : '连接后显示真实健康状态'}</p></div><span className="health-latency">{health ? 'API' : '—'}</span></div>{HEALTH_COMPONENTS.map(([key, label, desc]) => { const component = health?.components.find((item) => item.name === key); const status = component?.status ?? 'ok'; return <div className="health-row" key={key}><span className={`health-dot ${status}`} /><div className="health-info"><strong>{label}</strong><small>{status === 'ok' ? desc : translateHealthDetail(component?.detail ?? '')}</small></div><span>{status === 'ok' ? '正常' : status === 'degraded' ? '需关注' : '异常'}</span></div> })}<button className="text-button" onClick={() => window.location.hash = 'system'}>查看系统详情 →</button></section><section className="panel activity-chart"><PanelHeader title="诊断活动" meta="最近 7 天" /><div className="chart-placeholder"><div className="chart-bars">{[38, 52, 45, 72, 58, 84, 67].map((height, index) => <span key={index} style={{ height: `${height}%` }}><i /></span>)}</div><div className="chart-labels"><span>周一</span><span>周二</span><span>周三</span><span>周四</span><span>周五</span><span>周六</span><span>今天</span></div></div><div className="chart-legend"><span><i className="legend-teal" />完成 18</span><span><i className="legend-amber" />未决 4</span><span className="chart-total">22 次运行</span></div></section></div>
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
  return <div className="run-table"><div className="run-table-header"><span>任务</span><span>状态</span><span>进度</span><span>Profile</span><span>更新</span><span /></div>{runs.map((run) => <button className="run-table-row" key={run.run_id} onClick={() => onOpenRun(run.run_id)}><span className="task-cell"><strong>{run.question}</strong><span className="task-summary">{runSummaryText(run)}</span><small className="mono">{run.run_id}</small></span><span><AdminStatusPill run={run} /></span><span className="node-cell"><span className="mini-node">{run.current_node === 'done' ? '✓' : '◌'}</span>{nodeLabel(run.current_node)}</span><span className="mono muted">{run.profile}</span><span className="muted">{formatTime(run.updated_at)}</span><span className="row-arrow">→</span></button>)}</div>
}

function SessionsPage({ sessions, onOpenRun }: { sessions: AdminSession[]; onOpenRun: (runId: string) => void }) {
  const active = sessions.filter((session) => session.status === 'active').length
  const waiting = sessions.filter((session) => session.status === 'waiting').length
  return <div className="management-content"><PageHeading eyebrow="控制台 / Agent Sessions" title="Session 管理" description="每个 run_id + node 使用独立 SDK SQLiteSession；这里只展示生命周期元数据。" /><div className="session-summary"><MetricCard label="活跃 Session" value={String(active)} detail="正在执行" tone="teal" /><MetricCard label="等待输入" value={String(waiting)} detail="可恢复" tone="amber" /><MetricCard label="历史 Session" value={String(sessions.length)} detail="仅保留元数据" tone="blue" /></div><section className="panel sessions-panel"><PanelHeader title="Session 列表" meta={`${sessions.length} 个可见记录`} /><div className="session-table"><div className="session-header"><span>Session</span><span>节点</span><span>状态</span><span>消息数</span><span>更新时间</span><span /></div>{sessions.map((session) => <div className="session-row" key={session.session_id}><span><strong className="mono">{session.session_id}</strong><small>run {session.run_id}</small></span><span className="node-badge">{nodeLabel(session.node ?? 'unknown')}</span><span><span className={`session-status ${session.status}`}>{sessionStatusLabel(session.status)}</span></span><span className="mono">{session.message_count}</span><span className="muted">{session.updated_at}</span><button className="row-link" onClick={() => onOpenRun(session.run_id)}>查看运行 →</button></div>)}</div><div className="session-note">模型消息由 Agents SDK SQLiteSession 管理，业务状态和 checkpoint 不会复制消息内容。</div></section></div>
}

type EnvironmentPanel = 'overview' | 'plugins' | 'editor'
type DirectoryRecord = Record<string, unknown>

function directoryRecords(value: unknown): DirectoryRecord[] {
  if (Array.isArray(value)) return value.filter(isRecord)
  if (!isRecord(value)) return []
  return Object.entries(value).flatMap(([id, item]) => isRecord(item) ? [{ ...item, id: item.id ?? id }] : [])
}

function textValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value : fallback
}

function pluginDisplayName(pluginId: string): string {
  return ({ sqlite: 'SQLite 数据库', file_logs: '文件日志' } as Record<string, string>)[pluginId] ?? pluginId
}

function capabilityDisplayName(capability: string): string {
  return ({ database: '数据库', read_only_sql: '只读查询', schema_description: '表结构', logs: '日志检索', knowledge: '知识库检索', traffic: '流量查询', 'knowledge.search.v1': '知识库检索', 'traffic.search.v1': '流量查询', 'logs.search.v1': '日志检索', 'database.query.v1': '数据库查询', 'database.describe.v1': '数据库结构' } as Record<string, string>)[capability] ?? capability
}

function sourceDisplayName(kind: string): string {
  return ({ database: '数据库', logs: '日志', knowledge: '知识库', traffic: '流量', traces: 'Trace' } as Record<string, string>)[kind] ?? kind
}

function sourceIcon(kind: string): string {
  return kind === 'database' ? '▦' : kind === 'knowledge' ? '▤' : kind === 'traffic' || kind === 'traces' ? '⌁' : '≋'
}

function healthTone(status: string): string {
  if (status === 'ok') return 'ok'
  if (status === 'degraded') return 'degraded'
  if (status === 'error') return 'error'
  if (status === 'checking') return 'checking'
  if (status === 'disabled') return 'disabled'
  return 'neutral'
}

function healthLabel(status: string): string {
  return ({ ok: '连接正常', degraded: '需要关注', error: '不可用', checking: '检查中…', disabled: '已停用', configured: '已配置', neutral: '尚未检查' } as Record<string, string>)[status] ?? status
}

function EnvironmentPage({ environments, config, plugins, onConfigChange }: { environments: EnvironmentList; config: EnvironmentConfig | null; plugins: PluginList; onConfigChange: (config: EnvironmentConfig | null) => void }) {
  const [activePanel, setActivePanel] = useState<EnvironmentPanel>('overview')
  const [jsonText, setJsonText] = useState('')
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({})
  const [secretClears, setSecretClears] = useState<Record<string, boolean>>({})
  const [checkResults, setCheckResults] = useState<Record<string, { status: string; detail: string }>>({})
  const [message, setMessage] = useState('')
  const [messageTone, setMessageTone] = useState<'success' | 'error' | 'info'>('info')

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
  const directory = config?.config ?? {}
  const instances = directoryRecords(directory.plugin_instances)
  const sourceRecords = directoryRecords(directory.sources)
  const serviceRecords = directoryRecords(directory.services)
  const nodeRecords = directoryRecords(directory.nodes)
  const sqlitePlugin = plugins.items.find((plugin) => plugin.plugin_id === 'sqlite')
  const enabledInstances = instances.filter((instance) => instance.enabled !== false)
  const enabledSources = sourceRecords.filter((source) => source.enabled !== false)
  const environmentCards = environments.items.map((item) => {
    const inEnvironment = (record: DirectoryRecord) => textValue(record.environment_id) === item.environment_id
    const sources = enabledSources.filter(inEnvironment)
    return {
      ...item,
      serviceCount: serviceRecords.filter(inEnvironment).length,
      nodeCount: nodeRecords.filter(inEnvironment).length,
      sourceCount: sources.length,
      databaseCount: sources.filter((source) => source.kind === 'database').length,
    }
  })

  function showMessage(text: string, tone: 'success' | 'error' | 'info' = 'info') {
    setMessage(text)
    setMessageTone(tone)
  }

  async function validate() {
    if (!config || !payload) { showMessage('请输入合法的 JSON 配置，或等待后端连接', 'error'); return }
    try {
      const result = await validateAdminEnvironmentConfig({ config: payload, expected_revision: config.revision, secret_updates: buildSecretUpdates() })
      showMessage(result.valid ? '环境目录校验通过，尚未落盘' : result.errors.join('；'), result.valid ? 'success' : 'error')
    } catch (error) {
      showMessage(error instanceof Error ? error.message : '校验失败，请重试', 'error')
    }
  }

  function buildSecretUpdates() {
    const updates: Record<string, Record<string, { action: 'set' | 'clear'; value?: string }>> = {}
    for (const [key, selected] of Object.entries(secretClears)) {
      if (!selected) continue
      const separator = key.indexOf(':')
      const instanceId = key.slice(0, separator)
      const field = key.slice(separator + 1)
      if (!instanceId || !['username', 'password', 'token'].includes(field)) continue
      updates[instanceId] = { ...(updates[instanceId] ?? {}), [field]: { action: 'clear' } }
    }
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
    if (!config || !payload) { showMessage('请输入合法的 JSON 配置，或等待后端连接', 'error'); return }
    try {
      const next = await applyAdminEnvironmentConfig({ config: payload, expected_revision: config.revision, secret_updates: buildSecretUpdates() })
      onConfigChange(next)
      setSecretDrafts({})
      setSecretClears({})
      showMessage('环境目录已原子保存；已有运行继续使用原快照', 'success')
    } catch (error) {
      showMessage(error instanceof Error ? error.message : '保存失败，请重试', 'error')
    }
  }

  async function checkInstance(instanceId: string) {
    setCheckResults((current) => ({ ...current, [instanceId]: { status: 'checking', detail: '正在检查插件实例…' } }))
    try {
      const result = await checkAdminPluginInstance(instanceId)
      setCheckResults((current) => ({ ...current, [instanceId]: { status: result.status, detail: result.detail } }))
    } catch (error) {
      setCheckResults((current) => ({ ...current, [instanceId]: { status: 'error', detail: error instanceof Error ? error.message : '连接检查失败' } }))
    }
  }

  return <div className="management-content">
    <PageHeading
      eyebrow="管理 / 环境与插件"
      title="环境目录与工具插件"
      description="先确认可用环境，再查看插件健康状态；只有需要精细调整时才进入高级配置。"
      action={<div className="environment-heading-actions"><span className={`directory-badge ${config ? (config.writable ? 'ok' : 'neutral') : 'neutral'}`}>{config ? (config.writable ? '目录可写' : '只读目录') : '未连接'}</span>{config && <button className="quiet-button" onClick={() => setActivePanel('editor')}>编辑高级配置</button>}</div>}
    />
    <section className="panel directory-hero">
      <div className="directory-hero-copy"><span className="section-kicker">环境目录</span><h2>{environments.items.length ? '诊断可以访问这些目标' : '还没有可用的诊断目标'}</h2><p>{environments.items.length ? '环境确认后会生成不可变快照，诊断只能读取快照里已启用的数据源。' : '配置一个环境和只读数据源后，新的诊断就可以按目标使用外部证据。'}</p></div>
      <div className="directory-stats"><div><strong>{environments.items.length}</strong><span>可用环境</span></div><div><strong>{enabledSources.length}</strong><span>只读数据源</span></div><div><strong>{enabledInstances.length}</strong><span>启用实例</span></div><span className="directory-revision mono">{config ? `rev ${config.revision.slice(0, 10)}` : '未配置 revision'}</span></div>
    </section>
    <div className="environment-tabs" role="tablist" aria-label="环境管理视图"><button role="tab" aria-selected={activePanel === 'overview'} className={activePanel === 'overview' ? 'active' : ''} onClick={() => setActivePanel('overview')}>环境概览</button><button role="tab" aria-selected={activePanel === 'plugins'} className={activePanel === 'plugins' ? 'active' : ''} onClick={() => setActivePanel('plugins')}>插件状态 <span>{plugins.items.length}</span></button><button role="tab" aria-selected={activePanel === 'editor'} className={activePanel === 'editor' ? 'active' : ''} onClick={() => setActivePanel('editor')}>高级配置</button></div>

    {activePanel === 'overview' && <>
      <div className="environment-overview-grid">
        <section className="panel"><PanelHeader title="可用环境" meta={`${environmentCards.length} 个`} />{environmentCards.length === 0 ? <div className="empty-state"><strong>尚未配置环境目录</strong><span>进入“高级配置”粘贴或编辑目录，然后先校验再保存。</span></div> : <div className="environment-card-list">{environmentCards.map((item) => <article className="environment-card" key={item.environment_id}><div className="environment-card-head"><span className="environment-mark">◈</span><div><strong>{item.display_name}</strong><small className="mono">{item.environment_id} · {item.level}{item.region ? ` · ${item.region}` : ''}</small></div><span className="directory-badge ok">可用于诊断</span></div><p>{item.aliases.length ? `别名：${item.aliases.join('、')}` : '未设置别名'} · {item.timezone}</p><div className="environment-card-stats"><span><strong>{item.sourceCount}</strong> 数据源</span><span><strong>{item.databaseCount}</strong> 数据库</span><span><strong>{item.serviceCount}</strong> 服务</span><span><strong>{item.nodeCount}</strong> 节点</span></div></article>)}</div>}</section>
        <section className="panel sqlite-trial-card"><div className="sqlite-trial-top"><span className="plugin-icon">▦</span><div><span className="section-kicker">推荐试用</span><h2>SQLite 只读数据源</h2></div>{sqlitePlugin ? <span className="directory-badge ok">已接入</span> : <span className="directory-badge neutral">未安装</span>}</div><p>{sqlitePlugin ? '插件已被后端发现，可用于表结构查看和受限的参数化查询。' : '安装 SQLite 插件并重启后端，即可在这里完成实例检查。'}</p>{sqlitePlugin && <div className="sqlite-trial-meta"><span><strong>{instances.filter((instance) => instance.plugin_id === 'sqlite').length}</strong> 个插件实例</span><span><strong>{sourceRecords.filter((source) => source.kind === 'database' && instances.some((instance) => instance.id === source.plugin_instance_id && instance.plugin_id === 'sqlite')).length}</strong> 个数据库源</span></div>}<div className="sqlite-trial-actions"><button className="primary-button" onClick={() => setActivePanel('plugins')}>查看 SQLite 状态</button><button className="quiet-button" onClick={() => setActivePanel('editor')}>编辑数据源</button></div></section>
      </div>
      <section className="panel source-inventory"><PanelHeader title="只读数据源" meta={`${enabledSources.length} 个已启用`} />{enabledSources.length === 0 ? <div className="empty-state"><strong>还没有绑定数据源</strong><span>数据源会把环境、插件实例和具体数据位置连接起来。</span></div> : <div className="source-grid">{enabledSources.map((source) => { const instanceId = textValue(source.plugin_instance_id, '未绑定实例'); const instance = instances.find((item) => textValue(item.id) === instanceId); const kind = textValue(source.kind, '未知类型'); return <div className="source-card" key={textValue(source.id, instanceId)}><div className="source-card-icon">{sourceIcon(kind)}</div><div><strong>{textValue(source.id, '未命名数据源')}</strong><small>{sourceDisplayName(kind)} · {pluginDisplayName(textValue(instance?.plugin_id, '未安装插件'))}</small></div><span className="mono muted">{instanceId}</span></div> })}</div>}</section>
    </>}

    {activePanel === 'plugins' && <section className="panel plugin-directory-panel"><div className="plugin-directory-heading"><div><span className="section-kicker">工具插件目录</span><h2>已发现的只读连接器</h2><p>插件负责确定性读取；BugLens 控制环境边界、调用预算和审计。</p></div><span className="directory-badge neutral">{plugins.items.length} 个已发现</span></div>{plugins.items.length === 0 ? <div className="empty-state"><strong>没有已安装插件</strong><span>外部工具默认关闭。安装插件包并重启后端后，会在这里显示。</span></div> : <div className="plugin-detail-list">{plugins.items.map((plugin) => { const pluginInstances = instances.filter((instance) => instance.plugin_id === plugin.plugin_id); const enabledCount = pluginInstances.filter((instance) => instance.enabled !== false).length; return <article className={`plugin-detail-card ${plugin.plugin_id === 'sqlite' ? 'featured' : ''}`} key={plugin.plugin_id}><div className="plugin-detail-head"><span className="plugin-icon">{plugin.plugin_id === 'sqlite' ? '▦' : '◌'}</span><div className="plugin-detail-title"><span className="plugin-friendly-name">{pluginDisplayName(plugin.plugin_id)}</span><h2 className="mono">{plugin.plugin_id}</h2><p>{plugin.capabilities.map(capabilityDisplayName).join(' · ') || '已注册，只读能力由插件声明。'}</p></div><div className="plugin-version"><strong>v{plugin.implementation_version}</strong><small>Plugin API {plugin.api_major}</small></div></div><div className="plugin-detail-summary"><span className={`directory-badge ${enabledCount ? 'ok' : 'neutral'}`}>{enabledCount} 个启用实例</span><span>{plugin.health_check ? '支持连接检查' : '未提供连接检查'}</span></div><div className="plugin-instance-list">{pluginInstances.length === 0 ? <div className="plugin-unbound">插件已发现，但还没有在环境目录中创建实例。</div> : pluginInstances.map((instance) => { const id = textValue(instance.id, '未命名实例'); const result = checkResults[id]; const status = result?.status ?? (instance.enabled === false ? 'disabled' : 'configured'); return <div className="plugin-instance-card" key={id}><div className="plugin-instance-copy"><span className={`instance-pulse ${healthTone(status)}`} /><div><strong className="mono">{id}</strong><small>{instance.enabled === false ? '实例已停用' : result?.detail ?? '已配置，尚未执行连接检查'}</small></div></div><div className="plugin-instance-actions"><span className={`plugin-health ${healthTone(status)}`}><i />{healthLabel(status)}</span><button className="quiet-button" onClick={() => checkInstance(id)} disabled={status === 'checking' || !plugin.health_check}>检查连接</button></div></div> })}</div><details className="schema-details"><summary>查看配置 Schema</summary><div className="schema-grid"><div><span>实例配置</span><pre>{JSON.stringify(plugin.instance_config_schema, null, 2)}</pre></div><div><span>数据源配置</span><pre>{JSON.stringify(plugin.source_config_schema, null, 2)}</pre></div></div></details></article> })}</div>}</section>}

    {activePanel === 'editor' && <section className="panel environment-editor"><div className="editor-heading"><div><span className="section-kicker">高级配置</span><h2>环境目录 JSON</h2><p>仅编辑非敏感字段；保存前会先校验，已有运行继续使用原快照。</p></div><div className="editor-meta"><span className={`directory-badge ${config?.writable ? 'ok' : 'neutral'}`}>{config?.writable ? '可写' : '只读'}</span><span className="mono">{config ? `revision ${config.revision}` : '未连接'}</span></div></div><p className="config-callout">密码、token、用户名不会从后端返回；保留字段中的 is_set 标记即可。凭据只能通过下方 set/clear 操作更新。</p>{config ? <textarea value={jsonText} onChange={(event) => setJsonText(event.target.value)} rows={18} spellCheck={false} disabled={!config.writable} /> : <div className="empty-state">后端未提供环境目录配置。</div>}<div className="secret-editor"><div className="secret-editor-heading"><div><h3>凭据轮换</h3><p>只在需要更新时填写；提交后不会回显原值。</p></div><span className="mono">{instances.length} 个实例</span></div>{instances.length === 0 ? <div className="empty-state">当前目录没有需要管理凭据的插件实例。</div> : instances.map((instance) => { const id = textValue(instance.id); const pluginId = textValue(instance.plugin_id, 'unknown'); return <div className="secret-row" key={id}><div className="secret-row-title"><strong>{id || '未命名实例'}</strong><small>{pluginDisplayName(pluginId)} · 凭据仅用于本次提交</small></div>{(['username', 'password', 'token'] as const).map((field) => { const key = `${id}:${field}`; return <div className="secret-field" key={field}><label>{field === 'username' ? '用户名' : field === 'password' ? '密码' : 'Token'}<input type={field === 'username' ? 'text' : 'password'} value={secretDrafts[key] ?? ''} onChange={(event) => { const value = event.target.value; setSecretDrafts((current) => ({ ...current, [key]: value })); if (value) setSecretClears((current) => ({ ...current, [key]: false })) }} placeholder="留空表示不变" autoComplete="new-password" /></label><label className="clear-secret"><input type="checkbox" checked={secretClears[key] ?? false} onChange={(event) => { const checked = event.target.checked; setSecretClears((current) => ({ ...current, [key]: checked })); if (checked) setSecretDrafts((current) => ({ ...current, [key]: '' })) }} /> 清除</label></div> })}<button type="button" className="quiet-button secret-check-button" onClick={() => checkInstance(id)} disabled={!id}>检查连接</button></div> })}</div><div className="settings-actions"><button className="quiet-button" onClick={validate} disabled={!config?.writable}>验证目录</button><button className="primary-button" onClick={save} disabled={!config?.writable}>保存环境配置</button></div>{message && <div className={`form-message ${messageTone}`}>{message}</div>}</section>}
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
  const [messageTone, setMessageTone] = useState<'neutral' | 'success' | 'error'>('neutral')
  const [busyAction, setBusyAction] = useState<'validate' | 'save' | null>(null)
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
    if (!config) { setMessageTone('error'); setMessage('尚未连接后端，连接恢复后才能保存配置。'); return }
    setBusyAction('save')
    setMessage('')
    try {
      const validation = await validateAdminConfig({ profile, config: buildPayload(), expected_revision: config.revision })
      if (!validation.valid) {
        setMessageTone('error')
        setMessage(validation.errors.join('；') || '配置未通过校验，请检查标记的内容。')
        return
      }
      const next = await applyAdminConfig({ profile, config: buildPayload(), expected_revision: config.revision })
      onConfigChange(next)
      setSaved(true)
      setMessageTone('success')
      setMessage('配置已校验并保存。之后创建的诊断将使用新快照。')
      setTimeout(() => setSaved(false), 2500)
    } catch (error) {
      setMessageTone('error')
      setMessage(error instanceof Error ? error.message : '保存失败，请重试')
    } finally {
      setBusyAction(null)
    }
  }
  async function validate() {
    if (!config) { setMessageTone('error'); setMessage('尚未连接后端，连接恢复后才能校验配置。'); return }
    setBusyAction('validate')
    setMessage('')
    try {
      const result = await validateAdminConfig({ profile, config: buildPayload(), expected_revision: config.revision })
      setMessageTone(result.valid ? 'success' : 'error')
      setMessage(result.valid ? '校验通过，可以安全保存。' : result.errors.join('；'))
    } catch (error) {
      setMessageTone('error')
      setMessage(error instanceof Error ? error.message : '校验失败，请重试')
    } finally {
      setBusyAction(null)
    }
  }
  function addModel() { let i = 1; while (models[`model-${i}`]) i++; setModels((c) => ({ ...c, [`model-${i}`]: { model: 'gpt-4.1-mini', base_url: '', api_key: '', timeout: 60, streaming: null } })) }
  function updateModel(name: string, patch: Partial<ModelEntry>) { setModels((c) => ({ ...c, [name]: { ...c[name], ...patch } })) }
  function removeModel(name: string) { setModels((c) => { const next = { ...c }; delete next[name]; return next }) }
  const modelNames = Object.keys(models)
  const profiles = Object.keys(config?.profiles ?? { default: {} })
  const nodeCopy = {
    analyze: { index: '01', title: '理解问题', description: '整理现象、上下文与已有证据' },
    investigate: { index: '02', title: '定位原因', description: '调用只读数据源并形成原因假设' },
    evaluate: { index: '03', title: '独立评测', description: '检查证据链和验证步骤是否充分' },
    summarize: { index: '04', title: '生成报告', description: '区分结论、假设与信息缺口' },
  } as const
  const connected = config !== null
  const writable = config?.writable !== false
  const readyNodes = nodeKeys.filter((key) => modelNames.includes(nodeModels[key])).length
  function scrollToSetting(sectionId: string) {
    document.getElementById(sectionId)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return <div className="management-content settings-page">
    <PageHeading
      eyebrow="管理 / 配置与初始化"
      title="配置与初始化"
      description="把连接、模型和诊断策略整理成一条清晰的启用路径。"
    />

    <section className={`settings-hero ${connected ? 'is-connected' : 'is-offline'}`}>
      <div className="settings-hero-copy">
        <span className="settings-hero-orb" aria-hidden="true">{connected ? '✓' : '…'}</span>
        <div>
          <span className="settings-overline">INITIALIZATION STATUS</span>
          <h2>{connected ? (writable ? 'BugLens 已准备就绪' : '已连接，只读运行') : '正在等待后端连接'}</h2>
          <p>{fetchError || (connected ? '配置会以不可变快照应用到之后创建的诊断，现有任务不会被改变。' : '连接建立后即可校验模型端点和诊断策略。')}</p>
        </div>
      </div>
      <div className="settings-hero-meta">
        <span className="settings-status-chip"><i />{connected ? '后端在线' : '未连接'}</span>
        <span className="settings-status-chip subtle">{config ? (writable ? '配置可写' : '内置只读') : '演示视图'}</span>
        {config && <span className="settings-revision mono">rev {config.revision.slice(0, 10)}</span>}
      </div>
      <div className="setup-progress" aria-label="初始化进度">
        <div className={connected ? 'complete' : 'current'}><span>{connected ? '✓' : '1'}</span><div><strong>连接实例</strong><small>{connected ? '连接正常' : '等待连接'}</small></div></div>
        <i />
        <div className={modelNames.length > 0 ? 'complete' : connected ? 'current' : ''}><span>{modelNames.length > 0 ? '✓' : '2'}</span><div><strong>模型服务</strong><small>{modelNames.length} 个端点</small></div></div>
        <i />
        <div className={readyNodes === nodeKeys.length && modelNames.length > 0 ? 'complete' : modelNames.length > 0 ? 'current' : ''}><span>{readyNodes === nodeKeys.length && modelNames.length > 0 ? '✓' : '3'}</span><div><strong>诊断策略</strong><small>{readyNodes} / {nodeKeys.length} 节点</small></div></div>
      </div>
    </section>

    <form className="settings-form-shell" onSubmit={submit}>
      <aside className="settings-index" aria-label="配置页面目录">
        <span>配置目录</span>
        <button type="button" onClick={() => scrollToSetting('settings-models')}><b>01</b><div><strong>模型服务</strong><small>端点与凭据</small></div></button>
        <button type="button" onClick={() => scrollToSetting('settings-policy')}><b>02</b><div><strong>运行策略</strong><small>Profile 与阈值</small></div></button>
        <button type="button" onClick={() => scrollToSetting('settings-nodes')}><b>03</b><div><strong>节点策略</strong><small>模型与轮数</small></div></button>
        <div className="settings-index-note"><i>i</i><p>所有改动仅影响之后创建的诊断。</p></div>
      </aside>

      <main className="settings-sections">
        <section className="settings-card" id="settings-models">
          <div className="settings-section-heading">
            <div className="settings-section-number">01</div>
            <div><span className="settings-overline">MODEL PROVIDERS</span><h2>模型服务</h2><p>配置可复用的模型端点，再由各诊断节点按名称引用。</p></div>
            <span className="settings-count">{modelNames.length} 个端点</span>
          </div>
          <div className="settings-note"><span>⌁</span><p>Base URL 和 API Key 留空时使用服务端环境变量。密钥为只写字段，保存后不会显示原值。</p></div>
          <div className="model-list">
            {Object.entries(models).map(([name, model], index) => {
              const usedBy = nodeKeys.filter((key) => nodeModels[key] === name)
              const canRemove = modelNames.length > 1 && usedBy.length === 0
              return <article className="model-endpoint-card" key={name}>
                <div className="model-endpoint-head">
                  <span className="model-symbol" aria-hidden="true">✦</span>
                  <div><strong>{name}</strong><small>模型端点 {String(index + 1).padStart(2, '0')}</small></div>
                  <span className={`endpoint-state ${model.api_key ? 'custom' : ''}`}><i />{model.api_key ? '独立凭据' : '环境凭据'}</span>
                  <button type="button" className="icon-button danger" onClick={() => removeModel(name)} disabled={!canRemove} title={canRemove ? '删除模型端点' : usedBy.length ? '先从诊断节点解除引用' : '至少保留一个模型端点'} aria-label={`删除模型端点 ${name}`}>×</button>
                </div>
                <div className="endpoint-fields">
                  <label className="field-wide"><span>模型标识</span><input value={model.model} onChange={(event) => updateModel(name, { model: event.target.value })} placeholder="例如 gpt-4.1-mini" /></label>
                  <label><span>请求超时</span><div className="input-with-suffix"><input type="number" min="1" max="600" value={model.timeout} onChange={(event) => updateModel(name, { timeout: Number(event.target.value) })} /><em>秒</em></div></label>
                  <label className="field-wide"><span>Base URL <em>可选</em></span><input value={model.base_url} onChange={(event) => updateModel(name, { base_url: event.target.value })} placeholder="使用 OPENAI_BASE_URL" autoComplete="url" /></label>
                  <label className="field-wide"><span>API Key <em>只写</em></span><input value={model.api_key} onChange={(event) => updateModel(name, { api_key: event.target.value })} type="password" placeholder="留空以使用或保留环境凭据" autoComplete="new-password" /></label>
                  <label><span>流式响应</span><select value={model.streaming === null ? '' : String(model.streaming)} onChange={(event) => updateModel(name, { streaming: event.target.value === '' ? null : event.target.value === 'true' })}><option value="">跟随默认</option><option value="true">开启</option><option value="false">关闭</option></select></label>
                </div>
                {usedBy.length > 0 && <div className="endpoint-usage">用于 {usedBy.map((key) => nodeCopy[key].title).join('、')}</div>}
              </article>
            })}
            {modelNames.length === 0 && <div className="settings-empty"><span>✦</span><strong>还没有模型端点</strong><p>新增一个端点后，才能为诊断节点分配模型。</p></div>}
          </div>
          <button type="button" className="add-endpoint-button" onClick={addModel}><span>＋</span><div><strong>新增模型端点</strong><small>添加独立的模型、地址和凭据</small></div></button>
        </section>

        <section className="settings-card" id="settings-policy">
          <div className="settings-section-heading">
            <div className="settings-section-number">02</div>
            <div><span className="settings-overline">RUN POLICY</span><h2>运行策略</h2><p>定义配置身份、诊断上限与结论通过标准。</p></div>
            <span className="settings-count">仅影响新任务</span>
          </div>
          <div className="policy-grid">
            <label className="setting-field"><span>配置 Profile</span><input value={profile} onChange={(event) => setProfile(event.target.value)} list="profile-options" /><datalist id="profile-options">{profiles.map((name) => <option key={name} value={name} />)}</datalist><small>选择现有 Profile，或输入新名称创建一份策略。</small></label>
            <label className="setting-field"><span>配置版本</span><input value={configVersion} onChange={(event) => setConfigVersion(event.target.value)} /><small>用于审计和恢复，必须在所有 Profile 中保持唯一。</small></label>
            <label className="setting-field compact"><span>定位尝试上限</span><div className="stepper-input"><button type="button" onClick={() => setMaxAttempts(Math.max(1, maxAttempts - 1))}>−</button><input type="number" min="1" max="2" value={maxAttempts} onChange={(event) => setMaxAttempts(Number(event.target.value))} /><button type="button" onClick={() => setMaxAttempts(Math.min(2, maxAttempts + 1))}>＋</button></div><small>最多两次定位，避免无界循环。</small></label>
            <label className="setting-field compact"><span>澄清轮数上限</span><div className="stepper-input"><button type="button" onClick={() => setMaxClarifications(Math.max(0, maxClarifications - 1))}>−</button><input type="number" min="0" max="10" value={maxClarifications} onChange={(event) => setMaxClarifications(Number(event.target.value))} /><button type="button" onClick={() => setMaxClarifications(Math.min(10, maxClarifications + 1))}>＋</button></div><small>信息不足时允许向用户追问的次数。</small></label>
            <label className="setting-field score-field"><span>评测通过分 <b>{passingScore}</b></span><input type="range" min="0" max="100" value={passingScore} onChange={(event) => setPassingScore(Number(event.target.value))} style={{ '--score': `${passingScore}%` } as React.CSSProperties} /><div className="range-labels"><span>宽松 0</span><span>建议 75</span><span>严格 100</span></div></label>
          </div>
        </section>

        <section className="settings-card" id="settings-nodes">
          <div className="settings-section-heading">
            <div className="settings-section-number">03</div>
            <div><span className="settings-overline">AGENT WORKFLOW</span><h2>节点策略</h2><p>四个节点依次执行；每个节点独立选择模型和轮数。</p></div>
            <span className="settings-count">{configVersion}</span>
          </div>
          <div className="node-policy-list">
            {nodeKeys.map((key, index) => <div className="node-policy-row" key={key}>
              <div className="node-order"><span>{nodeCopy[key].index}</span>{index < nodeKeys.length - 1 && <i />}</div>
              <div className="node-policy-copy"><strong>{nodeCopy[key].title}</strong><small>{nodeCopy[key].description}</small></div>
              <label><span>模型</span><select value={nodeModels[key]} onChange={(event) => setNodeModels((current) => ({ ...current, [key]: event.target.value }))}>{modelNames.map((name) => <option key={name} value={name}>{name}</option>)}{nodeModels[key] && !modelNames.includes(nodeModels[key]) && <option value={nodeModels[key]}>{nodeModels[key]}（未配置）</option>}</select></label>
              <label className="node-turns"><span>最大轮数</span><div className="input-with-suffix"><input type="number" min="1" max="20" value={nodeTurns[key]} onChange={(event) => setNodeTurns((current) => ({ ...current, [key]: Number(event.target.value) }))} /><em>轮</em></div></label>
            </div>)}
          </div>
        </section>
      </main>

      <div className="settings-actionbar">
        <div className="actionbar-status" aria-live="polite">
          <span className={message ? messageTone : 'neutral'}>{message ? (messageTone === 'success' ? '✓' : messageTone === 'error' ? '!' : 'i') : 'i'}</span>
          <div><strong>{message || '保存前会先完成完整配置校验'}</strong><small>{config ? `当前 Profile：${profile} · ${configVersion}` : '等待后端连接'}</small></div>
        </div>
        <div className="actionbar-buttons">
          <button type="button" className="quiet-button" onClick={validate} disabled={!connected || busyAction !== null}>{busyAction === 'validate' ? '正在校验…' : '验证配置'}</button>
          <button type="submit" className="primary-button" disabled={!connected || !writable || busyAction !== null}>{busyAction === 'save' ? '正在保存…' : saved ? '已保存 ✓' : '保存并应用'}</button>
        </div>
      </div>
    </form>
  </div>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function SystemPage({ health, version }: { health: AdminHealth | null; version: string }) {
  const healthy = health?.status === 'ok' || health === null
  const componentStatus = (name: string) => health?.components.find((component) => component.name === name)?.status ?? 'ok'
  return <div className="management-content"><PageHeading eyebrow="管理 / 系统与更新" title="系统与更新" description="检查后端状态、协议能力和前后端版本，更新不会影响已有 checkpoint。" action={<button className="quiet-button">检查更新</button>} /><section className={`system-hero ${healthy ? '' : 'system-degraded'}`}><div className="system-orb">{healthy ? '✓' : '!'}</div><div><span className="section-kicker">BugLens backend</span><h2>{healthy ? '系统运行正常' : '系统需要关注'}</h2><p>{health ? `最近检查于 ${formatTime(health.checked_at)}` : '尚未连接后端，当前显示演示状态。'}</p></div><span className="mono system-version">v{version}</span></section><div className="system-grid"><section className="panel"><PanelHeader title="组件状态" meta={health ? '刚刚' : '演示'} />{HEALTH_COMPONENTS.map(([key, label, desc]) => { const status = componentStatus(key); return <div className="component-row" key={key}><i className={`component-dot ${status}`} /><div><strong>{label}</strong><small>{status === 'ok' ? desc : translateHealthDetail(health?.components.find((c) => c.name === key)?.detail ?? '')}</small></div><span>{status === 'ok' ? '正常' : status === 'degraded' ? '需关注' : '异常'}</span></div> })}</section><section className="panel update-panel"><PanelHeader title="更新通道" meta="stable" /><div className="update-version"><span className="version-badge">v{version}</span><div><strong>当前版本</strong><small>2026-09-07 · 版本信息来自后端</small></div></div><div className="update-divider" /><p>更新时会先备份配置和数据库，并执行兼容性检查。前端静态资源与后端 wheel 可独立更新。</p><button className="quiet-button" disabled>暂无可用更新</button></section></div><section className="panel install-panel"><PanelHeader title="安装方式" meta="推荐" /><div className="install-options"><div><span className="install-icon">▣</span><strong>仅后端</strong><p>适合已有前端或 CLI 的环境</p><code>.\install.ps1 -Mode backend</code></div><div><span className="install-icon">◫</span><strong>前后端一体</strong><p>根目录脚本一键启动</p><code>.\install.ps1</code></div><div><span className="install-icon">↻</span><strong>安全更新</strong><p>保留 checkpoint 与数据库</p><code>.\distribution\buglensctl.ps1 update</code></div></div></section></div>
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

function EvidenceView({ run, toolExecutions }: { run: Run; toolExecutions: ToolExecution[] }) {
  return <div className="evidence-layout">
    <section className="panel"><PanelHeader title="输入证据" meta={`${run.source_evidence.length} 条`} />{run.source_evidence.map((item, index) => <div className="evidence-row" key={`${item.source}-${index}`}><span className={`source-badge ${item.source}`}>{item.source}</span><div><p>{item.content}</p>{item.reference && <span className="mono muted">{item.reference}</span>}</div></div>)}</section>
    <section className="panel"><PanelHeader title="环境与数据源" meta={run.environment_snapshot?.display_name} />{run.environment_snapshot ? <div className="collect-box"><span className="mono">{run.environment_snapshot.snapshot_id}</span><p>{run.environment_snapshot.environment_id} · {run.environment_snapshot.level} · {run.environment_snapshot.timezone}</p>{run.environment_snapshot.sources.map((source) => <p key={source.id}>＋ {sourceDisplayName(source.kind)} · <span className="mono">{source.id}</span></p>)}</div> : <div className="empty-state">此运行没有环境快照</div>}</section>
    <section className="panel"><PanelHeader title="只读工具审计" meta={`${toolExecutions.length} 次`} />{toolExecutions.length ? toolExecutions.map((item) => <div className="evidence-row" key={item.tool_execution_id}><span className={`admin-status-pill ${item.status}`}><i />{item.status}</span><div><p>{item.operation ?? item.tool_name} · {item.source_id ?? '无数据源'}{item.duration_ms != null ? ` · ${item.duration_ms}ms` : ''}{item.truncated ? ' · 已截断' : ''}</p><span className="mono muted">{item.plugin_id ?? 'core'}{item.plugin_implementation_version ? `@${item.plugin_implementation_version}` : ''} · Evidence {parseEvidenceIds(item.evidence_ids_json).join(', ') || '—'}</span>{item.redacted_query && <code>{item.redacted_query}</code>}</div></div>) : <div className="empty-state">当前没有外部工具调用</div>}</section>
    <section className="panel"><PanelHeader title="待补充信息" />{run.investigation?.evidence_gaps.length ? <ul className="gap-list">{run.investigation.evidence_gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul> : <div className="empty-state">当前没有待补信息</div>}<div className="collect-box"><span>下一步建议</span>{run.investigation?.next_data_to_collect.map((item) => <p key={item}>＋ {item}</p>)}</div></section>
  </div>
}

function DiagnosisGraph({ run }: { run: { lifecycle_status: string; current_node: string; outcome?: string | null; attempt?: number } }) {
  const order = ['analyze', 'investigate', 'evaluate', 'summarize'] as const
  const states = deriveNodeStates(run)
  const flow = deriveFlowStatus(run)
  const positions: Record<string, { x: number; y: number }> = {
    analyze: { x: 80, y: 60 },
    investigate: { x: 260, y: 60 },
    evaluate: { x: 440, y: 60 },
    summarize: { x: 620, y: 60 },
  }
  const flowText = flow === 'retry' ? `重试定位 · 第 ${(run.attempt ?? 0) + 1} 次` : flow === 'completed' ? '诊断完成' : flow === 'failed' ? '诊断失败' : flow === 'forward' ? '推进中' : '等待中'
  return <section className="diagnosis-graph" aria-label="诊断流程图">
    <div className="graph-header"><span className={`graph-flow ${flow}`}>{flowText}</span><span className="graph-legend"><span><i className="pending" />未开始</span><span><i className="running" />执行中</span><span><i className="waiting" />等待</span><span><i className="completed" />完成</span><span><i className="failed" />失败</span></span></div>
    <svg viewBox="0 0 720 130" className="graph-svg" preserveAspectRatio="xMidYMid meet">
      {order.slice(0, -1).map((node, idx) => {
        const next = order[idx + 1]
        const from = positions[node]
        const to = positions[next]
        return <line key={`f-${node}`} x1={from.x + 36} y1={from.y} x2={to.x - 36} y2={to.y} className={`graph-edge ${states[node] === 'completed' ? 'active' : ''}`} markerEnd="url(#arrow)" />
      })}
      {/* 回环弧：evaluate 未通过 → investigate 重试 */}
      {(flow === 'retry' || states.evaluate === 'completed') && (
        <path d={`M ${positions.evaluate.x} ${positions.evaluate.y + 36} C ${positions.evaluate.x} 130, ${positions.investigate.x} 130, ${positions.investigate.x} ${positions.investigate.y + 36}`} className={`graph-loop ${flow === 'retry' ? 'active' : ''}`} fill="none" markerEnd="url(#arrow-loop)" />
      )}
      {order.map((node) => {
        const pos = positions[node]
        const state = states[node]
        const label = NODE_LABELS[node]
        return <g key={node} transform={`translate(${pos.x}, ${pos.y})`}>
          <circle r="36" className={`graph-node ${state}`} />
          {state === 'running' && <circle r="46" className="graph-pulse" />}
          <text textAnchor="middle" dy="-2" className="graph-node-label">{label}</text>
          <text textAnchor="middle" dy="14" className="graph-node-sub">{node}</text>
        </g>
      })}
      <defs>
        <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" /></marker>
        <marker id="arrow-loop" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" /></marker>
      </defs>
    </svg>
  </section>
}

function ExecutionTimeline({ events, nodeExecutions, toolExecutions }: { events: DomainEvent[]; nodeExecutions: NodeExecution[]; toolExecutions: ToolExecution[] }) {
  // 按节点分组事件
  const groups: Array<{ node: string; label: string; events: DomainEvent[] }> = []
  let current: { node: string; label: string; events: DomainEvent[] } | null = null
  for (const event of events) {
    const node = event.node ?? 'runtime'
    if (!current || current.node !== node) {
      current = { node, label: NODE_LABELS[node] ?? node, events: [] }
      groups.push(current)
    }
    current.events.push(event)
  }
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  function toggle(key: string) { setExpanded((current) => { const next = new Set(current); next.has(key) ? next.delete(key) : next.add(key); return next }) }
  return <section className="panel execution-timeline"><PanelHeader title="执行过程" meta={`${events.length} 个事件`} /><div className="timeline-list">{groups.length === 0 && <div className="empty-state">暂无执行事件</div>}{groups.map((group, gIdx) => {
    const nodeExec = nodeExecutions.filter((n) => n.node === group.node)
    const tools = toolExecutions.filter((t) => nodeExec.some((n) => n.execution_id === t.node_execution_id))
    return <div className="timeline-group" key={`g-${gIdx}`}>
      <div className="timeline-group-head"><span className="timeline-node-dot" /><strong>{group.label}</strong><small className="muted">{group.events.length} 个事件{tools.length > 0 ? ` · ${tools.length} 次工具调用` : ''}</small></div>
      <div className="timeline-events">{group.events.map((event) => {
        const tone = EVENT_TONES[event.event_type] ?? 'gray'
        const label = EVENT_LABELS[event.event_type] ?? event.event_type
        const icon = EVENT_ICONS[event.event_type] ?? '•'
        const key = `${event.event_id}-${event.sequence}`
        return <div className={`timeline-event ${tone}`} key={key}>
          <span className="timeline-event-icon">{icon}</span>
          <div className="timeline-event-body">
            <span className="timeline-event-type">{label}</span>
            {event.summary && <span className="timeline-event-summary">{event.summary}</span>}
            {event.reason && <span className="timeline-event-detail">原因：{event.reason}</span>}
            {event.tool_name && <span className="timeline-event-detail">工具：{event.tool_name}</span>}
            {event.failure && <span className="timeline-event-detail failure">{event.failure.code}：{event.failure.message}</span>}
            <span className="timeline-event-time mono">{event.occurred_at}</span>
          </div>
        </div>
      })}
      {tools.length > 0 && <div className="timeline-tools">{tools.map((tool) => {
        const toolKey = `tool-${tool.tool_execution_id}`
        const isOpen = expanded.has(toolKey)
        return <div className="timeline-tool" key={toolKey}>
          <button className="timeline-tool-head" onClick={() => toggle(toolKey)}><span className={`tool-status ${tool.status}`}><i />{tool.status}</span><strong>{tool.operation ?? tool.tool_name}</strong><small className="muted">{tool.duration_ms != null ? `${tool.duration_ms}ms` : ''}{tool.truncated ? ' · 已截断' : ''}</small><span className="tool-chevron">{isOpen ? '▾' : '▸'}</span></button>
          {isOpen && <div className="timeline-tool-body">
            <div><label>工具</label><code>{tool.tool_name}</code></div>
            {tool.plugin_id && <div><label>插件</label><code>{tool.plugin_id}{tool.plugin_implementation_version ? `@${tool.plugin_implementation_version}` : ''}</code></div>}
            {tool.redacted_query && <div><label>查询</label><code>{tool.redacted_query}</code></div>}
            {tool.source_id && <div><label>数据源</label><code>{tool.source_id}</code></div>}
            {parseEvidenceIds(tool.evidence_ids_json).length > 0 && <div><label>证据 ID</label><code>{parseEvidenceIds(tool.evidence_ids_json).join(', ')}</code></div>}
            {tool.error_code && <div className="failure"><label>错误</label><code>{tool.error_code}</code></div>}
          </div>}
        </div>
      })}</div>}
    </div>
    </div>
  })}</div></section>
}

function parseEvidenceIds(value: string): string[] {
  try {
    const parsed = JSON.parse(value) as unknown
    return Array.isArray(parsed) ? parsed.map(String) : []
  } catch {
    return []
  }
}

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
  const [environment, setEnvironment] = useState(environments[0]?.environment_id ?? '')
  const [environmentHint, setEnvironmentHint] = useState('')
  const [service, setService] = useState('')
  const [log, setLog] = useState('')
  const [profile, setProfile] = useState(profiles[0] ?? 'default')
  const [autoExplore, setAutoExplore] = useState(false)
  useEffect(() => {
    if (!environment && environments[0]) setEnvironment(environments[0].environment_id)
  }, [environment, environments])
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!question.trim() || (mode === 'explicit' && !environment)) return
    const hint = mode === 'infer' ? environmentHint.trim() : environment
    const context: Record<string, string> = { ...(hint ? { environment: hint } : {}), ...(service ? { service } : {}), ...(autoExplore ? { auto_explore: 'true' } : {}) }
    onSubmit(question, context, log.trim() ? [{ source: 'log', content: log }] : [], profile, { mode, environment_id: hint || null, primary_service_id: service || null })
  }
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><form className="new-diagnosis-modal" onSubmit={submit}><div className="modal-heading"><div><span className="eyebrow">新的诊断运行</span><h2>把问题说清楚，剩下的交给 BugLens</h2></div><button type="button" className="close-button" onClick={onClose}>×</button></div><label>问题描述<span className="required">必填</span><textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={4} placeholder="例如：生产环境订单接口从 10:20 开始大量超时…" autoFocus /></label><div className="form-row"><label>目标方式<select value={mode} onChange={(event) => setMode(event.target.value as 'explicit' | 'infer')}><option value="infer">自动推断后确认</option><option value="explicit">明确选择环境</option></select></label>{mode === 'explicit' ? <label>环境<select value={environment} onChange={(event) => setEnvironment(event.target.value)}><option value="" disabled>请选择已配置环境</option>{environments.map((item) => <option key={item.environment_id} value={item.environment_id}>{item.display_name} · {item.environment_id}</option>)}</select></label> : <label>环境/别名提示<input value={environmentHint} onChange={(event) => setEnvironmentHint(event.target.value)} placeholder="production、prod 或线上" /></label>}</div><label>主服务提示<span className="optional">可选</span><input value={service} onChange={(event) => setService(event.target.value)} placeholder="order-api" /></label><label>已有日志或证据<span className="optional">可选</span><textarea value={log} onChange={(event) => setLog(event.target.value)} rows={3} placeholder="粘贴一小段与问题直接相关的日志、指标或变更记录" /></label><div className="form-row profile-row"><label>策略 Profile<select value={profile} onChange={(event) => setProfile(event.target.value)}>{profiles.map((name) => <option key={name}>{name}</option>)}</select></label><label className="auto-explore-toggle"><input type="checkbox" checked={autoExplore} onChange={(event) => setAutoExplore(event.target.checked)} /><span><strong>自主探索模式</strong><small>自动跳过澄清，不等用户补充</small></span></label></div><div className="modal-footer"><span>确认后会生成不可变环境快照</span><button type="submit" className="primary-button" disabled={!question.trim() || (mode === 'explicit' && !environment)}>开始诊断 →</button></div></form></div>
}

function PanelHeader({ title, meta }: { title: string; meta?: string }) { return <div className="panel-header"><h2>{title}</h2>{meta && <span>{meta}</span>}</div> }

export default App
