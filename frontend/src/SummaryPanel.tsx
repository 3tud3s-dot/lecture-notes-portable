import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { timestamp } from './courses'
import type { CurrentSummary, SummaryState } from './live'

const version = (value: CurrentSummary | null) => value ? `${value.mode ?? 'legacy'}-${value.request_id}-${value.trigger}-${value.end_time}` : 'empty'

export function SummaryPanel({ current, history, state, enabled, busy }: {
  current: CurrentSummary | null; history: CurrentSummary[]; state?: SummaryState; enabled: boolean; busy: boolean
}) {
  const incoming = current ?? history.at(-1) ?? null
  const incomingVersion = version(incoming)
  const [shown, setShown] = useState(incoming)
  const [switching, setSwitching] = useState(false)
  const scroll = useRef<HTMLDivElement>(null)
  const reading = useRef<{ top: number; text?: string; offset?: number } | null>(null)
  const [atTop, setAtTop] = useState(true)
  useEffect(() => {
    if (version(incoming) === version(shown)) return
    if (!incoming || !shown) { setShown(incoming); setSwitching(false); return }
    const area = scroll.current
    if (area) {
      const paragraphs = [...area.querySelectorAll<HTMLElement>('.summary-paragraph')]
      const anchor = paragraphs.find(p => p.offsetTop + p.offsetHeight > area.scrollTop)
      reading.current = { top: area.scrollTop, text: anchor?.querySelector('p')?.textContent ?? undefined,
        offset: anchor ? anchor.offsetTop - area.scrollTop : undefined }
    }
    setSwitching(true)
    const delay = matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 120
    const timer = setTimeout(() => { setShown(incoming); setSwitching(false) }, delay)
    return () => clearTimeout(timer)
  }, [incomingVersion])
  useLayoutEffect(() => {
    const area = scroll.current
    const position = reading.current
    if (!area || !position) return
    const anchor = [...area.querySelectorAll<HTMLElement>('.summary-paragraph')].find(
      p => p.querySelector('p')?.textContent === position.text)
    area.scrollTop = anchor && position.offset !== undefined ? anchor.offsetTop - position.offset : position.top
    reading.current = null
    setAtTop(area.scrollTop < 30)
  }, [shown])
  const updating = state?.status === 'updating'
  const failed = state?.status === 'failed'
  const cumulative = shown?.mode === 'cumulative'
  return <aside className="summary-panel panel" aria-labelledby="summary-title">
    <header className="panel-header"><div className="panel-heading"><h2 id="summary-title">课堂摘要</h2></div><span className="label-badge">SUMMARY</span></header>
    <div className="summary-live-status" role="status">{updating ? '正在整合新内容…' : failed ? '本次更新未成功，上一版摘要已保留' : shown ? (shown.trigger === 'final' ? '本轮摘要已完成' : '已整合至最新内容') : enabled ? (busy ? '等待课堂原话…' : '开始课堂后逐步生成') : 'Summary 未启用'}</div>
    {state?.error && <p className="summary-warning">摘要更新遇到问题，已有摘要和 RAW 均已保留。</p>}
    {shown ? <div className="summary-document-scroll" ref={scroll} role="region" aria-label="完整课堂摘要" tabIndex={0} onScroll={() => setAtTop((scroll.current?.scrollTop ?? 0) < 30)}>
      {!cumulative && <p className="summary-legacy-note">这是旧版局部摘要；新课堂将使用逐步整合的完整摘要。历史结果保持原样。</p>}
      {Boolean(shown.missing_segment_ids?.length) && <p className="summary-legacy-note">部分 RAW 转录不可用，摘要未补写缺失内容。</p>}
      <article className={`summary-document ${switching ? 'is-switching' : ''}`} data-summary-id={shown.request_id} data-summary-mode={shown.mode ?? 'legacy'} aria-busy={updating || switching}>
        <div className="summary-window-label">{cumulative ? `已整合至 ${timestamp(Math.floor(shown.end_time))}` : `${timestamp(Math.floor(shown.start_time))} — ${timestamp(Math.floor(shown.end_time))}`}</div>
        <h3 className="current-topic">{shown.summary.topic}</h3>
        <div className="summary-paragraphs">{shown.summary.points.map((point, i) => <section className="summary-paragraph" key={i}>
          <p>{point.text}</p><span>来源 {point.source_segment_ids.map(id => `#${id}`).join(' · ')}</span>
        </section>)}</div>
      </article>
    </div> : <div className="summary-placeholder"><h3>一份随课堂推进的摘要</h3><p>{enabled ? '保留前文脉络，将新内容逐步融入，篇幅缓慢增长。' : 'Summary will be added later.'}</p><span className="outline-badge">LECTURE → ONE EVOLVING SUMMARY</span></div>}
    <div className="summary-footnote"><span>完整摘要逐步更新 · 历史版本保留</span>{shown && !atTop && <button className="summary-follow" onClick={() => { if (scroll.current) scroll.current.scrollTop = 0; setAtTop(true) }}>回到开头 ↑</button>}</div>
  </aside>
}
