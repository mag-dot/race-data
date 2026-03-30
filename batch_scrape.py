#!/usr/bin/env python3
"""
Batch scrape all race days from season-dates.json.
Skips dates that already have data files.
"""
import json
import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Import the scraper functions
sys.path.insert(0, os.path.dirname(__file__))
from scraper import scrape_single_result, wait_for_data, save_json, save_markdown, results_to_markdown, DATA_DIR, BASE_URL

def batch_scrape():
    dates_file = Path(__file__).parent / "data" / "season-dates.json"
    if not dates_file.exists():
        print("❌ data/season-dates.json not found. Run find_dates.py first.")
        return
    
    with open(dates_file) as f:
        race_days = json.load(f)
    
    # Filter out dates we already have
    existing = set()
    results_dir = DATA_DIR / "results"
    if results_dir.exists():
        for f in results_dir.glob("*.json"):
            existing.add(f.stem)  # e.g. "2026-03-29"
    
    to_scrape = []
    for rd in race_days:
        date_slug = rd["date"].replace("/", "-")
        if date_slug not in existing:
            to_scrape.append(rd)
    
    print(f"📊 Total race days: {len(race_days)}")
    print(f"✅ Already scraped: {len(existing)}")
    print(f"🔄 To scrape: {len(to_scrape)}")
    print()
    
    if not to_scrape:
        print("Nothing to scrape!")
        return
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = context.new_page()
        
        for i, rd in enumerate(to_scrape):
            date = rd["date"]
            num_races = rd["races"]
            date_slug = date.replace("/", "-")
            
            print(f"\n[{i+1}/{len(to_scrape)}] 🏆 Scraping {date} ({num_races} races)...")
            sys.stdout.flush()
            
            all_results = []
            for race_no in range(1, num_races + 1):
                try:
                    result = scrape_single_result(page, date, race_no)
                    if result:
                        all_results.append(result)
                except Exception as e:
                    print(f"  ⚠ Race {race_no} error: {e}")
                time.sleep(0.5)
            
            if all_results:
                save_json(all_results, DATA_DIR / "results" / f"{date_slug}.json")
                md = results_to_markdown(all_results, date)
                save_markdown(md, DATA_DIR / "results" / f"{date_slug}.md")
                
                total_horses = sum(len(r.get("horses", [])) for r in all_results)
                print(f"  ✓ {len(all_results)} races, {total_horses} horses")
            else:
                print(f"  ⚠ No data extracted")
            
            sys.stdout.flush()
            time.sleep(1)  # be polite
        
        browser.close()
    
    print(f"\n✅ Batch scrape complete! Scraped {len(to_scrape)} race days.")

if __name__ == "__main__":
    batch_scrape()
