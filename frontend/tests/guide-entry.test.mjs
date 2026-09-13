import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/guideEntry.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const entry = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const query = { question: '连接池超时', context: {}, evidence: [], target: { mode: 'infer' } }

test('a similar problem waits for user choice without creating a diagnosis', async () => {
  let starts = 0
  const found = { next_action: 'confirm_similarity', matches: [{ guide: { title: '连接池超时' }, similarity: 0.5 }] }
  assert.equal(await entry.lookupAtEntry(query, async () => found, async () => { starts++ }), found)
  assert.equal(starts, 0)
})

test('the backend diagnose action starts the existing diagnosis once', async () => {
  let starts = 0
  await entry.lookupAtEntry(query, async () => ({ next_action: 'diagnose', matches: [] }), async () => { starts++ })
  assert.equal(starts, 1)
})

test('a failed lookup cannot silently start a diagnosis', async () => {
  let starts = 0
  await assert.rejects(entry.lookupAtEntry(query, async () => { throw new Error('offline') }, async () => { starts++ }), /offline/)
  assert.equal(starts, 0)
})

test('manual JSON import supports files, arrays and a single guide without inventing content', () => {
  const guide = { title: 'timeout', category: 'performance', steps: [{ instruction: '读取日志' }] }
  assert.deepEqual(entry.parseGuideImport(JSON.stringify(guide)), { guides: [guide] })
  assert.deepEqual(entry.parseGuideImport(JSON.stringify([guide])), { guides: [guide] })
  assert.deepEqual(entry.parseGuideImport('\uFEFF' + JSON.stringify({ guides: [guide], tenant_id: 't1' })), { guides: [guide], tenant_id: 't1' })
  assert.throws(() => entry.parseGuideImport('null'))
})

test('editing cannot submit source and revision fields as guide content', () => {
  assert.deepEqual(entry.guideContent({ title: 'timeout', guide_id: 'g1', revision: 2, origin: 'memory', source_memory_id: 'm1', source_run_id: 'r1', created_at: 'today', updated_at: 'today' }), { title: 'timeout' })
})
