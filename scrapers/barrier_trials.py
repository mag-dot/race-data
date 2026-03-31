#!/usr/bin/env python3
"""
HKJC Barrier Trial Results Scraper

Scrapes barrier trial results from racing.hkjc.com.
Key data: steward comments, sectional times, running positions, pass/fail results.

Usage:
    python3 scrapers/barrier_trials.py --date 30/03/2026
    python3 scrapers/barrier_trials.py --recent 20
    python3 scrapers/barrier_trials.py --all
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

BASE_URL = "https://racing.hkjc.com/racing/information/english/Horse/Btresult.aspx"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "barrier-trials"


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def save_json(data, filepath: Path):
    ensure_dir(filepath.parent)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    size = os.path.getsize(filepath)
    print(f"  ✓ Saved {filepath} ({size:,} bytes)")


def save_markdown(content: str, filepath: Path):
    ensure_dir(filepath.parent)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ Saved {filepath}")


def date_to_slug(date_str: str) -> str:
    """Convert DD/MM/YYYY to YYYY-MM-DD."""
    parts = date_str.split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return date_str


def get_available_dates(page) -> list:
    """Get all available trial dates from the dropdown."""
    return page.evaluate('''() => {
        const sel = document.querySelector('#selectId');
        return sel ? Array.from(sel.options).map(o => o.value) : [];
    }''')


def select_date(page, date_str: str):
    """Navigate to a specific trial date.
    
    HKJC uses: /en-us/local/information/btresult?Date=YYYY/MM/DD
    Date input format: DD/MM/YYYY → needs converting to YYYY/MM/DD
    """
    parts = date_str.split("/")
    if len(parts) == 3:
        url_date = f"{parts[2]}/{parts[1]}/{parts[0]}"
    else:
        url_date = date_str
    
    url = f"https://racing.hkjc.com/en-us/local/information/btresult?Date={url_date}"
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(3)
    
    # Wait for data tables to render
    try:
        page.wait_for_selector("table.bigborder", timeout=10000)
    except PwTimeout:
        print(f"  ⚠ Timeout waiting for data on {date_str}")


def scrape_trial_date(page, date_str: str) -> dict:
    """Scrape all batches for a given trial date."""
    data = page.evaluate('''() => {
        const results = [];
        const tables = document.querySelectorAll('table');
        
        for (let i = 0; i < tables.length; i++) {
            const t = tables[i];
            const firstText = (t.querySelector('tr')?.innerText || '').replace(/\\u00a0/g, ' ');
            
            if (firstText.includes('Batch')) {
                const batchMatch = firstText.match(/Batch\\s+(\\d+)\\s+-\\s+(.+)/);
                const trackInfo = batchMatch ? batchMatch[2].trim() : firstText.trim();
                
                // Parse track info: "CONGHUA TURF - 800m" or "SHA TIN ALL WEATHER TRACK - 1200m"
                const venueMatch = trackInfo.match(/^(.+?)\\s*-\\s*(\\d+m?)$/);
                const venue = venueMatch ? venueMatch[1].trim() : trackInfo;
                const distance = venueMatch ? venueMatch[2].trim() : '';
                
                const batch = {
                    batchNo: batchMatch ? parseInt(batchMatch[1]) : null,
                    venue: venue,
                    distance: distance,
                    going: '',
                    time: '',
                    sectionalTimes: '',
                    horses: []
                };
                
                // Next table has going/time/sectionals
                if (i + 1 < tables.length) {
                    const metaText = tables[i+1].innerText || '';
                    const goingMatch = metaText.match(/Going:\\s*([A-Z\\s]+?)(?:\\s{2,}|\\t|$)/);
                    const timeMatch = metaText.match(/Time:\\s*([\\d.]+)/);
                    const sectMatch = metaText.match(/Sectional Time:\\s*([\\d.\\s]+)/);
                    batch.going = goingMatch ? goingMatch[1].trim() : '';
                    batch.time = timeMatch ? timeMatch[1] : '';
                    batch.sectionalTimes = sectMatch ? sectMatch[1].trim() : '';
                }
                
                // bigborder table has horses
                if (i + 2 < tables.length && tables[i+2].className.includes('bigborder')) {
                    const rows = tables[i+2].querySelectorAll('tr');
                    for (let r = 1; r < rows.length; r++) {
                        const cells = rows[r].querySelectorAll('td');
                        if (cells.length >= 10) {
                            const nameText = cells[0]?.innerText?.trim() || '';
                            // Extract horse code from name like "BUSTLING CITY (L210)"
                            const codeMatch = nameText.match(/\\(([A-Z]\\d+)\\)/);
                            const name = nameText.replace(/\\s*\\([A-Z]\\d+\\)/, '').trim();
                            
                            batch.horses.push({
                                name: name,
                                horseCode: codeMatch ? codeMatch[1] : '',
                                jockey: cells[1]?.innerText?.trim() || '',
                                trainer: cells[2]?.innerText?.trim() || '',
                                draw: cells[3]?.innerText?.trim() || '',
                                gear: cells[4]?.innerText?.trim() || '',
                                lbw: cells[5]?.innerText?.trim() || '',
                                runningPosition: cells[6]?.innerText?.trim() || '',
                                time: cells[7]?.innerText?.trim() || '',
                                result: cells[8]?.innerText?.trim() || '',
                                comment: cells[9]?.innerText?.trim() || '',
                            });
                        }
                    }
                }
                
                results.push(batch);
            }
        }
        return results;
    }''')
    
    return {
        "trialDate": date_str,
        "batches": data,
        "totalBatches": len(data),
        "totalHorses": sum(len(b["horses"]) for b in data),
    }


def trial_to_markdown(trial_data: dict) -> str:
    """Convert trial data to LLM-friendly markdown."""
    lines = [f"# Barrier Trial Results — {trial_data['trialDate']}"]
    lines.append(f"\n**{trial_data['totalBatches']} batches, {trial_data['totalHorses']} horses**\n")
    
    for batch in trial_data["batches"]:
        lines.append(f"## Batch {batch['batchNo']} — {batch['venue']} {batch['distance']}")
        lines.append(f"- **Going:** {batch['going']}")
        lines.append(f"- **Time:** {batch['time']}")
        if batch["sectionalTimes"]:
            lines.append(f"- **Sectionals:** {batch['sectionalTimes']}")
        lines.append("")
        
        lines.append("| # | Horse | Jockey | Trainer | Draw | LBW | Pos | Time | Result | Comment |")
        lines.append("|---|-------|--------|---------|------|-----|-----|------|--------|---------|")
        
        for i, h in enumerate(batch["horses"], 1):
            result_emoji = "✅" if h["result"] == "Passed" else "❌" if h["result"] == "Failed" else ""
            lines.append(
                f"| {i} | {h['name']} ({h['horseCode']}) | {h['jockey']} | {h['trainer']} "
                f"| {h['draw']} | {h['lbw']} | {h['runningPosition']} | {h['time']} "
                f"| {result_emoji} {h['result']} | {h['comment']} |"
            )
        lines.append("")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="HKJC Barrier Trial Scraper")
    parser.add_argument("--date", help="Trial date in DD/MM/YYYY format")
    parser.add_argument("--recent", type=int, default=0, help="Scrape N most recent trial dates")
    parser.add_argument("--all", action="store_true", help="Scrape all available trial dates")
    parser.add_argument("--skip-existing", action="store_true", help="Skip dates already scraped")
    args = parser.parse_args()
    
    if not args.date and not args.recent and not args.all:
        args.recent = 5  # Default: last 5 trial dates
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("🏇 HKJC Barrier Trial Scraper")
        print(f"  Navigating to {BASE_URL}...")
        page.goto(BASE_URL, wait_until="domcontentloaded")
        time.sleep(3)
        
        # Get available dates
        available_dates = get_available_dates(page)
        print(f"  Found {len(available_dates)} trial dates available")
        
        if args.date:
            dates_to_scrape = [args.date]
        elif args.all:
            dates_to_scrape = available_dates
        else:
            dates_to_scrape = available_dates[:args.recent]
        
        if args.skip_existing:
            dates_to_scrape = [
                d for d in dates_to_scrape
                if not (DATA_DIR / f"{date_to_slug(d)}.json").exists()
            ]
        
        print(f"  Scraping {len(dates_to_scrape)} trial dates...")
        
        total_horses = 0
        for i, date_str in enumerate(dates_to_scrape, 1):
            slug = date_to_slug(date_str)
            print(f"\n📋 [{i}/{len(dates_to_scrape)}] Scraping trials for {date_str}...")
            
            # Navigate directly to the date
            select_date(page, date_str)
            
            trial_data = scrape_trial_date(page, date_str)
            total_horses += trial_data["totalHorses"]
            
            if trial_data["totalBatches"] > 0:
                save_json(trial_data, DATA_DIR / f"{slug}.json")
                md = trial_to_markdown(trial_data)
                save_markdown(md, DATA_DIR / f"{slug}.md")
                print(f"  → {trial_data['totalBatches']} batches, {trial_data['totalHorses']} horses")
            else:
                print(f"  ⚠ No data found for {date_str}")
            
            time.sleep(1.5)  # polite delay
        
        browser.close()
        print(f"\n✅ Done! Scraped {len(dates_to_scrape)} trial dates, {total_horses} total horses")


if __name__ == "__main__":
    main()
