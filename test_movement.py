#!/usr/bin/env python3
"""Pure unit tests for leaderboard movement + snapshot roll.

No network, no clock, no API keys. Run: .venv/bin/python test_movement.py
"""
from build import compute_movement, roll_snapshot


def check(label, got, want):
    assert got == want, f"FAIL {label}: got {got!r}, want {want!r}"
    print(f"  ok  {label}")


def test_compute_movement():
    print("compute_movement:")
    base = ["A", "B", "C", "D"]

    # Full reversal: D climbs 3, A slips 3, B/C swap by 1
    m = compute_movement(["D", "C", "B", "A"], base)
    check("D climbs 3", m["D"], {"dir": "up", "delta": 3})
    check("A slips 3", m["A"], {"dir": "down", "delta": 3})
    check("C up 1", m["C"], {"dir": "up", "delta": 1})
    check("B down 1", m["B"], {"dir": "down", "delta": 1})

    # No change
    m = compute_movement(base, base)
    for n in base:
        check(f"{n} unchanged", m[n], {"dir": "same", "delta": 0})

    # No baseline -> everyone new
    m = compute_movement(base, None)
    for n in base:
        check(f"{n} new (no baseline)", m[n], {"dir": "new", "delta": 0})

    # New entrant pushes others down
    m = compute_movement(["E", "A", "B"], ["A", "B", "C"])
    check("E new", m["E"], {"dir": "new", "delta": 0})
    check("A down 1", m["A"], {"dir": "down", "delta": 1})
    check("B down 1", m["B"], {"dir": "down", "delta": 1})


def test_roll_snapshot():
    print("roll_snapshot:")
    o1, o2, o3 = ["A", "B", "C"], ["B", "A", "C"], ["C", "B", "A"]

    # First ever run: no file, no baseline
    base, snap = roll_snapshot({}, o1, "2026-06-26")
    check("first run baseline None", base, None)
    check("first run previous None", snap["previous"], None)
    check("first run current set", snap["current"], {"date": "2026-06-26", "order": o1})

    # New day: yesterday's current becomes the baseline
    base, snap2 = roll_snapshot(snap, o2, "2026-06-27")
    check("day2 baseline = day1 order", base, o1)
    check("day2 previous = day1", snap2["previous"], {"date": "2026-06-26", "order": o1})
    check("day2 current = day2", snap2["current"], {"date": "2026-06-27", "order": o2})

    # Same-day re-run: baseline frozen, current refreshes
    base, snap3 = roll_snapshot(snap2, o3, "2026-06-27")
    check("rerun baseline still day1", base, o1)
    check("rerun current refreshed", snap3["current"], {"date": "2026-06-27", "order": o3})
    check("rerun previous unchanged", snap3["previous"], {"date": "2026-06-26", "order": o1})


if __name__ == "__main__":
    test_compute_movement()
    test_roll_snapshot()
    print("\nAll movement tests passed.")
