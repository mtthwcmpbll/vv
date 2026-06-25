#!/usr/bin/env bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
uv cache clean vv
uv tool install $SCRIPT_DIR --force --reinstall --no-cache