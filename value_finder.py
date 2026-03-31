#!/usr/bin/env python3
"""
HKJC Value Bet Finder

Hunts for HIGH-ODDS opportunities the market underprices.
Uses signals the composite score misses: sectional closing speed,
energy spikes, barrier trial comebacks, and contrarian patterns.

This is NOT about finding winners — it's about finding horses
whose TRUE probability exceeds what the odds imply.

Usage:
    python3 value_finder.py --date 2026-04-01          # score upcoming race
    python3 value_finder.py --backtest 2026-03-29       # test against results
"""

import argparse
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).resolve().parent / "data"
ANALYSIS_DIR = Path(__file__).resolve().parent / "analysis"

# ============================================================
# VALUE SIGNALS — each returns (signal_strength, description)
# Signal strength: 0-100 (higher = stronger value signal)
# These specifically target OVERLAYS — horses the market misses
# ============================================================

def signal_closing_speed(horse: dict) -> tuple:
    """
    STRONGEST VALUE SIGNAL: Horse that closed fastest last 400m but didn't win.
    Market anchors on finishing position, not HOW the horse ran.
    A horse that ran on from last but closed fastest = huge next-start value.
    """
    form = horse.get("form", [])
    if not form:
        return 0, ""
    
    last_run = form[0]
    sectionals = last_run.get("sectionalTimes", [])
    comment = str(last_run.get("comment", ""))
    energy = last_run.get("energy")
    
    if not sectionals or len(sectionals) < 3:
        return 0, ""
    
    # Last sectional is the closing split
    last_sectional = sectionals[-1]
    if not isinstance(last_sectional, (int, float)):
        return 0, ""
    
    strength = 0
    notes = []
    
    # Fast closing sectional (under 23.0 for 400m is very fast in HK)
    if last_sectional < 22.5:
        strength += 40
        notes.append(f"blazing close {last_sectional}s")
    elif last_sectional < 23.0:
        strength += 25
        notes.append(f"strong close {last_sectional}s")
    elif last_sectional < 23.5:
        strength += 10
        notes.append(f"solid close {last_sectional}s")
    
    # Comment analysis for closing patterns
    close_keywords = [
        "ran on", "finished strongly", "closing", "best work late",
        "hit the line", "strong finish", "flew home", "rattled home",
        "flashing late", "never nearer", "came from last"
    ]
    for kw in close_keywords:
        if kw in comment.lower():
            strength += 15
            notes.append(f"comment: '{kw}'")
            break
    
    # High energy + non-winning position = unlucky
    if energy and energy >= 95:
        strength += 15
        notes.append(f"energy {energy}")
    
    # Didn't win despite closing fast = the value signal
    fp = last_run.get("fpTs", "")
    m = re.match(r"(\d+)", str(fp))
    if m:
        pos = int(m.group(1))
        if 2 <= pos <= 6:
            strength += 10  # near miss amplifies closing speed signal
            notes.append(f"finished {pos}th")
        elif pos > 6:
            strength += 20  # closed fast from way back = market will ignore
            notes.append(f"finished {pos}th (back-marker value)")
    
    return min(100, strength), " | ".join(notes) if notes else ""


def signal_energy_spike(horse: dict) -> tuple:
    """
    Horse had a massive energy spike last start (95+) but market might
    not have noticed because the finishing position was poor.
    High energy = the horse tried hard. Next start at same level = danger.
    """
    form = horse.get("form", [])
    if not form:
        return 0, ""
    
    energies = [f.get("energy") for f in form[:5] if isinstance(f.get("energy"), (int, float))]
    if not energies:
        return 0, ""
    
    last_energy = energies[0]
    strength = 0
    notes = []
    
    # Raw energy spike
    if last_energy >= 100:
        strength += 40
        notes.append(f"energy {last_energy} (exceptional)")
    elif last_energy >= 95:
        strength += 25
        notes.append(f"energy {last_energy} (outstanding)")
    elif last_energy >= 90:
        strength += 10
        notes.append(f"energy {last_energy} (strong)")
    
    # Rising trend amplifies signal
    if len(energies) >= 3:
        if energies[0] > energies[1] > energies[2]:
            strength += 20
            notes.append(f"3-run rising trend ({energies[2]}→{energies[1]}→{energies[0]})")
        elif energies[0] > energies[1]:
            strength += 10
            notes.append(f"improving ({energies[1]}→{energies[0]})")
    
    # Energy spike but finished poorly = market will underrate
    fp = form[0].get("fpTs", "")
    m = re.match(r"(\d+)", str(fp))
    if m and int(m.group(1)) > 4 and last_energy >= 92:
        strength += 15
        notes.append(f"high effort despite {m.group(1)}th — under-rated")
    
    return min(100, strength), " | ".join(notes) if notes else ""


def signal_trial_comeback(horse: dict, trials: list) -> tuple:
    """
    Horse returning from a break with a POSITIVE barrier trial.
    Market discounts horses coming back from layoffs.
    A strong trial + positive steward comment = fitness confirmed but odds stay long.
    """
    form = horse.get("form", [])
    if not trials:
        return 0, ""
    
    # Check days since last race
    days_since = None
    if form:
        days_since = form[0].get("daysSinceLast")
        if isinstance(days_since, str):
            try:
                days_since = int(days_since)
            except:
                days_since = None
    
    strength = 0
    notes = []
    
    last_trial = trials[0]
    comment = str(last_trial.get("comment", "")).lower()
    result = str(last_trial.get("result", "")).strip()
    
    # Positive trial comments
    positive_kw = [
        "plenty in hand", "responded well", "good early speed",
        "led all the way", "kept on", "ran on nicely", "strong finish",
        "hit the front", "never headed", "won going away", "impressive",
        "comfortably", "eased down", "not pushed out"
    ]
    
    for kw in positive_kw:
        if kw in comment:
            strength += 25
            notes.append(f"trial: '{kw}'")
            break
    
    # Trial winner
    if result in ["1", "1st", "won"]:
        strength += 20
        notes.append("trial winner")
    
    # Layoff amplifies the signal (market discounts returnees)
    if days_since and days_since > 45:
        strength += 20
        notes.append(f"{days_since}d layoff (market discount)")
    elif days_since and days_since > 30:
        strength += 10
        notes.append(f"{days_since}d since last run")
    
    # Multiple trials = very well prepared
    if len(trials) >= 2:
        strength += 10
        notes.append(f"{len(trials)} recent trials")
    
    return min(100, strength), " | ".join(notes) if notes else ""


def signal_class_dropper(horse: dict) -> tuple:
    """
    Horse dropping in class with recent competitive form.
    The market SOMETIMES catches this, but not always —
    especially when the horse's last few results look poor
    (because they were running against better horses).
    """
    form = horse.get("form", [])
    if not form:
        return 0, ""
    
    strength = 0
    notes = []
    
    # Check comment for class indicators
    for f in form[:3]:
        comment = str(f.get("comment", "")).lower()
        if any(kw in comment for kw in ["outclassed", "above average", "better class"]):
            strength += 15
            notes.append("ran in higher class recently")
            break
    
    # Good energy in higher class = will murder weaker field
    energies = [f.get("energy") for f in form[:3] if isinstance(f.get("energy"), (int, float))]
    if energies and max(energies) >= 90:
        strength += 20
        notes.append(f"peak energy {max(energies)} in harder company")
    
    return min(100, strength), " | ".join(notes) if notes else ""


def signal_jockey_upgrade(horse: dict) -> tuple:
    """
    Top jockey picks up a new ride = the connections are trying harder.
    Purton/Bowman choosing a horse they've never ridden = they see something.
    """
    jockey = horse.get("jockey", "")
    form = horse.get("form", [])
    
    elite_jockeys = {"Z Purton", "H Bowman", "J McDonald"}
    
    if jockey not in elite_jockeys:
        return 0, ""
    
    # Check if this jockey rode the horse before
    prev_jockeys = [f.get("jockey", "") for f in form[:5]]
    if jockey not in prev_jockeys:
        return 60, f"NEW ride for {jockey} — jockey upgrade signal"
    
    return 0, ""


def signal_wet_track_specialist(horse: dict) -> tuple:
    """
    Placeholder for track condition specialists.
    Some horses love wet tracks; if it's raining, they become value.
    Needs weather data integration.
    """
    return 0, ""


def signal_running_style_vs_pace(horse: dict, race_horses: list) -> tuple:
    """
    If most horses in the race are front-runners, a closer becomes value.
    If the race lacks speed, a front-runner becomes value.
    Uses comment analysis to classify running styles.
    """
    form = horse.get("form", [])
    if not form:
        return 0, ""
    
    # Classify this horse's style from comments
    comments = " ".join(str(f.get("comment", "")) for f in form[:3]).lower()
    
    is_closer = any(kw in comments for kw in [
        "came from last", "ran on", "closing", "back marker",
        "settled in rear", "at the rear", "from behind"
    ])
    is_leader = any(kw in comments for kw in [
        "led", "took the lead", "dictated", "in front",
        "set the pace", "made all"
    ])
    
    if not is_closer and not is_leader:
        return 0, ""
    
    # Count front-runners in the race
    leaders = 0
    closers = 0
    for h in race_horses:
        h_form = h.get("form", [])
        h_comments = " ".join(str(f.get("comment", "")) for f in h_form[:3]).lower()
        if any(kw in h_comments for kw in ["led", "took the lead", "dictated", "in front"]):
            leaders += 1
        if any(kw in h_comments for kw in ["came from last", "ran on", "closing", "settled in rear"]):
            closers += 1
    
    strength = 0
    notes = []
    
    # Many leaders + this horse closes = pace collapse advantage
    if is_closer and leaders >= 3:
        strength = 40
        notes.append(f"closer in speed-heavy race ({leaders} front-runners → pace collapse)")
    
    # Few leaders + this horse leads = easy lead
    if is_leader and leaders <= 1:
        strength = 30
        notes.append(f"front-runner with no pace pressure ({leaders} other leaders)")
    
    return min(100, strength), " | ".join(notes) if notes else ""


# ============================================================
# COMPOSITE VALUE SCORE
# ============================================================

def calculate_value_score(horse: dict, trials: list, race_horses: list) -> dict:
    """
    Calculate composite value score from all signals.
    Unlike the main scoring engine, this specifically targets OVERLAYS.
    """
    signals = {}
    
    s1, n1 = signal_closing_speed(horse)
    signals["closingSpeed"] = {"strength": s1, "note": n1}
    
    s2, n2 = signal_energy_spike(horse)
    signals["energySpike"] = {"strength": s2, "note": n2}
    
    s3, n3 = signal_trial_comeback(horse, trials)
    signals["trialComeback"] = {"strength": s3, "note": n3}
    
    s4, n4 = signal_class_dropper(horse)
    signals["classDropper"] = {"strength": s4, "note": n4}
    
    s5, n5 = signal_jockey_upgrade(horse)
    signals["jockeyUpgrade"] = {"strength": s5, "note": n5}
    
    s6, n6 = signal_running_style_vs_pace(horse, race_horses)
    signals["paceScenario"] = {"strength": s6, "note": n6}
    
    # Weighted value composite
    weights = {
        "closingSpeed": 0.30,    # strongest predictor of next-start value
        "energySpike": 0.25,     # effort not reflected in position
        "trialComeback": 0.15,   # market discounts returnees
        "classDropper": 0.10,    # class edge
        "jockeyUpgrade": 0.10,   # connections signal
        "paceScenario": 0.10,    # tactical edge
    }
    
    total = sum(signals[k]["strength"] * weights[k] for k in weights)
    
    # Count active signals (strength > 0)
    active = sum(1 for s in signals.values() if s["strength"] > 0)
    
    # Multi-signal bonus: 3+ active signals = compounding edge
    if active >= 4:
        total *= 1.3
    elif active >= 3:
        total *= 1.15
    
    return {
        "valueScore": round(min(100, total), 1),
        "activeSignals": active,
        "signals": signals,
    }


# ============================================================
# ANALYSIS
# ============================================================

def find_barrier_trials(horse_name: str) -> list:
    """Find recent barrier trials for a horse."""
    bt_dir = DATA_DIR / "barrier-trials"
    if not bt_dir.exists():
        return []
    results = []
    for f in sorted(bt_dir.glob("*.json"), reverse=True):
        data = json.load(open(f))
        for batch in data.get("batches", []):
            for h in batch.get("horses", []):
                if h.get("name", "").upper() == horse_name.upper():
                    results.append({
                        "trialDate": data.get("trialDate", ""),
                        "comment": h.get("comment", ""),
                        "result": h.get("result", ""),
                    })
        if len(results) >= 3:
            break
    return results


def analyse_formguide(date_slug: str):
    """Run value analysis on a formguide."""
    fg_path = DATA_DIR / "formguide" / f"{date_slug}.json"
    if not fg_path.exists():
        print(f"No formguide for {date_slug}")
        sys.exit(1)
    
    formguide = json.load(open(fg_path))
    
    print(f"🔍 VALUE FINDER — {date_slug}")
    print(f"{'='*90}")
    
    all_value_picks = []
    
    for race in formguide:
        rn = race.get("raceNo", 0)
        header = race.get("headerText", "")[:70]
        horses = race.get("horses", [])
        
        print(f"\n━━━ Race {rn} | {header}")
        
        scored = []
        for h in horses:
            name = h.get("name", "")
            trials = find_barrier_trials(name)
            result = calculate_value_score(h, trials, horses)
            scored.append({
                "name": name,
                "number": h.get("number", "?"),
                "jockey": h.get("jockey", ""),
                "odds": h.get("form", [{}])[0].get("odds") if h.get("form") else None,
                **result,
            })
        
        # Sort by value score
        scored.sort(key=lambda x: x["valueScore"], reverse=True)
        
        # Show top value picks (score > 20)
        picks = [s for s in scored if s["valueScore"] > 20]
        
        if not picks:
            print("  No strong value signals detected")
            continue
        
        for s in picks[:3]:
            active_notes = [
                sig["note"] for sig in s["signals"].values() 
                if sig["strength"] > 0 and sig["note"]
            ]
            print(f"  💎 #{s['number']} {s['name']:<22} VALUE: {s['valueScore']:5.1f}  ({s['activeSignals']} signals)")
            for note in active_notes:
                print(f"       → {note}")
            all_value_picks.append({"race": rn, **s})
    
    # Summary
    print(f"\n{'='*90}")
    print(f"📊 VALUE SUMMARY — {date_slug}")
    print(f"{'='*90}")
    
    strong = [p for p in all_value_picks if p["valueScore"] >= 40]
    moderate = [p for p in all_value_picks if 25 <= p["valueScore"] < 40]
    
    if strong:
        print(f"\n🔥 STRONG VALUE ({len(strong)} horses):")
        for p in strong:
            print(f"  R{p['race']} #{p['number']} {p['name']:<22} VALUE: {p['valueScore']:5.1f}")
    
    if moderate:
        print(f"\n💡 MODERATE VALUE ({len(moderate)} horses):")
        for p in moderate:
            print(f"  R{p['race']} #{p['number']} {p['name']:<22} VALUE: {p['valueScore']:5.1f}")
    
    if not strong and not moderate:
        print("\n  No value bets identified for this meeting.")
    
    return all_value_picks


def backtest_value(date_slug: str):
    """
    Backtest value finder against actual results.
    Check if high-value picks actually won/placed at good odds.
    """
    fg_path = DATA_DIR / "formguide" / f"{date_slug}.json"
    res_path = DATA_DIR / "results" / f"{date_slug}.json"
    
    if not fg_path.exists() or not res_path.exists():
        print(f"Need both formguide and results for {date_slug}")
        sys.exit(1)
    
    formguide = json.load(open(fg_path))
    results = json.load(open(res_path))
    
    # Build results lookup
    result_lookup = {}  # (raceNo, horseName) -> {placing, odds}
    for race in results:
        rn = race["raceNo"]
        for h in race.get("horses", []):
            result_lookup[(rn, h["horseName"].upper())] = {
                "placing": h["placing"],
                "odds": h.get("winOdds", ""),
            }
    
    print(f"🔍 VALUE BACKTEST — {date_slug}")
    print(f"{'='*90}")
    
    total_value_bets = 0
    value_wins = 0
    value_places = 0
    total_staked = 0
    total_win_return = 0
    total_place_return = 0  # approximate
    
    for race in formguide:
        rn = race.get("raceNo", 0)
        horses = race.get("horses", [])
        
        scored = []
        for h in horses:
            name = h.get("name", "")
            trials = find_barrier_trials(name)
            result = calculate_value_score(h, trials, horses)
            scored.append({"name": name, "number": h.get("number"), **result})
        
        scored.sort(key=lambda x: x["valueScore"], reverse=True)
        picks = [s for s in scored if s["valueScore"] >= 25]
        
        for p in picks[:2]:  # max 2 value bets per race
            key = (rn, p["name"].upper())
            actual = result_lookup.get(key, {})
            placing = actual.get("placing", "?")
            odds = actual.get("odds", "0")
            
            try:
                odds_f = float(odds)
            except:
                odds_f = 0
            
            total_value_bets += 1
            total_staked += 10
            
            won = placing == "1"
            placed = placing in ["1", "2", "3"]
            
            if won:
                value_wins += 1
                total_win_return += odds_f * 10
            if placed:
                value_places += 1
                # Approximate place return (odds / 3 roughly)
                total_place_return += (odds_f / 3) * 10
            
            marker = "✅" if won else ("📍" if placed else "  ")
            active_notes = [sig["note"] for sig in p["signals"].values() if sig["strength"] > 0 and sig["note"]]
            
            print(f"  {marker} R{rn} #{p['number']} {p['name']:<22} VAL:{p['valueScore']:5.1f} → {placing:>3} @ ${odds}")
            if active_notes:
                print(f"       {' | '.join(active_notes[:2])}")
    
    # Summary
    print(f"\n{'='*90}")
    print(f"📊 VALUE BACKTEST RESULTS — {date_slug}")
    print(f"{'='*90}")
    if total_value_bets > 0:
        print(f"  Value bets placed:    {total_value_bets}")
        print(f"  Winners:              {value_wins}/{total_value_bets} ({value_wins/total_value_bets*100:.0f}%)")
        print(f"  Placed (top 3):       {value_places}/{total_value_bets} ({value_places/total_value_bets*100:.0f}%)")
        print(f"  Staked:               ${total_staked}")
        print(f"  Win returns:          ${total_win_return:.0f}")
        print(f"  Win P&L:              ${total_win_return - total_staked:+.0f}")
        print(f"  Win ROI:              {(total_win_return - total_staked) / total_staked * 100:+.1f}%")
        if value_wins > 0:
            avg_odds = total_win_return / value_wins / 10
            print(f"  Avg winner odds:      ${avg_odds:.1f}")
    else:
        print("  No value bets triggered.")


def main():
    parser = argparse.ArgumentParser(description="HKJC Value Bet Finder")
    parser.add_argument("--date", help="Analyse formguide for date (YYYY-MM-DD)")
    parser.add_argument("--backtest", help="Backtest against results (YYYY-MM-DD)")
    args = parser.parse_args()
    
    if args.backtest:
        backtest_value(args.backtest)
    elif args.date:
        analyse_formguide(args.date)
    else:
        print("Usage: python3 value_finder.py --date 2026-04-01")
        print("       python3 value_finder.py --backtest 2026-03-29")


if __name__ == "__main__":
    main()
