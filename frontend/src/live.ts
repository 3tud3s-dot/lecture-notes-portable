import { useEffect, useRef, useState } from 'react'

export type RawSegment = { source_name?:string; source_file_index?:number; source_start_time?:number; segment_id: number; start_time: number; end_time: number; raw_text: string | null; status: string; error: string | null; http_status: number | null }
export type CurrentSummary = { summary: { topic: string; topic_source_segment_ids: number[]; points: { text: string; source_segment_ids: number[] }[] }; source_segment_ids: number[]; start_time: number; end_time: number; trigger: string; request_id: number; mode?: 'cumulative'; length_budget_chars?: number; missing_segment_ids?: number[] }
export type MicrophoneInfo = { device_index:number|null;device_name:string|null;host_api:string|null;microphone_status:'selected'|'unavailable';selection_mode?:string|null }
export type SummaryState = { enabled: boolean; status: string; pending: boolean; error: string | null }
export type LiveState = { revision: number; asr_progress?: { segment_id: number; attempt: number; max_attempts: number; status: string } | null; processing_phase?: string | null; record_sync_error?: string | null; source_kind?: 'microphone'|'import'; source_files?: {name:string;duration:number}[]; import_progress?: {file_index:number;file_count:number;filename:string;decoded_seconds:number;total_seconds:number}|null; captured_duration?: number; status: 'Idle' | 'Starting' | 'Recording' | 'Pausing' | 'Paused' | 'Importing' | 'Processing'; session_id: string | null; course_id: string | null; segments: RawSegment[]; error: string | null; summary: { duration: number; chunks: number; queue_depth: number } | null; stop_requested: boolean; current_summary?: CurrentSummary | null; summary_history?: CurrentSummary[]; summary_state?: SummaryState }
export const initialState: LiveState = { revision: -1, status: 'Idle', session_id: null, course_id: null, segments: [], error: null, summary: null, stop_requested: false }
const backend = 'http://127.0.0.1:8765'
export const activeStatus = (status: LiveState['status']) => status !== 'Idle'

export function useLiveSession() {
  const [state, setState] = useState<LiveState>(initialState)
  const [connection, setConnection] = useState<'connecting' | 'connected' | 'disconnected'>('connecting')
  const [actionError, setActionError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [maximumDuration, setMaximumDuration] = useState<number | null>(null)
  const [summaryEnabled, setSummaryEnabled] = useState(false)
  const [microphone, setMicrophone] = useState<MicrophoneInfo | null>(null)
  const revision = useRef(-1)
  const apply = (next: LiveState) => {
    if (next.revision <= revision.current) return
    revision.current = next.revision
    // A replayed full snapshot replaces by ID, never appends a second copy.
    const byId = new Map(next.segments.map(s => [s.segment_id, s]))
    const summaries = new Map((next.summary_history ?? (next.current_summary ? [next.current_summary] : [])).map(s => [s.request_id, s]))
    setState({ ...next, segments: [...byId.values()].sort((a, b) => a.segment_id - b.segment_id),
      summary_history: [...summaries.values()].sort((a, b) => a.request_id - b.request_id) })
  }
  useEffect(() => {
    const events = new EventSource(`${backend}/api/events`)
    events.onopen = () => { revision.current = -1; setConnection('connected') }
    events.addEventListener('snapshot', e => {
      try { apply(JSON.parse((e as MessageEvent).data)) }
      catch { setActionError('invalid_backend_event') }
    })
    events.onerror = () => setConnection('disconnected')
    const abort = new AbortController()
    fetch(`${backend}/api/health`, { signal: abort.signal }).then(r => r.json()).then(r => {
      setMaximumDuration(r.configuration.maximum_duration)
      setSummaryEnabled(r.configuration.summary_enabled === true)
      setMicrophone(r.configuration)
    }).catch(() => {})
    return () => { events.close(); abort.abort() }
  }, [])
  useEffect(() => {
    if (!activeStatus(state.status)) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    addEventListener('beforeunload', warn)
    return () => removeEventListener('beforeunload', warn)
  }, [state.status])
  const action = async (path: string, body: object) => {
    setPending(true); setActionError(null)
    try {
      const response = await fetch(`${backend}/api/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Lecture-Client': 'browser' }, body: JSON.stringify(body) })
      const data = await response.json()
      if (!response.ok) { setActionError(data.error ?? 'backend_request_failed'); return false }
      apply(data); return true
    } catch { setActionError('backend_unavailable'); return false }
    finally { setPending(false) }
  }
  return { state, connection, pending, maximumDuration, microphone, summaryEnabled: state.summary_state?.enabled ?? summaryEnabled, actionError,
    start: (courseId: string) => action('start', { course_id: courseId, confirmed: true }),
    stop: () => action('stop', { session_id: state.session_id }),
    pause: () => action('pause', { session_id: state.session_id }),
    resume: () => action('resume', { session_id: state.session_id }),
    importFiles: (courseId:string,batchId:string,fileIds:string[]) => action('import/start', { course_id:courseId, confirmed:true, batch_id:batchId, file_ids:fileIds }) }
}
export type LiveSession = ReturnType<typeof useLiveSession>

export function errorText(code: string | null) {
  if (!code) return null
  const messages: Record<string, string> = {
    session_already_active: '已有一轮课堂正在运行，请先停止并等待处理完成。',
    microphone_configuration_unavailable: '指定麦克风或参数不可用。请检查系统输入设备与录音权限，系统不会切换到其他设备。',
    asr_configuration_unavailable: 'ASR 配置不可用，请在本机检查 API 配置。',
    frozen_asr_configuration_required: 'ASR 模型或地址与已验证配置不一致。',
    summary_configuration_unavailable: 'Summary 配置或当前课程参考不可用；尚未打开麦克风。请检查本地配置。',
    session_failed_audio_and_results_preserved: '本轮发生错误。已经采集的音频与转录证据已保留，请检查本地结果。',
    some_segments_failed_audio_preserved: '部分片段仍未转录成功；其他结果与原始音频已保留。',
    media_duration_exceeded: '所选音频总时长超过 100 分钟。',
    media_selection_expired: '文件选择已失效，请重新选择。',
    media_source_changed: '所选文件已移动或发生变化，请重新选择。',
    media_invalid_file: '请移除无效文件后再导入。',
    media_tools_missing: '本地 FFmpeg 组件缺失，暂时无法导入音视频。',
    media_decode_failed: '音频解码失败，已处理的内容已保留。',
    media_empty_audio: '文件没有可解码的音频内容。',
    media_incomplete_pcm: '音频数据不完整，已保留处理结果。',
    invalid_media_selection: '文件选择无效，请重新选择。',
    cannot_pause: '当前状态不能暂停，请等待录音开始。',
    cannot_resume: '请等待暂停完成后再继续。',
    stale_session: '课堂状态已更新，请等待连接同步后再操作。',
    backend_unavailable: '无法连接本地后端。请确认后端已启动，录音状态以重新连接后的结果为准。',
  }
  return messages[code] ?? '操作未完成，请检查本地后端状态。'
}
