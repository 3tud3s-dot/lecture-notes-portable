import { useEffect, useRef, useState } from 'react'
import { RecordPanel } from './RecordPanel'
import { SummaryPanel } from './SummaryPanel'
import { timestamp, type Course } from './courses'
import { defaultRecordName, fetchRecord, recordError, type RecordMetadata, type SavedRecord, type SavedRecords } from './records'
import type { LiveState } from './live'

function NameDialog({ title, initial, pending, onSave, onClose }: {
  title: string; initial: string; pending: boolean; onSave: (name: string) => Promise<void>; onClose: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const [name, setName] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { dialog.current?.showModal(); input.current?.select() }, [])
  return <dialog className="start-dialog record-name-dialog" ref={dialog} onCancel={e => { if (pending) e.preventDefault(); else onClose() }} aria-labelledby="record-name-title">
    <form onSubmit={async e => {
      e.preventDefault(); setError(null)
      try { await onSave(name.trim()); onClose() } catch (err) { setError(recordError(err)) }
    }}>
      <span className="section-eyebrow">CLASSROOM RECORD</span><h2 id="record-name-title">{title}</h2>
      <label htmlFor="record-name">记录名称</label><input id="record-name" ref={input} value={name} onChange={e => setName(e.target.value)} required maxLength={100} disabled={pending}/>
      <p>保存在本机，可从“课堂记录”重新打开。</p>
      {error && <p className="record-error" role="alert">{error}</p>}
      <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose} disabled={pending}>取消</button><button type="submit" className="primary-button" disabled={pending || !name.trim()}>{pending ? '正在保存…' : '保存'}</button></div>
    </form>
  </dialog>
}

export function SaveRecordControl({ course, state, store, connected }: { course: Course; state: LiveState; store: SavedRecords; connected: boolean }) {
  const [dialog, setDialog] = useState(false)
  const belongs = state.course_id === course.course_id
  const saved = belongs ? store.records.find(r => r.record_id === state.session_id) : undefined
  const ready = belongs && (state.status === 'Idle' || state.status === 'Processing' || state.status === 'Paused' || state.status === 'Importing') && Boolean(state.session_id && state.segments.length)
  return <div className="save-record-control">
    {saved ? <><span className="record-saved-badge">{state.status === 'Processing' ? '已保存 · 后台补全中' : state.status !== 'Idle' ? '已保存 · 课堂未结束' : '已保存'}</span><a className="secondary-button" href={`#/record/${saved.record_id}`}>查看记录</a><button className="secondary-button" onClick={() => setDialog(true)} disabled={!connected || store.pending}>重命名</button></>
      : <button className="secondary-button" onClick={() => setDialog(true)} disabled={!ready || !connected || store.pending} title="暂停或停止后即可保存，后续结果自动补入；请保持后端运行">保存记录</button>}
    {saved && state.record_sync_error && <button className="secondary-button" onClick={() => setDialog(true)}>重试保存</button>}
    {dialog && <NameDialog title={saved ? '重命名记录' : '保存课堂记录'} initial={saved?.name ?? defaultRecordName(course.display_name+(state.source_kind === 'import' ? ' · 导入' : ''), state.session_id)} pending={store.pending} onClose={() => setDialog(false)} onSave={async name => {
      if (saved) { if (state.record_sync_error && state.session_id) await store.save(state.session_id, name); await store.rename(saved.record_id, name) }
      else if (state.session_id) await store.save(state.session_id, name)
    }}/>}
  </div>
}

export function RecordLibrary({ store }: { store: SavedRecords }) {
  const [renaming, setRenaming] = useState<RecordMetadata | null>(null)
  return <main className="library record-library" id="main-content" tabIndex={-1}>
    <div className="section-eyebrow">SAVED CLASSROOMS</div><div className="library-intro"><div><h1>课堂记录</h1><p>保留课堂原话与摘要，随时回来继续阅读。</p></div><button className="secondary-button" onClick={() => void store.refresh()} disabled={store.loading}>刷新列表</button></div>
    {store.error && <div role="alert" className="live-notice warning">{store.error}</div>}
    {store.unreadable > 0 && <div role="alert" className="live-notice warning">{store.unreadable} 份本地记录暂时无法读取，原文件已保留。</div>}
    {!store.records.length ? <div className="records-empty">{store.loading ? '正在读取记录…' : '还没有已保存的课堂。停止课堂后，点击“保存记录”。'}</div>
      : <div className="saved-record-list">{store.records.map(record => <article className="saved-record-card" key={record.record_id}>
        <a href={`#/record/${record.record_id}`} className="saved-record-link"><span className="section-eyebrow">{record.course_name} · {record.source_kind === 'import' ? '导入音视频' : '实时课堂'}</span><h2>{record.name}</h2><p>{new Date(record.created_at).toLocaleString('zh-CN')} · {timestamp(Math.floor(record.duration))} · {record.segment_count} 个 RAW 片段</p></a>
        <button className="secondary-button" onClick={() => setRenaming(record)} disabled={store.pending}>重命名</button>
      </article>)}</div>}
    <a className="back-link records-back" href="#/">返回全部课程</a>
    {renaming && <NameDialog title="重命名记录" initial={renaming.name} pending={store.pending} onClose={() => setRenaming(null)} onSave={async name => { await store.rename(renaming.record_id, name) }}/>}
  </main>
}

export function SavedRecordPage({ id, store }: { id: string; store: SavedRecords }) {
  const [record, setRecord] = useState<SavedRecord | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const abort = new AbortController()
    setRecord(null); setError(null)
    fetchRecord(id, abort.signal).then(value => { if (!abort.signal.aborted) setRecord(value) })
      .catch(err => { if (!abort.signal.aborted) setError(recordError(err)) })
    return () => abort.abort()
  }, [id, retry])
  useEffect(() => {
    if (!record?.processing_pending) return
    const abort = new AbortController()
    const timer = window.setInterval(() => {
      fetchRecord(id, abort.signal).then(value => { if (!abort.signal.aborted) setRecord(value) })
        .catch(() => {})
    }, 2000)
    return () => { window.clearInterval(timer); abort.abort() }
  }, [id, record?.processing_pending])
  return <main className="classroom saved-record-page" id="main-content" tabIndex={-1}>
    <a className="back-link" href="#/records">← 课堂记录</a>
    {error ? <div className="live-notice warning" role="alert">{error}<button className="secondary-button" onClick={() => setRetry(r => r+1)}>重试</button></div>
      : !record ? <p>正在读取课堂记录…</p> : <>
        <div className="classroom-heading"><div className="saved-record-heading"><div className="section-eyebrow">{record.course_name} · 已保存</div><h1>{record.name}</h1></div><div className="class-actions"><button className="secondary-button" onClick={() => setRenaming(true)} disabled={store.pending}>重命名</button><a className="secondary-button" href={`#/course/${record.course_id}`}>返回课程</a></div></div>
        <div className="session-bar"><span>本地课堂记录</span><span>{timestamp(Math.floor(record.duration))} · {record.segment_count} 个片段</span></div>
        {record.processing_pending && <div className="live-notice">已保存当前内容，后台仍在补全转录或摘要。请保持后端运行；本页会自动更新。</div>}
        {record.snapshot.error && <div className="live-notice warning">本次课堂包含失败片段；原始结果保持原样。</div>}
        <div className="workspace"><RecordPanel key={`raw-${id}`} rawSegments={record.snapshot.segments} status="Idle"/><SummaryPanel key={`summary-${id}`} current={record.snapshot.current_summary ?? null} history={record.snapshot.summary_history ?? []} state={record.snapshot.summary_state} enabled={record.snapshot.summary_state?.enabled ?? false} busy={false}/></div>
        {renaming && <NameDialog title="重命名记录" initial={record.name} pending={store.pending} onClose={() => setRenaming(false)} onSave={async name => setRecord(await store.rename(id, name))}/>}
      </>}
  </main>
}
