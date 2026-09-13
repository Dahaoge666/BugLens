import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/pluginForm.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const form = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const sqlite = { plugin_id: 'sqlite', capabilities: ['database', 'read_only_sql'], source_config_schema: {} }

test('all native plugins offer MCP and CLI without requiring driver settings', () => {
  for (const plugin_id of ['sqlite', 'postgresql', 'file_logs', 'ssh', 'ssh_logs']) {
    assert.ok(form.supportedTransports({ ...sqlite, plugin_id }).includes('mcp'))
    assert.ok(form.supportedTransports({ ...sqlite, plugin_id }).includes('cli'))
    assert.deepEqual(form.validateBasicConfig(plugin_id, {}, [{ config: {} }], { type: 'mcp', url: 'http://localhost/mcp', tool_map: { search_logs: 'search' } }), [])
    assert.deepEqual(form.validateBasicConfig(plugin_id, {}, [{ config: {} }], { type: 'cli', command: 'reader' }), [])
  }
  assert.deepEqual(form.supportedTransports({ ...sqlite, supported_transports: ['mcp', 'cli'] }), ['mcp', 'cli'])
})

test('generic sources only offer registered kinds and mapping follows source selection', () => {
  assert.deepEqual(form.sourceKinds({ capabilities: ['*'] }), ['database', 'logs', 'host', 'knowledge', 'traffic'])
  const original = { type: 'mcp', url: 'http://localhost/mcp', tool_map: { query_database: 'query', search_knowledge: 'search' } }
  const saved = form.transportForSave(original, [{ kind: 'knowledge' }])
  assert.deepEqual(saved.tool_map, { search_knowledge: 'search' })
  assert.deepEqual(form.operationsForSources([{ kind: 'database' }, { kind: 'logs' }]), ['describe_database', 'query_database', 'search_logs'])
  assert.equal(original.tool_map.query_database, 'query')
  assert.notEqual(form.validateBasicConfig('connector', {}, [], { ...saved, tool_map: {} }).length, 0)
  assert.deepEqual(form.exampleRequest('inspect_host'), { check: 'system' })
  assert.equal(form.exampleRequest('search_logs').text_query, 'timeout')
  assert.ok(form.exampleRequest('search_logs').start_time.endsWith('+08:00'))
})

test('service binding scopes new integrations and preserves legacy associations', () => {
  const source = { id: 's1', plugin_instance_id: 'old', service_ids: ['billing'], config: { paths: ['app.log'] } }
  const prepared = form.prepareSources([source], 'new', 'prod', true, 'orders')
  assert.deepEqual(prepared[0].service_ids, ['orders', 'billing'])
  assert.deepEqual(source.service_ids, ['billing'])
  assert.equal(form.instanceBelongsToService({ id: 'new', service_id: 'orders' }, prepared, 'orders'), true)
  assert.equal(form.instanceBelongsToService({ id: 'new', service_id: 'orders' }, prepared, 'billing'), false)
  assert.equal(form.instanceBelongsToService({ id: 'old' }, [source], 'billing'), true)
})

test('remote plugins validate required connection details and safe log selectors', () => {
  assert.match(form.validateBasicConfig('postgresql', { host: 'db' }, [], { type: 'driver' }).join(' '), /数据库名称/)
  assert.deepEqual(form.validateBasicConfig('ssh', { host: 'host' }, [{ config: { checks: ['system'] } }], { type: 'driver' }), [])
  assert.match(form.validateBasicConfig('ssh_logs', { host: 'host', root_path: 'relative' }, [{ config: { path: 'app.log' } }], { type: 'driver' }).join(' '), /绝对路径/)
  for (const path of ['**/*.log', '*/app.log', '../app.log', '/var/log/app.log']) {
    assert.notEqual(form.validateBasicConfig('ssh_logs', { host: 'host', root_path: '/logs' }, [{ config: { path } }], { type: 'driver' }).length, 0)
  }
  assert.deepEqual(form.validateBasicConfig('ssh_logs', { host: 'host', root_path: '/logs' }, [{ config: { paths: ['app.log', 'archive/*.log'] } }], { type: 'driver' }), [])
})

test('each new integration source has a valid unique id and no copied credentials', () => {
  const a = form.newSource(sqlite, 'sqlite-a')
  const b = form.newSource(sqlite, 'sqlite-a')
  assert.notEqual(a.id, b.id)
  assert.match(a.id, /^[A-Za-z0-9_.-]{1,128}$/)
  assert.equal(a.kind, 'database')
  assert.deepEqual(a.config, {})
  assert.equal(a.password, undefined)
  const clone = form.copyRecord({ nested: { items: [] } })
  const other = form.copyRecord({ nested: { items: [] } })
  clone.nested.items.push('a')
  assert.deepEqual(other.nested.items, [])
})

test('schema defaults are independent and omit write-only credentials', () => {
  const schema = { properties: { encodings: { default: ['utf-8'] }, password: { default: 'secret', writeOnly: true }, token: { default: 'secret', 'x-buglens-secret': true } } }
  const a = form.schemaDefaults(schema)
  const b = form.schemaDefaults(schema)
  a.encodings.push('latin-1')
  assert.deepEqual(b, { encodings: ['utf-8'] })
})

test('clearing optional fields omits them while false and zero remain explicit', () => {
  const config = { root_path: 'logs', health_path: 'old.sqlite', retained: { a: 1 } }
  assert.deepEqual(form.updateConfig(config, 'health_path', ''), { root_path: 'logs', retained: { a: 1 } })
  assert.equal(config.health_path, 'old.sqlite')
  assert.equal(form.updateConfig(config, 'flag', false).flag, false)
  assert.equal(form.updateConfig(config, 'number', 0).number, 0)
  assert.deepEqual(form.listValues(' one\n\n two\r\none\n'), ['one', 'two'])
})

test('changing transport removes settings from the previous connection method', () => {
  const current = { type: 'ssh', host: 'old', user: 'buglens', remote_command: '/probe', identity_file: '/key', timeout_seconds: 7 }
  assert.deepEqual(form.changeTransport(current, 'cli'), { type: 'cli', timeout_seconds: 7 })
  assert.deepEqual(form.changeTransport({ type: 'mcp', url: 'https://example/mcp', tool_map: { search_logs: 'search' }, command: '/cli', args: ['--arg'], token: 'never' }, 'driver'), { type: 'driver' })
  assert.equal(current.host, 'old')
})

test('log form preserves legacy selectors until edited and accepts several relative files', () => {
  const config = { path: 'active.log', paths: ['extra.log'], glob: 'archive/*.log', timestamp_fields: ['observed_at'] }
  assert.deepEqual(form.logPaths(config), ['extra.log', 'active.log', 'archive/*.log'])
  const updated = form.setLogPaths(config, 'orders.log\norders/*.log\n')
  assert.deepEqual(updated, { paths: ['orders.log', 'orders/*.log'], timestamp_fields: ['observed_at'] })
  assert.equal(config.path, 'active.log')
  assert.deepEqual(form.validateBasicConfig('file_logs', { root_path: 'data/logs' }, [{ config: updated }], { type: 'driver' }), [])
})

test('missing paths and invalid log locations have actionable validation messages', () => {
  assert.match(form.validateBasicConfig('sqlite', {}, [{ config: {} }], { type: 'driver' })[0], /数据库文件路径/)
  for (const path of ['/var/log/app.log', 'C:\\logs\\app.log', '..\\app.log', 'orders/../app.log']) {
    assert.match(form.validateBasicConfig('file_logs', { root_path: 'logs' }, [{ config: { paths: [path] } }], { type: 'driver' })[0], /相对于上方目录/)
  }
  assert.match(form.validateBasicConfig('mcp', {}, [], { type: 'mcp', url: 'https://example/mcp' })[0], /工具映射/)
})

test('saving binds only selected sources and preserves their advanced settings', () => {
  const sources = [{ id: 's1', kind: 'database', environment_id: 'old', plugin_instance_id: 'old', service_ids: ['orders'], config: { path: 'orders.sqlite', denied_columns: ['token'], custom: { x: 1 } }, limits: { max_results: 20 } }]
  const prepared = form.prepareSources(sources, 'new-instance', 'prod', true)
  assert.equal(prepared[0].environment_id, 'prod')
  assert.equal(prepared[0].plugin_instance_id, 'new-instance')
  assert.deepEqual(prepared[0].limits, { max_results: 20 })
  assert.deepEqual(prepared[0].service_ids, ['orders'])
  prepared[0].config.custom.x = 2
  assert.equal(sources[0].config.custom.x, 1)
  assert.equal(sources[0].environment_id, 'old')
  assert.equal(form.prepareSources(sources, 'new-instance', 'prod', false)[0].enabled, false)
})

test('SQLite checks target the first enabled database and retain an explicit check path', () => {
  const sources = [{ enabled: false, config: { path: 'disabled.sqlite' } }, { config: { path: 'orders.sqlite' } }]
  const config = { root_path: 'data' }
  assert.deepEqual(form.connectionForSave('sqlite', config, sources, { type: 'driver' }), { root_path: 'data', health_path: 'orders.sqlite' })
  assert.deepEqual(config, { root_path: 'data' })
  assert.equal(form.connectionForSave('sqlite', { health_path: 'probe.sqlite' }, sources, { type: 'driver' }).health_path, 'probe.sqlite')
  assert.deepEqual(form.connectionForSave('sqlite', config, sources, { type: 'mcp' }), config)
})
