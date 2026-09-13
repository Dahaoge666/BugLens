import type { DiagnosisGuide, GuideContent, GuideLookup, GuideQuery } from './types'

export async function lookupAtEntry(query: GuideQuery, lookup: (query: GuideQuery) => Promise<GuideLookup>, start: () => Promise<void>) {
  const result = await lookup(query)
  if (result.next_action === 'diagnose') await start()
  return result
}

export function guideContent(guide: DiagnosisGuide): GuideContent {
  const { guide_id, revision, origin, source_memory_id, source_run_id, created_at, updated_at, ...content } = guide
  return content
}

export function parseGuideImport(text: string): unknown {
  const value: unknown = JSON.parse(text.replace(/^\uFEFF/, ''))
  if (Array.isArray(value)) return { guides: value }
  if (value && typeof value === 'object') {
    return 'guides' in value ? value : { guides: [value] }
  }
  throw new Error('请导入指南对象、指南数组或包含 guides 的对象。')
}
