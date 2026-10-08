"""Input-device discovery only. No streams, recording, or network access."""

import math


class DeviceDiscoveryError(Exception):
    """The audio backend or its device metadata could not be read."""


def format_input_devices(devices: list[dict], host_apis: list[dict], default_index: int) -> dict:
    """Keep PortAudio indices; never choose a fallback on the user's behalf."""
    inputs = []
    for index, device in enumerate(devices):
        try:
            channels = device["max_input_channels"]
            if type(channels) is not int or channels < 0:
                raise ValueError
            if channels == 0:
                continue
            name = device["name"]
            sample_rate = float(device["default_samplerate"])
            host_index = device["hostapi"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError
            if not math.isfinite(sample_rate) or sample_rate <= 0:
                raise ValueError
            if type(host_index) is not int or not 0 <= host_index < len(host_apis):
                raise ValueError
            host_name = host_apis[host_index]["name"]
            if not isinstance(host_name, str) or not host_name.strip():
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise DeviceDiscoveryError(f"Invalid metadata for input device index {index}.") from error
        inputs.append({
            "index": index,
            "name": name,
            "max_input_channels": channels,
            "default_sample_rate_hz": sample_rate,
            "is_default_input": type(default_index) is int and index == default_index,
            "host_api_index": host_index,
            "host_api": host_name,
        })

    default = next((device for device in inputs if device["is_default_input"]), None)
    if default is not None:
        status = "identified"
    elif default_index == -1:
        status = "missing"
    else:
        status = "invalid_or_not_input_capable"
    return {
        "input_devices": inputs,
        "input_device_count": len(inputs),
        "backend_default_input_index": default_index,
        "default_input_status": status,
        "default_input_device": default,
    }


def discover_input_devices() -> dict:
    """Query PortAudio metadata without opening an input or output stream."""
    try:
        import sounddevice as sd
    except Exception as error:
        # Import itself initializes PortAudio and can raise its own exception type.
        raise DeviceDiscoveryError(f"Cannot initialize sounddevice / PortAudio: {error}") from error
    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        default_index = sd.default.device[0]
        portaudio_version = sd.get_portaudio_version()
    except sd.PortAudioError as error:
        raise DeviceDiscoveryError(f"PortAudio device discovery failed: {error}") from error
    report = format_input_devices(devices, host_apis, default_index)
    report.update({
        "audio_library": {"name": "sounddevice", "version": sd.__version__},
        "portaudio_version": {"number": portaudio_version[0], "text": portaudio_version[1]},
        "default_source": "sounddevice.default.device[0]; no application override or fallback",
        "recording_performed": False,
        "stream_opened": False,
    })
    return report
