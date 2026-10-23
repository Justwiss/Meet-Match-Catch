#!/usr/bin/env bash
# StudiRadar-Demo auf Mac/Linux starten (Mac: Doppelklick, Linux: ./start-demo.command)
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Richte StudiRadar einmalig ein, das dauert ca. 1 Minute ..."
  python3 -m venv .venv || { echo "Python 3.11+ fehlt: https://www.python.org/downloads/"; exit 1; }
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt || exit 1
fi
.venv/bin/python -m studiradar demo
