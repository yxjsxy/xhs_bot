#!/usr/bin/env bash
# Wrapper to run XHS scripts with the skill's venv
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${SKILL_DIR}/.venv"

if [ ! -d "$VENV" ]; then
  echo '{"ok":false,"error":"venv not found. Run: cd '"$SKILL_DIR"' && python3 -m venv .venv && source .venv/bin/activate && pip install playwright && playwright install chromium"}'
  exit 2
fi

source "$VENV/bin/activate"
python3 "$SKILL_DIR/scripts/$1.py" "${@:2}"
