#!/bin/sh
set -eu
cd "$(dirname "$0")/../frontend"
exec npm run dev
