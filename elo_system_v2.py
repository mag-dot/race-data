#!/usr/bin/env python3
"""
HKJC ELO Rating System v2

Improvements over v1:
  1. Confidence-based bet tiers (skip tight races)
  2. Form momentum scoring (last 3 runs weighted by recency)
  3. Rebalanced weights: 60% horse + 15% jockey + 10% trainer + 15% form
  4. Bayesian odds integration (blend ELO with market implied probability)

Usage:
  python3 elo_system_v2.py build
  python3 elo_system_v2.py predict <date> <race>
  python3 elo_system_v2.py backtest [--last N]
  python3 elo_system_v2.py compare              # Compare v1 vs v2
"""

import json
import glob
import os
import sys
import math
import re
from datetime import datetime, timedelta
from collections import defaultdict
from itertools import combinations

DATA_DIR = "data/results"
OUTPUT_DIR = "elo"
INITIAL_ELO = 1000

# --- ELO Parameters ---
K_HORSE = 32
K_JOCKEY = 20
K_TRAINER = 12
DECAY_RATE = 0.015
DECAY_START_DAYS = 35
MAX_DECAY = 100

# --- v2 Composite weights (rebalanced) ---
W_HORSE = 0.60
W_JOCKEY = 0.15    # reduced from 0.25
W_TRAINER = 0.10   # reduced from 0.15
W_FORM = 0.15      # NEW: form momentum

# --- Odds integration ---
ODDS_BLEND = 0.20  # 20% market signal, 80% model signal

# --- Confidence tiers ---
CONF_HIGH = 25     # score gap >= 25 between #1 and #2
CONF_MEDIUM = 10   # score gap 10-25
# gap < 10 = LOW confidence


def expected_score(rating_a, rating_b):
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def elo_update(rating, actual_score, expected, k):
    return rating + k * (actual_score - expected)


def placing_to_score(placing, num_runners):
    if placing is None or placing == 0:
        return 0.3
    return max(0, 1.0 - (placing - 1) / (num_runners - 1)) if num_runners > 1 else 0.5


def apply_decay(current_elo, days_inactive):
    if days_inactive <= DECAY_START_DAYS:
        return current_elo
    decay_days = days_inactive - DECAY_START_DAYS
    decay_amount = min(MAX_DECAY, DECAY_RATE * decay_days * abs(current_elo - INITIAL_ELO))
    if current_elo > INITIAL_ELO:
        return max(INITIAL_ELO, current_elo - decay_amount)
    else:
        return min(INITIAL_ELO, current_elo + decay_amount)


def parse_placing(p):
    if isinstance(p, int):
        return p
    try:
        return int(p)
    except (ValueError, TypeError):
        return None


def get_race_info_str(ri):
    """Handle raceInfo being either a string or dict with 'header' key."""
    if isinstance(ri, dict):
        return ri.get('header', '')
    return str(ri) if ri else ''


def odds_to_implied_prob(odds):
    """Convert decimal win odds to implied probability."""
    if not odds or odds <= 0:
        return None
    return 1.0 / (1.0 + odds / 10.0)  # HK odds are per $10


def load_all_results():
    files = sorted(glob.glob(f'{DATA_DIR}/*.json'))
    all_days = []
    for f in files:
        date_str = os.path.basename(f).replace('.json', '')
        with open(f) as fh:
            races = json.load(fh)
        if races:
            all_days.append({'date': date_str, 'races': races})
    return all_days


class FormTracker:
    """Tracks last N race results per horse for momentum scoring."""

    def __init__(self, window=3):
        self.window = window
        self.history = defaultdict(list)  # horse_name -> [(date, placing, num_runners)]

    def record(self, horse_name, race_date, placing, num_runners):
        self.history[horse_name].append((race_date, placing, num_runners))
        # Keep only last N+2 for safety
        if len(self.history[horse_name]) > self.window + 2:
            self.history[horse_name] = self.history[horse_name][-(self.window + 2):]

    def momentum_score(self, horse_name):
        """Calculate form momentum from last 3 runs.
        Returns a score centered on 1000 (like ELO).
        - Improving form = score > 1000
        - Declining form = score < 1000
        - No data = 1000 (neutral)

        Recency weights: most recent = 0.50, 2nd = 0.33, 3rd = 0.17
        """
        runs = self.history.get(horse_name, [])
        if not runs:
            return INITIAL_ELO  # neutral

        recent = runs[-self.window:]  # last 3
        weights = [0.50, 0.33, 0.17]

        # Pad if fewer than 3 runs
        while len(recent) < self.window:
            recent.insert(0, None)

        total_weight = 0
        weighted_score = 0

        for i, run in enumerate(reversed(recent)):  # most recent first
            if run is None:
                continue
            _, placing, num_runners = run
            if placing is None:
                continue
            # Convert placing to 0-1 score
            run_score = placing_to_score(placing, num_runners)
            w = weights[i] if i < len(weights) else 0.1
            weighted_score += run_score * w
            total_weight += w

        if total_weight == 0:
            return INITIAL_ELO

        # Normalize to 0-1, then map to ELO-like scale
        # 0.0 = worst form (900), 0.5 = average (1000), 1.0 = best form (1100)
        norm_score = weighted_score / total_weight
        return 900 + norm_score * 200

    def trend(self, horse_name):
        """Return trend direction: 'up', 'down', 'stable', or 'unknown'."""
        runs = self.history.get(horse_name, [])
        if len(runs) < 2:
            return 'unknown'
        recent = runs[-3:]
        scores = []
        for _, placing, nr in recent:
            if placing is not None:
                scores.append(placing_to_score(placing, nr))
        if len(scores) < 2:
            return 'unknown'
        if scores[-1] > scores[-2] + 0.1:
            return 'up'
        elif scores[-1] < scores[-2] - 0.1:
            return 'down'
        return 'stable'


class ELOSystemV2:
    def __init__(self):
        self.horse_elo = defaultdict(lambda: INITIAL_ELO)
        self.jockey_elo = defaultdict(lambda: INITIAL_ELO)
        self.trainer_elo = defaultdict(lambda: INITIAL_ELO)
        self.horse_last_race = {}
        self.horse_race_count = defaultdict(int)
        self.jockey_race_count = defaultdict(int)
        self.trainer_race_count = defaultdict(int)
        self.form = FormTracker(window=3)

    def process_race(self, race, race_date):
        horses = race.get('horses', [])
        num_runners = len(horses)
        if num_runners < 2:
            return

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

        # Apply inactivity decay
        for e in entries:
            name = e['name']
            if name in self.horse_last_race:
                last = datetime.strptime(self.horse_last_race[name], '%Y-%m-%d')
                current = datetime.strptime(race_date, '%Y-%m-%d')
                days_gap = (current - last).days
                self.horse_elo[name] = apply_decay(self.horse_elo[name], days_gap)

        # Field averages
        avg_h = sum(self.horse_elo[e['name']] for e in entries) / num_runners
        avg_j = sum(self.jockey_elo[e['jockey']] for e in entries) / num_runners
        avg_t = sum(self.trainer_elo[e['trainer']] for e in entries) / num_runners

        # Update each runner
        for e in entries:
            name = e['name']
            jockey = e['jockey']
            trainer = e['trainer']
            placing = e['placing']
            actual = placing_to_score(placing, num_runners)

            # Horse ELO
            exp_h = expected_score(self.horse_elo[name], avg_h)
            races_run = self.horse_race_count[name]
            k_adj = K_HORSE * (1.5 if races_run < 3 else (1.2 if races_run < 6 else 1.0))
            self.horse_elo[name] = elo_update(self.horse_elo[name], actual, exp_h, k_adj)

            # Jockey ELO
            exp_j = expected_score(self.jockey_elo[jockey], avg_j)
            self.jockey_elo[jockey] = elo_update(self.jockey_elo[jockey], actual, exp_j, K_JOCKEY)

            # Trainer ELO
            exp_t = expected_score(self.trainer_elo[trainer], avg_t)
            self.trainer_elo[trainer] = elo_update(self.trainer_elo[trainer], actual, exp_t, K_TRAINER)

            # Record form history
            self.form.record(name, race_date, placing, num_runners)

            # Update metadata
            self.horse_last_race[name] = race_date
            self.horse_race_count[name] += 1
            self.jockey_race_count[jockey] += 1
            self.trainer_race_count[trainer] += 1

    def composite_score(self, horse_name, jockey, trainer):
        """v2 composite: horse + jockey + trainer + form momentum."""
        h = self.horse_elo[horse_name]
        j = self.jockey_elo[jockey]
        t = self.trainer_elo[trainer]
        f = self.form.momentum_score(horse_name)
        return h * W_HORSE + j * W_JOCKEY + t * W_TRAINER + f * W_FORM

    def predict_race(self, race_horses, with_odds=False):
        """Predict race. If with_odds=True, blend with market implied probs."""
        predictions = []
        for h in race_horses:
            model_score = self.composite_score(h['name'], h['jockey'], h['trainer'])
            form_score = self.form.momentum_score(h['name'])
            trend = self.form.trend(h['name'])

            predictions.append({
                'name': h['name'],
                'no': h.get('no', '?'),
                'jockey': h['jockey'],
                'trainer': h['trainer'],
                'horse_elo': round(self.horse_elo[h['name']], 1),
                'jockey_elo': round(self.jockey_elo[h['jockey']], 1),
                'trainer_elo': round(self.trainer_elo[h['trainer']], 1),
                'form_score': round(form_score, 1),
                'form_trend': trend,
                'model_score': round(model_score, 1),
                'races': self.horse_race_count[h['name']],
                'odds': h.get('odds', 0),
            })

        # Sort by model score first
        predictions.sort(key=lambda x: x['model_score'], reverse=True)

        # If odds available, apply Bayesian blend
        if with_odds:
            predictions = self._blend_with_odds(predictions)

        # Calculate confidence tier
        if len(predictions) >= 2:
            gap = predictions[0]['model_score'] - predictions[1]['model_score']
            if gap >= CONF_HIGH:
                conf = 'HIGH'
            elif gap >= CONF_MEDIUM:
                conf = 'MEDIUM'
            else:
                conf = 'LOW'
        else:
            conf = 'LOW'

        return predictions, conf

    def _blend_with_odds(self, predictions):
        """Bayesian blend: combine model ranking with market implied probability."""
        # Get odds-based scores
        odds_scores = []
        has_odds = False
        for p in predictions:
            odds = p.get('odds', 0)
            try:
                odds_val = float(odds) if odds else 0
            except (ValueError, TypeError):
                odds_val = 0
            if odds_val > 0:
                has_odds = True
                # Convert odds to implied probability
                impl_prob = 1.0 / (1.0 + odds_val / 10.0)
                odds_scores.append(impl_prob)
            else:
                odds_scores.append(0)

        if not has_odds:
            return predictions

        # Normalize odds scores to same scale as model scores
        max_model = max(p['model_score'] for p in predictions)
        min_model = min(p['model_score'] for p in predictions)
        model_range = max_model - min_model if max_model > min_model else 1

        max_odds = max(odds_scores) if odds_scores else 1
        min_odds = min(s for s in odds_scores if s > 0) if any(s > 0 for s in odds_scores) else 0

        for i, p in enumerate(predictions):
            if odds_scores[i] > 0:
                # Normalize odds to model scale
                odds_norm = min_model + (odds_scores[i] - min_odds) / (max_odds - min_odds + 0.001) * model_range
                # Blend: 80% model + 20% market
                p['blended_score'] = round(p['model_score'] * (1 - ODDS_BLEND) + odds_norm * ODDS_BLEND, 1)
            else:
                p['blended_score'] = p['model_score']

        # Re-sort by blended score
        predictions.sort(key=lambda x: x.get('blended_score', x['model_score']), reverse=True)
        return predictions

    def build_from_results(self, all_days, stop_before_date=None):
        for day in all_days:
            if stop_before_date and day['date'] >= stop_before_date:
                break
            for race in day['races']:
                self.process_race(race, day['date'])

    def save(self, path=None):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = path or f'{OUTPUT_DIR}/elo_ratings_v2.json'
        data = {
            'version': 2,
            'weights': {'horse': W_HORSE, 'jockey': W_JOCKEY, 'trainer': W_TRAINER, 'form': W_FORM},
            'odds_blend': ODDS_BLEND,
            'horses': dict(sorted(self.horse_elo.items(), key=lambda x: x[1], reverse=True)),
            'jockeys': dict(sorted(self.jockey_elo.items(), key=lambda x: x[1], reverse=True)),
            'trainers': dict(sorted(self.trainer_elo.items(), key=lambda x: x[1], reverse=True)),
            'horse_last_race': self.horse_last_race,
            'horse_race_count': dict(self.horse_race_count),
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"Saved v2 ELO ratings to {path}")

    def print_top(self, n=20):
        print(f"\nTop {n} Horses:")
        sorted_h = sorted(self.horse_elo.items(), key=lambda x: x[1], reverse=True)
        for i, (name, elo) in enumerate(sorted_h[:n]):
            races = self.horse_race_count[name]
            form = self.form.momentum_score(name)
            trend = self.form.trend(name)
            arrow = {'up': '↑', 'down': '↓', 'stable': '→', 'unknown': '?'}[trend]
            print(f"  {i+1:>3}. {name:<25} ELO: {elo:>7.1f} Form: {form:>6.1f}{arrow} ({races} races)")

        print(f"\nTop Jockeys:")
        sorted_j = sorted(self.jockey_elo.items(), key=lambda x: x[1], reverse=True)
        for i, (name, elo) in enumerate(sorted_j[:15]):
            rides = self.jockey_race_count[name]
            print(f"  {i+1:>3}. {name:<20} ELO: {elo:>7.1f} ({rides} rides)")

        print(f"\nTop Trainers:")
        sorted_t = sorted(self.trainer_elo.items(), key=lambda x: x[1], reverse=True)
        for i, (name, elo) in enumerate(sorted_t[:15]):
            runners = self.trainer_race_count[name]
            print(f"  {i+1:>3}. {name:<20} ELO: {elo:>7.1f} ({runners} runners)")


# ============================================================
# CLI Commands
# ============================================================

def cmd_build():
    print("Building v2 ELO ratings from full season...")
    all_days = load_all_results()
    elo = ELOSystemV2()
    elo.build_from_results(all_days)
    elo.save()
    elo.print_top()
    print(f"\nProcessed {len(all_days)} race days")


def cmd_predict(date, race_no):
    all_days = load_all_results()
    elo = ELOSystemV2()
    elo.build_from_results(all_days, stop_before_date=date)

    target_day = next((d for d in all_days if d['date'] == date), None)
    if not target_day:
        print(f"No results found for {date}")
        return

    race = target_day['races'][race_no - 1]
    horses = []
    for h in race['horses']:
        odds = 0
        try:
            odds = float(h.get('winOdds', 0) or 0)
        except (ValueError, TypeError):
            pass
        horses.append({
            'name': h['horseName'],
            'no': int(h['horseNo']),
            'jockey': h.get('jockey', 'Unknown'),
            'trainer': h.get('trainer', 'Unknown'),
            'odds': odds,
        })

    predictions, conf = elo.predict_race(horses, with_odds=True)

    print(f"\nRace {race_no} — v2 ELO Predictions (Confidence: {conf}):")
    print(f"{'Rank':>4} {'#':>3} {'Horse':<22} {'Score':>7} {'Blend':>7} {'HorseE':>7} {'Form':>6} {'Trend'} {'JockE':>6} {'TrE':>6}")
    for i, p in enumerate(predictions):
        marker = ' ***' if i == 0 else (' **' if i <= 2 else '')
        blend = p.get('blended_score', p['model_score'])
        print(f"{i+1:>4}. #{p['no']:>2} {p['name']:<22} {p['model_score']:>7.1f} {blend:>7.1f} {p['horse_elo']:>7.1f} {p['form_score']:>6.1f} {p['form_trend']:<6} {p['jockey_elo']:>6.1f} {p['trainer_elo']:>6.1f}{marker}")

    top3 = predictions[:3]
    print(f"\nConfidence: {conf}")
    if conf == 'LOW':
        print(f"  -> SKIP WIN bet or bet PLACE only")
    print(f"WIN:   #{top3[0]['no']} {top3[0]['name']}")
    print(f"PLACE: #{top3[0]['no']}, #{top3[1]['no']}, #{top3[2]['no']}")
    print(f"QIN:   {top3[0]['no']}, {top3[1]['no']}, {top3[2]['no']}")


def cmd_backtest(last_n=None):
    all_days = load_all_results()
    dates = [d['date'] for d in all_days]

    if last_n:
        test_dates = dates[-last_n:]
    else:
        test_dates = dates[10:]  # skip warmup

    print(f"Backtesting v2 model across {len(test_dates)} race days...\n")

    stats = {
        'total': 0, 'win': 0, 'place': 0, 'wt3': 0,
        'qin_straight': 0, 'qin_box': 0,
        'fav_win': 0, 'winner_ranks': [],
        # By confidence tier
        'high': {'total': 0, 'win': 0, 'place': 0, 'wt3': 0, 'qin_box': 0},
        'medium': {'total': 0, 'win': 0, 'place': 0, 'wt3': 0, 'qin_box': 0},
        'low': {'total': 0, 'win': 0, 'place': 0, 'wt3': 0, 'qin_box': 0},
    }

    for test_date in test_dates:
        elo = ELOSystemV2()
        elo.build_from_results(all_days, stop_before_date=test_date)

        day_data = next((d for d in all_days if d['date'] == test_date), None)
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
                odds = 0
                try:
                    odds = float(h.get('winOdds', 0) or 0)
                except (ValueError, TypeError):
                    pass
                horses_input.append({
                    'name': h['horseName'], 'no': hno,
                    'jockey': h.get('jockey', 'Unknown'),
                    'trainer': h.get('trainer', 'Unknown'),
                    'odds': odds,
                })
            if len(horses_input) < 2:
                continue

            predictions, conf = elo.predict_race(horses_input, with_odds=True)
            top3_nos = [p['no'] for p in predictions[:3]]

            # Actual results
            winner = None
            actual_top3 = []
            for h in race['horses']:
                p = parse_placing(h.get('placing'))
                try:
                    hno = int(h['horseNo'])
                except (ValueError, TypeError):
                    continue
                if p == 1:
                    winner = {'no': hno, 'name': h['horseName']}
                if p and p <= 3:
                    actual_top3.append(hno)

            if not winner:
                continue

            stats['total'] += 1
            day_races += 1
            tier = conf.lower()
            stats[tier]['total'] += 1

            # Favourite
            valid_odds = []
            for h in race['horses']:
                try:
                    ov = float(h.get('winOdds', 0) or 0)
                    if ov > 0:
                        valid_odds.append((int(h['horseNo']), ov))
                except:
                    pass
            if valid_odds:
                fav_no = min(valid_odds, key=lambda x: x[1])[0]
                if fav_no == winner['no']:
                    stats['fav_win'] += 1

            # WIN
            if predictions[0]['no'] == winner['no']:
                stats['win'] += 1
                stats[tier]['win'] += 1
                day_wins += 1

            # PLACE
            if predictions[0]['no'] in actual_top3:
                stats['place'] += 1
                stats[tier]['place'] += 1

            # Winner in top 3
            if winner['no'] in top3_nos:
                stats['wt3'] += 1
                stats[tier]['wt3'] += 1

            # QIN straight
            actual_qin = set(actual_top3[:2]) if len(actual_top3) >= 2 else set()
            if actual_qin and set(top3_nos[:2]) == actual_qin:
                stats['qin_straight'] += 1

            # QIN box
            if actual_qin:
                for combo in combinations(top3_nos, 2):
                    if set(combo) == actual_qin:
                        stats['qin_box'] += 1
                        stats[tier]['qin_box'] += 1
                        break

            # Winner rank
            for idx, p in enumerate(predictions):
                if p['no'] == winner['no']:
                    stats['winner_ranks'].append(idx + 1)
                    break

        print(f"  {test_date}: {day_wins}/{day_races} wins")

    # Summary
    t = stats['total']
    pct = lambda n: f"{n}/{t} ({n/t*100:.1f}%)" if t > 0 else "---"
    avg_rank = sum(stats['winner_ranks']) / len(stats['winner_ranks']) if stats['winner_ranks'] else 0

    print(f"\n{'='*80}")
    print(f"v2 BACKTEST SUMMARY ({len(test_dates)} race days, {t} races)")
    print(f"{'='*80}")
    print(f"#1 Pick Won:        {pct(stats['win'])}")
    print(f"#1 Pick Placed:     {pct(stats['place'])}")
    print(f"Winner in Top 3:    {pct(stats['wt3'])}")
    print(f"QIN Straight:       {pct(stats['qin_straight'])}")
    print(f"QIN Box (top 3):    {pct(stats['qin_box'])}")
    print(f"Avg Winner Rank:    {avg_rank:.1f}")
    print(f"Favourite Win Rate: {pct(stats['fav_win'])}")

    print(f"\n{'='*80}")
    print(f"BY CONFIDENCE TIER")
    print(f"{'='*80}")
    for tier_name in ['high', 'medium', 'low']:
        tier = stats[tier_name]
        n = tier['total']
        if n == 0:
            continue
        tp = lambda v: f"{v}/{n} ({v/n*100:.0f}%)"
        print(f"\n  {tier_name.upper()} confidence ({n} races):")
        print(f"    WIN:      {tp(tier['win'])}")
        print(f"    PLACE:    {tp(tier['place'])}")
        print(f"    WT3:      {tp(tier['wt3'])}")
        print(f"    QIN box:  {tp(tier['qin_box'])}")


def cmd_compare():
    """Compare v1 vs v2 on same test dates."""
    all_days = load_all_results()
    test_dates = ['2026-03-18', '2026-03-25', '2026-04-01', '2026-04-08', '2026-04-15', '2026-04-22']

    # Import v1 class dynamically
    from elo_system import ELOSystem as ELOSystemV1

    results = {'v1': {'win': 0, 'place': 0, 'wt3': 0, 'qin_box': 0, 'total': 0},
               'v2': {'win': 0, 'place': 0, 'wt3': 0, 'qin_box': 0, 'total': 0}}

    for td in test_dates:
        # Build v1
        v1 = ELOSystemV1()
        v1.build_from_results(all_days, stop_before_date=td)

        # Build v2
        v2 = ELOSystemV2()
        v2.build_from_results(all_days, stop_before_date=td)

        day = next((d for d in all_days if d['date'] == td), None)
        if not day:
            continue

        for race in day['races']:
            horses_input = []
            for h in race['horses']:
                try:
                    hno = int(h['horseNo'])
                except:
                    continue
                odds = 0
                try:
                    odds = float(h.get('winOdds', 0) or 0)
                except:
                    pass
                horses_input.append({
                    'name': h['horseName'], 'no': hno,
                    'jockey': h.get('jockey', 'Unknown'),
                    'trainer': h.get('trainer', 'Unknown'),
                    'odds': odds,
                })
            if len(horses_input) < 2:
                continue

            # Actual
            winner_no = None
            actual_top3 = []
            for h in race['horses']:
                p = parse_placing(h.get('placing'))
                try:
                    hno = int(h['horseNo'])
                except:
                    continue
                if p == 1:
                    winner_no = hno
                if p and p <= 3:
                    actual_top3.append(hno)
            if not winner_no:
                continue

            actual_qin = set(actual_top3[:2]) if len(actual_top3) >= 2 else set()

            for ver, model in [('v1', v1), ('v2', v2)]:
                results[ver]['total'] += 1
                if ver == 'v1':
                    preds = model.predict_race(horses_input)
                else:
                    preds, _ = model.predict_race(horses_input, with_odds=True)

                top3 = [p['no'] for p in preds[:3]]

                if preds[0]['no'] == winner_no:
                    results[ver]['win'] += 1
                if preds[0]['no'] in actual_top3:
                    results[ver]['place'] += 1
                if winner_no in top3:
                    results[ver]['wt3'] += 1
                if actual_qin:
                    for combo in combinations(top3, 2):
                        if set(combo) == actual_qin:
                            results[ver]['qin_box'] += 1
                            break

    print(f"\n{'='*80}")
    print(f"v1 vs v2 COMPARISON — {results['v1']['total']} races across 6 HV meetings")
    print(f"{'='*80}")
    print(f"{'Metric':<25} {'v1':>15} {'v2':>15} {'Delta':>10}")
    print(f"{'-'*65}")
    for metric in ['win', 'place', 'wt3', 'qin_box']:
        t = results['v1']['total']
        v1_n = results['v1'][metric]
        v2_n = results['v2'][metric]
        v1_pct = v1_n / t * 100 if t > 0 else 0
        v2_pct = v2_n / t * 100 if t > 0 else 0
        delta = v2_pct - v1_pct
        arrow = '↑' if delta > 0 else ('↓' if delta < 0 else '→')
        labels = {'win': 'WIN (#1 pick)', 'place': 'PLACE (#1 top 3)', 'wt3': 'Winner in Top 3', 'qin_box': 'QIN Box'}
        print(f"{labels[metric]:<25} {v1_n:>4} ({v1_pct:>5.1f}%) {v2_n:>4} ({v2_pct:>5.1f}%) {arrow} {delta:>+.1f}%")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'build':
        cmd_build()
    elif cmd == 'predict':
        if len(sys.argv) != 4:
            print("Usage: python3 elo_system_v2.py predict <date> <race_number>")
            sys.exit(1)
        cmd_predict(sys.argv[2], int(sys.argv[3]))
    elif cmd == 'backtest':
        last_n = None
        if '--last' in sys.argv:
            idx = sys.argv.index('--last')
            last_n = int(sys.argv[idx + 1])
        cmd_backtest(last_n)
    elif cmd == 'compare':
        cmd_compare()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
