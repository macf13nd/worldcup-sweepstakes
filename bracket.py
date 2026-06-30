#!/usr/bin/env python3
"""Build the World Cup 2026 sweepstakes KNOCKOUT BRACKET page.

A purely additive, standalone companion to build.py — it does NOT touch the main
dashboard pipeline. It reuses build.py's pure helpers by import.

Data is pulled LIVE; nothing about teams/results/odds is hand-maintained:
  * Fixtures, results & scores — football-data.org (competitions/WC/matches).
    Round-of-32 ties are placed by GROUP STANDINGS, not kickoff order: each tie
    is mapped to its FIFA match number (73-88) via the official Winner/Runner-up
    group slots (assign_ro32_numbers + DEFINITE_SLOTS). The fixed bracket wiring
    lives in bracket.json.
  * Winners are propagated forward by US (via the wiring) the moment a tie
    finishes — we do NOT wait for the feed to advance the next-round fixture
    (which lags). Round-of-16+ results/odds are then matched by TEAM IDENTITY,
    not by match number (the feed's same-day numbering can't be assumed). A tie's
    winner is resolved by winner_side() — robust to penalty shootouts, where the
    feed can report a non-decisive score.winner.
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

# Knockout stages we care about -> their FIFA match-number ranges (informational;
# R32 ties are placed by group standings, not by these numbers — see assign_ro32_numbers).
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
    pen = score.get("penalties") or {}
    return {
        "home": (m.get("homeTeam") or {}).get("name"),
        "away": (m.get("awayTeam") or {}).get("name"),
        "hg": ft.get("home"), "ag": ft.get("away"),
        "ph": pen.get("home"), "pa": pen.get("away"),  # shootout goals only
        "duration": score.get("duration"),  # REGULAR / EXTRA_TIME / PENALTY_SHOOTOUT
        "status": m.get("status"),
        "winner": score.get("winner"),  # HOME_TEAM / AWAY_TEAM / DRAW / None
        "utcDate": m.get("utcDate"),
        "date": build.fmt_date(m.get("utcDate", "")),
    }


def winner_side(rec):
    """Resolve the winning side of a FINISHED tie: 'HOME_TEAM' / 'AWAY_TEAM' / None.

    score.winner is the source of truth, but for penalty shootouts the feed can
    return DRAW/None even though the tie was decided. Fall back to the penalty
    sub-score, then to a decisive fullTime (which, per the football-data API,
    already includes shootout goals — so a shootout always leaves it decisive).
    """
    if not rec or rec.get("status") != "FINISHED":
        return None
    w = rec.get("winner")
    if w in ("HOME_TEAM", "AWAY_TEAM"):
        return w
    ph, pa = rec.get("ph"), rec.get("pa")
    if ph is not None and pa is not None and ph != pa:
        return "HOME_TEAM" if ph > pa else "AWAY_TEAM"
    hg, ag = rec.get("hg"), rec.get("ag")
    if hg is not None and ag is not None and hg != ag:
        return "HOME_TEAM" if hg > ag else "AWAY_TEAM"
    return None


def fetch_knockout(key):
    """Return (ro32_records, all_records, err).

    ro32_records — the 16 Round-of-32 records (match number assigned later from
                   group positions, NOT kickoff order — FIFA numbers don't follow
                   the schedule).
    all_records  — every knockout record (later rounds match by team identity).
    """
    if not key:
        return [], [], "no FOOTBALL_DATA_KEY set"
    try:
        r = requests.get(
            f"{FD_BASE}/competitions/WC/matches",
            headers={"X-Auth-Token": key},
            timeout=30,
        )
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:  # noqa: BLE001
        return [], [], f"football-data error: {e}"

    ro32, all_records = [], []
    for m in matches:
        stage = m.get("stage")
        if stage not in STAGE_NOS:
            continue
        rec = _record(m)
        all_records.append(rec)
        if stage == "LAST_32":
            ro32.append(rec)
    return ro32, all_records, None


def fetch_standings(key):
    """canon(team) -> (group_letter, rank). Used to place R32 ties by group position."""
    if not key:
        return {}, "no FOOTBALL_DATA_KEY set"
    try:
        r = requests.get(
            f"{FD_BASE}/competitions/WC/standings",
            headers={"X-Auth-Token": key},
            timeout=30,
        )
        r.raise_for_status()
        groups = r.json().get("standings", [])
    except Exception as e:  # noqa: BLE001
        return {}, f"football-data standings error: {e}"

    pos = {}
    for g in groups:
        grp = (g.get("group") or "").split()[-1]  # "Group A" -> "A"
        for row in g.get("table", []):
            name = (row.get("team") or {}).get("name")
            if name and grp:
                pos[build.canon(name)] = (grp, row.get("position"))
    return pos, None


# Official FIFA 2026 R32 pairings — the definite (Winner/Runner-up) slot(s) per
# match number. The other side (where omitted) is a 3rd-placed team, which the
# group-position lookup treats as a wildcard. Source: Wikipedia knockout bracket.
DEFINITE_SLOTS = {
    73: {"RU_A", "RU_B"}, 74: {"W_E"}, 75: {"W_F", "RU_C"}, 76: {"W_C", "RU_F"},
    77: {"W_I"}, 78: {"RU_E", "RU_I"}, 79: {"W_A"}, 80: {"W_L"},
    81: {"W_D"}, 82: {"W_G"}, 83: {"RU_K", "RU_L"}, 84: {"W_H", "RU_J"},
    85: {"W_B"}, 86: {"W_J", "RU_H"}, 87: {"W_K"}, 88: {"RU_D", "RU_G"},
}
_DEF_BY_SET = {frozenset(v): n for n, v in DEFINITE_SLOTS.items()}


def assign_ro32_numbers(records, positions):
    """{match_no: record}, mapping each R32 tie to its FIFA number via group slots."""
    by_no = {}
    for rec in records:
        labels = set()
        for nm in (rec.get("home"), rec.get("away")):
            grp, rank = positions.get(build.canon(nm or ""), (None, None))
            if rank == 1:
                labels.add(f"W_{grp}")
            elif rank == 2:
                labels.add(f"RU_{grp}")
            # rank 3 (or unknown) is a wildcard 3rd-placed slot — ignore
        no = _DEF_BY_SET.get(frozenset(labels))
        if no:
            by_no[no] = rec
    return by_no


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
    side = winner_side(rec)
    if side:
        win_name = rec["home"] if side == "HOME_TEAM" else rec["away"]
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
    side = winner_side(rec)
    if side:
        wn = rec["home"] if side == "HOME_TEAM" else rec["away"]
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
    fd_key = os.environ.get("FOOTBALL_DATA_KEY")
    ro32, fd_all, fd_err = fetch_knockout(fd_key)
    positions, st_err = fetch_standings(fd_key)
    fd_by_no = assign_ro32_numbers(ro32, positions)
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
        "errors": [e for e in (fd_err, st_err, odds_err) if e],
    })
    out = ROOT / "docs" / "bracket.html"
    out.write_text(html)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build_bracket()
