import { useState } from 'react'
import type { FormEvent } from 'react'
import { api } from './api'
import type { Character, Reference, Run } from './api'
import { Dialog, useApi } from './ui'

const types: Record<string, string> = { front_half: '正面半身', side_half: '侧面半身', full_body: '全身', expression_happy: '喜悦表情', expression_angry: '生气表情', expression_sad: '悲伤表情' }

export default function References({ character, run, version, refresh }: {
  character: Character; run: Run; version: number; refresh: () => void
}) {
  const refs = useApi<Reference[]>(`/characters/${character.id}/refs`, version)
  const [reviewing, setReviewing] = useState<Reference | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const formElement = event.currentTarget, form = new FormData(formElement)
    const file = form.get('image') as File
    setError(''); setNotice(''); setBusy(true)
    try {
      if (!file?.size) throw new Error('请选择一张参考图')
      if (file.size > 20 * 1024 * 1024) throw new Error('参考图不能超过 20 MB')
      await api(`/characters/${character.id}/refs?anchor_version=${character.anchor_version}&ref_type=${form.get('ref_type')}`, { method: 'POST', body: file })
      formElement.reset(); refresh(); setNotice('图片已保存，请按当前角色特征进行人工审核。')
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  async function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!reviewing) return
    const form = new FormData(event.currentTarget), passed = form.get('verdict') === 'pass'
    setError(''); setBusy(true)
    try {
      await api(`/characters/refs/${reviewing.id}/review`, { method: 'POST', body: JSON.stringify({
        run_id: run.id, anchor_version: character.anchor_version, expected_review_version: reviewing.review_version,
        passed, is_primary: passed && form.get('primary') === 'on', reviewer: form.get('reviewer'), note: form.get('note'),
      }) })
      setReviewing(null); refresh(); setNotice('参考图审核已保存，相关分镜需要按新版本审核。')
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="references">
    <div className="section-heading"><div><h3>角色参考图</h3><p className="muted">生成前需有一张审核通过的正面半身主参考图。</p></div><span className="pill">{refs.data?.length ?? 0} / 20</span></div>
    {!reviewing && (error || refs.error) && <p className="error-banner" role="alert">{error || refs.error}<button className="button" onClick={refresh}>刷新</button></p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    <form onSubmit={event => void upload(event)} className="reference-upload">
      <label>参考图类型<select name="ref_type">{Object.entries(types).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>选择图片<input name="image" type="file" accept="image/png,image/jpeg,image/webp" required /></label>
      <button className="button" disabled={busy}>{busy ? '正在保存…' : '上传参考图'}</button>
    </form>
    <p className="form-hint">PNG / JPEG / WebP，最多 20 MB。上传后需要人工审核；修改角色后需要重新核对。</p>
    {refs.loading && <p role="status">正在加载参考图…</p>}
    {refs.data?.length === 0 && <p className="reference-empty">暂无参考图，可先上传已有的角色设定图。</p>}
    <div className="reference-grid">{refs.data?.map(ref => <article key={ref.id} className="reference-card">
      <a href={`/api/v1/characters/refs/${ref.id}/asset`} target="_blank" rel="noreferrer" aria-label={`查看${types[ref.ref_type]}原图`}><img src={`/api/v1/characters/refs/${ref.id}/asset`} alt={`${character.name} · ${types[ref.ref_type]}`} /></a>
      <div><h3>{types[ref.ref_type]} {ref.is_primary && <span className="pill success">主参考</span>}</h3>
        <p className="muted">{ref.qc_passed ? `已审核 · 角色 v${ref.reviewed_anchor_version}` : ref.reviewed_anchor_version === character.anchor_version ? '未通过审核' : ref.reviewed_anchor_version ? '角色已更新，需重审' : '等待人工审核'}</p>
        {ref.reviewed_by && <p className="form-hint">{ref.reviewed_by}：{ref.review_note}</p>}
        <button className="button" disabled={busy} onClick={() => { setError(''); setReviewing(ref) }}>审核参考图</button>
      </div>
    </article>)}</div>
    {reviewing && <Dialog title={`审核参考图 · ${character.name} v${character.anchor_version}`} busy={busy} close={() => setReviewing(null)}>
      {error && <p className="error-banner" role="alert">{error}</p>}
      <form onSubmit={event => void review(event)}><div className="form-body">
        <img className="review-image" src={`/api/v1/characters/refs/${reviewing.id}/asset`} alt="待审核的角色参考图" />
        <p className="prompt-text">{character.anchor_prompt}</p>
        <p className="form-hint">请核对脸型、发型、服饰、画风及肢体结构，并确认内容可用于本项目。</p>
        <label>审核结论<select name="verdict" defaultValue="pass"><option value="pass">通过</option><option value="reject">不通过</option></select></label>
        {reviewing.ref_type === 'front_half' && <label className="checkbox-label"><input type="checkbox" name="primary" defaultChecked={reviewing.is_primary || !refs.data?.some(r => r.is_primary)} />通过后设为主参考图</label>}
        <label>审核人<input name="reviewer" required maxLength={80} /></label>
        <label>审核意见<textarea name="note" required maxLength={2000} rows={3} placeholder="描述已核对的特征，或说明不通过的原因" /></label>
        <label className="checkbox-label"><input type="checkbox" required />我已对照当前角色特征检查这张图片</label>
      </div><div className="dialog-actions"><button type="button" className="button" disabled={busy} onClick={() => setReviewing(null)}>取消</button><button className="button primary" disabled={busy}>保存审核</button></div></form>
    </Dialog>}
  </section>
}
