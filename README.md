# race-data

HKJC horse racing data scraper and analysis toolkit. Scrapes form guides, race results, jockey/trainer statistics from [racing.hkjc.com](https://racing.hkjc.com).

## data sources

| Source | URL Pattern | Data |
|--------|-------------|------|
| SpeedPRO Form Guide | `/info/speedpro/formguide?raceno=N` | Per-horse form history, sectional times, energy ratings, running comments |
| Race Results | `/information/localresults?racedate=YYYY/MM/DD` | Official placings, finish times, odds, margins |
| Jockey Rankings | `/info/jockey-ranking` | Season win/place stats, stakes won |
| Trainer Rankings | `/info/trainer-ranking` | Season win/place stats, stakes won |

## setup

```bash
pip install playwright
playwright install chromium
```

## usage

```bash
# scrape form guide for upcoming race day
python scraper.py formguide --date 2026/04/01

# scrape results for a past race day
python scraper.py results --date 2026/03/29

# jockey/trainer season rankings
python scraper.py jockeys
python scraper.py trainers

# scrape everything for a date
python scraper.py all --date 2026/04/01
```

## output

data is saved in two formats:
- **JSON** — structured, machine-readable, ideal for analysis scripts
- **Markdown** — LLM-friendly, readable, good for AI-assisted analysis

```
data/
├── formguide/
│   ├── 2026-04-01.json
│   └── 2026-04-01.md
├── results/
│   ├── 2026-03-29.json
│   └── 2026-03-29.md
├── jockeys/
│   ├── rankings-current.json
│   └── rankings-current.md
└── trainers/
    ├── rankings-current.json
    └── rankings-current.md
```

## data schema

### form guide (per horse)

```json
{
  "number": 1,
  "name": "SUPERB GUY",
  "draw": "11",
  "bodyWeight": "1173",
  "weight": "134",
  "jockey": "J Orman",
  "trainer": "K W Lui",
  "age": "4",
  "form": [
    {
      "runDate": "15/03/2026",
      "daysSinceLast": "29",
      "courseDistGoing": "ST \"C+3\" 1400 GF",
      "draw": "13",
      "bodyWeight": "1186",
      "weight": "115",
      "jockey": "M Chadwick",
      "fpTs": "9 / 14",
      "energy": "83",
      "sectionalTimes": "14.16 | 21.46 | 22.86 | 23.62",
      "comment": "Pace Very fast; Soon taken back...",
      "odds": "36"
    }
  ]
}
```

### race results (per horse)

```json
{
  "placing": "1",
  "horseNo": "1",
  "horseName": "ACE",
  "horseCode": "K307",
  "jockey": "C L Chau",
  "trainer": "M Newnham",
  "actualWeight": "133",
  "draw": "5",
  "lbw": "-",
  "finishTime": "1:21.43",
  "winOdds": "3.4"
}
```

## future plans

- [ ] historical results scraper (batch past seasons)
- [ ] horse profile scraper (individual career records)
- [ ] draw statistics scraper (course/distance bias)
- [ ] jockey/trainer combo analysis
- [ ] automated pre-race analysis pipeline
- [ ] odds movement tracker
