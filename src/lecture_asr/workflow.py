"""One user-started recording followed by one existing ASR call; no retries."""

import sys
from datetime import datetime, timezone
from pathlib import Path

from . import asr, evidence, recording
from .config import Settings


def record_and_transcribe(device: int, duration: float, settings: Settings,
                         recordings_root: Path | None = None, output_root: Path | None = None,
                         expected_name: str | None = None, expected_host_api: str | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    recordings_root = recordings_root if recordings_root is not None else root / "recordings/v0.3"
    output_root = output_root if output_root is not None else root / "outputs/v0.3"
    key = settings.api_key or ""
    run = None
    session = {}
    phase = "preparation"

    def emit(message: str, *, error: bool = False) -> None:
        print(evidence.redact(message, key), file=sys.stderr if error else sys.stdout, flush=True)

    def save_session() -> None:
        evidence.write_json(run / "session.json", evidence.redact(session, key))

    try:
        asr.validate_settings(settings)
        recording.validate_recording(device, duration)
        if not sys.stdin.isatty():
            emit("An interactive terminal is required. Start this command yourself and press Enter when ready.", error=True)
            return 2
        config = recording.prepare_recording(device, duration, expected_name, expected_host_api)
        # Reuse the evidence writer's unique run ID; request.json is intent until ASR starts.
        request = {
            "stage": "V0.3",
            "method": "POST",
            "endpoint": settings.transcription_endpoint,
            "model": settings.asr_model,
            "request_content_type": "multipart/form-data",
            "timeout_seconds": {"connect": asr.TIMEOUT[0], "read_inactivity": asr.TIMEOUT[1]},
            "automatic_retries": 0,
            "follow_redirects": False,
            "state": "not_sent_waiting_for_recording",
        }
        run = evidence.start_run(output_root, request, key)
        audio_dir = recordings_root / run.name
        audio_dir.mkdir(parents=True, exist_ok=False)
        audio_path = audio_dir / "audio.wav"
        recording_metadata = run / "recording.json"
        session = {
            "stage": "V0.3",
            "session_id": run.name,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "state": "waiting_for_user",
            "audio_file": str(audio_path.resolve()),
            "recording_metadata": "recording.json",
            "asr_request_metadata": "request.json",
            "asr_result": "result.json",
            "raw_response": "response.txt",
            "parsed_response": "response.json",
            "transcript": "transcript.txt",
            "user_confirmed_start": False,
            "asr_call_attempted": False,
            "human_content_review": "pending",
            "stage_pass": False,
        }
        save_session()
        emit(f"Selected microphone: {config['device_name']} (index {config['device_index']})")
        emit(f"Host API: {config['host_api']}")
        emit(f"Recording: {config['sample_rate_hz']} Hz / {config['channels']} channel / {config['sample_format']}")
        emit(f"Duration: {config['requested_duration_seconds']:g} seconds")
        emit(f"WAV destination: {audio_path.resolve()}")
        emit(f"After recording, this WAV will be sent to {settings.transcription_endpoint}")
        emit(f"ASR model: {settings.asr_model}")
        emit(f"Session evidence: {run.resolve()}")
        phase = "user_confirmation"
        answer = input("Press Enter when you are ready to start recording (Ctrl+C to cancel). ")
        if answer != "":
            session["state"] = "cancelled"
            save_session()
            emit("Cancelled; no recording or ASR call.")
            return 130

        session.update(user_confirmed_start=True, confirmed_at_utc=datetime.now(timezone.utc).isoformat(),
                       state="recording")
        save_session()
        phase = "recording"
        emit("Starting recording; speak when the recorder displays RECORDING NOW.")
        recorded = recording.record_wav(config, audio_path, recording_metadata)
        emit(f"WAV saved: {audio_path.resolve()}")
        if recorded.get("technical_recording_result") != "PASS":
            session.update(state="recording_failed", error="recording_technical_checks_failed")
            save_session()
            emit("Recording technical checks failed; ASR was not called. Existing WAV and metadata are preserved.", error=True)
            return 1

        phase = "audio_validation"
        audio_bytes, audio_metadata = asr.read_audio(audio_path)
        phase = "asr_preparation"
        request.update(state="ready_to_send", session_id=run.name,
                       created_at_utc=datetime.now(timezone.utc).isoformat(), audio=audio_metadata)
        evidence.write_json(run / "request.json", evidence.redact(request, key))
        session.update(state="transcribing")
        save_session()
        phase = "asr_call"
        emit("Transcribing...")
        session["asr_call_attempted"] = True
        result = asr.transcribe(audio_bytes, audio_path.name, settings)
        emit(f"HTTP status: {result.http_status}")
        emit(f"Request latency: {result.latency_seconds:.3f} seconds")
        phase = "asr_evidence"
        evidence.finish_run(run, result)
        session.update(state="asr_failed" if result.error else "completed",
                       completed_at_utc=datetime.now(timezone.utc).isoformat(),
                       http_status=result.http_status, latency_seconds=result.latency_seconds,
                       error=result.error)
        save_session()
        if result.error:
            emit(f"ASR failed: {result.error}. WAV preserved; no automatic retry or new recording.", error=True)
            return 1
        # The existing ASR client already handles credential redaction. Do not alter its text.
        print("Transcript:", flush=True)
        sys.stdout.write(result.transcript)
        sys.stdout.flush()
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, (EOFError, KeyboardInterrupt))
        if run is not None and session:
            session.update(state="cancelled" if cancelled else f"{phase}_failed",
                           error=str(error) or type(error).__name__)
            try:
                save_session()
            except OSError:
                emit("Cannot update session evidence; retain existing files and do not retry automatically.", error=True)
        emit(f"One-shot workflow stopped during {phase}: {str(error) or type(error).__name__}. No automatic retry.", error=True)
        return 130 if cancelled else 1
