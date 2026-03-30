#!/usr/bin/env python3
"""
HKJC Race Data Scraper
Scrapes horse racing data from racing.hkjc.com using Playwright.

Data sources:
1. SpeedPRO Form Guide — per-race horse form with sectional times
2. Race Results — official race results with finishing positions
3. Jockey Rankings — season statistics for all jockeys
4. Trainer Rankings — season statistics for all trainers
5. Horse Profiles — individual horse career records

Usage:
    python scraper.py formguide --date 2026/04/01
    python scraper.py results --date 2026/03/29
    python scraper.py jockeys [--season Current|Previous]
    python scraper.py trainers [--season Current|Previous]
    python scraper.py horse --id HK_2024_K307
    python scraper.py all --date 2026/04/01
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

BASE_URL = "https://racing.hkjc.com"
DATA_DIR = Path(__file__).parent / "data"


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def save_json(data, filepath: Path):
    ensure_dir(filepath.parent)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved {filepath} ({len(json.dumps(data))} bytes)")


def save_markdown(content: str, filepath: Path):
    ensure_dir(filepath.parent)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ Saved {filepath}")


def wait_for_data(page, selector, timeout=15000):
    """Wait for JS-rendered content to appear."""
    try:
        page.wait_for_selector(selector, timeout=timeout)
        time.sleep(1)  # extra settle time for dynamic content
        return True
    except PwTimeout:
        print(f"  ⚠ Timeout waiting for {selector}")
        return False


# ---------------------------------------------------------------------------
# 1. FORM GUIDE (SpeedPRO)
# ---------------------------------------------------------------------------

def scrape_formguide(page, race_date: str):
    """Scrape SpeedPRO form guide for all races on a given date."""
    print(f"\n📋 Scraping Form Guide for {race_date}...")

    # Navigate to race 1 to discover how many races there are
    url = f"{BASE_URL}/en-us/local/info/speedpro/formguide?raceno=1"
    page.goto(url)
    wait_for_data(page, "table")

    # Get race count from the nav tabs
    race_tabs = page.query_selector_all("ul.race-list li, .race-tabs li, [class*='race'] li")
    # Fallback: look for the race number links in the page
    race_links = page.evaluate("""
        () => {
            const items = document.querySelectorAll('li');
            const races = [];
            items.forEach(li => {
                const text = li.textContent.trim();
                if (/^Race \\d+$/.test(text)) {
                    races.push(parseInt(text.replace('Race ', '')));
                }
            });
            return races;
        }
    """)

    num_races = max(race_links) if race_links else 9
    print(f"  Found {num_races} races")

    all_races = []
    for race_no in range(1, num_races + 1):
        print(f"  Scraping Race {race_no}/{num_races}...")
        race_data = scrape_single_formguide(page, race_no)
        if race_data:
            all_races.append(race_data)
        time.sleep(1)  # be polite

    date_slug = race_date.replace("/", "-") if race_date else datetime.utcnow().strftime("%Y-%m-%d")
    save_json(all_races, DATA_DIR / "formguide" / f"{date_slug}.json")

    # Also save LLM-friendly markdown
    md = formguide_to_markdown(all_races, race_date or date_slug)
    save_markdown(md, DATA_DIR / "formguide" / f"{date_slug}.md")

    return all_races


def scrape_single_formguide(page, race_no: int):
    """Scrape form guide data for a single race."""
    url = f"{BASE_URL}/en-us/local/info/speedpro/formguide?raceno={race_no}"
    page.goto(url)

    if not wait_for_data(page, "table"):
        return None

    # Extract race info
    race_info = page.evaluate("""
        () => {
            const info = {};
            // Race header
            const headers = document.querySelectorAll('.race-info div, .race-header div, [class*=race-detail] div');
            const allText = document.body.innerText;

            // Try to extract venue, class, distance from the header area
            const headerDivs = document.querySelectorAll('.race-info-detail div, .race-detail div');
            headerDivs.forEach(d => {
                const t = d.textContent.trim();
                if (t.includes('Class') || t.includes('1200m') || t.includes('1400m') || t.includes('1600m') || t.includes('1650m') || t.includes('2000m') || t.includes('1000m') || t.includes('1800m') || t.includes('2200m') || t.includes('2400m')) {
                    info.classDistance = t;
                }
                if (t.includes('Sha Tin') || t.includes('Happy Valley') || t.includes('ALL WEATHER')) {
                    info.venue = t;
                }
                if (t.includes('HANDICAP') || t.includes('CUP') || t.includes('PLATE') || t.includes('TROPHY')) {
                    info.raceName = t;
                }
            });

            return info;
        }
    """)

    # Extract horse entries and their form
    horses = page.evaluate("""
        () => {
            const horses = [];
            const table = document.querySelector('table');
            if (!table) return horses;

            const rows = table.querySelectorAll('tbody tr');
            let currentHorse = null;

            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length === 0) return;

                const firstCellText = cells[0]?.textContent?.trim() || '';

                // Horse header row (has horse name, number, weight etc)
                if (/^\\d+\\s+[A-Z]/.test(firstCellText)) {
                    // Save previous horse
                    if (currentHorse) horses.push(currentHorse);

                    // Parse horse header
                    const match = firstCellText.match(/^(\\d+)\\s+(.+)/);
                    currentHorse = {
                        number: match ? parseInt(match[1]) : null,
                        name: match ? match[2].trim() : firstCellText,
                        draw: cells[1]?.textContent?.trim()?.replace(/[()]/g, '') || '',
                        bodyWeight: cells[2]?.textContent?.trim() || '',
                        weight: cells[3]?.textContent?.trim() || '',
                        jockey: cells[4]?.textContent?.trim() || '',
                        trainer: cells[5]?.textContent?.trim() || '',
                        age: cells[6]?.textContent?.trim()?.replace('Age:', '') || '',
                        form: []
                    };
                }
                // Form history row (starts with a date)
                else if (/^\\d{2}\\/\\d{2}\\/\\d{4}/.test(firstCellText) && currentHorse) {
                    const formEntry = {
                        runDate: cells[0]?.textContent?.trim() || '',
                        daysSinceLast: cells[1]?.textContent?.trim() || '',
                        courseDistGoing: cells[2]?.textContent?.trim() || '',
                        draw: cells[3]?.textContent?.trim() || '',
                        bodyWeight: cells[4]?.textContent?.trim() || '',
                        weight: cells[5]?.textContent?.trim() || '',
                        jockey: cells[6]?.textContent?.trim() || '',
                        fpTs: cells[7]?.textContent?.trim() || '',
                        energy: cells[8]?.textContent?.trim() || '',
                        sectionalTimes: '',
                        comment: '',
                        odds: cells[10]?.textContent?.trim() || ''
                    };

                    // Sectional times cell has nested divs
                    const stCell = cells[9];
                    if (stCell) {
                        const timeDivs = stCell.querySelectorAll('div > div');
                        const times = [];
                        const comments = [];
                        timeDivs.forEach(d => {
                            const t = d.textContent.trim();
                            if (/^\\d+\\.\\d+/.test(t) || /^Pace/.test(t)) {
                                if (/^Pace/.test(t) || t.length > 30) {
                                    comments.push(t);
                                } else {
                                    times.push(t);
                                }
                            }
                        });

                        // Also check for comment text directly
                        const fullText = stCell.textContent.trim();
                        const paceMatch = fullText.match(/(Pace .+?)$/s);

                        formEntry.sectionalTimes = times.join(' | ');
                        formEntry.comment = comments.join(' ') || (paceMatch ? paceMatch[1] : '');
                    }

                    currentHorse.form.push(formEntry);
                }
            });

            // Don't forget last horse
            if (currentHorse) horses.push(currentHorse);

            return horses;
        }
    """)

    # Also grab race header text
    header_text = page.evaluate("""
        () => {
            const els = document.querySelectorAll('.race-info-detail, [class*="race-detail"]');
            let text = '';
            els.forEach(el => text += el.textContent.trim() + ' | ');
            return text;
        }
    """)

    return {
        "raceNo": race_no,
        "headerText": header_text.strip(' |'),
        "info": race_info,
        "horses": horses,
        "scrapedAt": datetime.utcnow().isoformat() + "Z"
    }


# ---------------------------------------------------------------------------
# 2. RACE RESULTS
# ---------------------------------------------------------------------------

def scrape_results(page, race_date: str):
    """Scrape official race results for a given date."""
    print(f"\n🏆 Scraping Results for {race_date}...")

    # First load all-results page to see how many races
    url = f"{BASE_URL}/en-us/local/information/localresults?racedate={race_date}"
    page.goto(url, wait_until="domcontentloaded")
    wait_for_data(page, ".table_bd")

    # Count race tabs
    race_count = page.evaluate("""
        () => {
            const links = document.querySelectorAll('a');
            let max = 0;
            links.forEach(a => {
                const href = a.href || '';
                const m = href.match(/RaceNo=(\\d+)/);
                if (m) max = Math.max(max, parseInt(m[1]));
            });
            return max || 11;
        }
    """)

    all_results = []
    for race_no in range(1, race_count + 1):
        print(f"  Scraping Race {race_no}/{race_count} results...")
        result = scrape_single_result(page, race_date, race_no)
        if result:
            all_results.append(result)
        time.sleep(1)

    date_slug = race_date.replace("/", "-")
    save_json(all_results, DATA_DIR / "results" / f"{date_slug}.json")

    md = results_to_markdown(all_results, race_date)
    save_markdown(md, DATA_DIR / "results" / f"{date_slug}.md")

    return all_results


def scrape_single_result(page, race_date: str, race_no: int):
    """Scrape results for a single race."""
    url = f"{BASE_URL}/en-us/local/information/localresults?racedate={race_date}&RaceNo={race_no}"
    page.goto(url, wait_until="domcontentloaded")

    if not wait_for_data(page, ".table_bd"):
        return None

    data = page.evaluate("""
        () => {
            const result = { raceInfo: {}, horses: [] };

            // Race header info - grab from the section above the table
            const headerEls = document.querySelectorAll('.race_head_info, .f_fs13, .race-info');
            let headerText = '';
            headerEls.forEach(el => headerText += el.textContent.trim() + ' ');
            result.raceInfo.header = headerText.trim().substring(0, 500);

            // Parse the results table - use .table_bd specifically
            const table = document.querySelector('.table_bd');
            if (!table) return result;

            const rows = table.querySelectorAll('tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 10) return;

                // First cell should be a placing number (1, 2, 3...) or DH/DNF/WV/UR/PU
                const placing = cells[0]?.textContent?.trim();
                if (!placing) return;
                if (!/^\\d+$/.test(placing) && !/^(DH|DNF|WV|UR|PU|WX|DISQ)$/.test(placing)) return;

                const horseCell = cells[2]?.textContent?.trim() || '';
                const horseLink = cells[2]?.querySelector('a');
                const horseId = horseLink?.href?.match(/horseid=([^&]+)/)?.[1] || '';

                result.horses.push({
                    placing: placing,
                    horseNo: cells[1]?.textContent?.trim() || '',
                    horseName: horseCell.split('(')[0]?.trim() || horseCell,
                    horseCode: horseCell.match(/\\(([^)]+)\\)/)?.[1] || '',
                    horseId: horseId,
                    jockey: cells[3]?.textContent?.trim() || '',
                    trainer: cells[4]?.textContent?.trim() || '',
                    actualWeight: cells[5]?.textContent?.trim() || '',
                    declaredWeight: cells[6]?.textContent?.trim() || '',
                    draw: cells[7]?.textContent?.trim() || '',
                    lbw: cells[8]?.textContent?.trim() || '',
                    runningPosition: cells[9]?.textContent?.trim() || '',
                    finishTime: cells[10]?.textContent?.trim() || '',
                    winOdds: cells[11]?.textContent?.trim() || ''
                });
            });

            return result;
        }
    """)

    if data:
        data["raceNo"] = race_no
        data["raceDate"] = race_date
        data["scrapedAt"] = datetime.utcnow().isoformat() + "Z"

    return data


# ---------------------------------------------------------------------------
# 3. JOCKEY RANKINGS
# ---------------------------------------------------------------------------

def scrape_jockeys(page, season="Current"):
    """Scrape jockey season statistics."""
    print(f"\n🏇 Scraping Jockey Rankings ({season})...")

    url = f"{BASE_URL}/en-us/local/info/jockey-ranking?season={season}&view=Numbers&racecourse=ALL"
    page.goto(url)
    wait_for_data(page, ".table_bd")

    jockeys = page.evaluate("""
        () => {
            const jockeys = [];
            const table = document.querySelector('.table_bd');
            if (!table) return jockeys;
            const rows = table.querySelectorAll('tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 7) return;

                const nameCell = cells[0];
                const nameLink = nameCell?.querySelector('a');
                const name = nameCell?.textContent?.trim();
                // Skip headers, section labels, and the ranking title row
                if (!name || !nameLink || /Jockey|Ranking|in Service|Others/.test(name)) return;

                const jockeyId = nameLink?.href?.match(/jockeyid=([^&]+)/)?.[1] || '';

                jockeys.push({
                    name: name,
                    jockeyId: jockeyId,
                    wins: parseInt(cells[1]?.textContent?.trim()) || 0,
                    seconds: parseInt(cells[2]?.textContent?.trim()) || 0,
                    thirds: parseInt(cells[3]?.textContent?.trim()) || 0,
                    fourths: parseInt(cells[4]?.textContent?.trim()) || 0,
                    fifths: parseInt(cells[5]?.textContent?.trim()) || 0,
                    totalRides: parseInt(cells[6]?.textContent?.trim()) || 0,
                    stakesWon: cells[7]?.textContent?.trim() || '$0'
                });
            });
            return jockeys;
        }
    """)

    season_slug = season.lower()
    save_json(jockeys, DATA_DIR / "jockeys" / f"rankings-{season_slug}.json")

    md = jockeys_to_markdown(jockeys, season)
    save_markdown(md, DATA_DIR / "jockeys" / f"rankings-{season_slug}.md")

    return jockeys


# ---------------------------------------------------------------------------
# 4. TRAINER RANKINGS
# ---------------------------------------------------------------------------

def scrape_trainers(page, season="Current"):
    """Scrape trainer season statistics."""
    print(f"\n👔 Scraping Trainer Rankings ({season})...")

    url = f"{BASE_URL}/en-us/local/info/trainer-ranking?season={season}&view=Numbers&racecourse=ALL"
    page.goto(url, wait_until="domcontentloaded")
    wait_for_data(page, ".table_bd td a", timeout=20000)

    trainers = page.evaluate("""
        () => {
            const trainers = [];
            const table = document.querySelector('.table_bd');
            if (!table) return trainers;
            const rows = table.querySelectorAll('tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                if (cells.length < 7) return;

                const nameCell = cells[0];
                const nameLink = nameCell?.querySelector('a');
                const name = nameCell?.textContent?.trim();
                // Skip headers, section labels, and the ranking title row
                if (!name || !nameLink || /Trainer|Ranking|in Service|Others/.test(name)) return;

                const href = nameLink?.href || '';
                const trainerIdMatch = href.match(/trainerid=([^&]+)/);
                const trainerId = trainerIdMatch ? trainerIdMatch[1] : '';

                trainers.push({
                    name: name,
                    trainerId: trainerId,
                    wins: parseInt(cells[1]?.textContent?.trim()) || 0,
                    seconds: parseInt(cells[2]?.textContent?.trim()) || 0,
                    thirds: parseInt(cells[3]?.textContent?.trim()) || 0,
                    fourths: parseInt(cells[4]?.textContent?.trim()) || 0,
                    fifths: parseInt(cells[5]?.textContent?.trim()) || 0,
                    totalRunners: parseInt(cells[6]?.textContent?.trim()) || 0,
                    stakesWon: cells[7]?.textContent?.trim() || '$0'
                });
            });
            return trainers;
        }
    """)

    season_slug = season.lower()
    save_json(trainers, DATA_DIR / "trainers" / f"rankings-{season_slug}.json")

    md = trainers_to_markdown(trainers, season)
    save_markdown(md, DATA_DIR / "trainers" / f"rankings-{season_slug}.md")

    return trainers


# ---------------------------------------------------------------------------
# MARKDOWN FORMATTERS (LLM-friendly output)
# ---------------------------------------------------------------------------

def formguide_to_markdown(races, race_date):
    lines = [f"# HKJC Form Guide — {race_date}\n"]
    lines.append(f"Scraped at {datetime.utcnow().isoformat()}Z\n")

    for race in races:
        lines.append(f"\n## Race {race['raceNo']}\n")
        if race.get("headerText"):
            lines.append(f"**{race['headerText']}**\n")

        for horse in race.get("horses", []):
            lines.append(f"\n### #{horse['number']} {horse['name']}")
            lines.append(f"- **Draw:** {horse['draw']} | **Body Wt:** {horse['bodyWeight']}lb | **Wt:** {horse['weight']}lb")
            lines.append(f"- **Jockey:** {horse['jockey']} | **Trainer:** {horse['trainer']} | **Age:** {horse['age']}")
            lines.append("")

            if horse.get("form"):
                lines.append("| Date | Days | Course/Dist/Going | Draw | BW | Wt | Jockey | FP/TS | Energy | Odds |")
                lines.append("|------|------|-------------------|------|-----|-----|--------|-------|--------|------|")
                for f in horse["form"]:
                    lines.append(
                        f"| {f['runDate']} | {f['daysSinceLast']} | {f['courseDistGoing']} | {f['draw']} "
                        f"| {f['bodyWeight']} | {f['weight']} | {f['jockey']} | {f['fpTs']} | {f['energy']} | {f['odds']} |"
                    )
                lines.append("")

                # Comments in a separate block for LLM readability
                for f in horse["form"]:
                    if f.get("comment"):
                        lines.append(f"  - **{f['runDate']}**: {f['comment']}")

                if any(f.get("sectionalTimes") for f in horse["form"]):
                    lines.append("\n  **Sectional Times:**")
                    for f in horse["form"]:
                        if f.get("sectionalTimes"):
                            lines.append(f"  - {f['runDate']}: {f['sectionalTimes']}")

            lines.append("")

    return "\n".join(lines)


def results_to_markdown(results, race_date):
    lines = [f"# HKJC Race Results — {race_date}\n"]
    lines.append(f"Scraped at {datetime.utcnow().isoformat()}Z\n")

    for race in results:
        lines.append(f"\n## Race {race['raceNo']}\n")
        if race.get("raceInfo", {}).get("header"):
            lines.append(f"**{race['raceInfo']['header'][:200]}**\n")

        lines.append("| Pl | No | Horse | Jockey | Trainer | Wt | Draw | LBW | Time | Odds |")
        lines.append("|----|-----|-------|--------|---------|-----|------|-----|------|------|")

        for h in race.get("horses", []):
            lines.append(
                f"| {h['placing']} | {h['horseNo']} | {h['horseName']} ({h['horseCode']}) "
                f"| {h['jockey']} | {h['trainer']} | {h['actualWeight']} | {h['draw']} "
                f"| {h['lbw']} | {h['finishTime']} | {h['winOdds']} |"
            )

        lines.append("")

    return "\n".join(lines)


def jockeys_to_markdown(jockeys, season):
    lines = [f"# HKJC Jockey Rankings — {season} Season\n"]
    lines.append(f"Scraped at {datetime.utcnow().isoformat()}Z\n")
    lines.append("| Jockey | W | 2nd | 3rd | 4th | 5th | Rides | Stakes Won |")
    lines.append("|--------|---|-----|-----|-----|-----|-------|------------|")
    for j in jockeys:
        win_pct = f"{j['wins']/j['totalRides']*100:.1f}%" if j['totalRides'] > 0 else "N/A"
        lines.append(
            f"| {j['name']} | {j['wins']} | {j['seconds']} | {j['thirds']} "
            f"| {j['fourths']} | {j['fifths']} | {j['totalRides']} | {j['stakesWon']} |"
        )
    lines.append("")

    # Win rate summary for LLM
    lines.append("\n## Win Rate Summary\n")
    for j in sorted(jockeys, key=lambda x: x['wins'], reverse=True):
        if j['totalRides'] > 0:
            wr = j['wins'] / j['totalRides'] * 100
            lines.append(f"- **{j['name']}**: {wr:.1f}% win rate ({j['wins']}/{j['totalRides']})")

    return "\n".join(lines)


def trainers_to_markdown(trainers, season):
    lines = [f"# HKJC Trainer Rankings — {season} Season\n"]
    lines.append(f"Scraped at {datetime.utcnow().isoformat()}Z\n")
    lines.append("| Trainer | W | 2nd | 3rd | 4th | 5th | Runners | Stakes Won |")
    lines.append("|---------|---|-----|-----|-----|-----|---------|------------|")
    for t in trainers:
        lines.append(
            f"| {t['name']} | {t['wins']} | {t['seconds']} | {t['thirds']} "
            f"| {t['fourths']} | {t['fifths']} | {t['totalRunners']} | {t['stakesWon']} |"
        )

    lines.append("")
    lines.append("\n## Win Rate Summary\n")
    for t in sorted(trainers, key=lambda x: x['wins'], reverse=True):
        if t['totalRunners'] > 0:
            wr = t['wins'] / t['totalRunners'] * 100
            lines.append(f"- **{t['name']}**: {wr:.1f}% win rate ({t['wins']}/{t['totalRunners']})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="HKJC Race Data Scraper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Form guide
    fg = subparsers.add_parser("formguide", help="Scrape SpeedPRO form guide")
    fg.add_argument("--date", help="Race date (YYYY/MM/DD). Omit for upcoming race day.", default=None)

    # Results
    res = subparsers.add_parser("results", help="Scrape race results")
    res.add_argument("--date", required=True, help="Race date (YYYY/MM/DD)")

    # Jockeys
    jk = subparsers.add_parser("jockeys", help="Scrape jockey rankings")
    jk.add_argument("--season", default="Current", help="Current or Previous")

    # Trainers
    tr = subparsers.add_parser("trainers", help="Scrape trainer rankings")
    tr.add_argument("--season", default="Current", help="Current or Previous")

    # All
    al = subparsers.add_parser("all", help="Scrape everything for a date")
    al.add_argument("--date", required=True, help="Race date (YYYY/MM/DD)")

    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            if args.command == "formguide":
                scrape_formguide(page, args.date)
            elif args.command == "results":
                scrape_results(page, args.date)
            elif args.command == "jockeys":
                scrape_jockeys(page, args.season)
            elif args.command == "trainers":
                scrape_trainers(page, args.season)
            elif args.command == "all":
                scrape_formguide(page, args.date)
                scrape_results(page, args.date)
                scrape_jockeys(page)
                scrape_trainers(page)
        finally:
            browser.close()

    print("\n✅ Done!")


if __name__ == "__main__":
    main()
