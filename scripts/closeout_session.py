from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from agent_mem_bridge.paths import (
    resolve_bridge_db_path,
    resolve_bridge_home,
    resolve_bridge_log_dir,
)
from agent_mem_bridge.session_closeout import closeout_session_from_json
from agent_mem_bridge.storage import MemoryStore


def _resolve_notes_root() -> Path:
    raw = os.environ.get("AGENT_MEMORY_BRIDGE_NOTES_ROOT")
    if raw:
        return Path(raw).expanduser()
    return resolve_bridge_home() / "session-notes" / "auto"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/closeout_session.py <payload.json>")

    payload_path = Path(sys.argv[1]).resolve()
    store = MemoryStore(
        db_path=resolve_bridge_db_path(),
        log_dir=resolve_bridge_log_dir(),
    )
    result = closeout_session_from_json(store, payload_path, _resolve_notes_root())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
