#!/usr/bin/env python3
"""
HKJC Draw Statistics Scraper

Scrapes draw statistics for upcoming or specific race meetings from racing.hkjc.com.
Data: per-draw Win%, Q%, P%, F% by track/distance combination.

Usage:
    python3 scrapers/draw_stats.py                     # upcoming meeting
    python3 scrapers/draw_stats.py --date 2026/04/01   # specific date
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

BASE_URL = "https://racing.hkjc.com/racing/information/english/Racing/Draw.aspx"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "draw-stats"


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


def scrape_draw_stats(page) -> dict:
    """Scrape draw statistics for the current meeting displayed."""
    data = page.evaluate("""() => {
        const result = {venue: '', date: '', races: []};
        
        // Get venue/date header
        const bodyText = document.body.innerText;
        const dateMatch = bodyText.match(/(\\d{2}\\/\\d{2}\\/\\d{4})/);
        const venueMatch = bodyText.match(/(Sha Tin|Happy Valley)/);
        if (dateMatch) result.date = dateMatch[1];
        if (venueMatch) result.venue = venueMatch[1];
        
        // Parse each race table
        const tables = document.querySelectorAll('table.table_bd');
        tables.forEach(table => {
            const rows = table.querySelectorAll('tr');
            if (rows.length < 3) return;
            
            // Header row has race info
            const headerText = rows[0]?.innerText?.trim() || '';
            const raceMatch = headerText.match(/Race (\\d+)\\s+(\\d+m)\\s+(.+)/);
            if (!raceMatch) return;
            
            const race = {
                raceNo: parseInt(raceMatch[1]),
                distance: raceMatch[2],
                surface: raceMatch[3].trim(),
                draws: [],
                favouriteStats: ''
            };
            
            // Data rows start from row 2 (row 1 is column headers)
            for (let r = 2; r < rows.length; r++) {
                const cells = rows[r].querySelectorAll('td');
                if (cells.length >= 10) {
                    const draw = parseInt(cells[0]?.innerText?.trim());
                    if (isNaN(draw)) continue;
                    
                    race.draws.push({
                        draw: draw,
                        runners: parseInt(cells[1]?.innerText?.trim()) || 0,
                        wins: parseInt(cells[2]?.innerText?.trim()) || 0,
                        seconds: parseInt(cells[3]?.innerText?.trim()) || 0,
                        thirds: parseInt(cells[4]?.innerText?.trim()) || 0,
                        fourths: parseInt(cells[5]?.innerText?.trim()) || 0,
                        winPct: parseInt(cells[6]?.innerText?.trim()) || 0,
                        quinellaPct: parseInt(cells[7]?.innerText?.trim()) || 0,
                        placePct: parseInt(cells[8]?.innerText?.trim()) || 0,
                        fourthPct: parseInt(cells[9]?.innerText?.trim()) || 0,
                    });
                }
            }
            
            result.races.push(race);
        });
        
        // Get favourite stats
        const favMatches = bodyText.match(/Favourites:\\s*Win (\\d+)% Placed (\\d+)% First 4 (\\d+)%/g);
        if (favMatches) {
            result.favouriteStats = favMatches.map(m => m.trim());
        }
        
        return result;
    }""")
    
    return data


def draw_to_markdown(data: dict) -> str:
    """Convert draw data to LLM-friendly markdown."""
    lines = [f"# Draw Statistics — {data['date']} ({data['venue']})"]
    lines.append(f"\n**{len(data['races'])} races**\n")
    
    for race in data["races"]:
        lines.append(f"## Race {race['raceNo']} — {race['distance']} {race['surface']}")
        lines.append("")
        lines.append("| Draw | Runners | Win | 2nd | 3rd | 4th | W% | Q% | P% | F% |")
        lines.append("|------|---------|-----|-----|-----|-----|----|----|----|----|")
        
        for d in race["draws"]:
            lines.append(
                f"| {d['draw']} | {d['runners']} | {d['wins']} | {d['seconds']} "
                f"| {d['thirds']} | {d['fourths']} | **{d['winPct']}** | {d['quinellaPct']} "
                f"| {d['placePct']} | {d['fourthPct']} |"
            )
        
        # Highlight best/worst draws
        if race["draws"]:
            best = max(race["draws"], key=lambda x: x["winPct"])
            worst = min(race["draws"], key=lambda x: x["winPct"])
            lines.append(f"\n🟢 **Best draw:** {best['draw']} (W%: {best['winPct']})")
            lines.append(f"🔴 **Worst draw:** {worst['draw']} (W%: {worst['winPct']})")
        lines.append("")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="HKJC Draw Statistics Scraper")
    parser.add_argument("--date", help="Meeting date (scraped from page if not specified)")
    args = parser.parse_args()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("🏇 HKJC Draw Statistics Scraper")
        print(f"  Navigating to {BASE_URL}...")
        page.goto(BASE_URL, wait_until="domcontentloaded")
        time.sleep(3)
        
        data = scrape_draw_stats(page)
        
        if not data["races"]:
            print("  ⚠ No draw data found")
            browser.close()
            return
        
        print(f"  Found draw stats for {data['date']} ({data['venue']}): {len(data['races'])} races")
        
        date_slug = data["date"].replace("/", "-") if data["date"] else "latest"
        # Convert DD-MM-YYYY to YYYY-MM-DD
        parts = date_slug.split("-")
        if len(parts) == 3 and len(parts[2]) == 4:
            date_slug = f"{parts[2]}-{parts[1]}-{parts[0]}"
        
        save_json(data, DATA_DIR / f"{date_slug}.json")
        md = draw_to_markdown(data)
        save_markdown(md, DATA_DIR / f"{date_slug}.md")
        
        total_draws = sum(len(r["draws"]) for r in data["races"])
        print(f"\n✅ Done! {len(data['races'])} races, {total_draws} draw entries")
        
        browser.close()


if __name__ == "__main__":
    main()
