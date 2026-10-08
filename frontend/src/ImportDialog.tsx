import { useEffect, useRef, useState } from 'react'
import { timestamp } from './courses'
import { errorText, type LiveSession } from './live'

type MediaFile = {id:string;name:string;duration?:number;size_bytes?:number;audio_tracks?:number;error?:string}
type Selection = {batch_id:string;files:MediaFile[];maximum_duration:number;cancelled?:boolean}
const messages:Record<string,string>={
  media_no_audio:'没有可用音轨',media_unknown_duration:'无法确定时长',media_unsupported:'不支持的格式',media_probe_failed:'文件损坏或无法读取',
  media_unreadable:'无法读取文件',media_tools_missing:'本地音视频组件缺失，请重建 FFmpeg 工具后再导入。',
  media_picker_failed:'文件选择窗口未正常完成，请重试。',media_too_many_files:'一次最多选择 100 个文件。',
  media_picker_busy:'已有文件选择窗口打开，请先完成选择。',
}
export function ImportDialog({courseId,live,onClose}:{courseId:string;live:LiveSession;onClose:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null)
  const [selection,setSelection]=useState<Selection|null>(null)
  const [files,setFiles]=useState<MediaFile[]>([])
  const [loading,setLoading]=useState(false)
  const [error,setError]=useState<string|null>(null)
  useEffect(()=>{dialog.current?.showModal()},[])
  const choose=async(mode:'files'|'folder')=>{
    setLoading(true);setError(null)
    try {
      const response=await fetch('http://127.0.0.1:8765/api/import/select',{method:'POST',
        headers:{'Content-Type':'application/json','X-Lecture-Client':'browser'},body:JSON.stringify({mode})})
      const data=await response.json()
      if(!response.ok)throw new Error(data.error)
      if(!data.cancelled){setSelection(data);setFiles(data.files)}
    } catch(e){const code=e instanceof Error?e.message:'';setError(messages[code]??errorText(code))}
    finally{setLoading(false)}
  }
  const total=files.reduce((n,f)=>n+(f.duration??0),0)
  const limit=selection?.maximum_duration??6000
  const invalid=files.some(f=>f.error)||total>limit||!files.length
  const move=(index:number,offset:number)=>setFiles(old=>{const next=[...old];[next[index],next[index+offset]]=[next[index+offset],next[index]];return next})
  return <dialog className="start-dialog import-dialog" ref={dialog} onCancel={e=>{if(loading||live.pending)e.preventDefault();else onClose()}} aria-labelledby="import-title">
    <span className="section-eyebrow">IMPORT CLASSROOM</span><h2 id="import-title">导入音视频</h2>
    <p>选择本地 MP4 或音频，按下面的顺序形成一份课堂记录。支持多选，也可以从文件夹挑选；子文件夹不会自动导入。</p>
    <div className="import-pickers"><button className="secondary-button" onClick={()=>void choose('files')} disabled={loading||live.pending}>选择文件…</button><button className="secondary-button" onClick={()=>void choose('folder')} disabled={loading||live.pending}>选择文件夹…</button></div>
    {loading&&<p role="status">请在系统窗口选择文件，随后检查音轨与时长…</p>}
    <ol className="import-files">{files.map((f,index)=><li key={f.id}>
      <div className="import-file-detail"><strong title={f.name}>{f.name}</strong><small>{f.error?(messages[f.error]??'文件不可用'):timestamp(Math.ceil(f.duration??0))}{(f.audio_tracks??0)>1?' · 使用第一个音轨':''}</small></div>
      <div className="import-file-actions"><button onClick={()=>move(index,-1)} disabled={index===0||loading||live.pending} aria-label={`上移 ${f.name}`}>↑</button><button onClick={()=>move(index,1)} disabled={index===files.length-1||loading||live.pending} aria-label={`下移 ${f.name}`}>↓</button><button onClick={()=>setFiles(old=>old.filter(row=>row.id!==f.id))} disabled={loading||live.pending} aria-label={`移除 ${f.name}`}>移除</button></div>
    </li>)}</ol>
    <p className={total>limit?'record-error':''}>已选 {files.length} 个文件 · 总时长 {timestamp(Math.ceil(total))} · 上限 100 分钟</p>
    {total>limit&&<p role="alert" className="record-error">所选文件总时长超过 100 分钟，请移除部分文件。</p>}
    {files.some(f=>f.error)&&<p role="alert" className="record-error">请移除无法读取或没有音轨的文件，再开始导入。</p>}
    <p className="dialog-hint">原文件保持不变。开始后仅音频片段会发送给 SiliconFlow，逐段显示 RAW 并更新摘要，无需等待按原时长播放。结果与实时课堂一样，可在“课堂记录”保存和重命名。</p>
    {(error||live.actionError)&&<p className="record-error" role="alert">{error??errorText(live.actionError)}</p>}
    <div className="dialog-actions"><button className="secondary-button" onClick={onClose} disabled={loading||live.pending}>取消</button><button className="primary-button" disabled={loading||live.pending||invalid} onClick={async()=>{
      if(selection&&await live.importFiles(courseId,selection.batch_id,files.map(f=>f.id)))onClose()
    }}>{live.pending?'正在开始…':'开始导入'}</button></div>
  </dialog>
}
