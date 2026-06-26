#!/usr/bin/env python3
"""Build the World Cup 2026 sweepstakes KNOCKOUT BRACKET page.

A purely additive, standalone companion to build.py — it does NOT touch the main
dashboard pipeline. It reads the same source data (draw.json, flags.json,
aliases.json) and reuses build.py's pure helpers (canon, fetch_odds, …) by
import, then renders a visual bracket to docs/bracket.html.

The bracket structure lives in bracket.json (manually editable: fill teams into
slots as groups conclude). For each Round-of-32 matchup where both teams are
known and have odds, a head-to-head "progress %" is derived from the existing
outright odds: implied(A) / (implied(A) + implied(B)).

Run locally with `python bracket.py` (uses .env keys via build.load_env, like
build.py). Network-free fallback: with no odds it still renders the structure.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import build  # reuse helpers only — importing runs no network code (guarded by __main__)

ROOT = Path(__file__).parent


def owner_map(draw):
    """canon(team) -> owner name, inverted from the sweepstakes draw."""
    out = {}
    for owner, teams in draw.items():
        for team in teams:
            out[build.canon(team)] = owner
    return out


def flag_map(flags):
    """canon(team) -> flagcdn url, tolerant of spelling variants."""
    return {build.canon(name): build.flag_url(name, flags) for name in flags}


def resolve_slot(slot, ctx):
    """Turn one bracket.json slot into a render-ready dict.

    slot is one of {"team":...}, {"seed":...}, {"from": match_id}.
    ctx carries the lookup tables (owners, flags, odds, alive, aliases, matches).
    """
    base = {
        "kind": None, "label": "", "team": None, "owner": None, "flag": None,
        "status": None, "implied": None, "progress": None,
    }

    if "team" in slot:
        team = slot["team"]
        keys = build.keyset_for(team, ctx["aliases"])
        od = build.lookup(keys, ctx["odds"])
        flag = next((ctx["flags"][k] for k in keys if ctx["flags"].get(k)), None)
        owner = next((ctx["owners"][k] for k in keys if k in ctx["owners"]), None)
        if ctx["alive"] is None:
            status = "unknown"
        else:
            status = "in" if (keys & ctx["alive"]) else "out"
        base.update({
            "kind": "team", "label": team, "team": team, "owner": owner,
            "flag": flag, "status": status,
            "implied": od["implied"] if od else None,
        })
    elif "from" in slot:
        child = ctx["matches"].get(slot["from"], {})
        base.update({"kind": "from", "label": f"W{child.get('no', '?')}"})
    else:  # seed placeholder
        base.update({"kind": "seed", "label": slot.get("seed", "TBD")})
    return base


def build_match(mid, m, ctx):
    a = resolve_slot(m["a"], ctx)
    b = resolve_slot(m["b"], ctx)
    # Head-to-head progress %: only when both sides are known teams with odds.
    if a["implied"] and b["implied"]:
        total = a["implied"] + b["implied"]
        a["progress"] = round(100 * a["implied"] / total)
        b["progress"] = round(100 * b["implied"] / total)
    return {"id": mid, "no": m.get("no"), "round": m["round"], "side": m["side"], "a": a, "b": b}


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

    import os
    odds, alive, odds_err = build.fetch_odds(os.environ.get("ODDS_API_KEY"))

    ctx = {
        "owners": owner_map(draw),
        "flags": flag_map(flags),
        "odds": odds,
        "alive": alive,
        "aliases": aliases,
        "matches": matches,
    }

    built = {mid: build_match(mid, m, ctx) for mid, m in matches.items()}

    # Columns for the visual: rounds 0..3 split L/R (preserving bracket.json order),
    # round 4 (the final) is the centre. Right side renders mirrored (outermost = R32).
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
        "have_odds": alive is not None,
        "errors": [e for e in (odds_err,) if e],
    })
    out = ROOT / "docs" / "bracket.html"
    out.write_text(html)
    print(f"Wrote {out}")


if __name__ == "__main__":
    build_bracket()
