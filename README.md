# Bon Temps

**What's happening in Lafayette, LA**: live music, theater, food and drink, arts, wellness,
family, sports, civic and community events, collected automatically from venue calendars and
local listings.

- `docs/` is the website (one page, no build step). GitHub Pages serves it.
- `collector/collect.py` gathers events and writes `docs/events.json`, plus for every event:
  - a calendar file in `docs/cal/` (the "Apple Calendar" button opens these). The Home Screen app
    can't open calendar files in its own window, so there the button uses an `x-safari-https://`
    link that opens the file in real Safari (iOS 17 and newer);
  - a small page in `docs/e/` that the Share button sends, so the link preview shows the event's
    name, day, time and venue before forwarding to the event.
- `.github/workflows/collect.yml` runs the collector **every 2 hours** for free on GitHub.
- [CHANGELOG.md](CHANGELOG.md) lists what's changed, in plain English.

The page reloads its event list when you tap the refresh button, when you come back to the tab,
and every 10 minutes while it's open. Today's events stay listed until midnight; finished ones
are dimmed and marked "Ended" (or "Earlier" when the listing gives no end time).

**When you change the page itself (`docs/index.html`), bump `APP_VERSION`** near the top of its
script (for example `"2026-11-18.8"` → `"2026-11-18.9"`). Open copies of the site, including
Home Screen apps, notice the new version and reload themselves; otherwise phones can keep the old
page for a while because GitHub Pages lets browsers cache it.

## Where events come from

| Source | How it's read |
|---|---|
| Hilliard Art Museum | Data feed |
| Acadiana Center for the Arts | Event data built into the page, plus every event page it lists |
| Moncus Park | Event data built into the page |
| Lafayette Travel (the tourism bureau's full calendar: festivals, parades, concerts) | Each event page listed in its sitemap. Only new or changed pages are opened (2 seconds apart, as the site asks); pages already read are remembered in `collector/lafayette-travel-cache.json`. Weekly regulars are skipped, and when a venue or organizer lists the same event, theirs wins (Lafayette Travel had Noël Acadien a week off) |
| Heymann Performing Arts Center | The venue's own calendar service |
| Downtown Lafayette | Each event page (venue, price and the full write-up) |
| Blue Moon Saloon | The venue's public "Subscribe" calendar feed |
| Cité des Arts, Wonderland Performing Arts | Each event page on their sites |
| Hideaway on Lee | The venue's own event page |
| Happening Next (music, theater, comedy, art, food, wellness, festivals…) | Event data built into the page, plus each event's page for the full write-up (third-party listings site) |
| Lafayette city government | The public events calendar; routine business (regular council and commission meetings, office closures, classes) is skipped |
| Ragin' Cajuns home games | UL Athletics' public calendar feed; only home games are kept |
| Cajundome | Each event page linked from its events page (games come from UL's feed instead; graduations and box-office notices are skipped) |
| Rock'n'Bowl de Lafayette | Event pages from its sitemap; New Orleans shows are left out |
| Lafayette Public Library | Its public LibCal calendar feed, special events only (concerts, films, author visits, festivals); storytimes, clubs, classes and anything repeating are skipped |
| Ticketmaster (Cajundome & touring shows) | Free API key (optional, see below) |
| Your own additions | `collector/manual.json` |
| Submitted by the public | "Submit an event" form, after you approve it |

**Off for now:** Parish Brewing (its events load from another service with no public feed), UL's own
arts calendar, Artmosphere and Instagram/Facebook can't be read. UL performances still come in through
Happening Next; cover the rest with `manual.json` and submissions.

**If a source breaks** (errors, or finds nothing when it usually has events) for more than a day,
the collector opens a GitHub issue called "Event sources not working" (GitHub emails you); it closes
by itself once the source works again. Its memory of each source is `collector/health.json`.

To add or turn off a source, edit `collector/sources.json`. Useful settings there:

- `hide.patterns`: titles that are always left out (clutter like "free parking").
- `skip` on a source: titles to leave out from that source only.
- `yield: true` on a listing site: its copy gives way when anyone else lists the same event.
- `venueNames`: street address → venue name, for listings that only give an address.
- `quality`: listings found only on listing sites (Happening Next, Lafayette Travel) are kept
  when they're music, theater, food, arts, civic or sports, or at a known venue.
- `bigEvents`: see below.

## Big yearly events

`bigEvents` in `collector/sources.json` lists the events Bon Temps should never miss (Festival
International, Festivals Acadiens et Créoles, Mardi Gras parades, Noël Acadien, Downtown Alive,
Bach Lunch). About six weeks before each one usually starts, the collector checks the site has it.
If not, it opens a GitHub issue called **"Big events missing dates"** (GitHub emails you) saying
what's missing. Add the dates to `manual.json`, and the issue closes itself once everything is
listed.

## Where it lives

- Website: https://brody-robicheaux.github.io/bon-temps/
- Project: https://github.com/brody-robicheaux/bon-temps (GitHub Pages serves the `docs/` folder of `main`)
- The *Collect events* workflow on the *Actions* tab runs every 2 hours. *Run workflow* runs it now.

### Optional: Ticketmaster (adds Cajundome concerts and touring shows)

Get a free key at https://developer.ticketmaster.com (sign up → *My Apps* → copy the
*Consumer Key*). In the repository: *Settings → Secrets and variables → Actions → New repository
secret*, name it `TICKETMASTER_API_KEY`, paste the key, save.

### Optional: visitor counts (private)

1. Sign up at https://www.goatcounter.com (free) and pick a code, for example `bontemps`.
2. Put the code in `GOATCOUNTER` near the top of the script in `docs/index.html`, and bump `APP_VERSION`.
3. In GoatCounter's settings, leave **"Allow public viewing"** off so only you can see the numbers.

It counts visits without cookies or personal data, and doesn't count the site while it runs on your computer.

## Approving submitted events

The "Submit an event" button opens a form right on the site, with no account needed. Link
straight to it with `https://brody-robicheaux.github.io/bon-temps/#submit` (handy for sending to
venues and bands).

Behind the scenes the form sends answers to a Google Form, and every submission lands as a row
in its Google Sheet (*Bon Temps event submissions*).

- **To publish an event,** type `yes` in its **Approved** column. It appears on the site within
  about 15 minutes: the *Approved submissions* workflow re-reads only the sheet every 10 minutes.
  You can fix a submission's Category (or anything else) in the sheet before or after approving.
- **Category:** the form starts on "Not sure", which arrives blank, and Bon Temps picks categories
  from the event's name and description.
- **Photo or Flyer** (optional): a direct image link is used as is; for a page link (a website,
  an Eventbrite event) the page's preview picture is used. Instagram links don't give a picture.
- **To take one down,** delete the `yes`.
- **Get an email for each new one:** in the Google Form, *Responses → ⋮ → Get email notifications
  for new responses*.
- **Corrections:** "Something wrong with this listing?" on each event sends a row with Category
  **Correction** and the event's name, date and link. Fix the listing (or tell the venue), but
  don't approve these rows: they're notes, not events.
- Don't rename, reorder or delete the form's questions or the sheet's columns: the site and the
  collector rely on that order. The question IDs are in `SUBMIT_FIELDS` in `docs/index.html`,
  and the sheet's published CSV link is the `Submitted` entry in `collector/sources.json`.
- The published sheet is readable by anyone with its link, so don't add columns for
  submitters' private details.

## Adding events by hand

Edit `collector/manual.json` (you can do it right on GitHub with the pencil icon):

```json
{
  "events": [
    { "title": "Zydeco Breakfast", "weekly": "Sat", "time": "08:00", "endTime": "11:00",
      "venue": "Buck & Johnny's", "cats": ["music", "food"] },
    { "title": "Holiday Market", "start": "2026-12-12T10:00", "end": "2026-12-12T16:00",
      "venue": "Moncus Park", "price": "Free", "url": "https://www.instagram.com/...",
      "cats": ["food", "community"] },
    { "title": "Noël Acadien au Village", "from": "2026-12-01", "to": "2026-12-23",
      "time": "17:30", "endTime": "21:00", "venue": "LARC's Acadian Village" }
  ]
}
```

The first two entries are only examples. `from`/`to` lists the event every night of a run
(add `"days": ["Fri", "Sat"]` for only those nights). Noël Acadien is in the real file: update
its dates each fall.

Categories: `music`, `theater`, `food`, `arts`, `wellness`, `family`, `sports`, `civic`, `community`.

## What's new

[CHANGELOG.md](CHANGELOG.md) is a plain-English list of every round of updates. Add an entry at
the top after each one.

## Photos and Top picks you set

In `collector/sources.json`:
- `photos`: pictures saved in `docs/img/` and attached to events by name (a flyer someone sent you,
  or the stadium photo every Ragin' Cajuns home game uses). Add `credit` for photos that need one.
- `topPicks`: events that always make Top picks while they're within the coming week.

Photo credits (Creative Commons, via Wikimedia Commons):
- `docs/img/cajun-field.jpg`: [Cajun Field](https://commons.wikimedia.org/wiki/File:Cajun_Field_(Lafayette,_Louisiana).jpg) by Spatms, CC BY-SA 4.0
- `docs/img/cajundome.jpg`: [Cajundome](https://commons.wikimedia.org/wiki/File:Cajundome_June_2023.jpg) by TheLionHasSeen, CC BY-SA 4.0

## Sharing

- The address bar always matches what's on screen, and **Share this list** at the bottom sends
  it: `?when=2026-11-21&cat=music` opens Saturday's music, `?when=weekend&free=1` this weekend's
  free events. `when` can be a date, `tonight`, `weekend` or `picks`.
- Texting the link shows a preview card (`docs/og-poster.png`). To redraw it, edit `tools/og.html` and run
  the command at the top of that file.

## Running it on your computer

```
python collector/collect.py              # check every source and rebuild docs/events.json
python collector/collect.py "heymann"    # test one source without changing the site
python -m http.server 8790 --directory docs
```

Then open http://localhost:8790.

## Free-tier limits

- GitHub Actions and Pages are free for public repositories.
- If a source fails, the site keeps showing that source's last good events.
- To test one source on GitHub: *Actions → Collect events → Run workflow*, and type part of its
  name in the box (for example `blue moon`). Test runs don't change the site.
