#!/usr/bin/env python3
"""
HKJC ELO + Value Betting System

Extends elo_system.py with odds-based value detection.
Compares ELO-implied probability to market odds to find overlays.

Usage:
  python3 elo_value.py backtest               # Full season backtest with value overlay
  python3 elo_value.py backtest --last N      # Last N days
  python3 elo_value.py predict <date> <race>  # Predict with value flags
"""

import json
import glob
import os
import sys
import operator
from collections import defaultdict
from datetime import datetime

# Import from elo_system
from elo_system import (
    ELOSystem, load_all_results, parse_placing,
    INITIAL_ELO
)

# Value betting thresholds
MIN_VALUE_EDGE = 0.10  # Minimum 10% edge over market to flag as value
MIN_ODDS = 2.0         # Don't bet below these odds (too short)
MAX_ODDS = 30.0        # Don't bet above these odds (too speculative)


def elo_to_win_prob(composite, field_composites):
    """Convert composite ELO to implied win probability.
    Uses softmax-style normalisation across the field."""
    # Scale factor — higher = more decisive
    scale = 400.0
    exps = []
    for c in field_composites:
        exps.append(10 ** (c / scale))
    total = sum(exps)
    my_exp = 10 ** (composite / scale)
    return my_exp / total


def odds_to_prob(odds):
    """Convert decimal odds to implied probability (after removing takeout)."""
    if not odds or odds <= 1:
        return 0
    raw_prob = 1.0 / odds
    # HKJC takeout is ~17.5% for win. Adjust to get true implied prob.
    # We approximate by just using raw implied prob (conservative)
    return raw_prob


def run_backtest(last_n=None):
    all_days = load_all_results()
    dates = [d['date'] for d in all_days]

    if last_n:
        test_dates = dates[-last_n:]
    else:
        test_dates = dates[10:]  # skip warm-up

    print(f"ELO + VALUE BACKTEST across {len(test_dates)} race days\n")

    # Track results for different strategies
    stats = {
        'elo_top1': {'bets': 0, 'wins': 0, 'placed': 0, 'staked': 0, 'returned': 0},
        'elo_top3': {'bets': 0, 'wins': 0, 'placed': 0, 'staked': 0, 'returned': 0},
        'value_bets': {'bets': 0, 'wins': 0, 'placed': 0, 'staked': 0, 'returned': 0},
        'fav': {'bets': 0, 'wins': 0, 'placed': 0, 'staked': 0, 'returned': 0},
    }
    total_races = 0
    winner_ranks = []
    value_details = []  # track individual value bets

    for test_date in test_dates:
        elo = ELOSystem()
        elo.build_from_results(all_days, stop_before_date=test_date)

        day_data = None
        for d in all_days:
            if d['date'] == test_date:
                day_data = d
                break
        if not day_data:
            continue

        for race in day_data['races']:
            horses_input = []
            odds_map = {}
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
                try:
                    odds_map[hno] = float(h.get('winOdds', 0) or 0)
                except (ValueError, TypeError):
                    odds_map[hno] = 0

            if len(horses_input) < 2:
                continue

            predictions = elo.predict_race(horses_input)
            field_composites = [p['composite'] for p in predictions]

            # Get actual winner
            winner = None
            actual_top3 = []
            for h in race['horses']:
                p = parse_placing(h['placing'])
                try:
                    hno = int(h['horseNo'])
                except (ValueError, TypeError):
                    continue
                if p == 1:
                    try:
                        w_odds = float(h.get('winOdds', 0) or 0)
                    except (ValueError, TypeError):
                        w_odds = 0
                    winner = {'no': hno, 'name': h['horseName'], 'odds': w_odds}
                if p and p <= 3:
                    actual_top3.append(hno)

            if not winner:
                continue

            total_races += 1

            # Find winner rank
            for idx, p in enumerate(predictions):
                if p['no'] == winner['no']:
                    winner_ranks.append(idx + 1)
                    break

            # --- Strategy 1: Flat bet on ELO #1 pick ---
            top1 = predictions[0]
            top1_odds = odds_map.get(top1['no'], 0)
            stats['elo_top1']['bets'] += 1
            stats['elo_top1']['staked'] += 10
            if top1['no'] == winner['no']:
                stats['elo_top1']['wins'] += 1
                stats['elo_top1']['returned'] += 10 * top1_odds
            if top1['no'] in actual_top3:
                stats['elo_top1']['placed'] += 1

            # --- Strategy 2: ELO Top 3 place (bet on all 3 to place) ---
            for pred in predictions[:3]:
                pred_odds = odds_map.get(pred['no'], 0)
                stats['elo_top3']['bets'] += 1
                stats['elo_top3']['staked'] += 10
                if pred['no'] in actual_top3:
                    stats['elo_top3']['placed'] += 1
                    # Approximate place return: odds / 3
                    place_odds = max(1.1, pred_odds / 3.0)
                    stats['elo_top3']['returned'] += 10 * place_odds
                if pred['no'] == winner['no']:
                    stats['elo_top3']['wins'] += 1

            # --- Strategy 3: VALUE bets — ELO prob > market prob by MIN_VALUE_EDGE ---
            for pred in predictions:
                pred_no = pred['no']
                pred_odds = odds_map.get(pred_no, 0)
                if pred_odds < MIN_ODDS or pred_odds > MAX_ODDS:
                    continue

                elo_prob = elo_to_win_prob(pred['composite'], field_composites)
                market_prob = odds_to_prob(pred_odds)

                if market_prob <= 0:
                    continue

                edge = elo_prob - market_prob
                if edge >= MIN_VALUE_EDGE:
                    stats['value_bets']['bets'] += 1
                    stats['value_bets']['staked'] += 10
                    if pred_no == winner['no']:
                        stats['value_bets']['wins'] += 1
                        stats['value_bets']['returned'] += 10 * pred_odds
                        value_details.append({
                            'date': test_date, 'horse': pred['name'],
                            'odds': pred_odds, 'edge': round(edge*100, 1),
                            'result': 'WIN'
                        })
                    if pred_no in actual_top3:
                        stats['value_bets']['placed'] += 1

            # --- Strategy 4: Flat bet on favourite ---
            best_odds_horse = None
            best_odds = 999
            for h in horses_input:
                o = odds_map.get(h['no'], 999)
                if 0 < o < best_odds:
                    best_odds = o
                    best_odds_horse = h
            if best_odds_horse:
                stats['fav']['bets'] += 1
                stats['fav']['staked'] += 10
                if best_odds_horse['no'] == winner['no']:
                    stats['fav']['wins'] += 1
                    stats['fav']['returned'] += 10 * best_odds
                if best_odds_horse['no'] in actual_top3:
                    stats['fav']['placed'] += 1

    # Print results
    print(f"{'='*90}")
    print(f"RESULTS ACROSS {len(test_dates)} RACE DAYS, {total_races} RACES")
    print(f"{'='*90}\n")

    def print_strat(name, s):
        bets = s['bets']
        if bets == 0:
            print(f"  {name}: No bets")
            return
        win_rate = s['wins'] / bets * 100
        place_rate = s['placed'] / bets * 100
        roi = (s['returned'] - s['staked']) / s['staked'] * 100 if s['staked'] > 0 else 0
        profit = s['returned'] - s['staked']
        print(f"  {name}:")
        print(f"    Bets: {bets} | Wins: {s['wins']} ({win_rate:.1f}%) | Placed: {s['placed']} ({place_rate:.1f}%)")
        print(f"    Staked: ${s['staked']:,.0f} | Returned: ${s['returned']:,.0f} | Profit: ${profit:+,.0f}")
        print(f"    ROI: {roi:+.1f}%")
        print()

    print_strat("ELO #1 Pick (flat $10 win)", stats['elo_top1'])
    print_strat("ELO Top 3 (flat $10 place each)", stats['elo_top3'])
    print_strat("VALUE Bets (ELO edge >= 10%)", stats['value_bets'])
    print_strat("Favourite (flat $10 win)", stats['fav'])

    avg_rank = sum(winner_ranks) / len(winner_ranks) if winner_ranks else 0
    print(f"  Avg Winner ELO Rank: {avg_rank:.1f}")

    if value_details:
        print(f"\n  Value bet winners ({len(value_details)}):")
        for v in value_details[-20:]:
            print(f"    {v['date']} {v['horse']} @ {v['odds']} (edge {v['edge']}%)")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'backtest':
        last_n = None
        if '--last' in sys.argv:
            idx = sys.argv.index('--last')
            last_n = int(sys.argv[idx + 1])
        run_backtest(last_n)
    else:
        print(__doc__)
