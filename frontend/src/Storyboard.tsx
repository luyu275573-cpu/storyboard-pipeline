import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api } from './api'
import type { Board, Gate, Project, Run, Scene, Shot } from './api'
import { Dialog, Empty, useApi } from './ui'

type Editor = { type: 'scene'; scene?: Scene } | { type: 'shot'; scene: Scene; shot?: Shot } | { type: 'review' } | { type: 'delete'; scene: Scene; shot?: Shot } | null

export default function Storyboard({ project, run, version, refresh }: {
  project: Project; run: Run; version: number; refresh: () => void
}) {
  const [revision, setRevision] = useState(0)
  const [connection, setConnection] = useState('正在连接进度')
  const board = useApi<Board>(`/projects/${project.id}/storyboard?run_id=${run.id}`, version + revision)
  const [draftBoard, setDraftBoard] = useState<Board | null>(null)
  const [editor, setEditor] = useState<Editor>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    const source = new EventSource(`/api/v1/stream/${run.id}`)
    source.onopen = () => setConnection('进度已连接')
    source.onerror = () => setConnection('进度重连中，可手动刷新')
    const reload = () => setRevision(value => value + 1)
    source.addEventListener('progress', reload)
    source.addEventListener('snapshot', reload)
    source.addEventListener('reset', reload)
    source.addEventListener('unavailable', () => setConnection('进度暂不可用，可手动刷新'))
    source.addEventListener('done', () => { source.close(); setConnection('运行已结束') })
    return () => source.close()
  }, [run.id])
  const data = board.data
  const open = (next: Editor) => { setError(''); setNotice(''); setDraftBoard(data); setEditor(next) }
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action(); setEditor(null); refresh() }
    catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    void perform(async () => {
      if (!editor || !draftBoard) return
      if (editor.type === 'scene') {
        const body = { project_id: project.id, seq: Number(form.get('seq')), location: form.get('location'),
          time_of_day: form.get('time_of_day'), mood: form.get('mood'), background_prompt: form.get('background_prompt') || null,
          ...(editor.scene ? { expected_storyboard_version: draftBoard.storyboard_version } : {}),
        }
        await api(editor.scene ? `/scenes/${editor.scene.id}` : '/scenes', { method: editor.scene ? 'PUT' : 'POST', body: JSON.stringify(body) })
        setNotice('场景已保存，分镜版本已更新。')
      } else if (editor.type === 'shot') {
        const body = { scene_id: editor.scene.id, seq: Number(form.get('seq')), shot_size: form.get('shot_size'),
          camera_move: form.get('camera_move') || null, composition: form.get('composition'), character_ids: form.getAll('character_ids'),
          action_text: form.get('action_text'), dialogue: form.get('dialogue') || null,
          duration_ms: Math.round(Number(form.get('duration')) * 1000), negative_prompt: form.get('negative_prompt') || null,
          ...(editor.shot ? { expected_version: editor.shot.version } : {}),
        }
        await api(editor.shot ? `/shots/${editor.shot.id}` : '/shots', { method: editor.shot ? 'PUT' : 'POST', body: JSON.stringify(body) })
        setNotice('镜头已保存，需按当前版本完成 B 关卡审核。')
      } else if (editor.type === 'review') {
        await api(`/projects/${project.id}/storyboard/review`, { method: 'POST', body: JSON.stringify({
          run_id: run.id, storyboard_version: draftBoard.storyboard_version, status: form.get('status'), reviewer: form.get('reviewer'), note: form.get('note'),
        }) })
        setNotice('B 关卡决定和完整内容快照已保存。')
      } else {
        await api(editor.shot ? `/shots/${editor.shot.id}?expected_version=${editor.shot.version}` : `/scenes/${editor.scene.id}?expected_storyboard_version=${draftBoard.storyboard_version}`, { method: 'DELETE' })
        setNotice('已移除，分镜版本已更新。')
      }
    })
  }
  const sceneShots = editor && 'scene' in editor ? draftBoard?.shots.filter(s => s.scene_id === editor.scene?.id) ?? [] : []
  return <>
    <section className="panel preparation-panel">
      <div className="section-heading"><div><span className="eyebrow">创作准备</span><h2>分镜版本 v{data?.storyboard_version ?? '—'}</h2></div><span className="muted">{connection}</span></div>
      <div className="workflow"><span className={data?.characters_ready ? 'current' : ''}>A 角色确认</span><i>→</i><span className={data?.gate?.status === 'approved' ? 'current' : ''}>B 分镜审核</span><i>→</i><span>关键帧生成</span><i>→</i><span>C 图像终审</span></div>
      {data?.blockers.length ? <ul className="blocker-list">{data.blockers.map(item => <li key={item}>{item}</li>)}</ul> : data && <p className="text-accent">角色、参考图和镜头准备项已齐备。</p>}
      {data?.gate?.status === 'approved' && <p className="notice">B 关卡已通过。下一步需要接入图像生成与视觉质检模型。</p>}
      {data?.gate?.status === 'rejected' && <p className="warning-note">此版本未通过审核。请根据审核意见修改场景或镜头，再提交新版本。</p>}
      <div className="board-actions">
        <button className="button" disabled={busy} onClick={() => void perform(async () => {
          const result = await api<Run>(`/projects/${project.id}/runs/${run.id}/resume`, { method: 'POST' })
          setNotice(result.error_message || '已检查并恢复到当前待办关卡。')
        })}>检查并恢复流程</button>
        <button className="button primary" disabled={busy || !data || !!data.blockers.length || !!data.gate} onClick={() => open({ type: 'review' })}>审核分镜 · B 关卡</button>
        {data?.gate?.status === 'approved' && <button className="button" disabled={busy} onClick={() => void perform(async () => {
          const gates = await api<Gate[]>(`/projects/${project.id}/gates`), gate = gates.find(g => g.id === data.gate?.id)
          if (!gate) throw new Error('审核记录已变化，请刷新')
          const url = URL.createObjectURL(new Blob([JSON.stringify(gate, null, 2)], { type: 'application/json' }))
          const link = document.createElement('a'); link.href = url; link.download = `storyboard-v${data.storyboard_version}.json`; link.click()
          setTimeout(() => URL.revokeObjectURL(url), 1000)
        })}>导出审核快照</button>}
        <button className="text-button" onClick={refresh}>刷新</button>
      </div>
    </section>
    {notice && <p className="notice" role="status">{notice}</p>}
    {(board.error || (!editor && error)) && <p className="error-banner" role="alert">{board.error || error}<button className="button" onClick={refresh}>重试</button></p>}
    <div className="section-heading"><h2>场景与镜头 <span className="muted">{data?.scenes.length ?? 0} 场 / {data?.shots.length ?? 0} 镜</span></h2><button className="button primary" disabled={!data || busy} onClick={() => open({ type: 'scene' })}>＋ 新建场景</button></div>
    {board.loading && <p role="status">正在读取分镜…</p>}
    {data && !data.scenes.length && <Empty title="把故事拆成可拍摄的镜头"><p>先建立场景，再填写景别、动作、构图与时长。可以直接手工编排分镜。</p></Empty>}
    {data?.scenes.map(scene => <section className="panel scene-panel" key={scene.id}>
      <div className="section-heading"><div><span className="eyebrow">场景 {String(scene.seq).padStart(2, '0')}</span><h2>{scene.location}</h2><p className="muted">{scene.time_of_day} {scene.mood && `· ${scene.mood}`}</p></div><div className="board-actions"><button className="button" onClick={() => open({ type: 'scene', scene })}>编辑场景</button><button className="button" onClick={() => open({ type: 'shot', scene })}>＋ 添加镜头</button><button className="text-button" onClick={() => open({ type: 'delete', scene })} disabled={data.shots.some(s => s.scene_id === scene.id)}>删除空场景</button></div></div>
      {scene.background_prompt && <p className="scene-background">{scene.background_prompt}</p>}
      <div className="shot-grid">{data.shots.filter(s => s.scene_id === scene.id).map(shot => <article className="shot-card" key={shot.id}>
        <div className="shot-card-heading"><strong>{scene.seq}-{String(shot.seq).padStart(2, '0')}</strong><span className="pill">{shot.shot_size}</span><span>{shot.duration_ms / 1000}s</span></div>
        <div className="shot-placeholder"><span>{shot.shot_size}</span><small>待生成关键帧</small></div>
        <div className="shot-card-body"><h3>{shot.action_text}</h3><p>{shot.composition}</p><p className="muted">{shot.camera_move || '固定镜头'} · {shot.character_ids.map(id => data.characters.find(c => c.id === id)?.name).join('、') || '纯场景'}</p>{shot.dialogue && <blockquote>{shot.dialogue}</blockquote>}
          <div className="card-footer"><span>镜头 v{shot.version}</span><button className="text-button" onClick={() => open({ type: 'shot', scene, shot })}>编辑镜头</button><button className="text-button" onClick={() => open({ type: 'delete', scene, shot })}>删除</button></div>
        </div>
      </article>)}</div>
      {!data.shots.some(s => s.scene_id === scene.id) && <p className="reference-empty">本场景还没有镜头。添加第一个镜头后即可继续编排。</p>}
    </section>)}
    {editor && draftBoard && <Dialog title={{scene: '场景设定', shot: '镜头编排', review: `审核分镜 · v${draftBoard.storyboard_version}`, delete: '确认移除'}[editor.type]} busy={busy} close={() => setEditor(null)}>
      {error && <p className="error-banner" role="alert">{error}</p>}
      <form onSubmit={submit}><div className="form-body">
        {editor.type === 'scene' && <>
          <div className="form-row"><label>场景序号<input name="seq" type="number" min="1" max="10000" required defaultValue={editor.scene?.seq ?? Math.max(0, ...draftBoard.scenes.map(s => s.seq)) + 1} /></label><label>时间<input name="time_of_day" required maxLength={40} defaultValue={editor.scene?.time_of_day ?? '白天'} /></label></div>
          <label>场景地点<input name="location" required maxLength={200} defaultValue={editor.scene?.location} placeholder="例如：山间茶馆 · 内景" /></label>
          <label>氛围<input name="mood" maxLength={80} defaultValue={editor.scene?.mood} /></label>
          <label>环境描述<textarea name="background_prompt" rows={4} maxLength={4000} defaultValue={editor.scene?.background_prompt ?? ''} /></label>
        </>}
        {editor.type === 'shot' && <>
          <p className="muted">场景 {editor.scene.seq} · {editor.scene.location}</p>
          <div className="form-row"><label>镜头序号<input name="seq" type="number" min="1" max="10000" required defaultValue={editor.shot?.seq ?? Math.max(0, ...sceneShots.map(s => s.seq)) + 1} /></label><label>景别<select name="shot_size" defaultValue={editor.shot?.shot_size ?? '中景'}>{['特写', '近景', '中景', '全景', '远景'].map(size => <option key={size}>{size}</option>)}</select></label></div>
          <div className="form-row"><label>时长（秒）<input name="duration" type="number" min="0.5" max="60" step="0.1" required defaultValue={(editor.shot?.duration_ms ?? 5000) / 1000} /></label><label>运镜<input name="camera_move" maxLength={40} defaultValue={editor.shot?.camera_move ?? ''} placeholder="例如：缓慢推近" /></label></div>
          <fieldset><legend>出场角色（可多选）</legend>{draftBoard.characters.map(c => <label key={c.id} className="checkbox-label"><input type="checkbox" name="character_ids" value={c.id} defaultChecked={editor.shot?.character_ids.includes(c.id)} />{c.name} · v{c.anchor_version}</label>)}{!draftBoard.characters.length && <p className="muted">暂无角色，可先编排纯场景镜头。</p>}</fieldset>
          <label>动作<textarea name="action_text" required rows={2} maxLength={4000} defaultValue={editor.shot?.action_text} /></label>
          <label>构图<textarea name="composition" required rows={2} maxLength={4000} defaultValue={editor.shot?.composition} /></label>
          <label>台词<textarea name="dialogue" rows={2} maxLength={4000} defaultValue={editor.shot?.dialogue ?? ''} /></label>
          <label>避免出现的内容<textarea name="negative_prompt" rows={2} maxLength={2000} defaultValue={editor.shot?.negative_prompt ?? ''} /></label>
        </>}
        {editor.type === 'review' && <>
          <p>本次审核包含 {draftBoard.scenes.length} 个场景、{draftBoard.shots.length} 个镜头及关联的角色与参考图。总时长 {(draftBoard.shots.reduce((total, shot) => total + shot.duration_ms, 0) / 1000).toFixed(1)} 秒。</p>
          <label>审核结论<select name="status"><option value="approved">通过</option><option value="rejected">驳回并修改</option></select></label>
          <label>审核人<input name="reviewer" required maxLength={80} /></label><label>审核意见<textarea name="note" required rows={3} maxLength={2000} /></label>
          <label className="checkbox-label"><input type="checkbox" required />我已检查镜头可实现性、角色与参考图、构图和时长，并同意记录本次决定</label>
        </>}
        {editor.type === 'delete' && <p>将移除{editor.shot ? `镜头 ${editor.scene.seq}-${editor.shot.seq}` : `空场景「${editor.scene.location}」`}，旧 B 审核将失效。</p>}
        {(editor.type === 'shot' || editor.type === 'scene') && <p className="form-hint">保存后分镜版本递增，历史审核保留，当前内容需要重新审核。</p>}
      </div><div className="dialog-actions"><button className="button" type="button" disabled={busy} onClick={() => setEditor(null)}>取消</button><button className="button primary" disabled={busy}>{busy ? '正在保存…' : editor.type === 'delete' ? '确认移除' : '保存'}</button></div></form>
    </Dialog>}
  </>
}
