export type LifecycleStatus =
  | 'created'
  | 'running'
  | 'waiting_user'
  | 'waiting_tool'
  | 'waiting_approval'
  | 'waiting_for_target_confirmation'
  | 'completed'
  | 'failed'
  | 'canceled'

export type Outcome = 'confirmed' | 'inconclusive'
export type NodeName = 'analyze' | 'investigate' | 'evaluate' | 'summarize'
export type ContextValue = string | number | boolean | string[] | null

export type TargetSpec = {
  mode: 'explicit' | 'infer'
  environment_id?: string | null
  primary_service_id?: string | null
}

export type TargetCandidate = {
  environment_id: string
  display_name: string
  aliases: string[]
  level: string
  region?: string | null
  timezone: string
  matched_by: string[]
}

export type PendingTargetConfirmation = {
  request_id: string
  requested_target: TargetSpec
  candidates: TargetCandidate[]
  config_revision?: string | null
}

export type EnvironmentSummary = {
  environment_id: string
  display_name: string
  aliases: string[]
  level: string
  region?: string | null
  timezone: string
  tags: Record<string, string>
}

export type EnvironmentList = {
  revision: string
  items: EnvironmentSummary[]
}

export type PluginView = {
  plugin_id: string
  implementation_version: string
  api_major: number
  capabilities: string[]
  health_check: boolean
  instance_ids: string[]
  instance_status: Record<string, string>
  instance_config_schema: Record<string, unknown>
  source_config_schema: Record<string, unknown>
}

export type PluginList = { items: PluginView[] }

export type PluginHealth = {
  status: 'ok' | 'degraded' | 'error'
  detail: string
  plugin_id?: string | null
  implementation_version?: string | null
}

export type EnvironmentConfig = {
  revision: string
  writable: boolean
  config: Record<string, unknown>
}

export type ToolExecution = {
  tool_execution_id: string
  tool_name: string
  plugin_id?: string | null
  plugin_implementation_version?: string | null
  plugin_instance_id?: string | null
  environment_snapshot_id?: string | null
  source_id?: string | null
  operation?: string | null
  redacted_query?: string | null
  query_fingerprint?: string | null
  status: string
  error_code?: string | null
  evidence_ids_json: string
  truncated: boolean
  started_at: string
  completed_at?: string | null
  duration_ms?: number | null
}

export type Evidence = {
  evidence_id?: string
  source: string
  source_type?: string
  content: string
  reference?: string | null
  source_reference?: string | null
  content_hash?: string
  storage_reference?: string | null
  observed_at?: string | null
  collected_at?: string
  tool_execution_id?: string | null
  metadata?: Record<string, unknown>
}

export type Hypothesis = {
  rank: number
  cause: string
  rationale: string
  supporting_evidence: string[]
  contradicting_evidence: string[]
  confidence: number
  verification_steps: string[]
  remediation_direction?: string | null
}

export type ClarificationQuestion = {
  id: string
  question: string
  rationale: string
  required: boolean
  answer_type: 'text' | 'single_select' | 'multi_select' | 'evidence_upload'
  options: string[]
}

export type InteractionRequest = {
  request_id: string
  source_node: 'analyze' | 'investigate' | 'evaluate'
  resume_node: 'analyze' | 'investigate'
  reason: string
  explanation: string
  questions: ClarificationQuestion[]
}

export type PendingApproval = {
  request_id: string
  tool_name: string
  explanation: string
  tool_call_ids: string[]
  arguments: Record<string, ContextValue>
}

export type Run = {
  run_id: string
  user_question: string
  user_context: Record<string, ContextValue>
  context?: Record<string, ContextValue>
  source_evidence: Evidence[]
  analysis: {
    category: string
    category_confidence: number
    summary: string
    symptoms: string[]
    impact?: string | null
    time_window?: string | null
    environment?: string | null
    missing_information: string[]
    extracted_evidence?: Evidence[]
  } | null
  investigation: {
    investigation_summary: string
    hypotheses: Hypothesis[]
    evidence_gaps: string[]
    next_data_to_collect: string[]
    limitations: string[]
    progress_delta?: ProgressDelta
  } | null
  evaluation: {
    passed: boolean
    score: number
    criteria_scores: Record<string, number>
    strengths: string[]
    deficiencies: string[]
    retry_guidance: string[]
    interaction_request?: InteractionRequest | null
  } | null
  report: {
    executive_summary: string
    primary_conclusion?: string | null
    status_explanation: string
    next_actions: string[]
    evidence_ids?: string[]
    limitations?: string[]
  } | null
  lifecycle_status: LifecycleStatus
  outcome?: Outcome | null
  current_node: NodeName | 'done'
  pending_interaction?: InteractionRequest | null
  pending_approval?: PendingApproval | null
  target: TargetSpec
  pending_target_confirmation?: PendingTargetConfirmation | null
  environment_snapshot_id?: string | null
  environment_snapshot?: {
    snapshot_id: string
    environment_id: string
    display_name: string
    level: string
    region?: string | null
    timezone: string
    primary_service_id?: string | null
    sources: Array<{
      id: string
      kind: 'database' | 'logs'
      service_ids: string[]
      node_ids: string[]
    }>
  } | null
  revision: number
  attempt: number
  clarification_round: number
  clarification_rounds?: Record<string, number>
  retry_cycle?: number
  retry_index?: number
  resume_available?: boolean
  active_execution_id?: string | null
  cancel_requested_at?: string | null
  available_actions?: string[]
  created_at: string
  updated_at: string
}

export type ProgressDelta = {
  new_evidence_ids: string[]
  resolved_gap_ids: string[]
  changed_hypothesis_ids: string[]
  discarded_hypothesis_ids: string[]
}

export type DomainEvent = {
  protocol_version?: string
  event_id: string
  id?: string
  run_id?: string
  runid?: string
  event_type: string
  type?: string
  specversion?: string
  source?: string
  subject?: string
  sequence: number
  revision: number
  occurred_at: string
  time?: string
  data?: Record<string, unknown>
  node?: NodeName
  next_node?: string
  summary?: string
  outcome?: Outcome
}

export type AdminRun = {
  run_id: string
  question: string
  lifecycle_status: string
  status: string
  outcome: string | null
  current_node: string
  profile: string
  config_version: string
  config_snapshot_id: string
  attempt: number
  clarification_round: number
  clarification_rounds?: Record<string, number>
  retry_cycle?: number
  retry_index?: number
  resume_available?: boolean
  active_execution_id?: string | null
  available_actions?: string[]
  cancel_requested?: boolean
  pending_input: boolean
  pending_approval?: boolean
  environment_id?: string | null
  environment_snapshot_id?: string | null
  pending_target_confirmation?: boolean
  created_at: string
  updated_at: string
  last_error: string | null
}

export type AdminSession = {
  session_id: string
  run_id: string
  node: string | null
  status: string
  created_at: string
  updated_at: string
  message_count: number
  config_snapshot_id: string | null
}

export type AdminConfig = {
  revision: string
  writable: boolean
  active_profile: string
  profiles: Record<string, Record<string, unknown>>
}

export type AdminHealth = {
  status: 'ok' | 'degraded' | 'error'
  checked_at: string
  components: Array<{ name: string; status: 'ok' | 'degraded' | 'error'; detail: string; checked_at: string }>
}

export type AdminVersion = {
  version: string
  protocol_version: string
  api_prefix: string
}
