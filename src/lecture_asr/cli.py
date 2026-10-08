"""Stage-scoped local diagnostics, audio tools, and separate file transcription."""

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from .config import Settings, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classroom ASR: diagnostics, device discovery, and offline file spike")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect local environment without network or audio")
    doctor.add_argument("--env-file", type=Path, help="Explicit .env path (default: project root)")
    doctor.add_argument("--require-key", action="store_true", help="Exit 2 if no non-empty key is configured")
    transcribe = commands.add_parser("transcribe-file", help="Send one local PCM WAV; no recording or retries")
    transcribe.add_argument("audio_path", type=Path)
    transcribe.add_argument("--env-file", type=Path, help="Explicit .env path (default: project root)")
    transcribe.add_argument("--output-dir", type=Path, help="Evidence root (default: project outputs/v0.1)")
    commands.add_parser("list-input-devices", help="List input devices and the backend default; no recording or network")
    record = commands.add_parser("record", help="Record one short PCM WAV from an explicit device; no ASR or network")
    record.add_argument("--device", type=int, required=True)
    record.add_argument("--duration", type=float, default=12, help="Seconds, greater than 0 and at most 25 (default: 12)")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--metadata", type=Path, help="New JSON path (default: outputs/v0.2/recording-metadata.json)")
    record.add_argument("--expect-name", help="Stop if the selected device name has changed")
    record.add_argument("--expect-host-api", help="Stop if the selected host API has changed")
    combined = commands.add_parser("record-and-transcribe", help="Press Enter to record one WAV, then send one ASR request")
    combined.add_argument("--device", type=int, required=True)
    combined.add_argument("--duration", type=float, default=20, help="Seconds, greater than 0 and at most 25 (default: 20)")
    combined.add_argument("--env-file", type=Path, help="Explicit .env path (default: project root)")
    combined.add_argument("--recordings-dir", type=Path, help="Session audio root (default: recordings/v0.3)")
    combined.add_argument("--output-dir", type=Path, help="Session evidence root (default: outputs/v0.3)")
    combined.add_argument("--expect-name", help="Stop if the selected device name has changed")
    combined.add_argument("--expect-host-api", help="Stop if the selected host API has changed")
    capture = commands.add_parser("capture-chunks", help="Press Enter for one continuous stream; save full/chunk WAVs, no ASR")
    capture.add_argument("--device", type=int, required=True)
    capture.add_argument("--duration", type=float, default=20, help="Seconds, greater than 0 and at most 25 (default: 20)")
    capture.add_argument("--chunk-duration", type=float, default=6, help="Fixed at 6 seconds in V0.4b")
    capture.add_argument("--recordings-dir", type=Path, help="Audio root (default: recordings/v0.4)")
    capture.add_argument("--output-dir", type=Path, help="Evidence root (default: outputs/v0.4)")
    capture.add_argument("--expect-name", help="Stop if the selected device name has changed")
    capture.add_argument("--expect-host-api", help="Stop if the selected host API has changed")
    session = commands.add_parser("transcribe-session", help="Press Enter for continuous capture with one background ASR worker")
    session.add_argument("--device", type=int, required=True)
    session.add_argument("--duration", type=float, default=30, help="Seconds, greater than 0 and at most 300 for V0.5c (default: 30)")
    session.add_argument("--chunk-duration", type=float, default=12, help="12-second V0 default or explicit 6-second historical low-latency baseline; overlap 0")
    session.add_argument("--env-file", type=Path)
    session.add_argument("--recordings-dir", type=Path)
    session.add_argument("--output-dir", type=Path)
    session.add_argument("--expect-name")
    session.add_argument("--expect-host-api")
    args = parser.parse_args(argv)

    if args.command == "list-input-devices":
        return list_input_devices()
    if args.command == "record":
        return record_audio(args)
    if args.command == "capture-chunks":
        from .chunk_capture import capture_chunks

        return capture_chunks(args.device, args.duration, args.chunk_duration,
                              args.recordings_dir, args.output_dir,
                              args.expect_name, args.expect_host_api)

    try:
        settings = load_settings(args.env_file)
    except (OSError, UnicodeError):
        print("Cannot read the selected .env file as UTF-8.", file=sys.stderr)
        return 2

    if args.command == "transcribe-file":
        return transcribe_file(args.audio_path, settings, args.output_dir)
    if args.command == "transcribe-session":
        from .session_cli import transcribe_session

        return transcribe_session(args, settings)
    if args.command == "record-and-transcribe":
        from .workflow import record_and_transcribe

        return record_and_transcribe(args.device, args.duration, settings,
                                    args.recordings_dir, args.output_dir,
                                    args.expect_name, args.expect_host_api)

    # Deliberately select safe fields instead of serializing Settings or os.environ.
    report = {
        "scope": "V0.0 local diagnostics only; not a stage acceptance result",
        "python": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "executable": sys.executable,
        "virtual_environment": sys.prefix != sys.base_prefix,
        "python_dotenv": version("python-dotenv"),
        "api_key_status": "configured" if settings.api_key else "missing",
        "base_url": settings.base_url,
        "asr_model": settings.asr_model,
        "transcription_endpoint": settings.transcription_endpoint,
        "asr_runtime_verified": False,
        "microphone_accessed": False,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 2 if args.require_key and not settings.api_key else 0


def list_input_devices() -> int:
    from .audio import DeviceDiscoveryError, discover_input_devices

    try:
        report = discover_input_devices()
    except DeviceDiscoveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    report.update({
        "scope": "V0.2a device discovery only; not a recording acceptance result",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["default_input_status"] == "identified" else 1


def record_audio(args: argparse.Namespace) -> int:
    from .recording import RecordingError, prepare_recording, record_wav, validate_output_paths

    metadata = args.metadata or Path(__file__).resolve().parents[2] / "outputs/v0.2/recording-metadata.json"
    try:
        validate_output_paths(args.output, metadata)
        config = prepare_recording(args.device, args.duration, args.expect_name, args.expect_host_api)
        for label, value in (
            ("Selected device index", config["device_index"]),
            ("Selected device name", config["device_name"]),
            ("Host API", config["host_api"]),
            ("Sample rate", f"{config['sample_rate_hz']} Hz"),
            ("Channels", config["channels"]),
            ("Sample format", "16-bit PCM (int16)"),
            ("Requested duration", f"{config['requested_duration_seconds']:g} seconds"),
            ("Output path", args.output.resolve()),
        ):
            print(f"{label}: {value}", flush=True)
        report = record_wav(config, args.output, metadata)
    except (RecordingError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Metadata: {metadata.resolve()}")
    print(f"Technical recording result: {report['technical_recording_result']}")
    print("Overall V0.2: WAITING FOR USER AUDIO REVIEW")
    return 0 if report["technical_recording_result"] == "PASS" else 1


def transcribe_file(audio_path: Path, settings: Settings, output_root: Path | None) -> int:
    from .asr import InputError, TIMEOUT, read_audio, transcribe, validate_settings
    from .evidence import finish_run, start_run

    try:
        validate_settings(settings)
        audio, metadata = read_audio(audio_path)
    except InputError as error:
        print(str(error), file=sys.stderr)
        return 2
    root = output_root if output_root is not None else Path(__file__).resolve().parents[2] / "outputs" / "v0.1"
    try:
        run = start_run(root, {
            "stage": "V0.1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "method": "POST",
            "endpoint": settings.transcription_endpoint,
            "model": settings.asr_model,
            "request_content_type": "multipart/form-data",
            "timeout_seconds": {"connect": TIMEOUT[0], "read_inactivity": TIMEOUT[1]},
            "automatic_retries": 0,
            "follow_redirects": False,
            "audio": metadata,
        }, settings.api_key)
    except OSError:
        print("Cannot prepare evidence directory; no request sent.", file=sys.stderr)
        return 2
    result = transcribe(audio, metadata["filename"], settings)
    try:
        finish_run(run, result)
    except OSError:
        print("Cannot finish saving evidence. A request was attempted; do not retry automatically.", file=sys.stderr)
        return 1
    print(f"HTTP status: {result.http_status}")
    print(f"Request latency: {result.latency_seconds:.3f} seconds")
    print(f"Evidence: {run}")
    if result.error:
        print(f"ASR failed: {result.error}. Inspect saved evidence before another request.", file=sys.stderr)
        return 1
    print(result.transcript)
    return 0
