from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_mem_bridge.storage import MemoryStore  # noqa: E402
from agent_mem_bridge.write_side_capture import capture_lifecycle_candidates  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture review-only memory candidates from one bounded lifecycle event."
    )
    parser.add_argument("event", type=Path, help="JSON lifecycle capture event.")
    parser.add_argument("--db", type=Path, required=True, help="Bridge database to update.")
    parser.add_argument("--log-dir", type=Path, required=True, help="Operational log directory.")
    args = parser.parse_args(argv)

    event = json.loads(args.event.read_text(encoding="utf-8"))
    store = MemoryStore(db_path=args.db, log_dir=args.log_dir)
    receipt = capture_lifecycle_candidates(store, event)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    if receipt.get("automatic_promotion"):
        return 1
    if receipt.get("disposition") == "rejected":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
