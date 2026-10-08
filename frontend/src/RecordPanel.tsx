import { useEffect, useRef, useState } from 'react'
import { timestamp } from './courses'
import type { LiveState, RawSegment } from './live'

export function RecordPanel({ rawSegments, status, progress }: { rawSegments: RawSegment[]; status: LiveState['status']; progress?: LiveState['asr_progress'] }) {
  const [follow, setFollow] = useState(true)
  const scroller = useRef<HTMLDivElement>(null)
  const busy = status !== 'Idle'
  useEffect(() => {
    if (follow && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight
  }, [rawSegments.length, follow])
  return <section className="transcript-panel panel" aria-labelledby="transcript-title">
    <header className="panel-header"><div className="panel-heading"><h2 id="transcript-title">课堂完整记录</h2><span className="selected-view">RAW</span></div></header>
    {progress && <div className="live-notice" role="status">第 {progress.segment_id+1} 段：{progress.attempt > 1 ? `网络或服务暂时异常，${progress.status === 'backoff' ? '等待重试' : '正在重试'}（第 ${progress.attempt}/${progress.max_attempts} 次请求）` : '正在转录…'}。原始音频已保留。</div>}
    <div className="transcript-scroll" ref={scroller} tabIndex={0} role="region" aria-label="可滚动的课堂记录" onScroll={() => {
      const e = scroller.current
      if (e && e.scrollHeight - e.clientHeight - e.scrollTop > 70) setFollow(false)
    }}>
      <div className="transcript-date"><span/>RAW transcript<span/></div>
      {rawSegments.map(segment => <article className="segment" key={segment.segment_id}>
        <div className="segment-meta"><time>{timestamp(Math.floor(segment.start_time))} — {timestamp(Math.floor(segment.end_time))}</time><span>SEGMENT {String(segment.segment_id).padStart(2, '0')}</span></div>
        {segment.source_name && <div className="segment-source" title={segment.source_name}>来源：{segment.source_name} · {timestamp(Math.floor(segment.source_start_time ?? 0))}</div>}
        <p className={`raw-text ${segment.status === 'failed' ? 'segment-failed' : ''}`}>{segment.raw_text ?? '本段转录失败，原始音频已保留。'}</p>
      </article>)}
      <div className="transcript-end">{status === 'Paused' ? '已暂停采集，后台仍会处理暂停前的片段。' : status === 'Pausing' ? '正在暂停并提交尾段…' : !rawSegments.length ? (busy ? '正在等待第一段 RAW 转录。' : '准备好后开始课堂，原始转录会按时间顺序出现在这里。') : status === 'Recording' || status === 'Importing' ? '正在等待下一段 RAW 转录…' : status === 'Processing' ? '尾段与后台队列仍在处理，可先保存已有记录。' : '本轮记录已结束'}</div>
    </div>
    <footer className="transcript-footer"><span>{rawSegments.length} 个片段 <span className="footer-dot">·</span> RAW 原始记录</span><button className={follow ? 'follow active' : 'follow'} onClick={() => setFollow(v => !v)} aria-pressed={follow}>↓ 跟随最新</button></footer>
  </section>
}
