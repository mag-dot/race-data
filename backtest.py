#!/usr/bin/env python3
"""
Backtest the HKJC betting strategy against actual race results.
Uses historical results to build horse profiles, then scores horses
for a target race day and compares predictions to actual outcomes.

Based on: office/strategy/HKJC_BETTING_STRATEGY.md
"""
import json
import glob
import re
import sys
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent / "data"

# ============================================================
# 1. Load all historical results into a horse database
# ============================================================

def load_horse_db(before_date: str):
    """
    Build a database of horse performance from all results BEFORE the target date.
    Returns: {horseName: [{"date", "raceNo", "placing", "draw", "weight", "jockey",
                           "trainer", "odds", "lbw", "runners", "class", "distance", "track"}]}
    """
    db = defaultdict(list)
    
    for f in sorted(glob.glob(str(DATA_DIR / "results" / "*.json"))):
        file_date = f.split("/")[-1].replace(".json", "")
        if file_date >= before_date:
            continue
        
        with open(f) as fh:
            races = json.load(fh)
        
        for race in races:
            race_info = race.get("raceInfo", {})
            header = ""
            if isinstance(race_info, dict):
                header = race_info.get("header", "")
            elif isinstance(race_info, str):
                header = race_info
            
            # Parse class and distance from header
            race_class = ""
            distance = ""
            track = "ST"  # default Sha Tin
            
            cls_match = re.search(r'Class\s+(\d)', header)
            if cls_match:
                race_class = cls_match.group(1)
            
            dist_match = re.search(r'(\d{3,4})M', header)
            if dist_match:
                distance = dist_match.group(1)
            
            if 'Happy Valley' in header:
                track = "HV"
            
            runners = len(race.get("horses", []))
            
            for h in race.get("horses", []):
                name = h["horseName"]
                db[name].append({
                    "date": file_date,
                    "raceNo": race["raceNo"],
                    "placing": h["placing"],
                    "draw": h.get("draw", ""),
                    "weight": h.get("actualWeight", ""),
                    "jockey": h.get("jockey", ""),
                    "trainer": h.get("trainer", ""),
                    "odds": h.get("winOdds", ""),
                    "lbw": h.get("lbw", ""),
                    "runners": runners,
                    "class": race_class,
                    "distance": distance,
                    "track": track,
                })
    
    return db


def load_jockey_stats():
    """Load jockey season rankings."""
    path = DATA_DIR / "jockeys" / "rankings-current.json"
    if not path.exists():
        return {}
    with open(path) as f:
        jockeys = json.load(f)
    return {j["name"]: j for j in jockeys}


def load_trainer_stats():
    """Load trainer season rankings."""
    path = DATA_DIR / "trainers" / "rankings-current.json"
    if not path.exists():
        return {}
    with open(path) as f:
        trainers = json.load(f)
    return {t["name"]: t for t in trainers}


# ============================================================
# 2. Scoring Functions (from strategy framework)
# ============================================================

def score_form(history, max_runs=5):
    """
    Score recent form with recency weighting.
    Returns 0-100 normalised score.
    """
    if not history:
        return 25  # neutral for unknowns
    
    multipliers = [1.5, 1.2, 1.0, 0.8, 0.5]
    placing_pts = {1: 10, 2: 7, 3: 5, 4: 3, 5: 1, 6: 1}
    
    total = 0
    max_total = 0
    recent = sorted(history, key=lambda x: x["date"], reverse=True)[:max_runs]
    
    for i, run in enumerate(recent):
        mult = multipliers[i] if i < len(multipliers) else 0.3
        max_total += 10 * mult
        try:
            pos = int(run["placing"])
            pts = placing_pts.get(pos, 0)
            total += pts * mult
        except (ValueError, TypeError):
            pass  # DNF, scratched, etc.
    
    if max_total == 0:
        return 25
    return (total / max_total) * 100


def score_jockey(jockey_name, jockey_stats):
    """
    Score jockey based on season win rate. Returns 0-100.
    Elite (20%+) = 100, Average (5%) = 30, Unknown = 25.
    """
    js = jockey_stats.get(jockey_name, {})
    wins = js.get("wins", 0)
    rides = js.get("totalRides", 1)
    if rides == 0:
        return 25
    wr = wins / rides * 100
    # Scale: 0% -> 0, 5% -> 30, 10% -> 60, 15% -> 80, 20%+ -> 100
    return min(100, wr * 5)


def score_trainer(trainer_name, trainer_stats):
    """
    Score trainer based on season win rate. Returns 0-100.
    """
    ts = trainer_stats.get(trainer_name, {})
    wins = ts.get("wins", 0)
    runners = ts.get("totalRunners", 1)
    if runners == 0:
        return 25
    wr = wins / runners * 100
    # Scale: 0% -> 0, 5% -> 30, 10% -> 60, 12%+ -> 80
    return min(100, wr * 6.5)


def score_draw(draw, distance, track, runners):
    """
    Score draw bias. Returns 0-100.
    Inside draws favoured at shorter distances, especially HV.
    """
    try:
        d = int(draw)
        n = int(runners)
    except (ValueError, TypeError):
        return 50  # neutral
    
    if n == 0:
        return 50
    
    # Position ratio (0 = inside, 1 = outside)
    ratio = (d - 1) / max(n - 1, 1)
    
    dist = int(distance) if distance else 1400
    
    if track == "HV":
        # HV has strong inside bias at short distances
        if dist <= 1200:
            return max(0, 100 - ratio * 120)  # harsh penalty for outside
        elif dist <= 1650:
            return max(0, 90 - ratio * 70)
        else:
            return max(0, 80 - ratio * 40)
    else:
        # Sha Tin — milder bias
        if dist <= 1200:
            return max(0, 90 - ratio * 60)
        elif dist <= 1400:
            return max(0, 85 - ratio * 45)
        elif dist <= 1600:
            return max(0, 80 - ratio * 30)
        else:
            return max(0, 75 - ratio * 20)  # minimal at 1800m+


def score_class_movement(history, target_class):
    """
    Score class drop/rise. Dropping = positive. Returns 0-100.
    """
    if not history or not target_class:
        return 50
    
    try:
        tc = int(target_class)
    except (ValueError, TypeError):
        return 50
    
    # Find last class the horse ran in
    recent = sorted(history, key=lambda x: x["date"], reverse=True)
    for run in recent:
        if run.get("class"):
            try:
                last_class = int(run["class"])
                if last_class < tc:
                    # Dropping in class (higher number = lower class)
                    return 75 + (tc - last_class) * 12  # bonus per class drop
                elif last_class > tc:
                    # Rising in class
                    return max(20, 50 - (last_class - tc) * 15)
                else:
                    return 50  # same class
            except (ValueError, TypeError):
                continue
    return 50


def score_weight(history, current_weight):
    """
    Score weight change. Lighter than last run = positive. Returns 0-100.
    """
    if not history:
        return 50
    
    try:
        cw = int(current_weight)
    except (ValueError, TypeError):
        return 50
    
    recent = sorted(history, key=lambda x: x["date"], reverse=True)
    for run in recent:
        try:
            lw = int(run["weight"])
            diff = lw - cw  # positive = carrying less now
            # Scale: -5 lbs -> 30, 0 -> 50, +3 -> 65, +5 -> 80
            return min(100, max(0, 50 + diff * 6))
        except (ValueError, TypeError):
            continue
    return 50


def score_specialist(history, target_distance, target_track):
    """
    Score track/distance specialisation. Returns 0-100.
    """
    if not history:
        return 50
    
    matching_runs = []
    for run in history:
        dist_match = (run.get("distance") == target_distance)
        track_match = (run.get("track") == target_track)
        if dist_match and track_match:
            matching_runs.append(run)
    
    if not matching_runs:
        return 40  # no data at this track/dist
    
    wins = sum(1 for r in matching_runs if r["placing"] == "1")
    places = sum(1 for r in matching_runs if r["placing"] in ["1", "2", "3"])
    total = len(matching_runs)
    
    # Specialist score based on win/place rate at this track/dist
    wr = wins / total * 100
    pr = places / total * 100
    return min(100, wr * 3 + pr * 1.5 + 20)


# ============================================================
# 3. Red/Green Flags
# ============================================================

def apply_flags(history, draw, distance, track, jockey, jockey_stats, trainer, trainer_stats):
    """
    Apply red and green flag adjustments. Returns adjustment in points (-50 to +50).
    """
    adj = 0
    flags = []
    
    if history:
        recent = sorted(history, key=lambda x: x["date"], reverse=True)
        
        # RED: 3+ consecutive finishes outside top 6
        bad_streak = 0
        for run in recent[:5]:
            try:
                if int(run["placing"]) > 6:
                    bad_streak += 1
                else:
                    break
            except:
                break
        if bad_streak >= 3:
            adj -= 15
            flags.append("🔴 3+ runs outside top 6")
        
        # RED: Wide draw at short distance HV
        try:
            d = int(draw)
            dist = int(distance) if distance else 1400
            if track == "HV" and dist <= 1200 and d >= 10:
                adj -= 20
                flags.append(f"🔴 Wide draw ({d}) at HV {dist}m")
        except:
            pass
        
        # GREEN: Last run closed strong but missed place (unlucky run)
        if len(recent) >= 1:
            last = recent[0]
            try:
                pos = int(last["placing"])
                if 4 <= pos <= 6 and last.get("lbw", ""):
                    # Close finish (within ~2 lengths)
                    lbw = last["lbw"].replace("-", "0").strip()
                    try:
                        margin = float(lbw)
                        if margin <= 2.5:
                            adj += 10
                            flags.append(f"🟢 Unlucky last ({pos}th, {margin}L off)")
                    except:
                        pass
            except:
                pass
        
        # GREEN: Won last start, jockey retained
        if recent and recent[0]["placing"] == "1" and recent[0].get("jockey") == jockey:
            adj += 8
            flags.append("🟢 Won last, jockey retained")
    
    # RED: Jockey with 0 wins
    js = jockey_stats.get(jockey, {})
    if js.get("wins", 0) == 0 and js.get("totalRides", 0) > 20:
        adj -= 10
        flags.append("🔴 Jockey 0 wins this season")
    
    # GREEN: Elite jockey booking
    jw = js.get("wins", 0)
    jr = js.get("totalRides", 1)
    if jr > 0 and jw / jr > 0.15:
        adj += 8
        flags.append(f"🟢 Elite jockey ({jw}/{jr} = {jw/jr*100:.0f}%)")
    
    return adj, flags


# ============================================================
# 4. Composite Scoring
# ============================================================

def composite_score(horse, history, race_info, jockey_stats, trainer_stats):
    """
    Calculate composite score for a horse. Returns (score, breakdown).
    """
    jockey = horse.get("jockey", "")
    trainer = horse.get("trainer", "")
    draw = horse.get("draw", "")
    weight = horse.get("actualWeight", horse.get("weight", ""))
    
    # Parse race info
    header = ""
    if isinstance(race_info, dict):
        header = race_info.get("header", "")
    elif isinstance(race_info, str):
        header = race_info
    
    track = "HV" if "Happy Valley" in header else "ST"
    distance = ""
    race_class = ""
    
    dm = re.search(r'(\d{3,4})M', header)
    if dm:
        distance = dm.group(1)
    cm = re.search(r'Class\s+(\d)', header)
    if cm:
        race_class = cm.group(1)
    
    is_awt = "All Weather" in header or "AWT" in header
    runners = horse.get("_runners", 14)
    
    # Calculate each factor
    form = score_form(history)
    jockey_sc = score_jockey(jockey, jockey_stats)
    trainer_sc = score_trainer(trainer, trainer_stats)
    draw_sc = score_draw(draw, distance, track, runners)
    class_sc = score_class_movement(history, race_class)
    weight_sc = score_weight(history, weight)
    specialist_sc = score_specialist(history, distance, track)
    
    # Flags
    flag_adj, flags = apply_flags(
        history, draw, distance, track, jockey, jockey_stats, trainer, trainer_stats
    )
    
    # Weights — adjust for track/distance
    w_form = 0.25
    w_energy = 0.00  # no energy data in results, skip
    w_jockey = 0.15
    w_trainer = 0.10
    w_draw = 0.15
    w_class = 0.15
    w_weight = 0.10
    w_specialist = 0.10
    
    if track == "HV" and not is_awt:
        w_draw = 0.20
        w_form = 0.20
        w_specialist = 0.10
    
    if is_awt:
        w_draw = 0.05
        w_form = 0.30
        w_specialist = 0.10
    
    dist_int = int(distance) if distance else 1400
    if dist_int >= 2000:
        w_draw = 0.05
        w_form = 0.30
        w_specialist = 0.15
    
    raw = (
        form * w_form +
        jockey_sc * w_jockey +
        trainer_sc * w_trainer +
        draw_sc * w_draw +
        class_sc * w_class +
        weight_sc * w_weight +
        specialist_sc * w_specialist
    )
    
    final = raw + flag_adj
    
    breakdown = {
        "form": form,
        "jockey": jockey_sc,
        "trainer": trainer_sc,
        "draw": draw_sc,
        "class": class_sc,
        "weight": weight_sc,
        "specialist": specialist_sc,
        "flag_adj": flag_adj,
        "flags": flags,
        "raw": raw,
        "final": final,
    }
    
    return final, breakdown


# ============================================================
# 5. Backtest Runner
# ============================================================

def backtest_race_day(target_date: str):
    """Run backtest for a single race day."""
    
    print(f"🏇 BACKTEST: {target_date} (Sha Tin)")
    print("=" * 100)
    
    # Load data
    results_path = DATA_DIR / "results" / f"{target_date}.json"
    if not results_path.exists():
        print(f"❌ No results file for {target_date}")
        return
    
    with open(results_path) as f:
        races = json.load(f)
    
    horse_db = load_horse_db(target_date)
    jockey_stats = load_jockey_stats()
    trainer_stats = load_trainer_stats()
    
    print(f"📊 Horse database: {len(horse_db)} horses from prior results")
    print(f"📊 Jockey stats: {len(jockey_stats)} jockeys")
    print(f"📊 Trainer stats: {len(trainer_stats)} trainers")
    print()
    
    # Track overall performance
    total_races = 0
    wins_top1 = 0  # our #1 pick won
    wins_top3 = 0  # winner was in our top 3
    place_top3 = 0  # our #1 pick placed (top 3)
    total_win_roi = 0  # simulated ROI for $10 win on #1 pick
    total_staked = 0
    
    for race in races:
        total_races += 1
        rn = race["raceNo"]
        race_info = race.get("raceInfo", {})
        horses = race.get("horses", [])
        runners = len(horses)
        
        header = ""
        if isinstance(race_info, dict):
            header = race_info.get("header", "")[:80]
        
        print(f"━━━ Race {rn} | {header}")
        
        # Score all horses
        scored = []
        for h in horses:
            h["_runners"] = runners
            name = h["horseName"]
            history = horse_db.get(name, [])
            score, breakdown = composite_score(h, history, race_info, jockey_stats, trainer_stats)
            scored.append({
                "horse": h,
                "score": score,
                "breakdown": breakdown,
                "history_count": len(history),
            })
        
        scored.sort(key=lambda x: x["score"], reverse=True)
        
        # Find actual winner
        winner = None
        placed = []
        for h in horses:
            if h["placing"] == "1":
                winner = h
            if h["placing"] in ["1", "2", "3"]:
                placed.append(h)
        
        if not winner:
            print("  ⚠ No winner found in results")
            continue
        
        # Print top 5 picks vs result
        print(f"  {'Rank':<5} {'#':<4} {'Horse':<24} {'Score':>6} {'Form':>5} {'Jky':>4} {'Draw':>5} {'Flg':>4} | {'Actual':>6} {'Odds':>6}")
        for i, s in enumerate(scored[:5]):
            h = s["horse"]
            bd = s["breakdown"]
            actual = h["placing"]
            odds = h.get("winOdds", "")
            flag_str = f"{bd['flag_adj']:+d}" if bd['flag_adj'] != 0 else "  0"
            marker = "✅" if actual == "1" else ("📍" if actual in ["2","3"] else "  ")
            print(f"  {marker}{i+1:<3} #{h['horseNo']:<3} {h['horseName']:<24} {s['score']:6.1f} {bd['form']:5.0f} {bd['jockey']:4.0f} {bd['draw']:5.0f} {flag_str:>4} | {actual:>6} {odds:>6}")
        
        # Where did actual winner rank in our model?
        winner_rank = None
        winner_score_entry = None
        for i, s in enumerate(scored):
            if s["horse"]["horseName"] == winner["horseName"]:
                winner_rank = i + 1
                winner_score_entry = s
                break
        
        winner_odds = winner.get("winOdds", "0")
        try:
            wo = float(winner_odds)
        except:
            wo = 0
        
        # Our #1 pick
        our_pick = scored[0]["horse"]
        our_pick_placing = our_pick["placing"]
        
        if our_pick["horseName"] == winner["horseName"]:
            wins_top1 += 1
            total_win_roi += wo * 10  # $10 win bet pays $odds * $10
            print(f"  🎯 TOP PICK WON! #{winner['horseNo']} {winner['horseName']} @ ${wo}")
        else:
            print(f"  ➤ Winner: #{winner['horseNo']} {winner['horseName']} @ ${wo} (ranked #{winner_rank} in model)")
        
        total_staked += 10
        
        try:
            if int(our_pick_placing) <= 3:
                place_top3 += 1
        except:
            pass
        
        if winner_rank and winner_rank <= 3:
            wins_top3 += 1
        
        # Show winner's flags if interesting
        if winner_score_entry and winner_score_entry["breakdown"]["flags"]:
            print(f"  ➤ Winner flags: {', '.join(winner_score_entry['breakdown']['flags'])}")
        
        print()
    
    # Summary
    print("=" * 100)
    print(f"📊 BACKTEST SUMMARY — {target_date}")
    print("=" * 100)
    print(f"  Races analysed:       {total_races}")
    print(f"  #1 pick won:          {wins_top1}/{total_races} ({wins_top1/total_races*100:.0f}%)")
    print(f"  #1 pick placed:       {place_top3}/{total_races} ({place_top3/total_races*100:.0f}%)")
    print(f"  Winner in top 3:      {wins_top3}/{total_races} ({wins_top3/total_races*100:.0f}%)")
    print(f"  Staked (sim $10/race): ${total_staked}")
    print(f"  Returns (win bets):   ${total_win_roi:.0f}")
    print(f"  P&L:                  ${total_win_roi - total_staked:+.0f}")
    print(f"  ROI:                  {(total_win_roi - total_staked) / total_staked * 100:+.1f}%")
    print()
    
    # Benchmark
    fav_wins = sum(1 for r in races for h in r["horses"] 
                   if h["placing"] == "1" and _is_favourite(h, r["horses"]))
    print(f"  (Benchmark: favourites won {fav_wins}/{total_races} = {fav_wins/total_races*100:.0f}%)")


def _is_favourite(winner, horses):
    """Check if this horse had the lowest odds."""
    try:
        winner_odds = float(winner.get("winOdds", "999"))
    except:
        return False
    for h in horses:
        try:
            if float(h.get("winOdds", "999")) < winner_odds and h["placing"] != winner["placing"]:
                return False
        except:
            continue
    return True


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-03-29"
    backtest_race_day(date)
