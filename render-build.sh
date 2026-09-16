#!/usr/bin/env bash
set -e

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ffmpeg
fi

python -m pip install --upgrade pip
python -m pip install --upgrade -r requirements.txt
