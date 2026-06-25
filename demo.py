#!/usr/bin/env python3
"""Visual, offline demo of the leaderboard movement arrows.

Fabricates two days of standings — NO API keys, NO real fixtures — so you can
SEE green ▲ / red ▼ / flat – / new · render immediately.

Run:  .venv/bin/python demo.py  &&  open docs/demo.html
"""
from build import compute_movement, render_page, ROOT

NAMES = [
    "Shah Qureshi", "Simon Champion-MacPherson", "Darren Cooney", "Sean Blakeley",
    "Chris McConney", "Adam Hollister", "Carmine Colicino", "Ryan Golds",
    "Dimitra Chaloglou", "Georgia Cottle", "Tom Reed", "John Jeeves",
]

# "Yesterday" omits John Jeeves so he renders as a NEW entrant today.
yesterday = [n for n in NAMES if n != "John Jeeves"]

# "Today" is a deliberate reshuffle to produce a clear up/down/same/new spread.
today_names = [
    "Darren Cooney",             # up
    "Shah Qureshi",              # down
    "Tom Reed",                  # big climb
    "Sean Blakeley",            # ~same
    "Adam Hollister",            # up
    "John Jeeves",               # NEW (absent yesterday)
    "Chris McConney",            # down
    "Simon Champion-MacPherson", # down
    "Ryan Golds",                # ~same
    "Dimitra Chaloglou",         # ~same
    "Georgia Cottle",            # ~same
    "Carmine Colicino",          # down
]

PCTS = [27.8, 23.4, 19.1, 14.6, 11.9, 9.4, 7.0, 5.1, 3.2, 2.0, 1.0, 0.3]


def fake_team(nm, status):
    return {
        "name": nm, "flag": None, "status": status,
        "result": "W 1-0 vs Demo", "outcome": "W", "score": "1-0", "last_opp": "Demo",
        "next_opp": "Demo Utd" if status == "in" else None,
        "next_date": "Sat 27 Jun" if status == "in" else None,
        "odds_frac": "5/1", "odds_implied": 16.7, "matched": True,
    }


def main():
    movement = compute_movement(today_names, yesterday)
    leaderboard = []
    for i, name in enumerate(today_names):
        alive = [4, 4, 3, 4, 4, 2, 3, 3, 4, 4, 2, 1][i]
        teams = [fake_team(f"{name.split()[0]} Team {j + 1}", "in" if j < alive else "out")
                 for j in range(4)]
        leaderboard.append({
            "name": name, "win_pct": PCTS[i], "alive": alive, "total": 4,
            "teams": teams, "move": movement.get(name),
        })

    people = sorted(leaderboard, key=lambda p: p["name"])  # standings ordered by name
    html = render_page(people, leaderboard, {
        "updated": "DEMO — synthetic data (movement preview)",
        "have_odds": True, "have_results": True, "errors": [],
    })
    out = ROOT / "docs" / "demo.html"
    out.write_text(html)
    print(f"Wrote {out}")
    print("Open it:  open docs/demo.html")
    print("\nExpected leaderboard arrows:")
    for i, name in enumerate(today_names, 1):
        mv = movement[name]
        sym = {"up": f"▲{mv['delta']}", "down": f"▼{mv['delta']}",
               "same": "–", "new": "·(new)"}[mv["dir"]]
        print(f"  {i:2}. {sym:7} {name}")


if __name__ == "__main__":
    main()
