"""One bounded, blocking microphone read to PCM WAV; no network or ASR."""

import array
import hashlib
import json
import math
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path


class RecordingError(Exception):
    """Invalid recording input or a failed capture; never triggers a retry."""


def validate_recording(device: int, duration: float, *, max_duration: float = 25) -> int:
    if type(device) is not int or device < 0:
        raise RecordingError("Specify a non-negative input device index; no default fallback.")
    if not math.isfinite(duration) or not 0 < duration <= max_duration:
        raise RecordingError(f"Duration must be finite, greater than 0 and at most {max_duration:g} seconds.")
    frames = round(duration * 44100)
    if frames < 1:
        raise RecordingError("Duration is too short for one audio frame.")
    return frames


def prepare_recording(device: int, duration: float, expected_name: str | None = None,
                      expected_host_api: str | None = None, *, max_duration: float = 25) -> dict:
    frames = validate_recording(device, duration, max_duration=max_duration)
    try:
        import sounddevice as sd
        info = sd.query_devices(device)
        host = sd.query_hostapis(info["hostapi"])
        if info["max_input_channels"] < 1:
            raise RecordingError("Selected device has no input channels.")
        if expected_name is not None and info["name"] != expected_name:
            raise RecordingError("Selected device name changed; no recording started.")
        if expected_host_api is not None and host["name"] != expected_host_api:
            raise RecordingError("Selected host API changed; no recording started.")
        sd.check_input_settings(device=device, samplerate=44100, channels=1, dtype="int16")
    except RecordingError:
        raise
    except Exception as error:
        raise RecordingError(f"Input configuration check failed: {error}") from error
    return {
        "device_index": device,
        "device_name": info["name"],
        "host_api": host["name"],
        "max_input_channels": info["max_input_channels"],
        "device_default_sample_rate_hz": info["default_samplerate"],
        "sample_rate_hz": 44100,
        "channels": 1,
        "sample_format": "int16",
        "bits_per_sample": 16,
        "requested_duration_seconds": duration,
        "requested_frames": frames,
        "input_settings_supported": True,
    }


def validate_output_paths(output: Path, metadata_path: Path) -> None:
    if output.suffix.lower() != ".wav" or metadata_path.suffix.lower() != ".json":
        raise RecordingError("Output must be .wav and metadata must be .json.")
    if output.resolve() == metadata_path.resolve():
        raise RecordingError("Audio and metadata paths must differ.")
    for path in (output, metadata_path):
        if path.exists():
            raise RecordingError(f"Refusing to overwrite existing output: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)


def inspect_pcm_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        if audio.getcomptype() != "NONE" or audio.getsampwidth() != 2:
            raise RecordingError("Expected uncompressed 16-bit PCM WAV.")
        channels, rate, frames = audio.getnchannels(), audio.getframerate(), audio.getnframes()
        pcm = audio.readframes(frames)
    if not frames or len(pcm) != frames * channels * 2:
        raise RecordingError("Empty or truncated WAV sample data.")
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    clipping = sum(sample in (-32768, 32767) for sample in samples)
    return {
        "output_file_exists": True,
        "file_size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "container": "WAV",
        "codec": "pcm_s16le",
        "bits_per_sample": 16,
        "sample_rate_hz": rate,
        "channels": channels,
        "total_frames": frames,
        "actual_duration_seconds": frames / rate,
        "sample_count": len(samples),
        "peak_amplitude_pcm": peak,
        "peak_amplitude_normalized": peak / 32768,
        "peak_dbfs": 20 * math.log10(peak / 32768) if peak else None,
        "rms_pcm": rms,
        "rms_normalized": rms / 32768,
        "rms_dbfs": 20 * math.log10(rms / 32768) if rms else None,
        "clipping_count": clipping,
        "clipping_ratio": clipping / len(samples),
        "clipping_definition": "Samples exactly equal to -32768 or 32767; does not detect all analog distortion.",
        "nonzero_sample_count": sum(sample != 0 for sample in samples),
        "digital_non_silence": peak > 0,
        "complete_pcm_payload": True,
    }


def record_wav(config: dict, output: Path, metadata_path: Path) -> dict:
    """Reserve new files, announce capture, read once, close, then inspect the WAV."""
    validate_output_paths(output, metadata_path)
    report = {
        "stage": "V0.2b",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        **config,
        "output_path": str(output.resolve()),
        "state": "prepared",
        "stream_opened": False,
        "recording_started": False,
        "processing": "none; original captured PCM saved without resampling or amplitude changes",
        "siliconflow_requests_made": 0,
        "user_audio_review": "pending",
        "overall_v0_2": "WAITING FOR USER AUDIO REVIEW",
    }

    def save_report():
        metadata_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    # Fail before opening the microphone if either destination cannot be reserved.
    with metadata_path.open("x", encoding="utf-8") as metadata:
        json.dump(report, metadata, ensure_ascii=False, indent=2)
    try:
        import sounddevice as sd
        with output.open("xb") as raw_file, wave.open(raw_file, "wb") as audio:
            audio.setnchannels(config["channels"])
            audio.setsampwidth(2)
            audio.setframerate(config["sample_rate_hz"])
            # The user prepares first, then runs this command themselves (HUMAN GATE).
            with sd.RawInputStream(device=config["device_index"], samplerate=config["sample_rate_hz"],
                                   channels=config["channels"], dtype=config["sample_format"],
                                   latency="high", dither_off=True) as stream:
                report.update(stream_opened=True, recording_started=True,
                              recording_started_at_utc=datetime.now(timezone.utc).isoformat(),
                              stream_sample_rate_hz=stream.samplerate,
                              stream_channels=stream.channels,
                              stream_dtype=stream.dtype,
                              input_latency_seconds=stream.latency)
                print(f"RECORDING NOW — speak for {config['requested_duration_seconds']:g} seconds.", flush=True)
                started = time.perf_counter()
                data, overflowed = stream.read(config["requested_frames"])
                pcm = bytes(data)
                report.update(read_elapsed_seconds=time.perf_counter() - started,
                              input_overflow_reported=bool(overflowed))
            report["stream_closed"] = True
            print("Recording finished; microphone closed.", flush=True)
            if sys.byteorder != "little":
                samples = array.array("h")
                samples.frombytes(pcm)
                samples.byteswap()
                pcm = samples.tobytes()
            audio.writeframes(pcm)
        integrity = inspect_pcm_wav(output)
        report["audio_integrity"] = integrity
        report["state"] = "recording_completed"
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["technical_checks"] = {
            "frame_count_matches": integrity["total_frames"] == config["requested_frames"],
            "parameters_match": integrity["sample_rate_hz"] == config["sample_rate_hz"]
                and integrity["channels"] == config["channels"]
                and report["stream_sample_rate_hz"] == config["sample_rate_hz"]
                and report["stream_channels"] == config["channels"]
                and report["stream_dtype"] == config["sample_format"],
            "no_reported_overflow": not report["input_overflow_reported"],
            "digital_non_silence": integrity["digital_non_silence"],
            "no_full_scale_clipping": integrity["clipping_count"] == 0,
            "complete_pcm_payload": integrity["complete_pcm_payload"],
        }
        report["technical_recording_result"] = "PASS" if all(report["technical_checks"].values()) else "FAIL"
        save_report()
        return report
    except (Exception, KeyboardInterrupt) as error:
        report.update(state="failed", technical_recording_result="FAIL", error=str(error) or type(error).__name__)
        save_report()
        raise RecordingError(f"Recording failed; no retry: {error}") from error
