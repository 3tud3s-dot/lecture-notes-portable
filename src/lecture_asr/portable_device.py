"""Select one local input device without opening a microphone stream.

An explicit index in the root .env wins over the OS default. Recording code
pins the selected index, name and host API so it never silently falls back.
"""

import os
from pathlib import Path

from dotenv import dotenv_values


class DeviceSelectionError(ValueError):
    pass


def selected_input(root: Path) -> dict:
    try:
        import sounddevice as sd

        path = Path(root) / '.env'
        local = dotenv_values(path, interpolate=False) if path.is_file() else {}
        value = os.environ.get('LECTURE_INPUT_DEVICE', local.get('LECTURE_INPUT_DEVICE'))
        if value is None or str(value).strip() == '':
            index = sd.default.device[0]
            mode = 'system_default'
        else:
            index = int(str(value).strip())
            mode = 'explicit'
        if type(index) is not int or index < 0:
            raise DeviceSelectionError('No valid microphone input device selected.')
        info = sd.query_devices(index)
        if info['max_input_channels'] < 1:
            raise DeviceSelectionError('Selected device has no input channels.')
        host = sd.query_hostapis(info['hostapi'])
        return {'device_index': index, 'device_name': info['name'],
                'host_api': host['name'], 'selection_mode': mode,
                'device_default_sample_rate_hz': info['default_samplerate']}
    except DeviceSelectionError:
        raise
    except Exception as error:
        raise DeviceSelectionError('Cannot discover the selected input device.') from error
