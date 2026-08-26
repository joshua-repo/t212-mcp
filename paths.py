# paths.py
"""Filesystem locations shared across the app — the one place that knows where things live."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

ENV_FILE = PROJECT_ROOT / ".env"

STATE_DIR = Path(os.environ.get("MCP_STATE_DIR", PROJECT_ROOT))
STATE_FILE = STATE_DIR / "oauth_state.json"
