"""Daily experience distill entry point.

Run once a day at 23:59:59 (via crontab) to summarise the engine's
day into:

1. A new §-delimited entry in /root/.hermes/memories/MEMORY.md
2. A daily skill at /root/.hermes/skills/daily-YYYY-MM-DD/SKILL.md

Idempotent-ish: rerunning the same day overwrites the daily skill
(deterministic path). The memory entry is deduplicated by Jaccard
overlap with existing entries.

Crontab install:
  59 23 * * * /root/hermes/venv/bin/python -m hermes_collab_engine.distill.daily_distill \\
    >> /var/log/hermes-distill.log 2>&1

CLI:
  python -m hermes_collab_engine.distill.daily_distill
  python -m hermes_collab_engine.distill.daily_distill --dry-run
  python -m hermes_collab_engine.distill.daily_distill --date 2026-06-15
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow `python -m hermes_collab_engine.distill.daily_distill` from
# the repo's src/ tree.
_SRC = Path("/root/hermes-collab-engine/src")
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_collab_engine.distill.extractor import fetch_today, summarise
from hermes_collab_engine.distill.memory_writer import append_entry
from hermes_collab_engine.distill.skill_writer import write_skill


def run(*, date: str | None = None, dry_run: bool = False) -> dict:
    """End-to-end: extract → summarise → write memory + skill.

    Returns a status dict suitable for logging or the cron caller.
    """
    # If --date given we use it as the SQL day filter; otherwise today.
    # (bug fix 2026-06-17: previously --date only changed the label
    #  but fetch_today still queried 'now, localtime', so back-fills
    #  silently returned 0 events for any historical date.)
    events = fetch_today(day=date)
    summary = summarise(events, day=date)

    title = f"Daily distill {summary['date']}"
    body_lines = [
        summary["leader_sentence"],
        "",
        f"counts: {json.dumps(summary['counts'], sort_keys=True, ensure_ascii=False)}",
        f"events captured: {len(events)}",
        f"meaningful: {summary['meaningful']}",
    ]
    body = "\n".join(body_lines)

    out: dict = {
        "date": summary["date"],
        "counts": summary["counts"],
        "events": len(events),
        "meaningful": summary["meaningful"],
        "leader_sentence": summary["leader_sentence"],
    }
    if dry_run:
        out["dry_run"] = True
        return out

    memory_result = append_entry(title, body)
    skill_result = write_skill(summary)
    out["memory"] = memory_result
    out["skill"] = skill_result
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily experience distill")
    parser.add_argument("--date", help="Override date label (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Compute summary but don't write files")
    args = parser.parse_args(argv)
    try:
        result = run(date=args.date, dry_run=args.dry_run)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
