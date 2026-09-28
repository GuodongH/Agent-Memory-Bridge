from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_mem_bridge.write_side_capture import run_write_side_capture_benchmark  # noqa: E402

DEFAULT_CASES_PATH = ROOT / "benchmark" / "write-side-capture-cases.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the frozen write-side capture benchmark.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--report-path", type=Path)
    args = parser.parse_args(argv)

    report = run_write_side_capture_benchmark(args.cases)
    if args.report_path is not None:
        args.report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "case_count": report["case_count"],
        "failed_case_count": report["failed_case_count"],
        "failed_case_ids": report["failed_case_ids"],
        "positive_capture_count": report["positive_capture_count"],
        "negative_control_write_count": report["negative_control_write_count"],
        "automatic_promotion_count": report["automatic_promotion_count"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report["failed_case_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
