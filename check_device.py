"""Read-only PortAudio device/configuration preflight. Never opens a stream."""
import json
from pathlib import Path

from lecture_asr.portable_device import selected_input


def main():
    import sounddevice as sd
    selected=selected_input(Path(__file__).resolve().parent)
    sd.check_input_settings(device=selected['device_index'],samplerate=44100,channels=1,dtype='int16')
    print(json.dumps({**selected,'target_sample_rate_hz':44100,'channels':1,
                      'sample_format':'int16','input_settings_supported':True},ensure_ascii=False))

if __name__=='__main__':main()
