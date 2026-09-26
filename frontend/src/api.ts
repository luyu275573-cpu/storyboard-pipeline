export type Project = {
  id: string; title: string; synopsis: string | null; style: string; budget_cents: number;
  status: string; created_at: string; updated_at: string
}
export type FeatureKey = 'face_features' | 'hair_features' | 'body_features' | 'outfit_features' | 'style_lock'
export type Character = Record<FeatureKey, Record<string, string>> & {
  id: string; project_id: string; name: string; anchor_prompt: string; anchor_version: number;
  confirmed: boolean; subjective_word_hits: string[]
}
export type Run = { id: string; status: string; current_stage: string }
export type Gate = { id: string; reviewer: string; decided_at: string; snapshot: Character }
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
    ...options, headers: { 'Content-Type': 'application/json', ...options.headers },
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
