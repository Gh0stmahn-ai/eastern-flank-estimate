# Eastern Flank Estimate

An open-source intelligence estimate of Russian hybrid and escalation risk against NATO's eastern flank, 2008 to 2028, laid out over an interactive map of Europe.

**Live site:** served by GitHub Pages from this repository.

The estimate is organised in five acts: the framework (gray zone warfare, phases of conflict, the treaties), the historical pattern (Georgia 2008, Crimea 2014, Ukraine 2022), the war in Ukraine, twelve months of incidents on NATO territory, and four scenarios to September 2028.

## What updates automatically, and what does not

**Automatic, every Monday 06:00 UTC** (`.github/workflows/weekly-update.yml`):

- Russian-controlled territory in Ukraine, measured from map geometry, with the week-on-week change
- Contested or unconfirmed-status ground
- The front line, derived from the edge of the controlled area
- The territorial source's own change log for the week, quoted verbatim with attribution
- Ukrainian General Staff loss figures, as a quarantined belligerent claim
- A plain-language summary of what changed, written to `CHANGELOG.md` and shown in the app under **What changed this week**

**Hand-curated, flagged when due for review** (`data/curated.json`): sector narratives, rate-of-advance comparison, casualty estimates, UN civilian data, the deep-strike campaign, negotiations, and the eastern flank record. These need analyst judgement, so the job never rewrites them. It marks them for review once they pass their date. After revising one, update its `as_of`.

To run a refresh immediately: **Actions → Weekly OSINT refresh → Run workflow**.

## Propaganda policy

1. **Fixed sources only.** Two named feeds. No news, social media or open-web scraping. No AI rewriting of anything.
2. **Measured, not reported.** Territorial area is computed here with an equal-area projection (Lambert azimuthal, centred on Ukraine). It does not repeat anyone's claimed figure, including the source's.
3. **Political layers stripped.** The territorial source mixes battlefield data with political statements, for example Kaliningrad labelled as "temporarily occupied East Prussia", Karelia, Petsamo, Salla, "occupied Estonian and Latvian territories" and the Kuril Islands. Only whitelisted battlefield layers inside Ukraine are used. Everything excluded is listed in each week's data.
4. **Claims quarantined.** A belligerent's figure never feeds a headline metric and is always labelled as a claim.
5. **Guardrails.** Area outside 90,000 to 160,000 km² is rejected. Weekly swings above 600 km² are flagged for review. A change contradicting the source's own change log is flagged. A failed fetch keeps last week's data and says so. Belligerent loss claims above 25,000 a week are withheld.
6. **Audit trail.** Every run is committed, so any figure traces to the exact data behind it (`data/history/`).

### Known limits

- **The territorial source is Ukrainian.** DeepStateMap.live is widely regarded as conservative about Ukrainian gains, but it is not neutral.
- **No independent geometry cross-check.** ISW's assessed control of terrain would be the ideal second source. Its licence forbids use without written consent, so it is not ingested. With ISW's permission it could be added as a cross-check.
- **Asymmetric claims.** Ukrainian General Staff figures are shown, labelled. Russian Ministry of Defence claims are not ingested: there is no machine-readable feed and ISW documents systematic inflation. The page says so rather than adding a second unverifiable number for balance.

## Sources and licensing

| Source | Used for | Tier | Terms |
|---|---|---|---|
| [DeepStateMap.live](https://deepstatemap.live/en) | Territorial geometry and weekly change log | Independent OSINT (Ukrainian) | Derived statistics and a generalised trace only; the raw API feed is not republished. Textual entries reused with attribution and link, which the [licence](https://deepstatemap.live/license-en.html) permits. Free API use is granted to volunteer and charitable activity; this non-commercial educational project should confirm with DeepState via [their request form](https://api.deepstatemap.live/request). |
| [russianwarship.rip](https://russianwarship.rip) | Mirror of Ukrainian General Staff loss figures | Belligerent claim | Public API |
| [Natural Earth](https://www.naturalearthdata.com) | Base map, Ukraine outline | Reference | Public domain. Natural Earth assigns Crimea to Russia as a cartographic convention; this project treats Crimea as occupied Ukrainian territory. |
| ISW | Not ingested | | Geodata licence requires written consent |

The hand-curated sections cite ISW, CSIS, UK Defence Intelligence and GCHQ, Mediazona and BBC Russian, UALosses, the UN Human Rights Monitoring Mission in Ukraine, the International Energy Agency, and national intelligence service reports. Full citations are in the app under **Sources**.

## Repository layout

```
index.html                         the app (self-contained apart from data/)
data/latest.json                   this week's derived data, read by the app
data/history_index.json            weekly series for the trend line
data/history/YYYY-MM-DD.json       one audit snapshot per run
data/curated.json                  review dates for hand-curated sections
data/ua_outline.json               Ukraine outline incl. Crimea (Natural Earth)
scripts/update.py                  the weekly pipeline
.github/workflows/weekly-update.yml  the schedule
CHANGELOG.md                       human-readable weekly summaries
```

## Running locally

```
pip install -r scripts/requirements.txt
python scripts/update.py
python -m http.server 8000
```

Then open `http://localhost:8000`. Opening `index.html` directly from disk will not load the weekly data, because browsers block local file fetches; the app falls back to its September 2026 baseline and says so.
