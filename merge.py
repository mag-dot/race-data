#!/usr/bin/env python3
"""
HKJC Race Data Merger

Merges all data sources into a single analysis-ready JSON per race meeting.
Creates one unified HorseRaceEntry per horse with all available intelligence.

Usage:
    python3 merge.py --date 2026-04-01
    python3 merge.py --date 2026-04-01 --output analysis/2026-04-01.json
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
ANALYSIS_DIR = Path(__file__).resolve().parent / "analysis"


def load_json(filepath: Path) -> dict | list | None:
    if not filepath.exists():
        return None
    with open(filepath) as f:
        return json.load(f)


def build_jockey_lookup() -> dict:
    """Build jockey name → stats lookup from rankings."""
    path = DATA_DIR / "jockeys" / "rankings-current.json"
    data = load_json(path)
    if not data:
        return {}
    lookup = {}
    for j in data:
        name = j["name"]
        total = j["totalRides"] or 1
        lookup[name] = {
            "wins": j["wins"],
            "seconds": j["seconds"],
            "thirds": j["thirds"],
            "totalRides": j["totalRides"],
            "winPct": round(j["wins"] / total * 100, 1),
            "placePct": round((j["wins"] + j["seconds"] + j["thirds"]) / total * 100, 1),
            "stakesWon": j["stakesWon"],
        }
    return lookup


def build_trainer_lookup() -> dict:
    """Build trainer name → stats lookup from rankings."""
    path = DATA_DIR / "trainers" / "rankings-current.json"
    data = load_json(path)
    if not data:
        return {}
    lookup = {}
    for t in data:
        name = t["name"]
        total = t["totalRunners"] or 1
        lookup[name] = {
            "wins": t["wins"],
            "seconds": t["seconds"],
            "thirds": t["thirds"],
            "totalRunners": t["totalRunners"],
            "winPct": round(t["wins"] / total * 100, 1),
            "placePct": round((t["wins"] + t["seconds"] + t["thirds"]) / total * 100, 1),
            "stakesWon": t["stakesWon"],
        }
    return lookup


def find_recent_barrier_trials(horse_name: str, horse_code: str, days_back: int = 60) -> list:
    """Find recent barrier trial entries for a horse."""
    bt_dir = DATA_DIR / "barrier-trials"
    if not bt_dir.exists():
        return []
    
    results = []
    for f in sorted(bt_dir.glob("*.json"), reverse=True):
        data = load_json(f)
        if not data:
            continue
        for batch in data.get("batches", []):
            for h in batch.get("horses", []):
                # Match by code (preferred) or name
                if (horse_code and h.get("horseCode") == horse_code) or \
                   h.get("name", "").upper() == horse_name.upper():
                    results.append({
                        "trialDate": data.get("trialDate", ""),
                        "venue": batch.get("venue", ""),
                        "distance": batch.get("distance", ""),
                        "going": batch.get("going", ""),
                        "batchTime": batch.get("time", ""),
                        "sectionalTimes": batch.get("sectionalTimes", ""),
                        "draw": h.get("draw", ""),
                        "gear": h.get("gear", ""),
                        "lbw": h.get("lbw", ""),
                        "runningPosition": h.get("runningPosition", ""),
                        "time": h.get("time", ""),
                        "result": h.get("result", ""),
                        "comment": h.get("comment", ""),
                    })
        if len(results) >= 5:
            break
    return results


def get_draw_stats(date_slug: str, race_no: int, draw: int) -> dict | None:
    """Get draw statistics for a specific race and draw position."""
    path = DATA_DIR / "draw-stats" / f"{date_slug}.json"
    data = load_json(path)
    if not data:
        return None
    for race in data.get("races", []):
        if race.get("raceNo") == race_no:
            for d in race.get("draws", []):
                if d.get("draw") == draw:
                    return {
                        "runners": d["runners"],
                        "winPct": d["winPct"],
                        "placePct": d["placePct"],
                        "quinellaPct": d["quinellaPct"],
                    }
    return None


def get_trackwork(date_slug: str, race_no: int, horse_name: str) -> dict | None:
    """Get trackwork data for a horse in a specific race."""
    path = DATA_DIR / "trackwork" / f"{date_slug}.json"
    data = load_json(path)
    if not data:
        return None
    for race in data:
        if race.get("raceNo") == race_no:
            for h in race.get("horses", []):
                if h.get("name", "").upper() == horse_name.upper():
                    return h
    return None


def classify_trial_comment(comment: str) -> str:
    """Classify barrier trial steward comment as positive/neutral/negative."""
    if not comment:
        return "unknown"
    
    c = comment.lower()
    positive = ["plenty in hand", "responded well", "good early speed", "led all the way",
                "kept on", "ran on nicely", "stayed on well", "impressive", "won going away",
                "strong finish", "hit the front", "never headed", "scored"]
    negative = ["unimpressive", "not much improvement", "limited response", "tailed off",
                "gave ground", "never in contention", "laboured", "not persevered",
                "failed", "eased", "pulled up", "reluctant"]
    
    for p in positive:
        if p in c:
            return "positive"
    for n in negative:
        if n in c:
            return "negative"
    return "neutral"


def count_recent_gallops(trackwork: dict, days: int = 14) -> dict:
    """Count gallop frequency and intensity from trackwork data."""
    gallops = trackwork.get("gallops", [])
    trotting = trackwork.get("trotting", [])
    swimming = trackwork.get("swimming", [])
    treadmill = trackwork.get("treadmill", [])
    
    result = {
        "gallopCount": len(gallops),
        "trottingCount": len(trotting),
        "swimmingCount": len(swimming),
        "treadmillCount": len(treadmill),
        "totalSessions": len(gallops) + len(trotting) + len(swimming) + len(treadmill),
    }
    
    # Count fast work from gallops
    fast_count = 0
    for g in gallops:
        if isinstance(g, dict):
            raw = g.get("raw", "")
        else:
            raw = str(g)
        if "Fast" in raw or "Rev Fast" in raw:
            fast_count += 1
    result["fastWorkCount"] = fast_count
    
    # Get best gallop time if available
    best_time = None
    for g in gallops:
        if isinstance(g, dict) and g.get("totalTime"):
            t = g["totalTime"]
            if isinstance(t, (int, float)):
                if best_time is None or t < best_time:
                    best_time = t
    result["bestGallopTime"] = best_time
    
    return result


def merge_race(date_slug: str, formguide: list, jockeys: dict, trainers: dict) -> list:
    """Merge all data sources for a single race meeting."""
    merged_races = []
    
    for race in formguide:
        race_no = race.get("raceNo", 0)
        race_info = race.get("info", {})
        header = race.get("headerText", "")
        
        # Parse distance from header
        dist_m = re.search(r"(\d{3,4})M", header, re.IGNORECASE)
        distance = int(dist_m.group(1)) if dist_m else None
        
        # Parse surface
        surface = "Turf"
        if "all weather" in header.lower() or "AWT" in header:
            surface = "AWT"
        
        # Parse class
        class_m = re.search(r"Class\s+(\d)", header)
        race_class = int(class_m.group(1)) if class_m else None
        
        # Parse venue
        venue = "Sha Tin"
        if "happy valley" in header.lower() or "HV" in header:
            venue = "Happy Valley"
        
        merged_horses = []
        for h in race.get("horses", []):
            horse_name = h.get("name", "")
            horse_code = h.get("horseCode", "")
            draw = h.get("draw", "")
            draw_int = int(draw) if str(draw).isdigit() else None
            
            # --- Jockey/Trainer stats ---
            jockey_name = h.get("jockey", "")
            trainer_name = h.get("trainer", "")
            jockey_stats = jockeys.get(jockey_name)
            trainer_stats = trainers.get(trainer_name)
            
            # --- Form analysis ---
            form_entries = h.get("form", [])
            energy_scores = [e.get("energy") for e in form_entries if isinstance(e.get("energy"), int)]
            recent_positions = []
            for e in form_entries:
                fp = e.get("fpTs", "")
                m = re.match(r"(\d+)\s*/\s*\d+", str(fp))
                if m:
                    recent_positions.append(int(m.group(1)))
            
            days_since = form_entries[0].get("daysSinceLast") if form_entries else None
            
            # --- Barrier trials ---
            trials = find_recent_barrier_trials(horse_name, horse_code)
            trial_sentiment = None
            last_trial_comment = None
            if trials:
                last_trial_comment = trials[0].get("comment", "")
                trial_sentiment = classify_trial_comment(last_trial_comment)
            
            # --- Draw stats ---
            draw_data = get_draw_stats(date_slug, race_no, draw_int) if draw_int else None
            
            # --- Trackwork ---
            tw = get_trackwork(date_slug, race_no, horse_name)
            tw_stats = count_recent_gallops(tw) if tw else None
            
            # --- Build unified entry ---
            entry = {
                # Identity
                "horseName": horse_name,
                "horseCode": horse_code,
                "horseNo": h.get("number"),
                
                # Race context
                "raceNo": race_no,
                "raceDate": date_slug,
                "venue": venue,
                "surface": surface,
                "distance": distance,
                "class": race_class,
                
                # Horse state
                "draw": draw_int or draw,
                "carryWeight": h.get("weight"),
                "bodyWeight": h.get("bodyWeight"),
                "jockey": jockey_name,
                "trainer": trainer_name,
                "age": h.get("age"),
                
                # Form
                "recentPositions": recent_positions,
                "energyScores": energy_scores,
                "avgEnergy": round(sum(energy_scores) / len(energy_scores), 1) if energy_scores else None,
                "lastRunDaysAgo": days_since if isinstance(days_since, int) else None,
                "formEntries": len(form_entries),
                
                # Barrier trials
                "recentTrials": trials[:3],
                "lastTrialComment": last_trial_comment,
                "trialSentiment": trial_sentiment,
                
                # Draw advantage
                "drawStats": draw_data,
                
                # Trackwork / fitness
                "trackwork": tw_stats,
                
                # Jockey/Trainer
                "jockeyStats": jockey_stats,
                "trainerStats": trainer_stats,
            }
            
            merged_horses.append(entry)
        
        merged_races.append({
            "raceNo": race_no,
            "raceName": header,
            "venue": venue,
            "surface": surface,
            "distance": distance,
            "class": race_class,
            "horses": merged_horses,
        })
    
    return merged_races


def analysis_to_markdown(races: list, date_slug: str) -> str:
    """Convert merged analysis to LLM-friendly markdown."""
    lines = [f"# Race Analysis — {date_slug}"]
    lines.append(f"\n**{len(races)} races** | All data merged from formguide, barrier trials, trackwork, draw stats, jockey/trainer rankings\n")
    
    for race in races:
        lines.append(f"## Race {race['raceNo']} — {race.get('surface', '')} {race.get('distance', '')}m | Class {race.get('class', '?')}")
        lines.append("")
        
        for h in race.get("horses", []):
            name = h["horseName"]
            no = h.get("horseNo", "?")
            draw = h.get("draw", "?")
            jockey = h.get("jockey", "")
            trainer = h.get("trainer", "")
            
            lines.append(f"### #{no} {name} (Draw {draw})")
            lines.append(f"- **J:** {jockey} | **T:** {trainer} | **Age:** {h.get('age', '?')}")
            lines.append(f"- **Weight:** {h.get('carryWeight', '?')}lbs | **Body:** {h.get('bodyWeight', '?')}lbs")
            
            # Form
            positions = h.get("recentPositions", [])
            if positions:
                lines.append(f"- **Recent form:** {'/'.join(str(p) for p in positions)} (avg {sum(positions)/len(positions):.1f})")
            
            energy = h.get("energyScores", [])
            if energy:
                lines.append(f"- **Energy:** {energy} (avg {h.get('avgEnergy', '?')})")
            
            if h.get("lastRunDaysAgo"):
                lines.append(f"- **Days since last run:** {h['lastRunDaysAgo']}")
            
            # Trials
            if h.get("recentTrials"):
                t = h["recentTrials"][0]
                lines.append(f"- **Last trial:** {t.get('trialDate', '')} {t.get('venue', '')} {t.get('distance', '')} — {t.get('comment', '')}")
                lines.append(f"  Sentiment: **{h.get('trialSentiment', '?')}** | Result: {t.get('result', '?')}")
            
            # Draw
            ds = h.get("drawStats")
            if ds:
                lines.append(f"- **Draw {draw} stats:** W%={ds['winPct']} P%={ds['placePct']} ({ds['runners']} runners)")
            
            # Trackwork
            tw = h.get("trackwork")
            if tw:
                lines.append(f"- **Trackwork:** {tw['gallopCount']} gallops, {tw['fastWorkCount']} fast works, {tw['totalSessions']} total sessions")
                if tw.get("bestGallopTime"):
                    lines.append(f"  Best gallop: {tw['bestGallopTime']}s")
            
            # Jockey
            js = h.get("jockeyStats")
            if js:
                lines.append(f"- **Jockey {jockey}:** W%={js['winPct']} P%={js['placePct']} ({js['wins']}/{js['totalRides']})")
            
            # Trainer
            ts = h.get("trainerStats")
            if ts:
                lines.append(f"- **Trainer {trainer}:** W%={ts['winPct']} P%={ts['placePct']} ({ts['wins']}/{ts['totalRunners']})")
            
            lines.append("")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="HKJC Race Data Merger")
    parser.add_argument("--date", required=True, help="Race date in YYYY-MM-DD format")
    parser.add_argument("--output", help="Output file path (default: analysis/{date}.json)")
    args = parser.parse_args()
    
    date_slug = args.date
    
    print(f"🔗 Merging race data for {date_slug}...")
    
    # Load formguide (required)
    fg_path = DATA_DIR / "formguide" / f"{date_slug}.json"
    formguide = load_json(fg_path)
    if not formguide:
        print(f"  ❌ No formguide found at {fg_path}")
        sys.exit(1)
    print(f"  ✓ Formguide: {len(formguide)} races")
    
    # Load lookups
    jockeys = build_jockey_lookup()
    trainers = build_trainer_lookup()
    print(f"  ✓ Jockeys: {len(jockeys)} | Trainers: {len(trainers)}")
    
    # Check available data
    bt_count = len(list((DATA_DIR / "barrier-trials").glob("*.json"))) if (DATA_DIR / "barrier-trials").exists() else 0
    tw_exists = (DATA_DIR / "trackwork" / f"{date_slug}.json").exists()
    ds_exists = (DATA_DIR / "draw-stats" / f"{date_slug}.json").exists()
    print(f"  ✓ Barrier trials: {bt_count} dates | Trackwork: {'✓' if tw_exists else '✗'} | Draw stats: {'✓' if ds_exists else '✗'}")
    
    # Merge
    merged = merge_race(date_slug, formguide, jockeys, trainers)
    
    total_horses = sum(len(r["horses"]) for r in merged)
    print(f"  → Merged {len(merged)} races, {total_horses} horses")
    
    # Save
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output) if args.output else ANALYSIS_DIR / f"{date_slug}.json"
    
    with open(out_path, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved {out_path} ({os.path.getsize(out_path):,} bytes)")
    
    md = analysis_to_markdown(merged, date_slug)
    md_path = out_path.with_suffix(".md")
    with open(md_path, "w") as f:
        f.write(md)
    print(f"  ✓ Saved {md_path}")
    
    # Summary stats
    trial_count = sum(1 for r in merged for h in r["horses"] if h.get("recentTrials"))
    tw_count = sum(1 for r in merged for h in r["horses"] if h.get("trackwork"))
    ds_count = sum(1 for r in merged for h in r["horses"] if h.get("drawStats"))
    js_count = sum(1 for r in merged for h in r["horses"] if h.get("jockeyStats"))
    
    print(f"\n📊 Coverage:")
    print(f"  Barrier trials: {trial_count}/{total_horses} horses ({trial_count/total_horses*100:.0f}%)")
    print(f"  Trackwork: {tw_count}/{total_horses} horses ({tw_count/total_horses*100:.0f}%)")
    print(f"  Draw stats: {ds_count}/{total_horses} horses ({ds_count/total_horses*100:.0f}%)")
    print(f"  Jockey stats: {js_count}/{total_horses} horses ({js_count/total_horses*100:.0f}%)")
    
    print(f"\n✅ Done!")


if __name__ == "__main__":
    main()
