#!/usr/bin/env python3
"""
HKJC Race Scorer — 8-factor composite model
Based on: office/strategy/horse-racing-betting/HKJC_BETTING_STRATEGY.md

Usage: python3 score_race.py <date> <race_number>
  e.g. python3 score_race.py 2026-04-01 7
"""
import json
import sys
import operator

def load_race(date, race_no):
    with open(f'analysis/{date}.json') as f:
        data = json.load(f)
    for race in data:
        if race['raceNo'] == race_no:
            return race
    raise ValueError(f"Race {race_no} not found for {date}")

def get_weights(surface, venue, distance):
    """Track-specific weight adjustments per strategy doc."""
    w = {
        'form': 0.25, 'energy': 0.15, 'jockey': 0.15, 'draw': 0.15,
        'trainer': 0.10, 'class': 0.10, 'weight': 0.05, 'specialist': 0.05
    }
    if surface == 'AWT':
        w['draw'] = 0.05
        w['energy'] = 0.20
    elif 'Happy Valley' in venue and surface == 'TURF':
        w['draw'] = 0.20
        w['form'] = 0.20
    elif surface == 'TURF' and distance >= 2000:
        w['draw'] = 0.05
        w['form'] = 0.30
    return w

def score_form(positions):
    """Recency-weighted form scoring (last 5 starts)."""
    form_pts = 0
    mults = [1.5, 1.2, 1.0, 0.8, 0.5]
    for i, pos in enumerate(positions[:5]):
        if i < len(mults):
            pts = {1: 10, 2: 7, 3: 5, 4: 3}.get(pos, 1 if pos <= 6 else 0)
            form_pts += pts * mults[i]
    return min(100, (form_pts / 75) * 100)

def score_energy(avg_energy):
    """Energy normalised to 0-100 (60-100 range)."""
    return min(100, max(0, ((avg_energy or 60) - 60) / 40 * 100))

def score_jockey(stats):
    """Jockey win rate scaled (20% = 100)."""
    win_pct = (stats or {}).get('winPct', 0) or 0
    return min(100, (win_pct / 20) * 100)

def score_draw(stats):
    """Draw win% scaled (20% = 100)."""
    win_pct = (stats or {}).get('winPct', 7) or 7
    return min(100, (win_pct / 20) * 100)

def score_trainer(stats):
    """Trainer win rate scaled (15% = 100)."""
    win_pct = (stats or {}).get('winPct', 0) or 0
    return min(100, (win_pct / 15) * 100)

def score_class(carry_weight):
    """Higher weight in class = higher rated."""
    return min(100, max(0, (carry_weight - 110) / 25 * 100))

def score_weight(carry_weight):
    """Lighter = advantage (inverted)."""
    return min(100, max(0, (135 - carry_weight) / 20 * 100))

def score_specialist(trackwork):
    """Trackwork volume as fitness/specialist proxy."""
    total = ((trackwork or {}).get('totalSessions') or 0)
    return min(100, (total / 40) * 100)

def apply_flags(h, composite):
    """Red and green flag adjustments per strategy doc."""
    flags = []
    positions = h.get('recentPositions', [])
    last_run = h.get('lastRunDaysAgo') or 0
    trials = h.get('recentTrials') or []
    escores = h.get('energyScores') or []

    # RED FLAGS
    if last_run > 60 and not trials:
        composite -= 20
        flags.append('60+d no trial')
    if len(positions) >= 3 and all(p > 6 for p in positions[:3]):
        composite -= 15
        flags.append('3+ outside top6')

    # GREEN FLAGS
    if h.get('trialSentiment') == 'positive':
        composite += 10
        flags.append('+trial')
    if len(escores) >= 3 and escores[0] > escores[1] > escores[2]:
        composite += 5
        flags.append('+energy trend')

    return composite, flags

def score_race(date, race_no):
    race = load_race(date, race_no)

    surface = race['surface']
    venue = race['venue']
    distance = race['distance']
    w = get_weights(surface, venue, distance)

    print(f"Race {race['raceNo']}: {race['raceName']}")
    print(f"{venue} | {surface} | {distance}m | Class {race['class']}")
    print(f"Weights: F={w['form']} E={w['energy']} J={w['jockey']} D={w['draw']} T={w['trainer']} C={w['class']} W={w['weight']} S={w['specialist']}")
    print(f"Runners: {len(race['horses'])}")
    print()

    results = []
    for h in race['horses']:
        positions = h.get('recentPositions', [])
        cw = h.get('carryWeight', 120)

        scores = {
            'form': score_form(positions),
            'energy': score_energy(h.get('avgEnergy')),
            'jockey': score_jockey(h.get('jockeyStats')),
            'draw': score_draw(h.get('drawStats')),
            'trainer': score_trainer(h.get('trainerStats')),
            'class': score_class(cw),
            'weight': score_weight(cw),
            'specialist': score_specialist(h.get('trackwork'))
        }

        composite = sum(scores[k] * w[k] for k in w)
        composite, flags = apply_flags(h, composite)

        form_str = '-'.join(str(p) for p in positions[:5]) if positions else 'DEBUT'
        results.append({
            'name': h['horseName'],
            'no': h['horseNo'],
            'draw': h['draw'],
            'jockey': h['jockey'],
            'weight': cw,
            'form': form_str,
            'avgEnergy': h.get('avgEnergy') or 60,
            'composite': round(composite, 1),
            'flags': flags
        })

    results.sort(key=operator.itemgetter('composite'), reverse=True)

    for i, h in enumerate(results):
        marker = ' *** WIN' if i == 0 else (' ** PLACE' if i <= 2 else '')
        flag_str = ' [' + ', '.join(h['flags']) + ']' if h['flags'] else ''
        print(f"{i+1:>2}. #{h['no']:>2} {h['name']:<25} D:{h['draw']:>2} W:{h['weight']} J:{h['jockey']:<18} Form:{h['form']:<15} AvgE:{h['avgEnergy']:>5.1f} Score:{h['composite']:>5.1f}{marker}{flag_str}")

    print()
    top3 = results[:3]
    gap = top3[0]['composite'] - top3[1]['composite']
    print(f"WIN:   #{top3[0]['no']} {top3[0]['name']} (score {top3[0]['composite']}, gap +{gap:.1f})")
    print(f"PLACE: #{top3[0]['no']} {top3[0]['name']}, #{top3[1]['no']} {top3[1]['name']}, #{top3[2]['no']} {top3[2]['name']}")
    print(f"QIN:   {top3[0]['no']}, {top3[1]['no']}, {top3[2]['no']}")

    if gap >= 15:
        print("CONVICTION: HIGH (15+ pt gap — strong win bet)")
    elif gap >= 8:
        print("CONVICTION: MEDIUM (8-14 pt gap — win + place)")
    else:
        print("CONVICTION: LOW (<8 pt gap — place/qin focus)")

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python3 score_race.py <date> <race_number>")
        print("  e.g. python3 score_race.py 2026-04-01 7")
        sys.exit(1)
    score_race(sys.argv[1], int(sys.argv[2]))
