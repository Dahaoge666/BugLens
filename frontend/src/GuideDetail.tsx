import type { DiagnosisGuide } from './types'

export function GuideDetail({ guide }: { guide: DiagnosisGuide }) {
  return <article className="guide-detail">
    <div className="guide-provenance">{guide.origin === 'memory' ? '从诊断 Memory 自动整理' : '手工维护'} · 更新于 {new Date(guide.updated_at).toLocaleDateString()}</div>
    <h3>问题现象</h3><p>{guide.phenomenon}</p>
    <ul>{guide.symptoms.map((item, index) => <li key={index}>{item}</li>)}</ul>
    <h3>适用条件</h3><p>{guide.applicability}</p>
    <dl className="guide-scope">{[['环境', guide.environment], ['服务', guide.service], ['组件', guide.component], ['版本', guide.version], ['运行环境', guide.runtime]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || (guide.origin === 'manual' ? '未限定' : '未记录，需核对')}</dd></div>)}</dl>
    <p className="guide-reference-note">指南提供排查与验证方向，本次根因仍需本次证据支持。</p>
    <h3>定位步骤</h3><ol className="guide-steps">{guide.steps.map((step, index) => <li key={index}><p>{step.instruction}</p>{step.expected_observation && <small>观察目标：{step.expected_observation}</small>}</li>)}</ol>
    {guide.historical_conclusion && <><h3>历史参考结论</h3><p>{guide.historical_conclusion}</p></>}
    {Boolean(guide.unverified_causes?.length) && <><h3>仍需验证的原因</h3><ul>{guide.unverified_causes?.map((cause, index) => <li key={index}>{cause}</li>)}</ul></>}
    {Boolean(guide.limitations?.length) && <><h3>信息与适用限制</h3><ul>{guide.limitations?.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
    {guide.source_run_id && <p className="guide-provenance">来源诊断：<span className="mono">{guide.source_run_id}</span></p>}
  </article>
}
