#!/usr/bin/env python3
"""
HKJC Unified Race Report

Combines:
  - score.py (8-factor composite scoring)
  - value_finder.py (overlay/value signal detection)

Into one report per race meeting with:
  - Combined score (composite + value bonus)
  - Bet type recommendations (WIN / PLACE / VALUE / PASS)
  - Per-race cards with full breakdown
  - Meeting summary with bankroll allocation

Usage:
    python3 race_report.py --date 2026-04-01
    python3 race_report.py --date 2026-04-01 --output reports/2026-04-01.md
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Import from sibling modules
sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import (
    score_form, score_energy, score_jockey, score_trainer,
    score_draw, score_class_movement, score_weight, score_specialist,
    score_trackwork, score_barrier_trials, apply_flags, _energy_trend
)
from value_finder import (
    signal_closing_speed, signal_energy_spike, signal_trial_comeback,
    signal_class_dropper, signal_jockey_upgrade, signal_running_style_vs_pace,
    find_barrier_trials
)

ANALYSIS_DIR = Path(__file__).resolve().parent / "analysis"
DATA_DIR = Path(__file__).resolve().parent / "data"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"


# ============================================================
# UNIFIED SCORING
# ============================================================

def unified_score(horse: dict, race_horses: list) -> dict:
    """
    Combine composite score + value signals into one unified assessment.
    """
    # --- COMPOSITE SCORE (from score.py) ---
    venue = horse.get("venue", "Sha Tin")
    surface = horse.get("surface", "Turf")
    distance = horse.get("distance", 1400)
    is_hv = venue == "Happy Valley"
    is_awt = surface == "AWT"

    w = {
        "form": 0.25, "energy": 0.15, "jockey": 0.15, "draw": 0.15,
        "trainer": 0.10, "class": 0.10, "weight": 0.05, "specialist": 0.05,
    }
    if is_hv and not is_awt:
        w["draw"] = 0.20; w["form"] = 0.20
    if is_awt:
        w["draw"] = 0.05; w["energy"] = 0.20; w["form"] = 0.25
    if distance and distance >= 2000:
        w["draw"] = 0.05; w["form"] = 0.30; w["specialist"] = 0.10

    total_w = sum(w.values())
    w = {k: v / total_w for k, v in w.items()}

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

    composite_raw = sum(factors[k] * w[k] for k in w)
    tw_bonus = score_trackwork(horse)
    bt_bonus = score_barrier_trials(horse)
    flag_adj, flags = apply_flags(horse)
    composite = composite_raw + tw_bonus + bt_bonus + flag_adj

    # --- VALUE SIGNALS (from value_finder.py) ---
    # Need raw form data for value signals
    raw_horse = horse  # merged analysis has form data embedded
    trials = horse.get("recentTrials", [])
    if not trials:
        trials = find_barrier_trials(horse.get("horseName", ""))

    # Build a simplified horse dict for value signals that expect form[] array
    form_proxy = _build_form_proxy(horse)

    v_signals = {}
    s1, n1 = signal_closing_speed(form_proxy)
    v_signals["closingSpeed"] = {"s": s1, "n": n1}

    s2, n2 = signal_energy_spike(form_proxy)
    v_signals["energySpike"] = {"s": s2, "n": n2}

    s3, n3 = signal_trial_comeback(form_proxy, trials)
    v_signals["trialComeback"] = {"s": s3, "n": n3}

    s4, n4 = signal_class_dropper(form_proxy)
    v_signals["classDropper"] = {"s": s4, "n": n4}

    s5, n5 = signal_jockey_upgrade(form_proxy)
    v_signals["jockeyUpgrade"] = {"s": s5, "n": n5}

    # For pace analysis, build form proxies for all race horses
    race_form_proxies = [_build_form_proxy(h) for h in race_horses]
    s6, n6 = signal_running_style_vs_pace(form_proxy, race_form_proxies)
    v_signals["paceScenario"] = {"s": s6, "n": n6}

    # Value composite
    v_weights = {
        "closingSpeed": 0.30, "energySpike": 0.25, "trialComeback": 0.15,
        "classDropper": 0.10, "jockeyUpgrade": 0.10, "paceScenario": 0.10,
    }
    value_raw = sum(v_signals[k]["s"] * v_weights[k] for k in v_weights)
    active_signals = sum(1 for v in v_signals.values() if v["s"] > 0)

    if active_signals >= 4:
        value_raw *= 1.3
    elif active_signals >= 3:
        value_raw *= 1.15

    value_score = min(100, value_raw)

    # --- COMBINED ---
    # Combined = composite is the base, value adds bonus for overlay detection
    # Value bonus scaled: 0-20 points max added to composite
    value_bonus = value_score * 0.20
    combined = composite + value_bonus

    # Active value signal descriptions
    value_notes = [v["n"] for v in v_signals.values() if v["s"] > 0 and v["n"]]

    return {
        "horseName": horse.get("horseName"),
        "horseNo": horse.get("horseNo"),
        "draw": horse.get("draw"),
        "jockey": horse.get("jockey", ""),
        "trainer": horse.get("trainer", ""),
        # Scores
        "combined": round(combined, 1),
        "composite": round(composite, 1),
        "valueScore": round(value_score, 1),
        "valueBonus": round(value_bonus, 1),
        # Breakdown
        "factors": {k: round(v, 1) for k, v in factors.items()},
        "bonuses": {
            "trackwork": round(tw_bonus, 1),
            "barrierTrials": round(bt_bonus, 1),
            "flags": round(flag_adj, 1),
            "value": round(value_bonus, 1),
        },
        "flags": flags,
        "valueSignals": value_notes,
        "activeSignals": active_signals,
        # Key data
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


def _build_form_proxy(horse: dict) -> dict:
    """
    Build a form-proxy dict compatible with value_finder signal functions.
    The value functions expect horse["form"] with sectionalTimes, energy, comment, etc.
    Merged analysis stores these differently — bridge the gap.
    """
    # If the horse already has a "form" key (from raw formguide), use it
    if "form" in horse:
        return horse

    # Otherwise reconstruct from merged fields
    proxy = dict(horse)
    # Build a fake form entry from what we have
    form_entries = []
    energies = horse.get("energyScores", [])
    positions = horse.get("recentPositions", [])

    for i in range(max(len(energies), len(positions), 1)):
        entry = {}
        if i < len(energies):
            entry["energy"] = energies[i]
        if i < len(positions):
            entry["fpTs"] = f"{positions[i]}/14"
        if i == 0 and horse.get("lastRunDaysAgo"):
            entry["daysSinceLast"] = horse["lastRunDaysAgo"]
        # We don't have sectionals in merged format per-entry
        # but the last trial comment can be used
        if i == 0 and horse.get("lastTrialComment"):
            entry["comment"] = horse["lastTrialComment"]
        form_entries.append(entry)

    proxy["form"] = form_entries
    proxy["jockey"] = horse.get("jockey", "")
    return proxy


# ============================================================
# RACE ANALYSIS
# ============================================================

def analyse_race(race: dict) -> dict:
    """Score, rank, and recommend bets for a race."""
    horses = race.get("horses", [])
    scored = [unified_score(h, horses) for h in horses]
    scored.sort(key=lambda x: x["combined"], reverse=True)

    for i, s in enumerate(scored):
        s["rank"] = i + 1

    gap = scored[0]["combined"] - scored[1]["combined"] if len(scored) >= 2 else 0
    top = scored[0]
    second = scored[1] if len(scored) >= 2 else None

    # --- BET RECOMMENDATION ---
    bets = []

    # Strong composite pick
    if gap >= 15:
        bets.append({
            "type": "WIN",
            "horse": f"#{top['horseNo']} {top['horseName']}",
            "stake": 2,
            "reason": f"clear top pick ({gap:.1f}pt gap)",
            "confidence": "HIGH",
        })
    elif gap >= 8:
        bets.append({
            "type": "PLACE",
            "horse": f"#{top['horseNo']} {top['horseName']}",
            "stake": 1,
            "reason": f"solid pick ({gap:.1f}pt gap)",
            "confidence": "MEDIUM",
        })

    # Quinella if top 2 are close and both strong
    if second and gap < 10 and top["combined"] > 60 and second["combined"] > 55:
        bets.append({
            "type": "QIN",
            "horse": f"#{top['horseNo']} + #{second['horseNo']}",
            "stake": 1,
            "reason": "top 2 close and strong",
            "confidence": "MEDIUM",
        })

    # Value plays — horses outside top 2 with high value scores
    for s in scored[2:6]:
        if s["valueScore"] >= 40 and s["activeSignals"] >= 3:
            bets.append({
                "type": "VALUE WIN",
                "horse": f"#{s['horseNo']} {s['horseName']}",
                "stake": 1,
                "reason": f"value overlay ({s['valueScore']:.0f} val, {s['activeSignals']} signals)",
                "confidence": "SPECULATIVE",
            })

    # Top value horse that's NOT the composite #1 — potential saver/exotic inclusion
    value_sorted = sorted(scored, key=lambda x: x["valueScore"], reverse=True)
    if value_sorted[0]["horseName"] != top["horseName"] and value_sorted[0]["valueScore"] >= 35:
        v = value_sorted[0]
        # Don't duplicate if already in bets
        if not any(v["horseName"] in b["horse"] for b in bets):
            bets.append({
                "type": "VALUE PLACE",
                "horse": f"#{v['horseNo']} {v['horseName']}",
                "stake": 1,
                "reason": f"top value pick ({v['valueScore']:.0f} val) — composite rank #{v['rank']}",
                "confidence": "SPECULATIVE",
            })

    if not bets:
        bets.append({
            "type": "PASS",
            "horse": "—",
            "stake": 0,
            "reason": f"no clear edge (gap {gap:.1f}pts)",
            "confidence": "—",
        })

    return {
        "raceNo": race.get("raceNo"),
        "raceName": race.get("raceName", ""),
        "venue": race.get("venue"),
        "surface": race.get("surface"),
        "distance": race.get("distance"),
        "class": race.get("class"),
        "runners": len(scored),
        "gap": round(gap, 1),
        "selections": scored,
        "bets": bets,
    }


# ============================================================
# REPORT GENERATION
# ============================================================

def generate_report(races: list, date_slug: str) -> str:
    """Generate the full markdown report."""
    lines = []
    lines.append(f"# 🏇 Race Report — {date_slug}")
    lines.append("")
    lines.append(f"**{len(races)} races** | Composite scoring + value overlay detection")
    lines.append(f"**Pipeline:** merge.py → score.py + value_finder.py → race_report.py")
    lines.append("")

    # ===== EXECUTIVE SUMMARY =====
    lines.append("---")
    lines.append("## 📋 Executive Summary")
    lines.append("")
    lines.append("| Race | Top Pick | Combined | Gap | Value Pick | Val Score | Bet |")
    lines.append("|------|----------|----------|-----|------------|-----------|-----|")

    total_units = 0
    for r in races:
        top = r["selections"][0]
        val_sorted = sorted(r["selections"], key=lambda x: x["valueScore"], reverse=True)
        top_val = val_sorted[0]
        bet_str = " / ".join(b["type"] for b in r["bets"])
        units = sum(b["stake"] for b in r["bets"])
        total_units += units

        val_str = f"#{top_val['horseNo']} {top_val['horseName']}" if top_val["horseName"] != top["horseName"] else "= top pick"

        lines.append(
            f"| R{r['raceNo']} | **#{top['horseNo']} {top['horseName']}** | "
            f"**{top['combined']:.1f}** | {r['gap']:.1f} | "
            f"{val_str} | {top_val['valueScore']:.0f} | {bet_str} |"
        )

    lines.append("")
    lines.append(f"**Total exposure: ~{total_units} units**")
    lines.append("")

    # ===== BETTING CARD =====
    lines.append("---")
    lines.append("## 💰 Betting Card")
    lines.append("")
    lines.append("| Race | Bet | Horse | Stake | Confidence | Reason |")
    lines.append("|------|-----|-------|-------|------------|--------|")

    for r in races:
        for b in r["bets"]:
            emoji = {"HIGH": "🔥", "MEDIUM": "✅", "SPECULATIVE": "💎", "—": "⏭️"}.get(b["confidence"], "")
            lines.append(
                f"| R{r['raceNo']} | **{b['type']}** | {b['horse']} | "
                f"{b['stake']}u | {emoji} {b['confidence']} | {b['reason']} |"
            )

    lines.append("")

    # ===== PER-RACE DETAILS =====
    lines.append("---")
    lines.append("## 📊 Race-by-Race Analysis")

    for r in races:
        lines.append("")
        lines.append(f"### Race {r['raceNo']} — {r.get('surface', '')} {r.get('distance', '')}m | Class {r.get('class', '?')} | {r['runners']} runners")
        lines.append("")

        # Top 5 ranked
        lines.append("| Rank | # | Horse | Combined | Composite | Value | Signals |")
        lines.append("|------|---|-------|----------|-----------|-------|---------|")

        for s in r["selections"][:5]:
            sig_count = f"({s['activeSignals']})" if s["activeSignals"] > 0 else ""
            flags_short = " ".join(s.get("flags", [])[:2]) or ""
            val_notes = " ".join(s.get("valueSignals", [])[:1]) or ""
            combined_notes = f"{flags_short} {val_notes}".strip() or "—"

            lines.append(
                f"| {s['rank']} | #{s.get('horseNo', '?')} | **{s['horseName']}** | "
                f"**{s['combined']:.1f}** | {s['composite']:.1f} | "
                f"{s['valueScore']:.0f} {sig_count} | {combined_notes} |"
            )

        # Detailed breakdown for #1 pick
        top = r["selections"][0]
        f = top["factors"]
        b = top["bonuses"]
        sig = top["signals"]
        lines.append("")
        lines.append(f"**#{top.get('horseNo', '?')} {top['horseName']}** — {top['combined']:.1f}pts (composite {top['composite']:.1f} + value {top['valueBonus']:.1f})")
        lines.append(f"- Factors: Form {f['form']:.0f} | Energy {f['energy']:.0f} | Jockey {f['jockey']:.0f} | Draw {f['draw']:.0f} | Trainer {f['trainer']:.0f} | Class {f['class']:.0f} | Weight {f['weight']:.0f}")
        lines.append(f"- Bonuses: TW {b['trackwork']:+.0f} | BT {b['barrierTrials']:+.0f} | Flags {b['flags']:+.0f} | Value {b['value']:+.1f}")

        form_str = "/".join(str(p) for p in sig.get("recentForm", [])) or "—"
        lines.append(f"- Form: {form_str} | Energy avg: {sig.get('avgEnergy', '—')} ({sig.get('energyTrend', '—')}) | Days since: {sig.get('daysSinceRun', '—')}")

        if top.get("flags"):
            lines.append(f"- Flags: {', '.join(top['flags'])}")
        if top.get("valueSignals"):
            lines.append(f"- Value signals: {' | '.join(top['valueSignals'])}")

        # Bets for this race
        lines.append("")
        for bet in r["bets"]:
            emoji = {"HIGH": "🔥", "MEDIUM": "✅", "SPECULATIVE": "💎", "—": "⏭️"}.get(bet["confidence"], "")
            lines.append(f"**{emoji} {bet['type']}** {bet['horse']} — {bet['stake']}u — {bet['reason']}")

    # ===== FOOTER =====
    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by race_report.py | Data: merge.py → score.py + value_finder.py*")
    lines.append(f"*Disclaimer: This is a statistical model, not financial advice. Bet responsibly.*")

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="HKJC Unified Race Report")
    parser.add_argument("--date", required=True, help="Race date (YYYY-MM-DD)")
    parser.add_argument("--output", help="Output path (default: reports/{date}.md)")
    parser.add_argument("--quiet", action="store_true")
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
        print(f"🏇 Generating unified report for {date_slug} ({len(races)} races)...")

    # Score all races
    results = [analyse_race(r) for r in races]

    # Generate report
    report = generate_report(results, date_slug)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else REPORTS_DIR / f"{date_slug}.md"

    with open(out_path, "w") as f:
        f.write(report)

    if not args.quiet:
        print(f"  ✓ Report: {out_path} ({os.path.getsize(out_path):,} bytes)")

    # Also save JSON
    json_path = out_path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    if not args.quiet:
        print(f"  ✓ Data: {json_path} ({os.path.getsize(json_path):,} bytes)")

    # Console summary
    if not args.quiet:
        print(f"\n{'='*80}")
        print(f"📋 SELECTIONS — {date_slug}")
        print(f"{'='*80}")
        for r in results:
            top = r["selections"][0]
            bet_str = " / ".join(b["type"] for b in r["bets"])
            units = sum(b["stake"] for b in r["bets"])

            print(f"  R{r['raceNo']:>2}  #{top.get('horseNo', '?'):>3} {top['horseName']:<22} {top['combined']:6.1f}pts (comp {top['composite']:.0f} + val {top['valueBonus']:.0f})  → {bet_str} ({units}u)")
            if top.get("valueSignals"):
                print(f"       💎 {' | '.join(top['valueSignals'][:2])}")
            if top.get("flags"):
                print(f"       {' '.join(top['flags'][:2])}")

        total = sum(sum(b["stake"] for b in r["bets"]) for r in results)
        print(f"\n  Total exposure: {total} units")
        print(f"  Report: {out_path}")


if __name__ == "__main__":
    main()
