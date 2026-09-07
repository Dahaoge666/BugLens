export type LifecycleStatus =
  | 'created'
  | 'running'
  | 'waiting_user'
  | 'waiting_tool'
  | 'waiting_approval'
  | 'completed'
  | 'failed'
  | 'canceled'

export type Outcome = 'confirmed' | 'inconclusive'
export type NodeName = 'analyze' | 'investigate' | 'evaluate' | 'summarize'

export type Evidence = {
  source: string
  content: string
  reference?: string | null
  observed_at?: string | null
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
  source_node: 'analyze' | 'investigate'
  resume_node: 'analyze' | 'investigate'
  reason: string
  explanation: string
  questions: ClarificationQuestion[]
}

export type Run = {
  run_id: string
  user_question: string
  user_context: Record<string, string | string[]>
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
  } | null
  investigation: {
    investigation_summary: string
    hypotheses: Hypothesis[]
    evidence_gaps: string[]
    next_data_to_collect: string[]
    limitations: string[]
  } | null
  evaluation: {
    passed: boolean
    score: number
    criteria_scores: Record<string, number>
    strengths: string[]
    deficiencies: string[]
    retry_guidance: string[]
  } | null
  report: {
    executive_summary: string
    primary_conclusion?: string | null
    status_explanation: string
    next_actions: string[]
  } | null
  lifecycle_status: LifecycleStatus
  outcome?: Outcome | null
  current_node: NodeName | 'done'
  pending_interaction?: InteractionRequest | null
  revision: number
  attempt: number
  clarification_round: number
  created_at: string
  updated_at: string
}

export type DomainEvent = {
  event_id: string
  event_type: string
  sequence: number
  revision: number
  occurred_at: string
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
  pending_input: boolean
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
