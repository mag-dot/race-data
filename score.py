#!/usr/bin/env python3
"""
HKJC Scoring Engine

Applies the 8-factor composite scoring framework to merged analysis data.
Uses ALL available intelligence: form, energy, barrier trials, trackwork,
draw stats, jockey/trainer rankings.

Input: analysis/{date}.json (from merge.py)
Output: scored/{date}.json + .md (ranked selections per race)

Usage:
    python3 score.py --date 2026-04-01
    python3 score.py --date 2026-04-01 --output scored/2026-04-01.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent / "analysis"
SCORED_DIR = Path(__file__).resolve().parent / "scored"


# ============================================================
# Factor Scoring Functions (each returns 0-100)
# ============================================================

def score_form(horse: dict) -> float:
    """
    Recent form: recency-weighted placing scores.
    Uses recentPositions from merged data.
    """
    positions = horse.get("recentPositions", [])
    if not positions:
        return 25.0  # unknown / griffin

    multipliers = [1.5, 1.2, 1.0, 0.8, 0.5]
    placing_pts = {1: 10, 2: 7, 3: 5, 4: 3, 5: 1, 6: 1}

    total = 0.0
    max_total = 0.0
    for i, pos in enumerate(positions[:5]):
        mult = multipliers[i] if i < len(multipliers) else 0.3
        max_total += 10 * mult
        pts = placing_pts.get(pos, 0) if pos <= 6 else 0
        total += pts * mult

    if max_total == 0:
        return 25.0
    return (total / max_total) * 100


def score_energy(horse: dict) -> float:
    """
    Energy / sectional times.
    HKJC SpeedPRO energy scores (0-120 scale).
    Trend analysis: rising energy = improving horse.
    """
    scores = horse.get("energyScores", [])
    if not scores:
        return 50.0  # neutral when no data

    avg = horse.get("avgEnergy") or (sum(scores) / len(scores))

    # Base score from average energy
    # 80 → 30, 90 → 55, 95 → 70, 100 → 85, 105+ → 95
    base = min(100, max(0, (avg - 70) * 2.8))

    # Trend bonus: if energy is rising over last 3 runs
    if len(scores) >= 3:
        recent_3 = scores[:3]
        if recent_3[0] > recent_3[1] > recent_3[2]:
            base = min(100, base + 10)  # rising trend bonus
        elif recent_3[0] < recent_3[1] < recent_3[2]:
            base = max(0, base - 8)  # declining trend penalty

    # Peak energy bonus: if any recent run was 100+
    if any(s >= 100 for s in scores[:3]):
        base = min(100, base + 5)

    return base


def score_jockey(horse: dict) -> float:
    """
    Jockey quality from season stats.
    """
    js = horse.get("jockeyStats")
    if not js:
        return 25.0

    win_pct = js.get("winPct", 0)
    place_pct = js.get("placePct", 0)

    # Scale: 0% → 0, 5% → 25, 10% → 50, 15% → 75, 20%+ → 95
    base = min(95, win_pct * 5)

    # Place rate bonus (consistent jockeys who place often)
    if place_pct > 30:
        base = min(100, base + 5)

    return base


def score_trainer(horse: dict) -> float:
    """
    Trainer quality from season stats.
    """
    ts = horse.get("trainerStats")
    if not ts:
        return 25.0

    win_pct = ts.get("winPct", 0)
    place_pct = ts.get("placePct", 0)

    # Scale: 0% → 0, 5% → 30, 10% → 60, 12%+ → 75
    base = min(85, win_pct * 6.5)

    if place_pct > 25:
        base = min(100, base + 5)

    return base


def score_draw(horse: dict) -> float:
    """
    Draw advantage using actual HKJC draw statistics.
    Falls back to positional estimate if no stats available.
    """
    ds = horse.get("drawStats")
    draw = horse.get("draw")
    venue = horse.get("venue", "Sha Tin")
    distance = horse.get("distance", 1400)

    # If we have actual draw stats, use them directly
    if ds:
        win_pct = ds.get("winPct", 0) or 0
        place_pct = ds.get("placePct", 0) or 0

        # Normalise: avg draw win% is ~7% for 14 runners
        # 0% → 20, 7% → 50, 15% → 80, 20%+ → 95
        base = min(95, 20 + win_pct * 4.5)

        # Place rate adds confidence
        if place_pct > 25:
            base = min(100, base + 5)

        return base

    # Fallback: estimate from position
    if not draw or not isinstance(draw, int):
        return 50.0

    # Estimate runners from a typical field
    runners = 14
    ratio = (draw - 1) / max(runners - 1, 1)

    if venue == "Happy Valley":
        if distance and distance <= 1200:
            return max(0, 100 - ratio * 120)
        elif distance and distance <= 1650:
            return max(0, 90 - ratio * 70)
        else:
            return max(0, 80 - ratio * 40)
    else:  # Sha Tin
        if distance and distance <= 1200:
            return max(0, 90 - ratio * 60)
        elif distance and distance <= 1600:
            return max(0, 85 - ratio * 40)
        else:
            return max(0, 75 - ratio * 20)


def score_class_movement(horse: dict) -> float:
    """
    Class drop/rise analysis.
    Uses form entries to detect class changes.
    """
    race_class = horse.get("class")
    positions = horse.get("recentPositions", [])
    if not race_class:
        return 50.0

    # We don't have per-run class data in merged format yet,
    # so use form quality as proxy: good form + lower class = likely dropping
    form_quality = score_form(horse)

    if form_quality > 60:
        return 65.0  # good form = likely competitive at this level
    elif form_quality < 30:
        return 35.0  # poor form = might be outclassed
    return 50.0


def score_weight(horse: dict) -> float:
    """
    Weight analysis.
    Lighter carry weight relative to field = advantage.
    """
    weight = horse.get("carryWeight")
    if not weight or not isinstance(weight, (int, float)):
        return 50.0

    # Typical HKJC weight range: 113-133 lbs
    # Lower weight = better (more lbs off = advantage)
    # Centre at 123 lbs
    diff = 123 - weight
    # Each lb off centre adds ~3 points
    return min(100, max(0, 50 + diff * 3))


def score_specialist(horse: dict) -> float:
    """
    Track/distance specialist score.
    For now, neutral — needs per-horse career records which
    we'll get when we have multi-date formguide data.
    """
    return 50.0


def score_trackwork(horse: dict) -> float:
    """
    BONUS FACTOR: Trackwork fitness signal.
    Not in the original 8 factors but adds value.
    Returns adjustment (-10 to +15) added to final score.
    """
    tw = horse.get("trackwork")
    if not tw:
        return 0.0  # no data = no adjustment

    total = tw.get("totalSessions", 0)
    gallops = tw.get("gallopCount", 0)
    fast = tw.get("fastWorkCount", 0)

    adj = 0.0

    # Well-prepared horse: 4+ total sessions
    if total >= 6:
        adj += 5
    elif total >= 4:
        adj += 3
    elif total <= 1:
        adj -= 5  # barely any work = concern

    # Fast work = race-ready
    if fast >= 2:
        adj += 5
    elif fast >= 1:
        adj += 2

    # Heavy gallop program = trainer means business
    if gallops >= 4:
        adj += 3

    return min(15, max(-10, adj))


def score_barrier_trials(horse: dict) -> float:
    """
    BONUS FACTOR: Recent barrier trial performance.
    Returns adjustment (-10 to +15) added to final score.
    """
    trials = horse.get("recentTrials", [])
    sentiment = horse.get("trialSentiment")

    if not trials:
        return 0.0  # no trials = no adjustment

    adj = 0.0

    # Sentiment from steward comments
    if sentiment == "positive":
        adj += 10
    elif sentiment == "negative":
        adj -= 8
    else:
        adj += 2  # had a trial = at least fitness confirmed

    # Trial winner bonus
    for t in trials[:2]:
        result = str(t.get("result", "")).strip()
        if result == "1" or result.lower() in ["won", "1st"]:
            adj += 5
            break

    # Recency: trial within last 2 weeks = fresh fitness signal
    # (we don't have exact date comparison here, but having ANY trial is positive)
    if len(trials) >= 2:
        adj += 2  # multiple trials = well-prepared

    return min(15, max(-10, adj))


# ============================================================
# Red / Green Flags
# ============================================================

def apply_flags(horse: dict) -> tuple[float, list[str]]:
    """
    Apply red and green flag adjustments.
    Returns (adjustment, [flag descriptions]).
    """
    adj = 0.0
    flags = []
    positions = horse.get("recentPositions", [])
    draw = horse.get("draw")
    venue = horse.get("venue", "")
    distance = horse.get("distance", 0)
    energy = horse.get("energyScores", [])
    days_ago = horse.get("lastRunDaysAgo")
    sentiment = horse.get("trialSentiment")
    trials = horse.get("recentTrials", [])

    # RED: 3+ consecutive finishes outside top 6
    if len(positions) >= 3:
        bad = sum(1 for p in positions[:3] if p > 6)
        if bad >= 3:
            adj -= 15
            flags.append("🔴 3 runs outside top 6")

    # RED: Wide draw at short HV
    if isinstance(draw, int) and venue == "Happy Valley" and distance and distance <= 1200 and draw >= 10:
        adj -= 20
        flags.append(f"🔴 Wide draw ({draw}) at HV {distance}m")

    # RED: Long layoff without trial
    if days_ago and days_ago > 60 and not trials:
        adj -= 15
        flags.append(f"🔴 {days_ago}d layoff, no trial")

    # GREEN: Energy 100+ last start, form is good
    if energy and energy[0] >= 100 and positions and positions[0] <= 4:
        adj += 12
        flags.append(f"🟢 Energy {energy[0]} + top-4 finish")

    # GREEN: Near-miss last start (4th-6th, unlucky)
    if positions and 4 <= positions[0] <= 6 and energy and energy[0] >= 92:
        adj += 8
        flags.append("🟢 Near miss + strong energy")

    # GREEN: Trial winner with positive sentiment
    if sentiment == "positive" and trials:
        for t in trials[:1]:
            result = str(t.get("result", "")).strip()
            if result in ["1", "won", "1st"]:
                adj += 12
                flags.append("🟢 Trial winner, positive comments")
                break

    # GREEN: Elite jockey on improving horse
    js = horse.get("jockeyStats")
    if js and js.get("winPct", 0) >= 15:
        if energy and len(energy) >= 2 and energy[0] > energy[1]:
            adj += 8
            flags.append(f"🟢 Elite jockey + improving energy")

    # GREEN: Inside draw at HV sprint
    if isinstance(draw, int) and venue == "Happy Valley" and distance and distance <= 1200 and draw <= 3:
        adj += 12
        flags.append(f"🟢 Inside draw ({draw}) at HV sprint")

    # GREEN: Heavy trackwork program
    tw = horse.get("trackwork")
    if tw and tw.get("totalSessions", 0) >= 6 and tw.get("fastWorkCount", 0) >= 2:
        adj += 5
        flags.append(f"🟢 Heavy preparation ({tw['totalSessions']} sessions, {tw['fastWorkCount']} fast)")

    return adj, flags


# ============================================================
# Composite Score
# ============================================================

def calculate_composite(horse: dict) -> dict:
    """
    Calculate the full composite score for a horse.
    Returns dict with score, rank-ready, and full breakdown.
    """
    venue = horse.get("venue", "Sha Tin")
    surface = horse.get("surface", "Turf")
    distance = horse.get("distance", 1400)
    is_hv = venue == "Happy Valley"
    is_awt = surface == "AWT"

    # Base weights (from strategy doc)
    w = {
        "form": 0.25,
        "energy": 0.15,
        "jockey": 0.15,
        "draw": 0.15,
        "trainer": 0.10,
        "class": 0.10,
        "weight": 0.05,
        "specialist": 0.05,
    }

    # Venue/distance adjustments
    if is_hv and not is_awt:
        w["draw"] = 0.20
        w["form"] = 0.20
    if is_awt:
        w["draw"] = 0.05
        w["energy"] = 0.20
        w["form"] = 0.25
    if distance and distance >= 2000:
        w["draw"] = 0.05
        w["form"] = 0.30
        w["specialist"] = 0.10

    # Normalise weights to sum to 1.0
    total_w = sum(w.values())
    w = {k: v / total_w for k, v in w.items()}

    # Calculate each factor
    factors = {
        "form": score_form(horse),
        "energy": score_energy(horse),
        "jockey": score_jockey(horse),
        "trainer": score_trainer(horse),
        "draw": score_draw(horse),
        "class": score_class_movement(horse),
        "weight": score_weight(horse),
        "specialist": score_specialist(horse),
    }

    # Weighted composite
    raw = sum(factors[k] * w[k] for k in w)

    # Bonus factors (not part of 8-factor weights, added directly)
    tw_bonus = score_trackwork(horse)
    bt_bonus = score_barrier_trials(horse)

    # Red/green flags
    flag_adj, flags = apply_flags(horse)

    final = raw + tw_bonus + bt_bonus + flag_adj

    return {
        "horseName": horse.get("horseName"),
        "horseNo": horse.get("horseNo"),
        "draw": horse.get("draw"),
        "jockey": horse.get("jockey"),
        "trainer": horse.get("trainer"),
        "final": round(final, 1),
        "raw": round(raw, 1),
        "factors": {k: round(v, 1) for k, v in factors.items()},
        "weights": {k: round(v, 3) for k, v in w.items()},
        "bonuses": {
            "trackwork": round(tw_bonus, 1),
            "barrierTrials": round(bt_bonus, 1),
            "flags": round(flag_adj, 1),
        },
        "flags": flags,
        # Key data points for manual review
        "signals": {
            "recentForm": horse.get("recentPositions", [])[:5],
            "avgEnergy": horse.get("avgEnergy"),
            "energyTrend": _energy_trend(horse.get("energyScores", [])),
            "trialSentiment": horse.get("trialSentiment"),
            "daysSinceRun": horse.get("lastRunDaysAgo"),
            "drawWinPct": (horse.get("drawStats") or {}).get("winPct"),
            "jockeyWinPct": (horse.get("jockeyStats") or {}).get("winPct"),
            "trainerWinPct": (horse.get("trainerStats") or {}).get("winPct"),
            "gallopCount": (horse.get("trackwork") or {}).get("gallopCount"),
            "fastWorkCount": (horse.get("trackwork") or {}).get("fastWorkCount"),
        },
    }


def _energy_trend(scores: list) -> str:
    if len(scores) < 2:
        return "unknown"
    if len(scores) >= 3 and scores[0] > scores[1] > scores[2]:
        return "rising"
    if len(scores) >= 3 and scores[0] < scores[1] < scores[2]:
        return "declining"
    if scores[0] > scores[1]:
        return "improving"
    if scores[0] < scores[1]:
        return "dropping"
    return "stable"


# ============================================================
# Race-level analysis
# ============================================================

def analyse_race(race: dict) -> dict:
    """Score and rank all horses in a race."""
    horses = race.get("horses", [])
    scored = [calculate_composite(h) for h in horses]
    scored.sort(key=lambda x: x["final"], reverse=True)

    # Add rank
    for i, s in enumerate(scored):
        s["rank"] = i + 1

    # Race competitiveness: gap between #1 and #2
    gap = scored[0]["final"] - scored[1]["final"] if len(scored) >= 2 else 0
    top3_avg = sum(s["final"] for s in scored[:3]) / min(3, len(scored))

    return {
        "raceNo": race.get("raceNo"),
        "raceName": race.get("raceName"),
        "venue": race.get("venue"),
        "surface": race.get("surface"),
        "distance": race.get("distance"),
        "class": race.get("class"),
        "runners": len(scored),
        "competitiveness": round(gap, 1),
        "top3Avg": round(top3_avg, 1),
        "selections": scored,
    }


def race_to_markdown(race_result: dict) -> str:
    """Convert scored race to readable markdown."""
    lines = []
    r = race_result
    gap = r["competitiveness"]

    lines.append(f"## Race {r['raceNo']} — {r.get('surface', '')} {r.get('distance', '')}m | Class {r.get('class', '?')} | {r['runners']} runners")

    # Race verdict
    if gap >= 15:
        verdict = "🎯 STRONG PICK — clear top selection"
    elif gap >= 8:
        verdict = "✅ SOLID — good separation"
    elif gap >= 3:
        verdict = "⚖️ COMPETITIVE — close call"
    else:
        verdict = "🎲 WIDE OPEN — no clear edge"
    lines.append(f"**Verdict:** {verdict} (gap: {gap:.1f}pts)")
    lines.append("")

    # Top 5 selections
    lines.append(f"| Rank | # | Horse | Score | Form | Energy | Jockey | Draw | Flags |")
    lines.append(f"|------|---|-------|-------|------|--------|--------|------|-------|")
    for s in r["selections"][:5]:
        f = s["factors"]
        flags_str = " ".join(s.get("flags", [])) or "—"
        sig = s.get("signals", {})
        form_str = "/".join(str(p) for p in sig.get("recentForm", [])[:3]) or "—"
        energy_str = str(sig.get("avgEnergy") or "—")
        jockey_pct = f"{sig.get('jockeyWinPct', 0):.0f}%" if sig.get("jockeyWinPct") else "—"
        draw_pct = f"{sig.get('drawWinPct', 0):.0f}%" if sig.get("drawWinPct") else "—"

        lines.append(
            f"| {s['rank']} | #{s.get('horseNo', '?')} | **{s['horseName']}** | "
            f"**{s['final']:.1f}** | {form_str} | {energy_str} | "
            f"{s.get('jockey', '')} ({jockey_pct}) | {s.get('draw', '?')} ({draw_pct}) | {flags_str} |"
        )

    lines.append("")

    # Detailed breakdown for top 3
    lines.append("### Breakdown — Top 3")
    for s in r["selections"][:3]:
        f = s["factors"]
        b = s["bonuses"]
        sig = s["signals"]
        lines.append(f"**#{s.get('horseNo', '?')} {s['horseName']}** — {s['final']:.1f}pts")
        lines.append(f"  Form: {f['form']:.0f} | Energy: {f['energy']:.0f} | Jockey: {f['jockey']:.0f} | Draw: {f['draw']:.0f} | Trainer: {f['trainer']:.0f} | Class: {f['class']:.0f} | Weight: {f['weight']:.0f}")
        lines.append(f"  Bonuses: TW {b['trackwork']:+.0f} | BT {b['barrierTrials']:+.0f} | Flags {b['flags']:+.0f}")
        if sig.get("energyTrend") and sig["energyTrend"] != "unknown":
            lines.append(f"  Energy trend: {sig['energyTrend']} | Days since run: {sig.get('daysSinceRun', '?')}")
        if sig.get("gallopCount"):
            lines.append(f"  Trackwork: {sig['gallopCount']} gallops, {sig.get('fastWorkCount', 0)} fast")
        if s.get("flags"):
            lines.append(f"  Flags: {', '.join(s['flags'])}")
        lines.append("")

    return "\n".join(lines)


def betting_recommendations(races: list) -> str:
    """Generate betting recommendations from scored races."""
    lines = ["## 💰 Betting Recommendations", ""]

    for r in races:
        sels = r["selections"]
        gap = r["competitiveness"]
        rn = r["raceNo"]

        top = sels[0] if sels else None
        if not top:
            continue

        lines.append(f"### Race {rn}")

        if gap >= 15:
            # Strong pick — win bet
            lines.append(f"**WIN:** #{top.get('horseNo', '?')} {top['horseName']} ({top['final']:.1f}pts)")
            lines.append(f"  Stake: 2 units | Clear separation ({gap:.1f}pts over #{sels[1]['horseName'] if len(sels) > 1 else '?'})")
        elif gap >= 8:
            # Solid — place bet or small win
            lines.append(f"**PLACE:** #{top.get('horseNo', '?')} {top['horseName']} ({top['final']:.1f}pts)")
            if len(sels) >= 2:
                lines.append(f"**QIN box:** #{top.get('horseNo', '?')} + #{sels[1].get('horseNo', '?')} {sels[1]['horseName']}")
            lines.append(f"  Stake: 1 unit each")
        elif gap >= 3:
            # Competitive — quinella/place
            if len(sels) >= 2:
                lines.append(f"**QIN box:** #{top.get('horseNo', '?')} {top['horseName']} + #{sels[1].get('horseNo', '?')} {sels[1]['horseName']}")
                lines.append(f"  Stake: 1 unit")
        else:
            # Wide open — skip or small place
            lines.append(f"**PASS** — No clear edge (gap {gap:.1f}pts)")
            if top["final"] > 65:
                lines.append(f"  Optional small PLACE on #{top.get('horseNo', '?')} {top['horseName']}")

        if top.get("flags"):
            lines.append(f"  Signals: {', '.join(top['flags'])}")
        lines.append("")

    # Summary
    strong = sum(1 for r in races if r["competitiveness"] >= 15)
    solid = sum(1 for r in races if 8 <= r["competitiveness"] < 15)
    competitive = sum(1 for r in races if 3 <= r["competitiveness"] < 8)
    skip = sum(1 for r in races if r["competitiveness"] < 3)

    lines.append("---")
    lines.append(f"**Summary:** {strong} strong | {solid} solid | {competitive} competitive | {skip} skip")
    max_units = strong * 2 + solid * 2 + competitive * 1
    lines.append(f"**Max exposure:** ~{max_units} units")

    return "\n".join(lines)


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="HKJC Scoring Engine")
    parser.add_argument("--date", required=True, help="Race date (YYYY-MM-DD)")
    parser.add_argument("--output", help="Output path (default: scored/{date}.json)")
    parser.add_argument("--quiet", action="store_true", help="Suppress console output")
    args = parser.parse_args()

    date_slug = args.date
    analysis_path = ANALYSIS_DIR / f"{date_slug}.json"

    if not analysis_path.exists():
        print(f"❌ No analysis file: {analysis_path}")
        print(f"   Run: python3 merge.py --date {date_slug}")
        sys.exit(1)

    with open(analysis_path) as f:
        races = json.load(f)

    if not args.quiet:
        print(f"🏇 Scoring {len(races)} races for {date_slug}...")

    # Score all races
    results = [analyse_race(r) for r in races]

    # Output JSON
    SCORED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else SCORED_DIR / f"{date_slug}.json"

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    if not args.quiet:
        print(f"  ✓ Saved {out_path} ({os.path.getsize(out_path):,} bytes)")

    # Output markdown report
    md_lines = [f"# Race Selections — {date_slug}", ""]
    md_lines.append(f"**{len(races)} races** | Scored using 8-factor composite + trackwork/trial bonuses + red/green flags")
    md_lines.append("")

    for r in results:
        md_lines.append(race_to_markdown(r))

    md_lines.append(betting_recommendations(results))

    md_path = out_path.with_suffix(".md")
    with open(md_path, "w") as f:
        f.write("\n".join(md_lines))

    if not args.quiet:
        print(f"  ✓ Saved {md_path}")

    # Console summary
    if not args.quiet:
        print(f"\n{'='*80}")
        print(f"📊 SELECTIONS — {date_slug}")
        print(f"{'='*80}")
        for r in results:
            top = r["selections"][0] if r["selections"] else None
            gap = r["competitiveness"]
            if gap >= 15:
                confidence = "🎯 STRONG"
            elif gap >= 8:
                confidence = "✅ SOLID"
            elif gap >= 3:
                confidence = "⚖️ CLOSE"
            else:
                confidence = "🎲 OPEN"

            if top:
                print(f"  R{r['raceNo']:>2} {confidence:12s} #{top.get('horseNo', '?'):>3} {top['horseName']:<22} {top['final']:6.1f}pts  (gap {gap:+.1f})")
                flags = top.get("flags", [])
                if flags:
                    print(f"       {' '.join(flags)}")
            else:
                print(f"  R{r['raceNo']:>2} — No horses scored")

        print(f"\n✅ Done! Full report: {md_path}")


if __name__ == "__main__":
    main()
