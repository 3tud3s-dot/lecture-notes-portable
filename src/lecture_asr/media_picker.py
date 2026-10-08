"""User-invoked OS file dialog; run in its own main-thread process."""
import json
import sys
import tkinter as tk
from tkinter import filedialog


def main():
    root=tk.Tk();root.withdraw();root.attributes('-topmost',True)
    try:
        if '--folder' in sys.argv:
            folder=filedialog.askdirectory(parent=root,title='选择包含课堂音视频的文件夹')
            result={'folder':folder} if folder else {}
        else:
            paths=filedialog.askopenfilenames(parent=root,title='选择课堂音视频（可多选）',filetypes=[
                ('音视频','*.mp4 *.m4v *.mov *.mkv *.webm *.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus'),
                ('所有文件','*.*')])
            result={'paths':list(paths)}
        print(json.dumps(result,ensure_ascii=True),flush=True)
    finally:root.destroy()

if __name__=='__main__':main()
