"""Gate scheduled discovery to alternate weeks; manual runs always proceed."""

from __future__ import annotations

import argparse
import datetime as dt


def scan_due(event: str, today: dt.date, anchor: dt.date) -> bool:
    """Use Monday-anchored weeks so a delayed Monday run still works."""
    if anchor.weekday() != 0:
        raise ValueError("The biweekly anchor must be a Monday")
    if event == "workflow_dispatch":
        return True
    days = (today - anchor).days
    return event == "schedule" and days >= 0 and (days // 7) % 2 == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--anchor", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--date", type=dt.date.fromisoformat,
                        default=dt.datetime.now(dt.timezone.utc).date())
    args = parser.parse_args()
    try:
        due = scan_due(args.event, args.date, args.anchor)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"run={str(due).lower()}")
    if args.event == "workflow_dispatch":
        reason = "Manual run: execute discovery v2 and optional DeepSeek review."
    elif due:
        reason = f"Biweekly scan due: {args.date} (UTC; anchor {args.anchor})."
    else:
        reason = (f"No scan due: {args.date} (UTC; anchor {args.anchor}). "
                  "Discovery and DeepSeek are skipped.")
    print(f"reason={reason}")


if __name__ == "__main__":
    main()
