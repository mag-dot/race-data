#!/usr/bin/env python3
"""
HKJC ELO Rating System v1

Calculates ELO ratings for horses, jockeys, and trainers across
the full season. Produces ratings that can be used for race-day predictions.

Key features:
  - Horse ELO: starts at 1000, updates based on finish position vs expected
  - Jockey ELO: starts at 1000, separate rating
  - Trainer ELO: starts at 1000, separate rating
  - Inactivity decay: horse ELO decays toward 1000 when idle
  - K-factor varies by number of runners and placing confidence
  - Composite race-day score = horse_elo * 0.60 + jockey_elo * 0.25 + trainer_elo * 0.15

Usage:
  python3 elo_system.py build                  # Build ratings from all results
  python3 elo_system.py predict <date> <race>   # Predict a race using current ratings
  python3 elo_system.py backtest               # Backtest across full season
  python3 elo_system.py backtest --last N      # Backtest last N race days
"""

import json
import glob
import os
import sys
import math
import operator
from datetime import datetime, timedelta
from collections import defaultdict

DATA_DIR = "data/results"
OUTPUT_DIR = "elo"
INITIAL_ELO = 1000

# --- ELO Parameters ---
K_HORSE = 32        # Base K-factor for horses
K_JOCKEY = 16       # Base K-factor for jockeys
K_TRAINER = 12      # Base K-factor for trainers
DECAY_RATE = 0.015  # Daily decay toward 1000 when inactive (1.5% per day)
DECAY_START_DAYS = 35  # Start decaying after this many days inactive
MAX_DECAY = 100     # Maximum total decay from inactivity

# Composite weights
W_HORSE = 0.60
W_JOCKEY = 0.25
W_TRAINER = 0.15

# --- Odds integration ---
# When win odds are available, we can compute implied probability
# and compare to ELO-predicted probability for value detection

def expected_score(rating_a, rating_b):
    """Expected score of A in a matchup with B."""
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))

def elo_update(rating, actual_score, expected, k):
    """Standard ELO update."""
    return rating + k * (actual_score - expected)

def placing_to_score(placing, num_runners):
    """Convert finishing position to a score between 0 and 1.
    Winner gets 1.0, last gets 0.0, linear interpolation."""
    if placing is None or placing == 0:
        return 0.3  # scratched/unknown, neutral-low
    return max(0, 1.0 - (placing - 1) / (num_runners - 1)) if num_runners > 1 else 0.5

def apply_decay(current_elo, days_inactive):
    """Decay ELO toward INITIAL_ELO when horse hasn't raced."""
    if days_inactive <= DECAY_START_DAYS:
        return current_elo
    decay_days = days_inactive - DECAY_START_DAYS
    decay_amount = min(MAX_DECAY, DECAY_RATE * decay_days * abs(current_elo - INITIAL_ELO))
    if current_elo > INITIAL_ELO:
        return max(INITIAL_ELO, current_elo - decay_amount)
    else:
        return min(INITIAL_ELO, current_elo + decay_amount)

def parse_placing(p):
    """Parse placing string to int. Handle 'WV', 'DNF', 'PU', 'UR', 'DISQ'."""
    if isinstance(p, int):
        return p
    try:
        return int(p)
    except (ValueError, TypeError):
        return None  # non-finisher

def load_all_results():
    """Load all results files in date order."""
    files = sorted(glob.glob(f'{DATA_DIR}/*.json'))
    all_days = []
    for f in files:
        date_str = os.path.basename(f).replace('.json', '')
        with open(f) as fh:
            races = json.load(fh)
        all_days.append({'date': date_str, 'races': races})
    return all_days

class ELOSystem:
    def __init__(self):
        self.horse_elo = defaultdict(lambda: INITIAL_ELO)
        self.jockey_elo = defaultdict(lambda: INITIAL_ELO)
        self.trainer_elo = defaultdict(lambda: INITIAL_ELO)
        self.horse_last_race = {}  # horse_name -> date string
        self.horse_race_count = defaultdict(int)
        self.jockey_race_count = defaultdict(int)
        self.trainer_race_count = defaultdict(int)

    def process_race(self, race, race_date):
        """Process a single race and update all ELO ratings."""
        horses = race.get('horses', [])
        num_runners = len(horses)
        if num_runners < 2:
            return

        # Parse all placings
        entries = []
        for h in horses:
            placing = parse_placing(h.get('placing'))
            entries.append({
                'name': h['horseName'],
                'jockey': h.get('jockey', 'Unknown'),
                'trainer': h.get('trainer', 'Unknown'),
                'placing': placing,
                'odds': h.get('winOdds'),
                'draw': h.get('draw'),
            })

        # Apply inactivity decay before rating update
        for e in entries:
            name = e['name']
            if name in self.horse_last_race:
                last = datetime.strptime(self.horse_last_race[name], '%Y-%m-%d')
                current = datetime.strptime(race_date, '%Y-%m-%d')
                days_gap = (current - last).days
                self.horse_elo[name] = apply_decay(self.horse_elo[name], days_gap)

        # Calculate average ELO of field for expected scores
        field_horse_elos = [self.horse_elo[e['name']] for e in entries]
        field_jockey_elos = [self.jockey_elo[e['jockey']] for e in entries]
        field_trainer_elos = [self.trainer_elo[e['trainer']] for e in entries]
        avg_horse_elo = sum(field_horse_elos) / len(field_horse_elos)
        avg_jockey_elo = sum(field_jockey_elos) / len(field_jockey_elos)
        avg_trainer_elo = sum(field_trainer_elos) / len(field_trainer_elos)

        # Update each runner
        for e in entries:
            name = e['name']
            jockey = e['jockey']
            trainer = e['trainer']
            placing = e['placing']

            actual = placing_to_score(placing, num_runners)

            # Horse ELO update
            exp_h = expected_score(self.horse_elo[name], avg_horse_elo)
            # Adjust K based on experience (newer horses update faster)
            races_run = self.horse_race_count[name]
            k_adj = K_HORSE * (1.5 if races_run < 3 else (1.2 if races_run < 6 else 1.0))
            self.horse_elo[name] = elo_update(self.horse_elo[name], actual, exp_h, k_adj)

            # Jockey ELO update
            exp_j = expected_score(self.jockey_elo[jockey], avg_jockey_elo)
            self.jockey_elo[jockey] = elo_update(self.jockey_elo[jockey], actual, exp_j, K_JOCKEY)

            # Trainer ELO update
            exp_t = expected_score(self.trainer_elo[trainer], avg_trainer_elo)
            self.trainer_elo[trainer] = elo_update(self.trainer_elo[trainer], actual, exp_t, K_TRAINER)

            # Update metadata
            self.horse_last_race[name] = race_date
            self.horse_race_count[name] += 1
            self.jockey_race_count[jockey] += 1
            self.trainer_race_count[trainer] += 1

    def composite_score(self, horse_name, jockey, trainer):
        """Calculate composite race-day ELO score."""
        h = self.horse_elo[horse_name]
        j = self.jockey_elo[jockey]
        t = self.trainer_elo[trainer]
        return h * W_HORSE + j * W_JOCKEY + t * W_TRAINER

    def predict_race(self, race_horses):
        """Given a list of {name, jockey, trainer}, return sorted predictions."""
        predictions = []
        for h in race_horses:
            score = self.composite_score(h['name'], h['jockey'], h['trainer'])
            predictions.append({
                'name': h['name'],
                'no': h.get('no', '?'),
                'jockey': h['jockey'],
                'trainer': h['trainer'],
                'horse_elo': round(self.horse_elo[h['name']], 1),
                'jockey_elo': round(self.jockey_elo[h['jockey']], 1),
                'trainer_elo': round(self.trainer_elo[h['trainer']], 1),
                'composite': round(score, 1),
            })
        predictions.sort(key=operator.itemgetter('composite'), reverse=True)
        return predictions

    def build_from_results(self, all_days, stop_before_date=None):
        """Process all race days up to (but not including) stop_before_date."""
        for day in all_days:
            if stop_before_date and day['date'] >= stop_before_date:
                break
            for race in day['races']:
                self.process_race(race, day['date'])

    def save(self, path=None):
        """Save current ELO state to JSON."""
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = path or f'{OUTPUT_DIR}/elo_ratings.json'
        data = {
            'horses': dict(sorted(self.horse_elo.items(), key=lambda x: x[1], reverse=True)),
            'jockeys': dict(sorted(self.jockey_elo.items(), key=lambda x: x[1], reverse=True)),
            'trainers': dict(sorted(self.trainer_elo.items(), key=lambda x: x[1], reverse=True)),
            'horse_last_race': self.horse_last_race,
            'horse_race_count': dict(self.horse_race_count),
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Saved ELO ratings to {path}")

    def print_top(self, n=20):
        """Print top rated horses, jockeys, trainers."""
        print(f"\nTop {n} Horses:")
        sorted_h = sorted(self.horse_elo.items(), key=lambda x: x[1], reverse=True)
        for i, (name, elo) in enumerate(sorted_h[:n]):
            races = self.horse_race_count[name]
            print(f"  {i+1:>3}. {name:<25} ELO: {elo:>7.1f} ({races} races)")

        print(f"\nTop Jockeys:")
        sorted_j = sorted(self.jockey_elo.items(), key=lambda x: x[1], reverse=True)
        for i, (name, elo) in enumerate(sorted_j[:15]):
            races = self.jockey_race_count[name]
            print(f"  {i+1:>3}. {name:<20} ELO: {elo:>7.1f} ({races} rides)")

        print(f"\nTop Trainers:")
        sorted_t = sorted(self.trainer_elo.items(), key=lambda x: x[1], reverse=True)
        for i, (name, elo) in enumerate(sorted_t[:15]):
            races = self.trainer_race_count[name]
            print(f"  {i+1:>3}. {name:<20} ELO: {elo:>7.1f} ({races} runners)")


def cmd_build():
    """Build ELO ratings from all results."""
    print("Building ELO ratings from full season...")
    all_days = load_all_results()
    elo = ELOSystem()
    elo.build_from_results(all_days)
    elo.save()
    elo.print_top()
    print(f"\nProcessed {len(all_days)} race days")


def cmd_predict(date, race_no):
    """Predict a race using current ELO ratings."""
    all_days = load_all_results()
    elo = ELOSystem()
    # Build up to but not including the target date
    elo.build_from_results(all_days, stop_before_date=date)

    # Find the target race
    target_day = None
    for day in all_days:
        if day['date'] == date:
            target_day = day
            break
    if not target_day:
        print(f"No results found for {date}")
        return

    race = target_day['races'][race_no - 1]
    horses = []
    for h in race['horses']:
        horses.append({
            'name': h['horseName'],
            'no': int(h['horseNo']),
            'jockey': h['jockey'],
            'trainer': h['trainer'],
        })

    predictions = elo.predict_race(horses)
    print(f"\nRace {race_no} — ELO Predictions:")
    print(f"{'Rank':>4} {'#':>3} {'Horse':<25} {'Composite':>9} {'HorseELO':>9} {'JockELO':>8} {'TrELO':>7}")
    for i, p in enumerate(predictions):
        marker = ' ***' if i == 0 else (' **' if i <= 2 else '')
        print(f"{i+1:>4}. #{p['no']:>2} {p['name']:<25} {p['composite']:>9.1f} {p['horse_elo']:>9.1f} {p['jockey_elo']:>8.1f} {p['trainer_elo']:>7.1f}{marker}")

    top3 = predictions[:3]
    print(f"\nWIN:   #{top3[0]['no']} {top3[0]['name']} ({top3[0]['composite']:.1f})")
    print(f"PLACE: #{top3[0]['no']}, #{top3[1]['no']}, #{top3[2]['no']}")
    print(f"QIN:   {top3[0]['no']}, {top3[1]['no']}, {top3[2]['no']}")


def cmd_backtest(last_n=None):
    """Backtest ELO predictions across the season."""
    all_days = load_all_results()
    dates = [d['date'] for d in all_days]

    if last_n:
        test_dates = dates[-last_n:]
    else:
        # Skip first 10 race days for ELO warm-up
        test_dates = dates[10:]

    print(f"Backtesting ELO model across {len(test_dates)} race days...")
    print(f"(Warm-up period: first {len(dates) - len(test_dates)} days)\n")

    total_races = 0
    win_hits = 0
    place_hits = 0  # #1 pick in top 3
    top3_winners = 0  # winner in model top 3
    qin_hits = 0
    winner_ranks = []
    value_wins = 0  # elo #1 was not market fav but won
    fav_win_count = 0

    for test_date in test_dates:
        # Build ELO up to this date
        elo = ELOSystem()
        elo.build_from_results(all_days, stop_before_date=test_date)

        # Find this day's races
        day_data = None
        for d in all_days:
            if d['date'] == test_date:
                day_data = d
                break
        if not day_data:
            continue

        day_wins = 0
        day_races = 0

        for race in day_data['races']:
            horses_input = []
            for h in race['horses']:
                try:
                    hno = int(h['horseNo'])
                except (ValueError, TypeError):
                    continue
                horses_input.append({
                    'name': h['horseName'],
                    'no': hno,
                    'jockey': h.get('jockey', 'Unknown'),
                    'trainer': h.get('trainer', 'Unknown'),
                })
            if len(horses_input) < 2:
                continue

            predictions = elo.predict_race(horses_input)
            top3_nos = [p['no'] for p in predictions[:3]]
            model_win_no = predictions[0]['no']

            # Get actual results
            winner = None
            actual_top3 = []
            for h in race['horses']:
                p = parse_placing(h['placing'])
                try:
                    hno = int(h['horseNo'])
                except (ValueError, TypeError):
                    continue
                if p == 1:
                    winner = {'no': hno, 'name': h['horseName'], 'odds': h.get('winOdds', 0)}
                if p and p <= 3:
                    actual_top3.append(hno)

            if not winner:
                continue

            total_races += 1
            day_races += 1

            # Track favourite wins
            valid_odds_horses = []
            for h in race['horses']:
                try:
                    odds_val = float(h.get('winOdds', 0) or 0)
                    if odds_val > 0:
                        valid_odds_horses.append((h, odds_val))
                except (ValueError, TypeError):
                    pass
            if valid_odds_horses:
                min_odds_horse, _ = min(valid_odds_horses, key=lambda x: x[1])
                try:
                    fav_no = int(min_odds_horse['horseNo'])
                    if fav_no == winner['no']:
                        fav_win_count += 1
                except (ValueError, TypeError):
                    pass

            # #1 pick won?
            if model_win_no == winner['no']:
                win_hits += 1
                day_wins += 1

            # #1 pick placed?
            if model_win_no in actual_top3:
                place_hits += 1

            # Winner in top 3?
            if winner['no'] in top3_nos:
                top3_winners += 1

            # QIN hit?
            if len(actual_top3) >= 2:
                if actual_top3[0] in top3_nos and actual_top3[1] in top3_nos:
                    qin_hits += 1

            # Winner rank in model
            for idx, p in enumerate(predictions):
                if p['no'] == winner['no']:
                    winner_ranks.append(idx + 1)
                    break

        # Print day summary
        print(f"  {test_date}: {day_wins}/{day_races} wins")

    # Summary
    pct = lambda n, d: f"{n/d*100:.1f}%" if d > 0 else "—"
    avg_rank = sum(winner_ranks) / len(winner_ranks) if winner_ranks else 0

    print(f"\n{'='*80}")
    print(f"ELO BACKTEST SUMMARY ({len(test_dates)} race days, {total_races} races)")
    print(f"{'='*80}")
    print(f"#1 Pick Won:        {win_hits}/{total_races} ({pct(win_hits, total_races)})")
    print(f"#1 Pick Placed:     {place_hits}/{total_races} ({pct(place_hits, total_races)})")
    print(f"Winner in Top 3:    {top3_winners}/{total_races} ({pct(top3_winners, total_races)})")
    print(f"QIN Hit:            {qin_hits}/{total_races} ({pct(qin_hits, total_races)})")
    print(f"Avg Winner Rank:    {avg_rank:.1f}")
    print(f"Favourite Win Rate: {fav_win_count}/{total_races} ({pct(fav_win_count, total_races)})")

    # ROI simulation: flat $10 win bet on #1 pick
    # (simplified — we'd need actual odds for proper calc)
    print(f"\n{'='*80}")
    print(f"COMPARISON BENCHMARKS")
    print(f"{'='*80}")
    print(f"ELO #1 Win Rate:    {pct(win_hits, total_races)}")
    print(f"Favourite Win Rate: {pct(fav_win_count, total_races)}")
    print(f"Random pick rate:   ~{100/12:.1f}% (avg 12 runners)")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'build':
        cmd_build()
    elif cmd == 'predict':
        if len(sys.argv) != 4:
            print("Usage: python3 elo_system.py predict <date> <race_number>")
            sys.exit(1)
        cmd_predict(sys.argv[2], int(sys.argv[3]))
    elif cmd == 'backtest':
        last_n = None
        if '--last' in sys.argv:
            idx = sys.argv.index('--last')
            last_n = int(sys.argv[idx + 1])
        cmd_backtest(last_n)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
