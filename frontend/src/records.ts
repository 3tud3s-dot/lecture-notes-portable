import { useCallback, useEffect, useState } from 'react'
import type { LiveState } from './live'

const backend = 'http://127.0.0.1:8765'
export type RecordMetadata = { source_kind?: 'microphone'|'import'; record_id: string; name: string; course_id: string; course_name: string; created_at: string; updated_at: string; duration: number; segment_count: number }
export type SavedRecord = RecordMetadata & { schema_version: 1; processing_pending?: boolean; snapshot: Omit<LiveState, 'revision'> }

export function recordError(error: unknown) {
  const messages: Record<string, string> = {
    invalid_record_name: '名称需为 1–100 个字符，不能含换行或控制字符。',
    record_not_ready: '请先停止课堂，再保存已有内容。后台结果会自动补入。',
    stale_session: '当前课堂已变化，请刷新后重试。',
    record_empty: '当前还没有可保存的课堂内容。',
    record_not_found: '没有找到这份课堂记录。',
    record_unreadable: '这份记录暂时无法读取，本地文件未被修改。',
  }
  return messages[error instanceof Error ? error.message : ''] ?? '无法访问本地记录，请确认后端正在运行后重试。'
}

async function request<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${backend}/api/records${path}`, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Lecture-Client': 'browser' },
    body: JSON.stringify(body), signal,
  } : { signal })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error ?? 'record_storage_failed')
  return data as T
}

export const fetchRecord = (id: string, signal?: AbortSignal) => request<SavedRecord>(`/${encodeURIComponent(id)}`, undefined, signal)

export function defaultRecordName(courseName: string, sessionId: string | null) {
  const match = sessionId?.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{6})Z-/)
  const date = match ? new Date(Date.UTC(+match[1], +match[2]-1, +match[3], +match[4], +match[5], +match[6])) : new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${courseName} · ${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function useSavedRecords(connection: string) {
  const [records, setRecords] = useState<RecordMetadata[]>([])
  const [error, setError] = useState<string | null>(null)
  const [unreadable, setUnreadable] = useState(0)
  const [loading, setLoading] = useState(false)
  const [pending, setPending] = useState(false)
  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError(null)
    try {
      const data = await request<{ records: RecordMetadata[]; unreadable_count: number }>('', undefined, signal)
      setRecords(data.records); setUnreadable(data.unreadable_count)
    } catch (e) { if (!signal?.aborted) setError(recordError(e)) }
    finally { if (!signal?.aborted) setLoading(false) }
  }, [])
  useEffect(() => {
    if (connection !== 'connected') return
    const abort = new AbortController(); void refresh(abort.signal)
    return () => abort.abort()
  }, [connection, refresh])
  const mutate = async (path: string, body: object) => {
    setPending(true)
    try {
      const value = await request<SavedRecord>(path, body)
      setRecords(previous => [value, ...previous.filter(r => r.record_id !== value.record_id)]
        .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.record_id.localeCompare(a.record_id)))
      return value
    } finally { setPending(false) }
  }
  return { records, loading, error, unreadable, pending, refresh,
    save: (sessionId: string, name: string) => mutate('/save', { session_id: sessionId, name }),
    rename: (id: string, name: string) => mutate('/rename', { record_id: id, name }) }
}
export type SavedRecords = ReturnType<typeof useSavedRecords>
