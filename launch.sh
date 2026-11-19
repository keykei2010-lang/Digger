#!/bin/sh
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
export RECOVERY_BIN_DIR="$root/engines"
export PYTHONHOME="$root/runtime"
export PYTHONNOUSERSITE=1
export TERMINFO="$root/runtime/share/terminfo"
export LD_LIBRARY_PATH="$root/runtime/lib"
exec "$root/runtime/bin/python3" -B "$root/server.py"
