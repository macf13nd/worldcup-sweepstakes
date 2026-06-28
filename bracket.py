#!/usr/bin/env python3
"""Build the World Cup 2026 sweepstakes KNOCKOUT BRACKET page.

A purely additive, standalone companion to build.py — it does NOT touch the main
dashboard pipeline. It reuses build.py's pure helpers by import.

Data is pulled LIVE; nothing about teams/results/odds is hand-maintained:
  * Fixtures, results & scores  — football-data.org (competitions/WC/matches).
    Each knockout stage is sorted by kickoff time and assigned official FIFA match
    numbers ascending (73-88 R32, 89-96 R16, 97-100 QF, 101-102 SF, 104 Final);
    the fixed bracket wiring lives in bracket.json.
  * Matchups & odds — the-odds-api `soccer_fifa_world_cup` h2h market, matched to
    each tie by kickoff time. The 3-way (home/draw/away) price becomes an
    "advance %" = normalised P(win) + P(draw)/2, so the pair sums to 100.
  * Owners come from draw.json, flags from flags.json; API team-name variants are
    resolved to our spellings via aliases.json (reverse index).

Run locally with `.venv/bin/python bracket.py`. Degrades gracefully with no keys.
"""

import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

import build  # reuse helpers only — importing runs no network code (guarded by __main__)
import json

ROOT = Path(__file__).parent
FD_BASE = "https://api.football-data.org/v4"
ODDS_BASE = "https://api.the-odds-api.com/v4"

# Official FIFA match numbers per knockout stage, in chronological (kickoff) order.
STAGE_NOS = {
    "LAST_32": list(range(73, 89)),
    "LAST_16": list(range(89, 97)),
    "QUARTER_FINALS": list(range(97, 101)),
    "SEMI_FINALS": [101, 102],
    "THIRD_PLACE": [103],
    "FINAL": [104],
}


# ---------------------------------------------------------------------------
# Name resolution (API spelling -> our draw/flags spelling)
# ---------------------------------------------------------------------------
def reverse_alias(draw, aliases):
    """canon(any known spelling) -> our canonical (draw) team name."""
    rev = {}
    for teams in draw.values():
        for t in teams:
            rev[build.canon(t)] = t
            for alt in aliases.get(t, []):
                rev[build.canon(alt)] = t
    return rev


def owner_index(draw):
    """team name -> owner."""
    return {team: owner for owner, teams in draw.items() for team in teams}


# ---------------------------------------------------------------------------
# Live data
# ---------------------------------------------------------------------------
def fetch_knockout(key):
    """{match_no: {home, away, status, winner, score, utcDate, date}} from football-data."""
    if not key:
        return {}, "no FOOTBALL_DATA_KEY set"
    try:
        r = requests.get(
            f"{FD_BASE}/competitions/WC/matches",
            headers={"X-Auth-Token": key},
            timeout=30,
        )
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:  # noqa: BLE001
        return {}, f"football-data error: {e}"

    by_stage = defaultdict(list)
    for m in matches:
        if m.get("stage") in STAGE_NOS:
            by_stage[m["stage"]].append(m)

    out = {}
    for stage, nos in STAGE_NOS.items():
        ms = sorted(by_stage.get(stage, []), key=lambda x: x.get("utcDate") or "")
        for no, m in zip(nos, ms):
            score = m.get("score") or {}
            ft = score.get("fullTime") or {}
            hg, ag = ft.get("home"), ft.get("away")
            out[no] = {
                "home": (m.get("homeTeam") or {}).get("name"),
                "away": (m.get("awayTeam") or {}).get("name"),
                "status": m.get("status"),
                "winner": score.get("winner"),  # HOME_TEAM / AWAY_TEAM / DRAW / None
                "score": f"{hg}–{ag}" if hg is not None and ag is not None else None,
                "utcDate": m.get("utcDate"),
                "date": build.fmt_date(m.get("utcDate", "")),
            }
    return out, None


def advance_pcts(prices):
    """3-way decimal prices {name|'Draw': dec} -> {canon_team: advance %} summing to 100.

    De-vig by normalising implied probabilities, then split the draw evenly
    (knockouts can't draw, so a draw resolves 50/50 in expectation)."""
    impl = {k: 1.0 / v for k, v in prices.items() if v}
    s = sum(impl.values()) or 1.0
    impl = {k: v / s for k, v in impl.items()}
    draw = impl.get("Draw", 0.0)
    return {
        build.canon(name): round(100 * (p + draw / 2))
        for name, p in impl.items() if name != "Draw"
    }


def fetch_h2h(key):
    """{utc_minute: {home, away, adv:{canon_team: %}}} from the-odds-api h2h market."""
    if not key:
        return {}, "no ODDS_API_KEY set"
    try:
        r = requests.get(
            f"{ODDS_BASE}/sports/soccer_fifa_world_cup/odds",
            params={"regions": "uk,eu,us", "markets": "h2h", "oddsFormat": "decimal", "apiKey": key},
            timeout=30,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as e:  # noqa: BLE001
        return {}, f"the-odds-api error: {e}"

    out = {}
    for e in events:
        prices = defaultdict(list)
        for book in e.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for o in market.get("outcomes", []):
                    if isinstance(o.get("price"), (int, float)) and o["price"] > 0:
                        prices[o["name"]].append(float(o["price"]))
        med = {k: statistics.median(v) for k, v in prices.items() if v}
        out[e["commence_time"][:16]] = {
            "home": e.get("home_team"),
            "away": e.get("away_team"),
            "adv": advance_pcts(med),
        }
    return out, None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def teams_for(fd, ev):
    """Resolve the two team names for a tie, filling gaps from the odds feed."""
    h, a = fd.get("home"), fd.get("away")
    if ev:
        oh, oa = ev.get("home"), ev.get("away")
        if not h and not a:
            h, a = oh, oa
        elif h and not a and oh and oa:
            a = oa if build.canon(oa) != build.canon(h) else oh
        elif a and not h and oh and oa:
            h = oh if build.canon(oh) != build.canon(a) else oa
    return h, a


def make_slot(name, pos, spec, fd, ev, ctx):
    """Build one render-ready slot (a team, a 'winner of Wxx', or TBD)."""
    if name:
        our = ctx["rev"].get(build.canon(name), name)
        slot = {
            "kind": "team", "label": our,
            "owner": ctx["owner"].get(our),
            "flag": build.flag_url(our, ctx["flags"]),
            "progress": None, "dead": False, "won": False,
        }
        finished = fd.get("status") == "FINISHED"
        winner = fd.get("winner")
        if finished and winner in ("HOME_TEAM", "AWAY_TEAM"):
            won = (winner == "HOME_TEAM") == (pos == "a")
            slot["won"], slot["dead"] = won, not won
        elif ev:
            slot["progress"] = ev["adv"].get(build.canon(our))
        return slot

    sub = spec.get(pos) or {}
    if "from" in sub:
        child = ctx["matches"][sub["from"]]
        return {"kind": "from", "label": f"W{child['no']}"}
    return {"kind": "tbd", "label": "TBD"}


def resolve_match(key, spec, ctx):
    fd = ctx["fd"].get(spec["no"], {})
    ev = ctx["odds"].get(fd.get("utcDate", "")[:16]) if fd.get("utcDate") else None
    home, away = teams_for(fd, ev)
    return {
        "id": key, "no": spec["no"], "round": spec["round"], "side": spec["side"],
        "score": fd.get("score"), "date": fd.get("date"),
        "finished": fd.get("status") == "FINISHED",
        "a": make_slot(home, "a", spec, fd, ev, ctx),
        "b": make_slot(away, "b", spec, fd, ev, ctx),
    }


def render_page(ctx_template):
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template("bracket.html").render(**ctx_template)


def build_bracket():
    build.load_env()
    draw = json.loads((ROOT / "draw.json").read_text())
    aliases = json.loads((ROOT / "aliases.json").read_text())
    aliases = {k: v for k, v in aliases.items() if not k.startswith("_")}
    flags = json.loads((ROOT / "flags.json").read_text())
    flags = {k: v for k, v in flags.items() if not k.startswith("_")}
    spec = json.loads((ROOT / "bracket.json").read_text())
    matches = {k: v for k, v in spec["matches"].items() if not k.startswith("_")}

    rev = reverse_alias(draw, aliases)
    fd, fd_err = fetch_knockout(os.environ.get("FOOTBALL_DATA_KEY"))
    odds, odds_err = fetch_h2h(os.environ.get("ODDS_API_KEY"))

    # Re-key each event's advance% to OUR canonical team names, so a slot named
    # "United States" matches an odds outcome named "USA" (etc.) via aliases.
    for ev in odds.values():
        ev["adv"] = {build.canon(rev.get(ck, ck)): v for ck, v in ev["adv"].items()}

    ctx = {
        "rev": rev,
        "owner": owner_index(draw),
        "flags": flags,
        "fd": fd,
        "odds": odds,
        "matches": matches,
    }
    built = {k: resolve_match(k, s, ctx) for k, s in matches.items()}

    left = [[] for _ in range(4)]
    right = [[] for _ in range(4)]
    final = None
    for m in built.values():
        if m["round"] == 4:
            final = m
        elif m["side"] == "L":
            left[m["round"]].append(m)
        elif m["side"] == "R":
            right[m["round"]].append(m)

    html = render_page({
        "round_names": spec["rounds"][:4],
        "left": left,
        "right": right,
        "final": final,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "have_odds": bool(odds),
        "have_fixtures": bool(fd),
        "errors": [e for e in (fd_err, odds_err) if e],
    })
    out = ROOT / "docs" / "bracket.html"
    out.write_text(html)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build_bracket()
