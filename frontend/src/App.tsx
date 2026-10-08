import { ImportDialog } from './ImportDialog'
import { RecordPanel } from './RecordPanel'
import { SummaryPanel } from './SummaryPanel'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useLiveSession, activeStatus, errorText, type LiveSession } from './live'
import { courses, coursePresentation, timestamp, type Course } from './courses'
import { useSavedRecords, type SavedRecords } from './records'
import { RecordLibrary, SavedRecordPage, SaveRecordControl } from './SavedRecords'

function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    book: <><path d="M3 4h5a4 4 0 0 1 4 4v13a5 5 0 0 0-5-3H3z"/><path d="M21 4h-5a4 4 0 0 0-4 4v13a5 5 0 0 1 5-3h4z"/></>,
    grid: <><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></>,
    chip: <><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/><path d="M10 10h4v4h-4z"/></>,
    chart: <><path d="M4 3v17h17M7 14l4-5 4 3 6-8"/><path d="M17 4h4v4"/></>,
    nodes: <><circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="5" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="m7.5 6 9-1M6 8l5 8m7-9-5 9"/></>,
    sliders: <><path d="M5 3v5m0 4v9M12 3v11m0 4v3M19 3v2m0 4v12"/><path d="M2 8h6v4H2zm7 6h6v4H9zm7-9h6v4h-6z"/></>,
    bolt: <path d="m13 2-9 12h7l-1 8 10-13h-8z"/>,
    arrow: <path d="M5 12h14m-5-5 5 5-5 5"/>,
    back: <path d="M19 12H5m5-5-5 5 5 5"/>,
    play: <path d="m8 5 11 7-11 7z"/>,
    pause: <><path d="M8 5v14M16 5v14"/></>,
    stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
    lines: <><path d="M6 4h12M6 9h12M6 14h8M6 19h10"/></>,
    clock: <><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/></>,
    down: <path d="M12 4v16m-5-5 5 5 5-5"/>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.book}</svg>
}

function useRoute() {
  const [route, setRoute] = useState(location.hash || '#/')
  useEffect(() => { const update = () => setRoute(location.hash || '#/'); addEventListener('hashchange', update); return () => removeEventListener('hashchange', update) }, [])
  return route
}

function Library() {
  return <main className="library" id="main-content" tabIndex={-1}>
    <div className="section-eyebrow"><span className="small-line"/>YOUR CLASSROOM, IN FOCUS</div>
    <div className="library-intro"><div><h1>选择课程，开始专注。</h1><p>给课堂中的每一个想法，留一个可以回来的地方。</p></div><span className="course-count">{courses.length.toString().padStart(2, '0')} <span>门课程</span></span></div>
    <div className="section-label"><h2>我的课程</h2><span>选择一门课程进入课堂 <Icon name="arrow" size={15}/></span></div>
    <div className="course-grid">{courses.map((course, i) => {
      const presentation = coursePresentation[course.course_id]
      return <a className="course-card" key={course.course_id} href={`#/course/${course.course_id}`}>
        <div className="card-top"><span className="course-icon"><Icon name={presentation?.glyph ?? 'book'} size={27}/></span><span className="course-number">{String(i + 1).padStart(2, '0')}</span></div>
        <div className="card-copy"><h3>{course.display_name}</h3><div className="course-english">{presentation?.english ?? 'COURSE'}</div><p>{presentation?.topic ?? '课程记录工作区'}</p></div>
        <div className="card-bottom"><span><span className="ready-dot"/>课程参考已就绪</span><span className="card-arrow"><Icon name="arrow" size={19}/></span></div>
      </a>
    })}</div>
    <div className="library-note"><Icon name="book" size={24}/><div><strong>先记录，再理解。</strong><p>进入课堂，准备好后开始录音，按时间顺序保留原始转录。</p></div><span className="outline-badge">RAW TRANSCRIPT</span></div>
    <footer className="library-footer"><span>课记 · Lecture Notes</span><span>为专注的课堂而设计</span></footer>
  </main>
}

function Classroom({ course, live, records }: { course: Course; live: LiveSession; records: SavedRecords }) {
  const { state, connection, pending, maximumDuration, summaryEnabled, microphone } = live
  const status = state.status
  const belongs = state.course_id === course.course_id
  const visible = belongs ? state.segments : []
  const busy = activeStatus(status)
  const [importing, setImporting] = useState(false)
  const isImport = belongs && state.source_kind === 'import'
  const [confirm, setConfirm] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { if (confirm) dialog.current?.showModal() }, [confirm])
  const closeDialog = () => { dialog.current?.close(); setConfirm(false) }
  const start = () => { closeDialog(); void live.start(course.course_id) }
  const elapsed = belongs ? (state.captured_duration ?? state.summary?.duration ?? visible.at(-1)?.end_time ?? 0) : 0
  const message = errorText(live.actionError || state.error)
  return <main className="classroom" id="main-content" tabIndex={-1}>
    <a className="back-link" href="#/"><Icon name="back" size={15}/>全部课程</a>
    <div className="classroom-heading"><div><div className="section-eyebrow">CLASSROOM WORKSPACE</div><h1>{course.display_name}</h1></div><div className="class-actions"><SaveRecordControl course={course} state={state} store={records} connected={connection === 'connected'}/><button className="secondary-button" onClick={() => setImporting(true)} disabled={busy || pending || connection !== 'connected'}>导入音视频</button><button className="primary-button" onClick={() => setConfirm(true)} disabled={busy || pending || connection !== 'connected' || microphone?.microphone_status !== 'selected'}><Icon name="play" size={16}/>开始课堂</button>{!isImport && <button className="secondary-button" onClick={() => void (status === 'Paused' ? live.resume() : live.pause())} disabled={!belongs || !['Recording','Paused'].includes(status) || pending || connection !== 'connected'}><Icon name={status === 'Paused' ? 'play' : 'pause'} size={15}/>{status === 'Paused' ? '继续' : status === 'Pausing' ? '暂停中…' : '暂停'}</button>}<button className="secondary-button" onClick={() => void live.stop()} disabled={!busy || state.stop_requested || pending || connection !== 'connected'}><Icon name="stop" size={15}/>停止</button></div></div>
    <div className="session-bar"><span className={`status status-${status.toLowerCase()}`} role="status"><span className="status-dot"/>{status}</span><span className="session-divider"/><span className="demo-note">{status === 'Starting' ? (isImport ? '正在准备文件转录…' : '正在检查设备并打开麦克风…') : status === 'Importing' ? `正在导入 ${state.import_progress ? `${state.import_progress.file_index+1}/${state.import_progress.file_count} · ${state.import_progress.filename}` : '音视频'}，可随时停止或保存已有内容。` : status === 'Recording' ? '正在录音 · 每 12 秒返回一段 RAW 转录' : status === 'Pausing' ? '正在关闭麦克风并提交暂停前的尾段…' : status === 'Paused' ? '麦克风已暂停，尾段仍会转录；点击继续恢复，暂停不计时。' : status === 'Processing' ? (state.processing_phase === 'summary' ? 'RAW 已处理完，摘要仍在后台更新；可先保存记录。' : '正在处理尾段与转录队列；可先保存已有记录，后续结果自动补入。') : '点击开始，准备好后再确认录音'}</span><span className="session-time"><Icon name="clock" size={15}/>音频进度 {timestamp(Math.floor(elapsed))}</span></div>
    {connection === 'connected' && microphone?.microphone_status === 'unavailable' && <div className="live-notice" role="alert">未找到可用麦克风。请检查系统输入设备，再重启后端；音视频文件导入仍可使用。</div>}
    {connection !== 'connected' && <div className="live-notice" role="alert">{connection === 'connecting' ? '正在连接本地后端…' : '连接中断，正在重连。后端可能仍在录音；请保持后端窗口运行。'}</div>}
    {message && <div className="live-notice warning" role="alert">{message}</div>}
    {busy && !belongs && <div className="live-notice">另一门课程正在运行。<a href={`#/course/${state.course_id}`}>返回正在录制的课程</a></div>}
    {state.record_sync_error && <div role="alert" className="live-notice warning">保存记录自动更新失败；音频和转录证据仍保留，请再次点击保存记录。</div>}
    <div className="workspace">
      <RecordPanel key={`${course.course_id}-${state.session_id ?? "idle"}`} rawSegments={visible} status={status} progress={belongs ? state.asr_progress : null}/>
      <SummaryPanel key={`summary-${course.course_id}-${state.session_id ?? 'idle'}`} current={belongs ? state.current_summary ?? null : null} history={belongs ? state.summary_history ?? [] : []} state={belongs ? state.summary_state : undefined} enabled={summaryEnabled} busy={busy && belongs}/>
    </div>
    {importing && <ImportDialog courseId={course.course_id} live={live} onClose={() => setImporting(false)}/>}
    {confirm && <dialog ref={dialog} className="start-dialog" onCancel={() => setConfirm(false)} aria-labelledby="start-title">
      <span className="section-eyebrow">READY TO START</span><h2 id="start-title">准备好开始课堂了吗？</h2>
      <p>确认后将打开所选麦克风，并把录音片段发送给 SiliconFlow 进行转录。</p>
      {summaryEnabled && <p>上一版完整摘要与尚未整合的新增 RAW 将发送给 SiliconFlow，逐步更新一份连贯课堂摘要。每新增 2 段更新，停止后整合剩余内容。</p>}
      <dl><dt>课程</dt><dd>{course.display_name}</dd><dt>麦克风</dt><dd>{microphone?.microphone_status === 'selected' ? `${microphone.device_name} · ${microphone.host_api} · 设备 ${microphone.device_index}` : '未找到可用输入设备，请先配置麦克风'}</dd><dt>采集</dt><dd>44100 Hz · 单声道 PCM16 · 12 秒分段</dd><dt>本轮时长上限</dt><dd>{maximumDuration === null ? '—' : maximumDuration / 60} 分钟，暂停不计时，可随时停止</dd></dl>
      <p className="dialog-hint">看到 Recording 后正常讲话。暂停会转录不足 12 秒的尾段；停止后可先保存，后台结果自动补入。</p>
      <div className="dialog-actions"><button className="secondary-button" onClick={closeDialog}>取消</button><button className="primary-button" onClick={start}>准备好了，开始录音</button></div>
    </dialog>}
  </main>
}

export default function App() {
  const live = useLiveSession()
  const records = useSavedRecords(live.connection)
  const route = useRoute()
  const selected = courses.find(c => route === `#/course/${c.course_id}`)
  const home = route === '#/' || route === '#'
  const recordList = route === '#/records'
  const savedId = route.startsWith('#/record/') ? route.slice('#/record/'.length) : null
  useEffect(() => { document.title = selected ? `${selected.display_name} · 课记` : recordList || savedId ? '课堂记录 · 课记' : '课记 · 课堂转录工作区' }, [selected, recordList, savedId])
  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={e => { e.preventDefault(); document.getElementById('main-content')?.focus() }}>跳转到内容</a>
    <aside className="sidebar"><a className="brand" href="#/" aria-label="课记，返回全部课程"><span className="brand-mark"><Icon name="book" size={23}/></span><span>课记<small>LECTURE NOTES</small></span></a>
      <div className="nav-caption">工作区</div><a className={`overview-link ${home ? 'nav-active' : ''}`} href="#/" aria-current={home ? 'page' : undefined}><Icon name="grid" size={18}/>全部课程<span>{courses.length}</span></a>
      <a className={`overview-link saved-records-nav ${recordList || savedId ? 'nav-active' : ''}`} href="#/records" aria-current={recordList || savedId ? 'page' : undefined}><Icon name="lines" size={18}/>课堂记录<span>{records.records.length}</span></a>
      <div className="nav-caption courses-caption">我的课程</div><nav aria-label="课程导航">{courses.map(course => <a key={course.course_id} className={`nav-course ${selected?.course_id === course.course_id ? 'nav-active' : ''}`} href={`#/course/${course.course_id}`} aria-current={selected?.course_id === course.course_id ? 'page' : undefined}><Icon name={coursePresentation[course.course_id]?.glyph ?? 'book'} size={17}/><span>{course.display_name}</span></a>)}</nav>
      <div className="sidebar-bottom"><span className="workspace-avatar"><Icon name="book" size={17}/></span><div>课堂工作区<small>Frontend v1 · 本地课堂</small></div><span className="ready-dot"/></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div><span>工作区</span><span className="breadcrumb-slash">/</span><strong>{selected ? '课堂记录' : recordList || savedId ? '已保存的课堂' : '全部课程'}</strong></div><div className="topbar-actions"><a href="#/records" className="mobile-records-link">课堂记录</a><span className="preview-label"><span className="status-dot"/>{live.connection === 'connected' ? '后端已连接' : '等待后端连接'}</span></div></header>
      {activeStatus(live.state.status) && !selected && <div className="live-notice global-session">课堂仍在运行。<a href={`#/course/${live.state.course_id}`}>返回当前课堂</a><button onClick={() => void live.stop()} disabled={live.state.stop_requested || live.pending}>停止本轮</button></div>}
      {selected ? <Classroom key={selected.course_id} course={selected} live={live} records={records}/> : recordList ? <RecordLibrary store={records}/> : savedId ? <SavedRecordPage key={savedId} id={savedId} store={records}/> : home ? <Library/> : <main className="not-found" id="main-content" tabIndex={-1}><h1>没有找到这门课程</h1><p>请从课程列表重新选择。</p><a className="primary-button" href="#/">返回全部课程</a></main>}
    </div>
  </div>
}
