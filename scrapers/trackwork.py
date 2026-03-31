#!/usr/bin/env python3
"""
HKJC Trackwork Scraper

Scrapes trackwork data for upcoming/past race meetings from racing.hkjc.com.
Shows barrier trials, gallops, trotting, swimming, treadmill, and aqua walker per horse.

Usage:
    python3 scrapers/trackwork.py                     # upcoming meeting
    python3 scrapers/trackwork.py --race 1            # specific race
    python3 scrapers/trackwork.py --date 2026/04/01   # specific date
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

BASE_URL = "https://racing.hkjc.com/en-us/local/information/localtrackwork"
SEARCH_URL = "https://racing.hkjc.com/racing/information/english/Trackwork/TrackworkSearch.aspx"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "trackwork"


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


def parse_trackwork_entries(text: str) -> list:
    """Parse raw trackwork text into structured entries."""
    entries = []
    if not text or not text.strip():
        return entries
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        entries.append({"raw": line})
    return entries


def parse_gallop_string(raw: str) -> dict:
    """Parse a gallop string like '30/03: SHA TIN AWT 29.6 24.1 (53.7) (J Orman) Wave Garden' into structured data."""
    result = {"raw": raw}
    
    # Date
    m = re.match(r"(\d{2}/\d{2}):\s*(.+)", raw)
    if not m:
        return result
    result["date"] = m.group(1)
    rest = m.group(2)
    
    # Venue: SHA TIN or CONGHUA
    vm = re.match(r"(SHA TIN|CONGHUA)\s+(.+)", rest)
    if vm:
        result["venue"] = vm.group(1)
        rest = vm.group(2)
    
    # Surface: AWT, Turf, Stall Test Turf, SmT, TroR
    sm = re.match(r"(AWT|Turf|Stall Test Turf|Stall Test|SmT|TroR)\s*(.*)", rest)
    if sm:
        result["surface"] = sm.group(1)
        rest = sm.group(2)
    
    # Sectional times: numbers like "29.6 24.1" and total in parens "(53.7)"
    times = re.findall(r"(\d+\.\d+)", rest)
    total_m = re.search(r"\((\d+\.\d+)\)", rest)
    if total_m:
        result["totalTime"] = float(total_m.group(1))
        # Sectionals are the numbers before the total
        total_str = total_m.group(1)
        result["sectionals"] = [float(t) for t in times if t != total_str]
    elif times:
        result["sectionals"] = [float(t) for t in times]
    
    # Also capture minute:second totals like "(1.29.7)"
    min_total = re.search(r"\((\d+\.\d+\.\d+)\)", rest)
    if min_total:
        result["totalTime"] = min_total.group(1)
        # Re-extract sectionals excluding the total
        total_parts = min_total.group(1)
        result["sectionals"] = [float(t) for t in times if t not in total_parts.split(".")]
    
    # Rider in parens: (J Orman) or (R.B.) or (A.T.)
    rider_m = re.search(r"\(([A-Z][A-Za-z. ]+)\)(?:\s|$)", rest)
    if rider_m and not re.match(r"^\d", rider_m.group(1)):
        result["rider"] = rider_m.group(1).strip()
    
    # Companion: anything after the last parens group
    companion_m = re.search(r"\)\s+([A-Z][A-Za-z ]+)$", rest)
    if companion_m:
        result["companion"] = companion_m.group(1).strip()
    
    # Work type for trotting: "TroR Canter", "1 Round - Fast", "2 Round - Rev Fast"
    work_m = re.search(r"(TroR Canter|\d+ Round - (?:Rev )?Fast|Treadmill[^)]*)", rest)
    if work_m:
        result["workType"] = work_m.group(1)
    
    return result


def parse_treadmill_string(raw: str) -> dict:
    """Parse treadmill entry like '27/03: SHA TIN Treadmill - Canter With Incline'."""
    result = {"raw": raw}
    m = re.match(r"(\d{2}/\d{2}):\s*(SHA TIN|CONGHUA)\s*(.*)", raw)
    if m:
        result["date"] = m.group(1)
        result["venue"] = m.group(2)
        result["description"] = m.group(3).strip()
    return result


def scrape_race_trackwork(page, race_date: str, racecourse: str, race_no: int) -> dict:
    """Scrape trackwork for a single race."""
    url = f"{BASE_URL}?racedate={race_date}&Racecourse={racecourse}&RaceNo={race_no}"
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(3)

    data = page.evaluate(
        """() => {
        const result = {raceNo: 0, raceInfo: '', venue: '', date: '', horses: []};

        // Get race header info
        const bodyText = document.body.innerText;

        // Extract race info line like "Race 1 - SHEK KIP MEI HANDICAP"
        const raceMatch = bodyText.match(/Race (\\d+)\\s*-\\s*(.+?)\\n/);
        if (raceMatch) {
            result.raceNo = parseInt(raceMatch[1]);
            result.raceName = raceMatch[2].trim();
        }

        // Date/venue/time line
        const dateMatch = bodyText.match(/(\\w+day, \\w+ \\d+, \\d+), (\\w[\\w ]+), (\\d+:\\d+)/);
        if (dateMatch) {
            result.date = dateMatch[1];
            result.venue = dateMatch[2];
            result.time = dateMatch[3];
        }

        // Track/distance/class
        const trackMatch = bodyText.match(/(All Weather Track|Turf[^,]*), (\\d+M)/);
        if (trackMatch) {
            result.surface = trackMatch[1];
            result.distance = trackMatch[2];
        }

        const classMatch = bodyText.match(/Rating:\\s*([\\d-]+),\\s*(Class \\d+)/);
        if (classMatch) {
            result.rating = classMatch[1];
            result.class = classMatch[2];
        }

        const prizeMatch = bodyText.match(/Prize Money:\\s*\\$([\\d,]+)/);
        if (prizeMatch) result.prizeMoney = prizeMatch[1];

        // Parse horse data from the table structure
        // The page uses a complex nested table layout
        // Each horse block starts with a number and name
        const allText = document.body.innerText;
        const lines = allText.split('\\n').map(l => l.trim()).filter(l => l);

        // Find horse entries — they start with a number followed by horse name
        let currentHorse = null;
        let section = '';
        const horses = [];
        let inHorseSection = false;

        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];

            // Horse header: number + TAB + name
            const horseStart = line.match(/^(\\d+)\\t([A-Z][A-Z '\\-]+)$/);
            if (horseStart) {
                if (currentHorse) horses.push(currentHorse);
                currentHorse = {
                    horseNo: horseStart[1],
                    name: horseStart[2].trim(),
                    trainer: '',
                    lastSixRuns: '',
                    barrierTrials: [],
                    gallops: [],
                    trotting: [],
                    swimming: [],
                    treadmill: [],
                    aquaWalker: [],
                    spelling: []
                };
                inHorseSection = true;
                continue;
            }

            if (!currentHorse) continue;

            // Trainer line
            if (line.match(/^[A-Z] [A-Za-z ]+$/) && !currentHorse.trainer) {
                currentHorse.trainer = line;
                continue;
            }

            // Last 6 runs
            if (line.match(/^[\\d\\/]+$/) && line.includes('/')) {
                currentHorse.lastSixRuns = line;
                continue;
            }

            // Trackwork entries: DD/MM: VENUE ...
            if (line.match(/^\\d{2}\\/\\d{2}:/)) {
                currentHorse._rawEntries = currentHorse._rawEntries || [];
                currentHorse._rawEntries.push(line);
            }

            // "Details" link marks end of horse
            if (line === 'Details') {
                continue;
            }
        }
        if (currentHorse) horses.push(currentHorse);

        result.horses = horses;
        return result;
    }"""
    )

    # Now do a more targeted extraction using the table structure
    table_data = page.evaluate(
        """() => {
        const horses = [];
        // Find the main content area
        const tables = document.querySelectorAll('table');

        for (const table of tables) {
            const rows = table.querySelectorAll('tr');
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length < 2) continue;

                // Look for horse number in first cell
                const firstText = cells[0]?.innerText?.trim() || '';
                if (/^\\d+$/.test(firstText) && parseInt(firstText) <= 14) {
                    const horseNo = firstText;

                    // Get all cell texts
                    const cellTexts = Array.from(cells).map(c => c.innerText.trim());

                    // Second cell typically has name + trainer + form
                    const nameBlock = cellTexts[1] || '';
                    const nameLines = nameBlock.split('\\n').map(l => l.trim()).filter(l => l);

                    const horse = {
                        horseNo: horseNo,
                        name: nameLines[0] || '',
                        trainer: nameLines[1] || '',
                        lastSixRuns: nameLines[2] || '',
                        barrierTrials: (cellTexts[2] || '').split('\\n').filter(l => l.trim() && l.match(/^\\d{2}\\//)),
                        gallops: (cellTexts[3] || '').split('\\n').filter(l => l.trim() && l.match(/^\\d{2}\\//)),
                        trotting: (cellTexts[4] || '').split('\\n').filter(l => l.trim() && l.match(/^\\d{2}\\//)),
                        swimming: (cellTexts[5] || '').split('\\n').filter(l => l.trim() && l.match(/^\\d{2}\\//)),
                        treadmill: (cellTexts[6] || '').split('\\n').filter(l => l.trim() && l.match(/^\\d{2}\\//)),
                        aquaWalker: (cellTexts[7] || '').split('\\n').filter(l => l.trim() && l.match(/^\\d{2}\\//)),
                    };

                    horses.push(horse);
                }
            }
        }
        return horses;
    }"""
    )

    # Merge: prefer table_data if it has good results
    if table_data and len(table_data) > 0:
        data["horses"] = table_data

    # Post-process: parse raw strings into structured data
    for horse in data.get("horses", []):
        # Parse gallop strings
        if horse.get("gallops"):
            horse["gallops"] = [parse_gallop_string(g) for g in horse["gallops"]]
        
        # Parse barrier trial strings  
        if horse.get("barrierTrials"):
            horse["barrierTrials"] = [parse_gallop_string(bt) for bt in horse["barrierTrials"]]
        
        # Parse trotting strings
        if horse.get("trotting"):
            horse["trotting"] = [parse_gallop_string(t) for t in horse["trotting"]]
        
        # Parse swimming (just date + venue)
        if horse.get("swimming"):
            horse["swimming"] = [parse_gallop_string(s) for s in horse["swimming"]]
        
        # Parse treadmill
        if horse.get("treadmill"):
            horse["treadmill"] = [parse_treadmill_string(t) for t in horse["treadmill"]]
        
        # Parse aqua walker
        if horse.get("aquaWalker"):
            horse["aquaWalker"] = [parse_gallop_string(a) for a in horse["aquaWalker"]]
        
        # Add computed fields
        if horse.get("lastSixRuns"):
            runs = [int(x) for x in horse["lastSixRuns"].split("/") if x.isdigit()]
            horse["formNumbers"] = runs
            horse["avgFinish"] = round(sum(runs) / len(runs), 1) if runs else None

    return data


def trackwork_to_markdown(all_races: list, race_date: str) -> str:
    """Convert trackwork data to LLM-friendly markdown."""
    lines = [f"# Trackwork Report — {race_date}"]
    
    for race in all_races:
        race_no = race.get("raceNo", "?")
        race_name = race.get("raceName", "")
        venue = race.get("venue", "")
        surface = race.get("surface", "")
        distance = race.get("distance", "")
        race_class = race.get("class", "")
        
        lines.append(f"\n## Race {race_no} — {race_name}")
        lines.append(f"**{venue}** | {surface} {distance} | {race_class}")
        lines.append("")
        
        for h in race.get("horses", []):
            lines.append(f"### #{h['horseNo']} {h['name']}")
            lines.append(f"- **Trainer:** {h.get('trainer', '')}")
            lines.append(f"- **Form:** {h.get('lastSixRuns', '')}")
            
            if h.get("barrierTrials"):
                lines.append("- **Barrier Trials:**")
                for bt in h["barrierTrials"]:
                    lines.append(f"  - {bt}")
            
            if h.get("gallops"):
                lines.append("- **Gallops:**")
                for g in h["gallops"][:5]:  # last 5 gallops
                    lines.append(f"  - {g}")
            
            if h.get("trotting"):
                lines.append(f"- **Trotting:** {len(h['trotting'])} sessions")
            
            if h.get("swimming"):
                lines.append(f"- **Swimming:** {len(h['swimming'])} sessions")
            
            if h.get("treadmill"):
                lines.append("- **Treadmill:**")
                for t in h["treadmill"][:3]:
                    lines.append(f"  - {t}")
            
            if h.get("aquaWalker"):
                lines.append(f"- **Aqua Walker:** {len(h['aquaWalker'])} sessions")
            
            lines.append("")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="HKJC Trackwork Scraper")
    parser.add_argument("--date", help="Race date in YYYY/MM/DD format (e.g. 2026/04/01)")
    parser.add_argument("--race", type=int, help="Specific race number to scrape")
    parser.add_argument("--venue", default="ST", help="Venue code: ST (Sha Tin) or HV (Happy Valley)")
    args = parser.parse_args()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("🏇 HKJC Trackwork Scraper")
        
        if not args.date:
            # Discover upcoming meeting date from search page
            print(f"  Discovering upcoming meeting...")
            page.goto(SEARCH_URL, wait_until="domcontentloaded")
            time.sleep(3)
            
            meeting_info = page.evaluate("""() => {
                const text = document.body.innerText;
                const match = text.match(/Upcoming Race Meeting:\\s*(\\d{2}\\/\\d{2}\\/\\d{4})/);
                if (match) {
                    const parts = match[1].split('/');
                    return {date: parts[2] + '/' + parts[1] + '/' + parts[0], display: match[1]};
                }
                return null;
            }""")
            
            if meeting_info:
                race_date = meeting_info["date"]
                print(f"  Upcoming meeting: {meeting_info['display']} → {race_date}")
            else:
                print("  ⚠ Could not find upcoming meeting date")
                browser.close()
                return
            
            # Get number of races
            race_count = page.evaluate("""() => {
                const sel = document.querySelector('#raceNo');
                return sel ? sel.options.length : 0;
            }""")
            print(f"  {race_count} races")
        else:
            race_date = args.date
            # Get race count from search page
            page.goto(SEARCH_URL, wait_until="domcontentloaded")
            time.sleep(2)
            race_count = page.evaluate("""() => {
                const sel = document.querySelector('#raceNo');
                return sel ? sel.options.length : 9;
            }""")
        
        # Determine venue from first race page
        venue = args.venue
        
        if args.race:
            races_to_scrape = [args.race]
        else:
            races_to_scrape = list(range(1, race_count + 1))
        
        print(f"  Scraping {len(races_to_scrape)} races at {venue}...")
        
        all_races = []
        for race_no in races_to_scrape:
            print(f"\n📋 Scraping Race {race_no}/{race_count}...")
            race_data = scrape_race_trackwork(page, race_date, venue, race_no)
            horse_count = len(race_data.get("horses", []))
            print(f"  → {horse_count} horses")
            all_races.append(race_data)
            time.sleep(1.5)
        
        # Save
        date_slug = race_date.replace("/", "-")
        save_json(all_races, DATA_DIR / f"{date_slug}.json")
        md = trackwork_to_markdown(all_races, race_date)
        save_markdown(md, DATA_DIR / f"{date_slug}.md")
        
        total_horses = sum(len(r.get("horses", [])) for r in all_races)
        print(f"\n✅ Done! {len(all_races)} races, {total_horses} horses")
        
        browser.close()


if __name__ == "__main__":
    main()
