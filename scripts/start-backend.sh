#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -B -u -m lecture_asr.local_server --duration 6000 --summary
