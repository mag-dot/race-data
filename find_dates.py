#!/usr/bin/env python3
"""
Find all HKJC race dates for the 2025-26 season by probing likely days.
HK racing: typically Wed evening (Happy Valley) + Sat or Sun day (Sha Tin).
Some holidays have special meetings (Tue, Mon etc).
"""
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta
import time
import json
import sys

BASE = "https://racing.hkjc.com/en-us/local/information/localresults"

def find_race_dates():
    start = datetime(2025, 9, 1)
    end = datetime(2026, 3, 30)
    
    # Generate candidate dates: all Wed(2), Sat(5), Sun(6) + Mon(0), Tue(1), Thu(3) holidays
    candidates = []
    current = start
    while current <= end:
        dow = current.weekday()
        # Primary race days: Wed, Sat, Sun
        # Also check Mon, Tue, Thu, Fri for public holidays / special meetings
        candidates.append(current)
        current += timedelta(days=1)
    
    race_dates = []
    checked = 0
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for dt in candidates:
            date_str = dt.strftime("%Y/%m/%d")
            checked += 1
            
            try:
                page.goto(f"{BASE}?racedate={date_str}", wait_until="domcontentloaded", timeout=10000)
                time.sleep(1)
                
                race_count = page.evaluate('''
                    () => {
                        const links = document.querySelectorAll('a');
                        let max = 0;
                        links.forEach(a => {
                            const m = (a.href || '').match(/RaceNo=(\\d+)/);
                            if (m) max = Math.max(max, parseInt(m[1]));
                        });
                        return max;
                    }
                ''')
                
                if race_count > 0:
                    venue = "ST" if dt.weekday() in [5, 6] else "HV"
                    print(f"✓ {date_str} ({dt.strftime('%a')}): {race_count} races [{venue}]")
                    race_dates.append({"date": date_str, "races": race_count, "day": dt.strftime("%a")})
                    sys.stdout.flush()
                    
            except Exception as e:
                pass  # skip errors silently
            
            # Progress every 20 days
            if checked % 30 == 0:
                print(f"  ... checked {checked} days, found {len(race_dates)} race days so far")
                sys.stdout.flush()
        
        browser.close()
    
    with open("data/season-dates.json", "w") as f:
        json.dump(race_dates, f, indent=2)
    
    print(f"\n✅ Total: {len(race_dates)} race days found (checked {checked} days)")
    return race_dates

if __name__ == "__main__":
    find_race_dates()
