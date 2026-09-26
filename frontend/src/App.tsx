import { useState } from 'react'
import type { FormEvent } from 'react'
import { Dialog, Empty, Icon, useApi } from './ui'
import References from './References'
import Storyboard from './Storyboard'
import { api, centsFromInput, money } from './api'
import type { Budget, CallPage, Character, FeatureKey, Gate, Page, Preview, Project, Run } from './api'

const groups: [FeatureKey, string, string][] = [
  ['face_features', '面部特征', '脸型=鹅蛋脸\n眼睛=深棕色杏眼'],
  ['hair_features', '发型与发色', '发型=及肩直发\n发色=#1A1A1A'],
  ['body_features', '体型特征', '身高=168cm'],
  ['outfit_features', '服饰与配件', '上衣=月白色立领长衫'],
  ['style_lock', '画风锁定', '画风=日系厚涂\n全局色调=冷色调'],
]
const tabs = [
  ['projects', '项目管理', 'M3 7h6l2 2h10v11H3z M3 7V4h6l2 3'],
  ['characters', '角色与锚定', 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2'],
  ['storyboard', '分镜工作台', 'M3 4h18v16H3z M3 9h18 M10 9v11'],
  ['qc', '质检与评估', 'm12 3 8 3v5c0 5-5 9-8 10-3-1-8-5-8-10V6z m-4 9 3 3 5-5'],
  ['budget', '预算与成本', 'M4 20V10 M10 20V4 M16 20v-7 M22 20H2'],
] as const
type Tab = typeof tabs[number][0]
type Modal = 'project' | 'character' | 'edit' | 'confirm' | 'preview' | null

function parseFeatures(value: FormDataEntryValue | null): Record<string, string> {
  const result: Record<string, string> = Object.create(null)
  for (const line of String(value || '').split('\n').filter(line => line.trim())) {
    const index = line.indexOf('=')
    const key = line.slice(0, index).trim(), description = line.slice(index + 1).trim()
    if (index < 1 || !description) throw new Error('特征请按“名称=描述”填写，每行一项')
    if (Object.hasOwn(result, key)) throw new Error(`特征“${key}”重复，请合并到同一行`)
    result[key] = description
  }
  return result
}

export default function App() {
  const [tab, setTab] = useState<Tab>('projects')
  const [version, setVersion] = useState(0)
  const [page, setPage] = useState(1)
  const [callsPage, setCallsPage] = useState(1)
  const [search, setSearch] = useState('')
  const [project, setProject] = useState<Project | null>(null)
  const [selectedCharacter, setSelectedCharacter] = useState('')
  const [modal, setModal] = useState<Modal>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const projects = useApi<Page>(`/projects?page=${page}&page_size=9&q=${encodeURIComponent(search)}`, version)
  const characters = useApi<Character[]>(project ? `/characters?project_id=${project.id}` : null, version)
  const runs = useApi<Run[]>(project ? `/projects/${project.id}/runs` : null, version)
  const gates = useApi<Gate[]>(project ? `/projects/${project.id}/gates` : null, version)
  const budget = useApi<Budget>(project && tab === 'budget' ? `/budget/${project.id}` : null, version)
  const calls = useApi<CallPage>(project && tab === 'budget' ? `/budget/${project.id}/calls?page=${callsPage}&limit=20` : null, version)
  const character = characters.data?.find(c => c.id === selectedCharacter) ?? characters.data?.[0]
  const currentRun = runs.data?.[0]
  const run = runs.data?.find(r => r.current_stage === 'character' && r.status === 'waiting_gate')
  const confirmed = characters.data?.filter(c => c.confirmed).length ?? 0
  const refresh = () => setVersion(v => v + 1)
  const openModal = (value: Modal) => { setError(''); setNotice(''); setModal(value) }
  const chooseProject = (value: Project) => {
    setProject(value); setCallsPage(1); setSelectedCharacter(''); setTab('characters'); setNotice(''); setError('')
  }
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action(); refresh(); setModal(null) }
    catch (err) { setError(err instanceof Error ? err.message : '连接失败，请重试') }
    finally { setBusy(false) }
  }
  function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    void perform(async () => {
      const item = await api<Project>('/projects', { method: 'POST', body: JSON.stringify({
        title: form.get('title'), style: form.get('style'), synopsis: form.get('synopsis') || null,
        budget_cents: centsFromInput(String(form.get('budget'))),
      }) })
      chooseProject(item); setNotice('项目已创建。接下来建立角色档案。')
    })
  }
  function saveCharacter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    void perform(async () => {
      const editing = modal === 'edit' && character
      const body = { project_id: project!.id, name: form.get('name'),
        ...Object.fromEntries(groups.map(([key]) => [key, parseFeatures(form.get(key))])),
        ...(editing ? { expected_version: character.anchor_version } : {}),
      }
      const saved = await api<Character>(editing ? `/characters/${character.id}` : '/characters', {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify(body),
      })
      setSelectedCharacter(saved.id)
      setNotice(editing ? '新版本已保存，请重新审核确认。' : '角色已建立，锚定描述已自动生成。')
    })
  }
  function confirmCharacter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    void perform(async () => {
      await api(`/characters/${character!.id}/confirm`, { method: 'POST', body: JSON.stringify({
        run_id: run!.id, anchor_version: character!.anchor_version, reviewer: form.get('reviewer'),
      }) })
      setNotice('角色版本已确认，审核快照已保存。')
    })
  }
  async function showPreview() {
    if (!character) return
    setBusy(true); setError('')
    try {
      setPreview(await api<Preview>(`/characters/${character.id}/anchor-preview`, {
        method: 'POST', body: JSON.stringify({ level: 'normal' }),
      })); setModal('preview')
    } catch (err) { setError(err instanceof Error ? err.message : '预览失败') }
    finally { setBusy(false) }
  }

  const activeDataError = tab === 'projects' ? projects.error
    : characters.error || runs.error || gates.error || budget.error || calls.error

  return <div className="app-shell">
    <aside className="sidebar"><a className="brand" href="#" onClick={e => { e.preventDefault(); setTab('projects') }}>
      <span className="brand-mark">镜</span><span>分镜流水线<small>STORYBOARD STUDIO</small></span></a>
      <div className="nav-caption">创作工作区</div><nav aria-label="主导航">{tabs.map(([key, label, path]) =>
        <button key={key} className={`nav-item ${tab === key ? 'active' : ''}`} aria-current={tab === key ? 'page' : undefined}
          onClick={() => { setTab(key); setNotice(''); setError('') }}><Icon path={path} />{label}</button>)}</nav>
      <div className="sidebar-note"><span className="stage-dot" /> 本地工作区<p>先确认角色，再推进分镜</p><span className="version">准备工作台 · v0.3</span></div>
    </aside>
    <main><header className="topbar"><div className="breadcrumb">工作台 <span>/</span><strong>{tabs.find(t => t[0] === tab)?.[1]}</strong></div>
      <div className="top-actions">{project && tab !== 'projects' && <span className="project-label">{project.title}</span>}
        <button className="button primary" onClick={() => openModal('project')}>＋ 新建项目</button></div></header>
      <div className="content">
        {notice && <div className="notice" role="status">✓ {notice}</div>}
        {(activeDataError || (!modal && error)) && <div className="error-banner" role="alert">{activeDataError || error}<button className="button" onClick={refresh}>重新加载</button></div>}
        {tab === 'projects' ? <>
          <div className="page-heading"><div><span className="eyebrow">从一个故事开始</span><h1>我的项目</h1><p>让角色、分镜和每一次生成都有迹可循。</p></div>
            <form className="search" onSubmit={e => { e.preventDefault(); setSearch(String(new FormData(e.currentTarget).get('q') || '')); setPage(1) }}>
              <input name="q" aria-label="搜索项目" placeholder="搜索项目名称…" /><button type="submit" className="button">搜索</button></form></div>
          <div className="summary-grid"><div className="metric"><span>项目数量{search && ' · 搜索结果'}</span><strong>{projects.data?.total ?? '—'}</strong><small>按项目组织创作与素材</small></div>
            <div className="metric"><span>人工审核关卡</span><strong>A <em>/</em> B <em>/</em> C</strong><small>角色确认 · 分镜审核 · 合规终审</small></div>
            <div className="metric"><span>预算控制</span><strong>¥200 <em>/ 项目上限</em></strong><small>创建时可设置更低的预算</small></div></div>
          <div className="section-heading"><h2>项目列表</h2><span className="muted">创建项目，开始准备角色档案</span></div>
          {projects.loading && <p className="loading" role="status">正在加载项目…</p>}
          {projects.data && !projects.data.items.length && <Empty title={search ? '没有找到匹配的项目' : '你的第一个故事，准备开场'}>
            <p>{search ? '试试其他项目名称。' : '写下项目名称和故事概要，为角色建立稳定的创作起点。'}</p>
            <button className="button primary" onClick={() => openModal('project')}>＋ 创建项目</button></Empty>}
          <div className="project-grid">{projects.data?.items.map((item, i) => <button className="project-card" key={item.id} onClick={() => chooseProject(item)}>
            <div className={`cover tone-${i % 3}`}><span className="cover-letter">{item.title.slice(0, 1)}</span><span className="pill">准备中</span><span className="cover-caption">STORYBOARD / {String((page - 1) * 9 + i + 1).padStart(2, '0')}</span></div>
            <div className="project-card-body"><h3>{item.title}</h3><p>{item.style}</p><div className="synopsis">{item.synopsis || '还没有填写故事概要'}</div>
              <div className="card-footer"><span>项目预算 <b>{money(item.budget_cents)}</b></span><span className="text-accent">准备角色 →</span></div></div></button>)}</div>
          {projects.data && projects.data.total > 9 && <div className="pagination"><button className="button" disabled={page === 1} onClick={() => setPage(p => p - 1)}>上一页</button><span>第 {page} 页</span><button className="button" disabled={page * 9 >= projects.data.total} onClick={() => setPage(p => p + 1)}>下一页</button></div>}
        </> : !project ? <Empty title="先选择一个项目"><p>角色、分镜和预算会按项目保存。</p><button className="button primary" onClick={() => setTab('projects')}>查看项目</button></Empty> : <>
          <button className="back-link" onClick={() => setTab('projects')}>← 返回项目列表 / 切换项目</button>
          <div className="page-heading"><div><span className="eyebrow">{project.title}</span><h1>{tabs.find(t => t[0] === tab)?.[1]}</h1><p>{tab === 'characters' ? '把角色特征固化为版本，让每个镜头有一致的依据。' : '每一个决定，都能回到它的依据。'}</p></div>
            {tab === 'characters' && <button className="button primary" onClick={() => openModal('character')}>＋ 新建角色</button>}</div>
          {tab === 'characters' ? <>
            <div className="workflow"><span className="current">A 角色确认 <b>{confirmed} / {characters.data?.length ?? '—'}</b></span><i>→</i><span>B 分镜审核</span><i>→</i><span>C 合规终审</span></div>
            {characters.loading && <p className="loading" role="status">正在加载角色…</p>}
            {characters.data && !characters.data.length && <Empty title="让故事里的角色先有一个清晰的样子"><p>记录脸型、发色、服饰等客观特征，系统会自动生成稳定的锚定描述。</p><button className="button primary" onClick={() => openModal('character')}>建立第一个角色</button></Empty>}
            {character && <div className="character-layout"><div className="character-list">{characters.data?.map(c => <button key={c.id} className={`character-item ${c.id === character.id ? 'selected' : ''}`} onClick={() => setSelectedCharacter(c.id)}>
              <span className="character-avatar">{c.name.slice(0, 1)}</span><span><strong>{c.name}</strong><small>锚定 v{c.anchor_version} · {c.confirmed ? '已确认' : '待确认'}</small></span><span className={`status-dot ${c.confirmed ? 'ok' : ''}`} /></button>)}</div>
              <section className="panel character-detail"><div className="section-heading"><div><span className={`pill ${character.confirmed ? 'success' : 'warning'}`}>{character.confirmed ? '已人工确认' : '等待人工确认'}</span><h2>{character.name}<small>v{character.anchor_version}</small></h2></div><button className="button" onClick={() => openModal('edit')}>编辑特征</button></div>
                <div className="feature-grid">{groups.map(([key, label]) => <div key={key}><h3>{label}</h3><dl>{Object.entries(character[key]).length ? Object.entries(character[key]).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>) : <p className="muted">尚未填写</p>}</dl></div>)}</div>
                {character.subjective_word_hits.length > 0 && <p className="warning-note">特征含主观词：{character.subjective_word_hits.join('、')}。建议改为可观察的具体描述。</p>}
                <div className="prompt"><div className="section-heading"><h3>锚定描述</h3><button className="text-button" disabled={busy} onClick={() => void showPreview()}>预览完整提示词 ↗</button></div><p>{character.anchor_prompt}</p></div>
                <div className="detail-footer"><p>{character.confirmed ? '修改特征会生成新版本，并要求重新确认。' : '请核对以上特征，确认后会保存当前版本的审核快照。'}</p><button className="button primary" disabled={character.confirmed || !run} onClick={() => openModal('confirm')}>{character.confirmed ? '✓ 当前版本已确认' : '审核并确认角色'}</button></div>
                {currentRun && <References key={character.id} character={character} run={currentRun} version={version} refresh={refresh} />}
              </section></div>}
            {!!gates.data?.length && <section className="panel audit-panel"><h2>审核记录</h2>{gates.data.map(gate => <div className="audit-row" key={gate.id}><span className="text-accent">✓</span><strong>{gate.snapshot.name || ({character: '角色', reference: '参考图', storyboard: '分镜'}[gate.gate_type] || gate.gate_type)}</strong><span>v{gate.snapshot.anchor_version || gate.snapshot.storyboard_version}</span><span>{gate.reviewer} · {gate.status === 'approved' ? '通过' : '驳回'}</span><time>{new Date(`${gate.decided_at}Z`).toLocaleString('zh-CN')}</time></div>)}</section>}
          </> : tab === 'budget' ? <>
            {budget.loading && <p role="status">正在读取预算…</p>}
            {budget.data && <>{budget.data.billing_disputed && <p className="error-banner" role="alert">供应商账单超出预留报价，已暂停新调用。请先核实账单。</p>}<div className="summary-grid budget-metrics"><div className="metric"><span>项目预算</span><strong>{money(budget.data.budget_cents)}</strong><small>当前项目独立额度</small></div><div className="metric"><span>已花费</span><strong>{money(budget.data.spent_cents)}</strong><small>按实际调用账本统计</small></div><div className="metric"><span>已预留</span><strong>{money(budget.data.reserved_cents)}</strong><small>运行中或结果待确认的调用</small></div><div className="metric"><span>可用余额</span><strong className="text-accent">{money(budget.data.remaining_cents)}</strong><small>预算减去已花费与预留</small></div></div>
              <section className="panel"><div className="section-heading"><h2>预算使用情况</h2><span>{(budget.data.ratio * 100).toFixed(1)}%</span></div><progress max="1" value={budget.data.ratio} aria-label="预算占用比例" /><p className="muted">占用 = 已花费 + 已预留；未知结果核实后再释放额度</p><div className="table-wrap"><table><thead><tr><th>预算范围</th><th>已花费</th><th>已预留</th><th>上限</th></tr></thead><tbody>{budget.data.ledgers.map(row => <tr key={`${row.scope}/${row.scope_key}`}><td>{{total: '项目总额', image: '图像生成', video: '视频生成', vision: '视觉质检', llm: '剧本与分镜'}[row.scope_key] || row.scope_key}</td><td>{money(row.spent_cents)}</td><td>{money(row.reserved_cents)}</td><td>{money(row.budget_cents)}</td></tr>)}</tbody></table></div><p className="muted">类型额度受项目总额共同约束，不能相加当作可用预算。</p></section><section className="panel"><div className="section-heading"><h2>模型调用记录</h2><button className="button" onClick={refresh}>刷新</button></div>
                {calls.loading && <p role="status">正在读取调用记录…</p>}
                {calls.data && !calls.data.items.length && <p className="muted">尚无模型调用。真实生成将在后续批次接入。</p>}
                {!!calls.data?.items.length && <div className="table-wrap"><table className="call-table"><thead><tr><th>供应商 / 模型</th><th>结果</th><th>已花费</th><th>已预留</th><th>供应商报账</th></tr></thead><tbody>{calls.data.items.map(call => <tr key={call.id}><td>{call.provider}<small className="call-model">{call.model}</small></td><td>{{calling:'处理中',unknown:'待对账',succeeded:'成功',failed:'失败'}[call.status] || call.status}</td><td>{money(call.cost_cents)}</td><td>{money(call.reserved_cents)}</td><td>{call.reported_cost_cents === null ? '待确认' : money(call.reported_cost_cents)}</td></tr>)}</tbody></table></div>}
                {calls.data && (callsPage > 1 || calls.data.has_more) && <div className="pagination"><button className="button" disabled={callsPage === 1} onClick={() => setCallsPage(p => p - 1)}>上一页</button><span>第 {callsPage} 页</span><button className="button" disabled={!calls.data.has_more} onClick={() => setCallsPage(p => p + 1)}>下一页</button></div>}
              </section></>}
          </> : tab === 'storyboard' && currentRun ? <Storyboard key={project.id} project={project} run={currentRun} version={version} refresh={refresh} /> : <Empty title="质检从第一张真实关键帧开始"><p>五维判定规则已就绪。完成分镜审核并接入图像与视觉模型后，将在此展示真实报告和人工复核记录。</p><button className="button" onClick={() => setTab('storyboard')}>查看分镜准备情况</button></Empty>}
        </>}
        <footer className="page-footer">分镜流水线 <span>每一次生成，都有依据。</span></footer>
      </div>
    </main>
    {modal && <Dialog title={{project: '创建新项目', character: '建立角色档案', edit: '编辑角色特征', confirm: '确认角色版本', preview: '锚定提示词预览'}[modal]} busy={busy} close={() => setModal(null)}>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {modal === 'project' && <form onSubmit={createProject}><div className="form-body"><label>项目名称<input name="title" required maxLength={200} placeholder="例如：山海拾遗 · 第一集" autoFocus /></label><div className="form-row"><label>画风<input name="style" required maxLength={80} defaultValue="日系厚涂" /></label><label>项目预算（元）<input name="budget" type="number" min="0" max="200" step="0.01" required defaultValue="200.00" /></label></div><label>故事概要 / 剧本<textarea name="synopsis" rows={5} maxLength={15000} placeholder="这段故事发生在哪里，谁将做出怎样的选择？" /></label><p className="form-hint">创建项目不会调用模型或产生费用。</p></div><div className="dialog-actions"><button type="button" className="button" disabled={busy} onClick={() => setModal(null)}>取消</button><button className="button primary" disabled={busy}>{busy ? '正在创建…' : '创建并准备角色'}</button></div></form>}
      {(modal === 'character' || modal === 'edit') && <form onSubmit={saveCharacter}><div className="form-body"><label>角色名称<input name="name" required maxLength={80} autoFocus defaultValue={modal === 'edit' ? character?.name : ''} placeholder="例如：林晚" /></label><p className="form-hint">每行填写一项：名称=描述。建议用明确的形状、颜色和服饰细节。</p><div className="feature-form">{groups.map(([key, label, placeholder]) => <label key={key}>{label}<textarea name={key} rows={3} placeholder={placeholder} defaultValue={modal === 'edit' && character ? Object.entries(character[key]).map(([k, v]) => `${k}=${v}`).join('\n') : ''} /></label>)}</div>{modal === 'edit' && <p className="warning-note">保存后版本递增，已有确认将失效，需要重新审核。</p>}</div><div className="dialog-actions"><button type="button" className="button" disabled={busy} onClick={() => setModal(null)}>取消</button><button className="button primary" disabled={busy}>{busy ? '正在保存…' : '保存角色档案'}</button></div></form>}
      {modal === 'confirm' && character && <form onSubmit={confirmCharacter}><div className="form-body"><p>正在确认 <strong>{character.name} · v{character.anchor_version}</strong></p><div className="prompt"><p>{character.anchor_prompt}</p></div><label>确认人<input name="reviewer" required maxLength={80} placeholder="填写你的姓名或称呼" autoFocus /></label><label className="checkbox-label"><input type="checkbox" required />我已核对当前角色特征，确认该版本可用于后续分镜准备。</label><p className="form-hint">本次操作会保存审核快照；版本变化时需重新确认。</p></div><div className="dialog-actions"><button className="button" type="button" disabled={busy} onClick={() => setModal(null)}>返回检查</button><button className="button primary" disabled={busy || !run}>{busy ? '正在确认…' : '确认当前版本'}</button></div></form>}
      {modal === 'preview' && preview && <div className="form-body"><h3>角色锚定描述</h3><div className="prompt"><p>{preview.anchor_prompt}</p></div><h3>负面提示词</h3><div className="prompt"><p>{preview.negative_prompt || '尚未设置'}</p></div><p className="form-hint">由已保存的结构化特征自动生成，同一输入保持相同描述。</p></div>}
    </Dialog>}
  </div>
}
