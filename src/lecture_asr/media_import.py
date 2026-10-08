"""Local file selection/probing/decoding; ASR is owned by the existing consumer."""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path
from uuid import uuid4

from . import chunk_capture, evidence
from .chunk_pipeline import AudioChunk, SAMPLE_RATE
from .live_capture import MAX_DURATION

EXTENSIONS={'.mp4','.m4v','.mov','.mkv','.webm','.wav','.mp3','.m4a','.aac','.flac','.ogg','.opus'}
FORMATS='mov,matroska,webm,wav,mp3,flac,ogg,aac'
MAX_FILES=100


class MediaError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def tool(root,name):
    candidate=Path(root)/'.heavy/tools/ffmpeg/bin'/(name+'.exe' if os.name=='nt' else name)
    found=str(candidate) if candidate.is_file() else shutil.which(name)
    if not found:raise MediaError('media_tools_missing')
    return found


def process_options():
    return {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}


def pick(mode):
    if mode not in ('files','folder'):raise MediaError('invalid_media_selection')
    try:
        result=subprocess.run([sys.executable,'-B','-X','utf8','-m','lecture_asr.media_picker',
            *(['--folder'] if mode=='folder' else [])],capture_output=True,timeout=300,check=True,
            **process_options())
        value=json.loads(result.stdout)
        if mode=='folder':
            if not value.get('folder'):return []
            # Only the selected folder; do not silently recurse into subfolders.
            paths=[p for p in Path(value['folder']).iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS]
            paths.sort(key=lambda p:[int(x) if x.isdigit() else x.casefold() for x in re.split('(\\d+)',p.name)])
            return paths
        return [Path(p) for p in value.get('paths',[])]
    except (OSError,ValueError,subprocess.SubprocessError):
        raise MediaError('media_picker_failed') from None


def fingerprint(path):
    stat=path.stat()
    return {'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}


def probe(root,path):
    path=Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in EXTENSIONS:raise MediaError('media_unsupported')
    try:
        result=subprocess.run([tool(root,'ffprobe'),'-v','error','-protocol_whitelist','file,pipe',
            '-format_whitelist',FORMATS,'-show_entries',
            'format=duration:stream=codec_type,codec_name,duration,sample_rate,channels',
            '-of','json',str(path)],capture_output=True,timeout=20,check=True,**process_options())
        data=json.loads(result.stdout)
        tracks=[s for s in data.get('streams',[]) if s.get('codec_type')=='audio']
        if not tracks:raise MediaError('media_no_audio')
        durations=[]
        for v in (data.get('format',{}).get('duration'),tracks[0].get('duration')):
            try:d=float(v)
            except (TypeError,ValueError):continue
            if math.isfinite(d) and d>0:durations.append(d)
        if not durations:raise MediaError('media_unknown_duration')
        return {'path':path,'name':path.name,'duration':max(durations),'audio_codec':tracks[0].get('codec_name'),
                'sample_rate':tracks[0].get('sample_rate'),'channels':tracks[0].get('channels'),
                'audio_tracks':len(tracks),**fingerprint(path)}
    except MediaError:raise
    except (OSError,ValueError,subprocess.SubprocessError):raise MediaError('media_probe_failed') from None


class MediaSelection:
    def __init__(self,root):
        self.root=Path(root);self.lock=threading.Lock();self.batch_id=None;self.entries={}

    def select(self,mode):
        if not self.lock.acquire(blocking=False):raise MediaError('media_picker_busy')
        try:
            tool(self.root,'ffmpeg');tool(self.root,'ffprobe')
            paths=pick(mode)
            if not paths:return {'cancelled':True}
            if len(paths)>MAX_FILES:raise MediaError('media_too_many_files')
            entries={};seen=set()
            for path in paths:
                try:
                    path=path.resolve(strict=True)
                    if path in seen:continue
                    seen.add(path)
                    entry=probe(self.root,path)
                except (MediaError,OSError) as error:
                    entry={'path':path,'name':path.name,'error':getattr(error,'code','media_unreadable')}
                entries[uuid4().hex]=entry
            self.batch_id=uuid4().hex;self.entries=entries
            return {'batch_id':self.batch_id,'files':[dict({k:v for k,v in entry.items() if k!='path'},id=id)
                                                     for id,entry in entries.items()],
                    'maximum_duration':MAX_DURATION,'maximum_files':MAX_FILES}
        finally:self.lock.release()

    def resolve(self,batch_id,ids):
        if not self.lock.acquire(blocking=False):raise MediaError('media_picker_busy')
        try:
            if batch_id!=self.batch_id or not self.batch_id:raise MediaError('media_selection_expired')
            if not isinstance(ids,list) or not ids or len(ids)>MAX_FILES or any(not isinstance(i,str) for i in ids):
                raise MediaError('invalid_media_selection')
            if len(ids)!=len(set(ids)) or any(i not in self.entries for i in ids):raise MediaError('invalid_media_selection')
            rows=[dict(self.entries[i]) for i in ids]
            if any(row.get('error') for row in rows):raise MediaError('media_invalid_file')
            if sum(row['duration'] for row in rows)>MAX_DURATION:raise MediaError('media_duration_exceeded')
            for row in rows:
                try:current=fingerprint(row['path'])
                except OSError:raise MediaError('media_source_changed') from None
                if any(current[k]!=row[k] for k in current):raise MediaError('media_source_changed')
            return rows
        finally:self.lock.release()


def imported_producer(root,files,controller,finished):
    """One queued chunk at a time, no wall-clock playback wait or microphone.

    ffmpeg pipe backpressure bounds decoded audio; Stop terminates decoding and
    drains the current ASR rather than scheduling the rest of a 100-minute file.
    """
    def produce(publish,directory):
        frames=segment_id=0
        report={'source_kind':'import','sample_rate_hz':SAMPLE_RATE,'channels':1,'sample_format':'int16',
                'requested_duration_seconds':sum(f['duration'] for f in files),'actual_captured_frames':0,
                'stream_open_count':0,'stream_close_count':0,'portaudio_status_events':[],
                'files':[],'chunk_boundaries':[],'pause_events':[],
                'processing':'decode first audio track; mono PCM16 44100 Hz; no enhancement or silence removal'}
        executable=tool(root,'ffmpeg')
        try:
            with (directory/'full.wav').open('xb') as raw,wave.open(raw,'wb') as full:
                full.setparams((1,2,SAMPLE_RATE,0,'NONE','not compressed'))
                for index,source in enumerate(files):
                    if controller.stop_event.is_set():break
                    if fingerprint(source['path'])!={k:source[k] for k in ('size_bytes','mtime_ns')}:
                        raise MediaError('media_source_changed')
                    file_start=frames
                    record={k:v for k,v in source.items() if k!='path'}
                    record.update(file_index=index,start_frame=frames,status='decoding')
                    report['files'].append(record)
                    command=[executable,'-nostdin','-v','error','-protocol_whitelist','file,pipe',
                        '-format_whitelist',FORMATS,'-i',str(source['path']),'-map','0:a:0','-vn','-sn','-dn',
                        '-ac','1','-ar',str(SAMPLE_RATE),'-c:a','pcm_s16le','-f','s16le','pipe:1']
                    with (directory/f'decode-{index:03d}.stderr.log').open('wb') as errors:
                        proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=errors,**process_options())
                        with controller.condition:
                            controller.import_decoder=proc
                            if controller.stop_event.is_set():proc.terminate()
                        try:
                            while not controller.stop_event.is_set():
                                # Read one extra sample at the hard limit to detect lying metadata.
                                count=min(12*SAMPLE_RATE,MAX_DURATION*SAMPLE_RATE-frames+1)
                                pcm=proc.stdout.read(count*2)
                                if not pcm:break
                                if len(pcm)%2:raise MediaError('media_incomplete_pcm')
                                n=len(pcm)//2
                                if frames+n>MAX_DURATION*SAMPLE_RATE:raise MediaError('media_duration_exceeded')
                                if controller.stop_event.is_set():break
                                path=directory/f'chunk-{segment_id:03d}.wav'
                                chunk_capture._write_wav(path,pcm);full.writeframes(pcm)
                                with controller.condition:
                                    controller.import_sources[segment_id]={'source_name':source['name'],
                                        'source_file_index':index,'source_start_time':(frames-file_start)/SAMPLE_RATE}
                                controller.update(import_progress={'file_index':index,'file_count':len(files),
                                    'filename':source['name'],'decoded_seconds':(frames+n)/SAMPLE_RATE,
                                    'total_seconds':sum(f['duration'] for f in files)})
                                finished.clear()
                                publish(AudioChunk(path,frames,n))
                                frames+=n;segment_id+=1
                                report['actual_captured_frames']=frames
                                report['chunk_boundaries'].append({'segment_id':segment_id-1,'end_frame':frames,
                                                                  'reason':'full' if n==12*SAMPLE_RATE else 'file_end'})
                                evidence.write_json(directory/'capture.json',report)
                                # Consumer always signals completion (success OR failure).
                                finished.wait()
                                controller.update(captured_duration=frames/SAMPLE_RATE)
                            if controller.stop_event.is_set():
                                record['status']='stopped'
                            else:
                                if proc.wait(timeout=10)!=0:raise MediaError('media_decode_failed')
                                if frames==file_start:raise MediaError('media_empty_audio')
                                if fingerprint(source['path'])!={k:source[k] for k in ('size_bytes','mtime_ns')}:
                                    raise MediaError('media_source_changed')
                                record['status']='completed'
                        except MediaError as error:
                            record.update(status='failed',error=error.code);controller.update(error=error.code)
                            raise
                        finally:
                            with controller.condition:controller.import_decoder=None
                            if proc.poll() is None:
                                proc.terminate()
                                try:proc.wait(timeout=2)
                                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                            proc.stdout.close()
                            record['end_frame']=frames
        except MediaError as error:
            controller.update(error=error.code)
            raise
        finally:
            report['actual_captured_frames']=frames
            report['stop_requested']=controller.stop_event.is_set()
            evidence.write_json(directory/'capture.json',report)
            controller.update(status='Processing',processing_phase='asr')
        return report
    return produce


def inspect_import(directory,segments):
    combined=hashlib.sha256();total=0;cursor=0;valid=True
    for index,s in enumerate(segments):
        with wave.open(str(s.audio_path),'rb') as wav:
            if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate())!=(1,2,SAMPLE_RATE):
                raise MediaError('media_incomplete_pcm')
            frames=0
            while pcm:=wav.readframes(65536):combined.update(pcm);frames+=len(pcm)//2
            valid &= frames==wav.getnframes()==s.end_frame-s.start_frame and s.start_frame==cursor and s.segment_id==index
            cursor=s.end_frame;total+=frames
    full=hashlib.sha256();full_frames=0
    with wave.open(str(directory/'full.wav'),'rb') as wav:
        while pcm:=wav.readframes(65536):full.update(pcm);full_frames+=len(pcm)//2
        valid &= full_frames==wav.getnframes()
    return {'chunk_pcm_equals_full':combined.hexdigest()==full.hexdigest(),'frame_sum_matches_full':total==full_frames,
            'contiguous_frame_metadata':bool(valid),'full_frames':full_frames,'full_pcm_sha256':full.hexdigest(),
            'chunk_concat_pcm_sha256':combined.hexdigest()}
