import { JsonConfigField } from './PluginConfigFields'
import { catalogRecords, copyRecord, exampleRequest, isObject, operationLabels } from './pluginForm'
import type { ConfigRecord } from './pluginForm'

export function ConnectorUsageFields({ value, onChange, operations, required = false, sourceIds }: {
  value: ConfigRecord; onChange: (value: ConfigRecord) => void; operations: string[]; required?: boolean; sourceIds: string[]
}) {
  const examples = catalogRecords(value.examples)
  const update = (key: string, next: unknown) => onChange({ ...value, [key]: next })
  const updateExample = (index: number, next: ConfigRecord) => update('examples', examples.map((example, position) => position === index ? next : example))
  return <section className="config-section connector-usage"><div className="config-section-heading"><div><h3>节点使用说明</h3><p>帮助定位节点了解用途、适用场景和调用参数；按本次接入独立保存。</p></div><span className="directory-badge neutral">随诊断快照保存</span></div>
    <div className="config-fields"><label className="config-field">接入名称<input value={String(value.name ?? '')} required={required} maxLength={128} placeholder="例如 订单知识库 / 预发布慢查询" onChange={(event) => update('name', event.target.value)} /></label><label className="config-field">适用场景<input value={String(value.when_to_use ?? '')} maxLength={2000} placeholder="例如 请求超时、订单失败时检索" onChange={(event) => update('when_to_use', event.target.value)} /></label></div>
    <label className="config-field">用途说明<textarea rows={2} value={String(value.description ?? '')} required={required} maxLength={2000} placeholder="说明能读取哪些数据、覆盖哪些对象与时间范围" onChange={(event) => update('description', event.target.value)} /></label>
    <label className="config-field">使用步骤与参数约定<textarea rows={4} value={String(value.instructions ?? '')} required={required} maxLength={4096} placeholder="例如 先查询结构，再按 request_id 检索；说明必填参数、过滤规则、空结果和错误的含义" onChange={(event) => update('instructions', event.target.value)} /><small>填写只读工具的用法；凭据请放在认证配置中。说明本身不作为故障证据。</small></label>
    <details className="config-advanced"><summary>调用示例与节点可见内容</summary><p className="field-hint">示例中的 source_id 自动绑定实际数据源；节点根据本次问题调整参数。</p>
      {examples.map((example, index) => <article className="usage-example" key={index}><div className="config-section-heading"><label className="config-field">工具<select value={String(example.operation)} required onChange={(event) => updateExample(index, { ...example, operation: event.target.value, request: exampleRequest(event.target.value) })}>{[...new Set([...operations, String(example.operation)])].map((operation) => <option value={operation} key={operation}>{operationLabels[operation] ?? operation}</option>)}</select></label><button type="button" className="text-button" onClick={() => update('examples', examples.filter((_, position) => position !== index))}>移除此示例</button></div><JsonConfigField label="请求参数 JSON" value={copyRecord(example.request)} onChange={(next) => { if (!isObject(next)) throw new Error('请求参数必须是 JSON 对象'); updateExample(index, { ...example, request: next }) }} /></article>)}
      <button type="button" className="quiet-button" disabled={examples.length >= 8 || !operations.length} onClick={() => update('examples', [...examples, { operation: operations[0], request: exampleRequest(operations[0]) }])}>＋ 添加调用示例</button>
      <p className="field-hint">可用只读工具：{operations.join('、') || '请先添加数据源'}。MCP 仅开放已映射的能力。</p><p className="field-hint mono">数据源：{sourceIds.join('、') || '尚未添加'}</p>
    </details>
  </section>
}
