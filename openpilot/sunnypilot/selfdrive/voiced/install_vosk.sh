#!/usr/bin/env bash
# Install Vosk and the small Vietnamese model for voiced under /data/voice (the root filesystem is read-only).
# Run once on the comma over SSH, with internet: bash install_vosk.sh
set -euo pipefail

VOICE_DIR="${VOICED_DIR:-/data/voice}"
MODEL="vosk-model-small-vn-0.4"
PY="${PYTHON:-/usr/local/venv/bin/python}"

mkdir -p "$VOICE_DIR/pylib"

# vosk only; its other requirements (requests, tqdm, srt, websockets) are for downloading models, not needed here
"$PY" -m pip install --no-deps --target "$VOICE_DIR/pylib" "vosk==0.3.45"
"$PY" -c "import cffi" 2>/dev/null || "$PY" -m pip install --target "$VOICE_DIR/pylib" cffi

if [ ! -d "$VOICE_DIR/$MODEL" ]; then
  tmp="$(mktemp -d /data/tmp.voice.XXXXXX)"
  curl -fL -o "$tmp/$MODEL.zip" "https://alphacephei.com/vosk/models/$MODEL.zip"
  "$PY" -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$tmp/$MODEL.zip" "$VOICE_DIR"
  rm -rf "$tmp"
fi

PYTHONPATH="$VOICE_DIR/pylib" "$PY" -c "import vosk; vosk.SetLogLevel(-1); vosk.Model('$VOICE_DIR/$MODEL'); print('vosk + model OK')"
echo "Turn on: Settings > Cruise > Voice set speed (or: echo -n 1 > /data/params/d/VoiceCruiseControl), then restart onroad."
