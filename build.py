#!/usr/bin/env python3
"""Build the World Cup 2026 sweepstakes dashboard.

Fetches match results (football-data.org) and outright winner odds
(the-odds-api.com), works out who's still in, and renders a self-contained
HTML page to docs/index.html.

Runs fine with no API keys — it just renders a "waiting for data" page so the
layout can be previewed. Set FOOTBALL_DATA_KEY and ODDS_API_KEY (e.g. via a
local .env or GitHub Actions secrets) for live data.
"""

import json
import os
import statistics
import unicodedata
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parent
FD_BASE = "https://api.football-data.org/v4"
ODDS_SPORT = "soccer_fifa_world_cup_winner"
ODDS_BASE = "https://api.the-odds-api.com/v4"


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------
def canon(name: str) -> str:
    """Accent-stripped, lowercased, alnum-only key for fuzzy name matching."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return "".join(c for c in no_accents.lower() if c.isalnum())


def to_fractional(decimal):
    """Decimal odds -> UK/European fractional string, e.g. 5.0 -> '4/1', 3.5 -> '5/2'."""
    if not decimal or decimal <= 1:
        return None
    net = decimal - 1
    if net >= 10:  # long shots: clean whole-number fractions
        return f"{round(net)}/1"
    fr = Fraction(net).limit_denominator(20)
    return f"{fr.numerator}/{fr.denominator}"


def flag_url(team, flags):
    code = flags.get(team)
    return f"https://flagcdn.com/h20/{code}.png" if code else None


def load_env():
    """Minimal .env loader (no dependency on python-dotenv)."""
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_results(key):
    """Return {canon_team: {'text': 'W 2-0 vs Opp', 'date': iso, 'sort': dt}}.

    Keeps the most recent finished match per team.
    """
    if not key:
        return {}, "no FOOTBALL_DATA_KEY set"
    try:
        r = requests.get(
            f"{FD_BASE}/competitions/WC/matches?status=FINISHED",
            headers={"X-Auth-Token": key},
            timeout=30,
        )
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:  # noqa: BLE001 - surface any failure on the page
        return {}, f"football-data error: {e}"

    results = {}
    for m in matches:
        home = m.get("homeTeam", {}).get("name") or ""
        away = m.get("awayTeam", {}).get("name") or ""
        ft = (m.get("score") or {}).get("fullTime") or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        when = m.get("utcDate", "")
        for team, opp, gf, ga in ((home, away, hg, ag), (away, home, ag, hg)):
            outcome = "W" if gf > ga else "L" if gf < ga else "D"
            entry = {
                "text": f"{outcome} {gf}-{ga} vs {opp}",
                "outcome": outcome,
                "date": when,
                "sort": when,
            }
            ck = canon(team)
            if ck not in results or when > results[ck]["sort"]:
                results[ck] = entry
    return results, None


def fetch_odds(key):
    """Return ({canon_team: {'decimal': float, 'implied': pct}}, alive_set, err).

    alive_set = canon names still present in the outright winner market
    (bookmakers drop eliminated teams), used as the elimination signal.
    """
    if not key:
        return {}, None, "no ODDS_API_KEY set"
    try:
        r = requests.get(
            f"{ODDS_BASE}/sports/{ODDS_SPORT}/odds",
            params={
                "regions": "uk,eu,us",
                "markets": "outrights",
                "oddsFormat": "decimal",
                "apiKey": key,
            },
            timeout=30,
        )
        r.raise_for_status()
        events = r.json()
    except Exception as e:  # noqa: BLE001
        return {}, None, f"the-odds-api error: {e}"

    prices = {}  # canon_team -> [decimal prices across books]
    for event in events:
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "outrights":
                    continue
                for outcome in market.get("outcomes", []):
                    ck = canon(outcome.get("name", ""))
                    price = outcome.get("price")
                    if ck and isinstance(price, (int, float)) and price > 0:
                        prices.setdefault(ck, []).append(float(price))

    odds = {}
    for ck, plist in prices.items():
        dec = statistics.median(plist)
        odds[ck] = {"decimal": round(dec, 1), "implied": round(100.0 / dec, 1)}
    alive = set(prices.keys())  # only meaningful if we actually got odds
    return odds, alive, None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def keyset_for(team, aliases):
    """All acceptable canonical keys for a sweepstakes team name."""
    keys = {canon(team)}
    for alt in aliases.get(team, []):
        keys.add(canon(alt))
    return keys


def lookup(keys, table):
    for k in keys:
        if k in table:
            return table[k]
    return None


def build():
    load_env()
    draw = json.loads((ROOT / "draw.json").read_text())
    aliases = json.loads((ROOT / "aliases.json").read_text())
    aliases = {k: v for k, v in aliases.items() if not k.startswith("_")}
    flags = json.loads((ROOT / "flags.json").read_text())
    flags = {k: v for k, v in flags.items() if not k.startswith("_")}
    overrides_path = ROOT / "overrides.json"
    overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}

    results, results_err = fetch_results(os.environ.get("FOOTBALL_DATA_KEY"))
    odds, alive, odds_err = fetch_odds(os.environ.get("ODDS_API_KEY"))
    have_odds = alive is not None

    people = []
    for person in sorted(draw.keys()):
        teams = []
        for team in draw[person]:
            keys = keyset_for(team, aliases)
            res = lookup(keys, results)
            od = lookup(keys, odds)

            # Status: manual override wins; else odds-presence; else unknown
            ov = overrides.get(team, "").lower()
            if ov in ("in", "out"):
                status = ov
            elif have_odds:
                status = "in" if (keys & alive) else "out"
            else:
                status = "unknown"

            teams.append({
                "name": team,
                "flag": flag_url(team, flags),
                "status": status,
                "result": res["text"] if res else "—",
                "outcome": res["outcome"] if res else "",
                "odds_decimal": od["decimal"] if od else None,
                "odds_frac": to_fractional(od["decimal"]) if od else None,
                "odds_implied": od["implied"] if od else None,
                "matched": bool(res or od),
            })

        alive_count = sum(1 for t in teams if t["status"] == "in")
        best = min((t["odds_decimal"] for t in teams if t["odds_decimal"]), default=None)
        people.append({
            "name": person,
            "teams": teams,
            "alive": alive_count,
            "total": len(teams),
            "best_odds": best,
        })

    # Leaderboard: most teams alive, then best (lowest) odds among them
    leaderboard = sorted(
        people,
        key=lambda p: (-p["alive"], p["best_odds"] if p["best_odds"] else 9999),
    )

    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("dashboard.html")
    html = template.render(
        people=people,
        leaderboard=leaderboard,
        updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        have_odds=have_odds,
        have_results=bool(results),
        errors=[e for e in (results_err, odds_err) if e],
    )
    out = ROOT / "docs" / "index.html"
    out.write_text(html)
    print(f"Wrote {out}")
    unmatched = [
        f"{p['name']}: {t['name']}"
        for p in people for t in p["teams"]
        if not t["matched"] and (results or odds)
    ]
    if unmatched:
        print("Unmatched teams (add aliases):")
        for u in unmatched:
            print("  " + u)


if __name__ == "__main__":
    build()
