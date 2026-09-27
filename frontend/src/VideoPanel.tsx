import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, money } from './api'
import type { Attempt, Project, Run, Shot } from './api'
import { Dialog } from './ui'

type ExportJob = { id: string; status: string; duration_ms?: number }

export default function VideoPanel({ project, run, shots, refresh }: {
  project: Project; run: Run; shots: Shot[]; refresh: () => void
}) {
  const [attempts, setAttempts] = useState<Attempt[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [review, setReview] = useState<{ attempt: Attempt; shot: Shot } | null>(null)
  const [job, setJob] = useState<ExportJob | null>(null)
  const ids = shots.map(s => s.id).join(',')
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const rows = await Promise.all(ids.split(',').filter(Boolean).map(id => api<Attempt[]>(`/shots/${id}/attempts`)))
        if (active) setAttempts(rows.flat().filter(a => a.stage === 'video'))
      } catch (e) { if (active) setError((e as Error).message) }
      if (active) timer = setTimeout(() => void poll(), 5000)
    }
    void poll()
    return () => { active = false; clearTimeout(timer) }
  }, [ids])
  useEffect(() => {
    if (!job || ['succeeded', 'failed'].includes(job.status)) return
    const timer = setTimeout(() => {
      void api<ExportJob>(`/shots/exports/${job.id}`).then(setJob).catch(e => setError(e.message))
    }, 2000)
    return () => clearTimeout(timer)
  }, [job])
  async function act(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action() } catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }
  function decide(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    if (!review) return
    void act(async () => {
      await api(`/shots/${review.shot.id}/gate/video`, { method: 'POST', body: JSON.stringify({
        run_id: run.id, attempt_id: review.attempt.id, expected_version: review.shot.version,
        status: form.get('status'), reviewer: form.get('reviewer'), note: form.get('note'),
      }) })
      setReview(null); refresh()
    })
  }
  return <section className="panel scene-panel">
    <div className="section-heading"><h2>视频生成与终审</h2><span className="muted">Wan 2.2 · 预计 ¥2 / 条</span></div>
    <p className="muted">已锁定关键帧可生成视频。重复点击会恢复同一任务；费用按报价记账，最终以供应商账单为准。</p>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {shots.map(shot => {
      const videos = attempts.filter(a => a.shot_id === shot.id)
      const latest = videos.at(-1)
      return <article key={shot.id} className="shot-card-body">
        <h3>镜头 {shot.seq} · {shot.action_text}</h3>
        <button className="button primary" disabled={busy || !shot.locked_attempt_id} onClick={() => void act(async () => {
          const row = await api<Attempt>(`/shots/${shot.id}/video`, { method: 'POST' })
          setAttempts(current => [...current.filter(a => a.id !== row.id), row])
        })}>{latest ? '查询 / 恢复原视频任务' : '生成视频 · 预计 ¥2'}</button>
        {videos.map(a => <div key={a.id}>
          <p>尝试 {a.attempt_no} · {a.status} · 本地记账 {money(a.cost_cents)} {a.error_code && `· ${a.error_code}`}</p>
          {a.status === 'succeeded' && <>
            <video className="review-image" controls preload="metadata" src={`/api/v1/shots/attempts/${a.id}/file`} />
            <p>{shot.accepted_video_attempt_id === a.id ? '已通过人工终审' : '待审核或历史片段'}</p>
            <button className="button" disabled={busy || shot.accepted_video_attempt_id === a.id} onClick={() => setReview({ attempt: a, shot })}>审核此视频</button>
          </>}
        </div>)}
      </article>
    })}
    <div className="board-actions">
      {[true, false].map(preview => <button key={String(preview)} className="button" disabled={busy || !shots.length || (job != null && !['succeeded', 'failed'].includes(job.status)) || (!preview && shots.some(s => !s.accepted_video_attempt_id))} onClick={() => void act(async () => {
        setJob(await api<ExportJob>(`/shots/video-exports?project_id=${project.id}&run_id=${run.id}&preview=${preview}`, { method: 'POST' }))
      })}>{preview ? '合成未终审预览' : '导出终审成片'}</button>)}
      {job && <span role="status">导出：{job.status}{job.status === 'succeeded' && <> · <a href={`/api/v1/shots/exports/${job.id}/file`} download>下载 MP4</a></>}</span>}
    </div>
    {review && <Dialog title="视频人工终审" busy={busy} close={() => setReview(null)}>
      <form onSubmit={decide}><div className="form-body">
        {error && <p role="alert" className="error-banner">{error}</p>}
        <video className="review-image" controls src={`/api/v1/shots/attempts/${review.attempt.id}/file`} />
        <label>结论<select name="status"><option value="approved">通过</option><option value="rejected">驳回</option></select></label>
        <label>审核人<input name="reviewer" required maxLength={80} /></label>
        <label>审核意见<textarea name="note" required maxLength={2000} /></label>
        <label className="checkbox-label"><input type="checkbox" required />我已播放并核对人物、动作、画面和内容合规</label>
      </div><div className="dialog-actions"><button className="button primary" disabled={busy}>保存审核决定</button></div></form>
    </Dialog>}
  </section>
}
