export type Project = {
  id: string; title: string; synopsis: string | null; style: string; budget_cents: number;
  storyboard_version: number; status: string; created_at: string; updated_at: string
}
export type FeatureKey = 'face_features' | 'hair_features' | 'body_features' | 'outfit_features' | 'style_lock'
export type Character = Record<FeatureKey, Record<string, string>> & {
  id: string; project_id: string; name: string; anchor_prompt: string; anchor_version: number;
  confirmed: boolean; subjective_word_hits: string[]
}
export type Run = { id: string; status: string; current_stage: string; graph_state: { blockers?: string[] }; error_message: string | null }
export type Gate = { id: string; gate_type: string; status: string; reviewer: string; note: string; decided_at: string; snapshot: Partial<Character> & { storyboard_version?: number } }
export type Budget = {
  budget_cents: number; spent_cents: number; reserved_cents: number; remaining_cents: number; ratio: number;
  billing_disputed: boolean;
  ledgers: { scope: string; scope_key: string; budget_cents: number; spent_cents: number; reserved_cents: number }[]
}
export type CallPage = { items: {
  id: number; request_id: string; call_no: number; kind: string; provider: string; model: string;
  status: string; cost_cents: number; reserved_cents: number; reported_cost_cents: number | null;
  error_code: string | null;
}[]; page: number; has_more: boolean }
export type Page = { items: Project[]; total: number }
export type Preview = { anchor_prompt: string; negative_prompt: string; subjective_word_hits: string[] }

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...options, headers: { 'Content-Type': options.body instanceof Blob ? options.body.type || 'application/octet-stream' : 'application/json', ...options.headers },
    signal: options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
  })
  const result = await response.json().catch(() => null)
  if (!response.ok || result?.code !== 'OK') {
    const fields = result?.data?.errors?.map((e: {loc: string[]; msg: string}) =>
      `${e.loc.slice(1).join('.')}: ${e.msg}`).join('；')
    throw new Error(fields || result?.message || `请求失败（${response.status}），请稍后重试`)
  }
  return result.data as T
}

export const money = (cents: number) => new Intl.NumberFormat('zh-CN', {
  style: 'currency', currency: 'CNY', minimumFractionDigits: 2,
}).format(cents / 100)

export function centsFromInput(value: string): number {
  if (!/^\d+(\.\d{1,2})?$/.test(value)) throw new Error('预算最多保留两位小数')
  const [whole, decimals = ''] = value.split('.')
  const cents = Number(whole) * 100 + Number(decimals.padEnd(2, '0'))
  if (cents > 20000) throw new Error('项目预算上限为 ¥200.00')
  return cents
}

export type Reference = {
  id: string; ref_type: string; asset_sha256: string; qc_passed: boolean; is_primary: boolean;
  reviewed_anchor_version: number | null; reviewed_by: string | null; review_note: string | null;
  review_version: number
}
export type Scene = {
  id: string; project_id: string; seq: number; location: string; time_of_day: string;
  mood: string; background_prompt: string | null
}
export type Shot = {
  id: string; scene_id: string; seq: number; shot_size: string; camera_move: string | null;
  composition: string; character_ids: string[]; action_text: string; dialogue: string | null;
  duration_ms: number; negative_prompt: string | null; version: number; status: string; locked_attempt_id?: string | null
}
export type ExportResult = { id: string; status: string; output_path: string; duration_ms: number; shots: number }
export type Attempt = {
  id: string; shot_id: string; attempt_no: number; stage: string; provider: string; model: string;
  seed: string | null; asset_path: string | null; status: string; error_code: string | null;
  cost_cents: number; latency_ms: number | null; created_at: string
}
export type QCReport = {
  id: string; attempt_id: string; model: string; verdict: string; dimensions: Record<string, { score: number; ok: boolean; note: string }>;
  severity: string | null; suggestion: string | null; confidence: number | null; reasoning: string | null;
  human_verdict: string | null; human_note: string | null; reviewed_at: string | null; created_at: string
}
export type Board = {
  project_id: string; run_id: string; storyboard_version: number; characters: Character[];
  references: Reference[]; scenes: Scene[]; shots: Shot[]; blockers: string[]; characters_ready: boolean;
  gate: { id: string; status: string } | null
}
