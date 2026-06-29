#!/usr/bin/env python3
"""Build the World Cup 2026 sweepstakes KNOCKOUT BRACKET page.

A purely additive, standalone companion to build.py — it does NOT touch the main
dashboard pipeline. It reuses build.py's pure helpers by import.

Data is pulled LIVE; nothing about teams/results/odds is hand-maintained:
  * Fixtures, results & scores — football-data.org (competitions/WC/matches).
    Round-of-32 ties are placed by sorting that stage by kickoff and assigning
    FIFA match numbers (73-88); the fixed bracket wiring lives in bracket.json.
  * Winners are propagated forward by US (via the wiring) the moment a tie
    finishes — we do NOT wait for the feed to advance the next-round fixture
    (which lags). Round-of-16+ results/odds are then matched by TEAM IDENTITY,
    not by match number (the feed's same-day numbering can't be assumed).
  * Matchups & odds — the-odds-api `soccer_fifa_world_cup` h2h market. The 3-way
    price becomes an "advance %" = normalised P(win) + P(draw)/2 (sums to 100).
  * Owners from draw.json, flags from flags.json; API name variants resolved to
    our spellings via aliases.json (reverse index).

Run locally with `.venv/bin/python bracket.py`. Degrades gracefully with no keys.
"""

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

import build  # reuse helpers only — importing runs no network code (guarded by __main__)

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


def our_name(name, rev):
    """Resolve any team spelling to our canonical (draw) spelling."""
    return rev.get(build.canon(name), name) if name else name


def team_key(a, b, rev):
    """Stable key for a tie, independent of home/away order or spelling."""
    return frozenset({build.canon(our_name(a, rev)), build.canon(our_name(b, rev))})


# ---------------------------------------------------------------------------
# Live data
# ---------------------------------------------------------------------------
def _record(m):
    score = m.get("score") or {}
    ft = score.get("fullTime") or {}
    return {
        "home": (m.get("homeTeam") or {}).get("name"),
        "away": (m.get("awayTeam") or {}).get("name"),
        "hg": ft.get("home"), "ag": ft.get("away"),
        "status": m.get("status"),
        "winner": score.get("winner"),  # HOME_TEAM / AWAY_TEAM / DRAW / None
        "utcDate": m.get("utcDate"),
        "date": build.fmt_date(m.get("utcDate", "")),
    }


def fetch_knockout(key):
    """Return (by_no, all_records, err).

    by_no       — {match_no: record} for the Round of 32 (chronological → 73-88).
    all_records — every knockout record (used to match later rounds by team identity).
    """
    if not key:
        return {}, [], "no FOOTBALL_DATA_KEY set"
    try:
        r = requests.get(
            f"{FD_BASE}/competitions/WC/matches",
            headers={"X-Auth-Token": key},
            timeout=30,
        )
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:  # noqa: BLE001
        return {}, [], f"football-data error: {e}"

    by_stage = defaultdict(list)
    for m in matches:
        if m.get("stage") in STAGE_NOS:
            by_stage[m["stage"]].append(m)

    by_no, all_records = {}, []
    for stage, nos in STAGE_NOS.items():
        ms = sorted(by_stage.get(stage, []), key=lambda x: x.get("utcDate") or "")
        for no, m in zip(nos, ms):
            rec = _record(m)
            all_records.append(rec)
            if stage == "LAST_32":
                by_no[no] = rec
    return by_no, all_records, None


def advance_pcts(prices):
    """3-way decimal prices {name|'Draw': dec} -> {canon_team: advance %} summing to 100."""
    impl = {k: 1.0 / v for k, v in prices.items() if v}
    s = sum(impl.values()) or 1.0
    impl = {k: v / s for k, v in impl.items()}
    draw = impl.get("Draw", 0.0)
    return {
        build.canon(name): round(100 * (p + draw / 2))
        for name, p in impl.items() if name != "Draw"
    }


def fetch_h2h(key):
    """Return (events, err); each event {home, away, adv:{canon: %}, commence}."""
    if not key:
        return [], "no ODDS_API_KEY set"
    try:
        r = requests.get(
            f"{ODDS_BASE}/sports/soccer_fifa_world_cup/odds",
            params={"regions": "uk,eu,us", "markets": "h2h", "oddsFormat": "decimal", "apiKey": key},
            timeout=30,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as e:  # noqa: BLE001
        return [], f"the-odds-api error: {e}"

    out = []
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
        out.append({
            "home": e.get("home_team"), "away": e.get("away_team"),
            "adv": advance_pcts(med), "commence": (e.get("commence_time") or "")[:16],
        })
    return out, None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def teams_for(rec, ev):
    """Resolve a tie's two team names from the fixtures rec, filling gaps from odds."""
    h, a = rec.get("home"), rec.get("away")
    if ev:
        oh, oa = ev.get("home"), ev.get("away")
        if not h and not a:
            h, a = oh, oa
        elif h and not a and oh and oa:
            a = oa if build.canon(oa) != build.canon(h) else oh
        elif a and not h and oh and oa:
            h = oh if build.canon(oh) != build.canon(a) else oa
    return h, a


def team_slot(name, rec, ev, ctx):
    our = our_name(name, ctx["rev"])
    slot = {
        "kind": "team", "label": our,
        "owner": ctx["owner"].get(our),
        "flag": build.flag_url(our, ctx["flags"]),
        "progress": None, "won": False, "dead": False,
    }
    if rec and rec.get("status") == "FINISHED" and rec.get("winner") in ("HOME_TEAM", "AWAY_TEAM"):
        win_name = rec["home"] if rec["winner"] == "HOME_TEAM" else rec["away"]
        won = build.canon(our) == build.canon(our_name(win_name, ctx["rev"]))
        slot["won"], slot["dead"] = won, not won
    elif ev:
        slot["progress"] = ev["adv"].get(build.canon(our))
    return slot


def from_slot(spec, pos, ctx):
    sub = spec.get(pos) or {}
    if "from" in sub:
        return {"kind": "from", "label": f'W{ctx["matches"][sub["from"]]["no"]}'}
    return {"kind": "tbd", "label": "TBD"}


def resolve(key, spec, ctx, winners):
    """Resolve one match -> (render dict, winning-team-name or None)."""
    rnd = spec["round"]
    if rnd == 0:
        rec = ctx["fd_by_no"].get(spec["no"], {})
        ev0 = ctx["odds_time"].get((rec.get("utcDate") or "")[:16])
        home, away = teams_for(rec, ev0)
    else:
        home = winners.get((spec.get("a") or {}).get("from"))
        away = winners.get((spec.get("b") or {}).get("from"))
        rec = ctx["fd_teams"].get(team_key(home, away, ctx["rev"]), {}) if home and away else {}

    ev = None
    if home and away:
        ev = ctx["odds_teams"].get(team_key(home, away, ctx["rev"]))
    elif rnd == 0:
        ev = ctx["odds_time"].get((rec.get("utcDate") or "")[:16])

    a = team_slot(home, rec, ev, ctx) if home else from_slot(spec, "a", ctx)
    b = team_slot(away, rec, ev, ctx) if away else from_slot(spec, "b", ctx)

    # Score, oriented to our a/b order (rec may list teams the other way round).
    score = None
    if rec and rec.get("hg") is not None and rec.get("ag") is not None and home and away:
        a_is_home = build.canon(our_name(home, ctx["rev"])) == build.canon(our_name(rec["home"], ctx["rev"]))
        score = f'{rec["hg"]}–{rec["ag"]}' if a_is_home else f'{rec["ag"]}–{rec["hg"]}'

    wname = None
    if rec and rec.get("winner") in ("HOME_TEAM", "AWAY_TEAM"):
        wn = rec["home"] if rec["winner"] == "HOME_TEAM" else rec["away"]
        wname = our_name(wn, ctx["rev"])

    m = {"id": key, "no": spec["no"], "round": rnd, "side": spec["side"],
         "a": a, "b": b, "score": score, "date": (rec or {}).get("date")}
    return m, wname


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
    fd_by_no, fd_all, fd_err = fetch_knockout(os.environ.get("FOOTBALL_DATA_KEY"))
    events, odds_err = fetch_h2h(os.environ.get("ODDS_API_KEY"))

    # Index live data by team identity (robust) and by kickoff time (R32 fallback).
    fd_teams = {
        team_key(r["home"], r["away"], rev): r
        for r in fd_all if r.get("home") and r.get("away")
    }
    odds_teams, odds_time = {}, {}
    for ev in events:
        ev["adv"] = {build.canon(our_name(name, rev)): v for name, v in ev["adv"].items()}
        odds_time[ev["commence"]] = ev
        if ev.get("home") and ev.get("away"):
            odds_teams[team_key(ev["home"], ev["away"], rev)] = ev

    ctx = {
        "rev": rev, "owner": owner_index(draw), "flags": flags, "matches": matches,
        "fd_by_no": fd_by_no, "fd_teams": fd_teams,
        "odds_teams": odds_teams, "odds_time": odds_time,
    }

    # Resolve round by round so each tie's winner propagates forward immediately.
    built, winners = {}, {}
    for rnd in range(5):
        for key, mspec in matches.items():
            if mspec["round"] != rnd:
                continue
            m, wname = resolve(key, mspec, ctx, winners)
            built[key] = m
            winners[key] = wname

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
        "left": left, "right": right, "final": final,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "have_odds": bool(events), "have_fixtures": bool(fd_all),
        "errors": [e for e in (fd_err, odds_err) if e],
    })
    out = ROOT / "docs" / "bracket.html"
    out.write_text(html)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build_bracket()
