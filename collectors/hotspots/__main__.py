import argparse
import json
from pathlib import Path

from .runner import build_from_env


def main():
    parser = argparse.ArgumentParser(description="Together hotspot collector")
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--submit", action="store_true")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    selected = "submit" if args.submit else "dry-run"
    runner = build_from_env(args.config)
    try:
        with runner.state.lock():
            report = runner.run(selected)
    except Exception as exc:
        report = {"schema_version": 1, "mode": selected, "fatal_error": str(exc)}
        code = 1
    else:
        code = 2 if report.get("source_errors") or report.get("counts", {}).get("failed") else 0
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
