#!/opt/homebrew/bin/python3
"""
End-of-day Zoom notes scraper.

Reads today's standup note for the ### Meetings list, then uses Playwright
with Firefox to visit hub.zoom.us/notes, open each meeting note, expand the
transcript, extract the content via the bookmarklet logic, and save a
markdown file to the Obsidian week folder.

Usage:
    python3 zoom_notes_scraper.py [--date YYYY-MM-DD] [--headless]
"""

import os, re, glob, argparse, logging, sqlite3, shutil
from datetime import date, datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

VAULT          = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
LOG_FILE       = os.path.join(VAULT, "Scripts", "zoom_notes_scraper.log")
ZOOM_NOTES     = "https://hub.zoom.us/notes"
FF_COOKIES_DB  = os.path.expanduser(
    "~/Library/Application Support/Firefox/Profiles/6gqnyzas.default-nightly/cookies.sqlite"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Args ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--date",     help="Override date (YYYY-MM-DD)")
parser.add_argument("--headless", action="store_true", help="Run headless (needs prior login)")
args = parser.parse_args()

TARGET = date.fromisoformat(args.date) if args.date else date.today()


# ── Vault helpers ──────────────────────────────────────────────────────────────

def quarter(d):
    return f"Q{(d.month - 1) // 3 + 1}"

def week_file_for(d):
    _, week_num, _ = d.isocalendar()
    q = quarter(d)
    filename = f"{d.year}-W{week_num:02d}.md"
    quarter_dir = os.path.join(VAULT, "Work", str(d.year), q)
    matches = glob.glob(os.path.join(quarter_dir, "**", filename), recursive=True)
    return matches[0] if matches else None

def week_folder_for(d):
    wf = week_file_for(d)
    return os.path.dirname(wf) if wf else None

def find_day_idx(lines, d):
    month, day_num, year = d.strftime('%b'), str(d.day), str(d.year)
    for i, line in enumerate(lines):
        if '📅' not in line:
            continue
        if month in line and year in line:
            if re.search(r'(?<!\d)' + re.escape(day_num) + r'(?!\d)', line):
                return i
    return -1

def find_next_h2(lines, start):
    for i in range(start + 1, len(lines)):
        if lines[i].startswith('## '):
            return i
    return len(lines)

def get_meetings_from_standup(d):
    """Return list of meeting names from ### Meetings section in today's note."""
    wf = week_file_for(d)
    if not wf:
        log.error(f"No week file for {d}")
        return []

    with open(wf) as f:
        lines = f.read().split('\n')

    day_idx = find_day_idx(lines, d)
    if day_idx == -1:
        log.error(f"No day entry for {d}")
        return []

    day_end = find_next_h2(lines, day_idx)

    meetings_idx = next(
        (i for i in range(day_idx, day_end) if lines[i].strip() == '### Meetings'),
        -1
    )
    if meetings_idx == -1:
        log.info("No ### Meetings section found in today's note")
        return []

    meetings_end = meetings_idx + 1
    while meetings_end < day_end and not (
        lines[meetings_end].startswith('###') or lines[meetings_end].startswith('## ')
    ):
        meetings_end += 1

    names = []
    for line in lines[meetings_idx + 1: meetings_end]:
        m = re.match(r'^-\s+(.+)', line.strip())
        if m:
            names.append(m.group(1).strip())
    return names

def safe_filename(name, suffix=""):
    """Sanitise a meeting name for use as a filename (no slashes, colons, etc.)."""
    clean = re.sub(r'[\\/*?"<>|]', '', name.replace(':', '-')).strip()
    if suffix:
        return f"{clean} {suffix}.md"
    return f"{clean}.md"

def output_path(folder, name, date_str, duplicated):
    fname = safe_filename(name, date_str if duplicated else "")
    return os.path.join(folder, fname)


# ── Bookmarklet JS (modified to return content instead of downloading) ─────────

EXTRACT_JS = """
(function() {
    var t = [];
    document.querySelectorAll('span[role="img"][aria-label]').forEach(function(a) {
        var name = a.getAttribute('aria-label');
        var header = a.parentElement;
        if (!header) return;
        var turn = header.parentElement;
        if (!turn) return;
        var time = '';
        header.querySelectorAll('span').forEach(function(s) {
            if (/^\\d{2}:\\d{2}:\\d{2}$/.test(s.textContent.trim())) time = s.textContent.trim();
        });
        var msgs = [];
        Array.prototype.forEach.call(turn.children, function(c) {
            if (c === header) return;
            var x = c.textContent.trim();
            if (x) msgs.push(x);
        });
        if (!msgs.length) return;
        t.push('**' + name + '**' + (time ? ' [' + time + ']' : '') + '\\n' + msgs.join(' '));
    });
    if (!t.length) return null;
    return '# Transcript\\n\\n' + t.join('\\n\\n') + '\\n';
})()
"""


# ── Playwright helpers ─────────────────────────────────────────────────────────

def find_and_open_note(page, meeting_name):
    """
    Find a note on hub.zoom.us/notes whose title matches meeting_name and click it.
    Returns True on success.
    """
    log.info(f"Looking for note: {meeting_name}")

    # Notes are listed as clickable items; try a few selector strategies
    # Strategy 1: exact text match in a link or list item
    locators = [
        page.get_by_text(meeting_name, exact=True),
        page.get_by_text(meeting_name, exact=False),
    ]

    for loc in locators:
        try:
            first = loc.first
            if first.is_visible(timeout=2000):
                first.click()
                page.wait_for_load_state("networkidle", timeout=10000)
                return True
        except PWTimeoutError:
            pass
        except Exception as e:
            log.debug(f"Locator attempt failed: {e}")

    log.warning(f"Could not find note for: {meeting_name}")
    return False

def ensure_transcript_expanded(page):
    """
    Find the transcript section and expand it if it's collapsed.
    Returns True if transcript content is visible.
    """
    # Look for a "Transcript" heading/button that might be collapsed
    transcript_selectors = [
        'button:has-text("Transcript")',
        '[aria-label*="Transcript"]',
        'div:has-text("Transcript") > button',
        'summary:has-text("Transcript")',
        '[data-testid*="transcript"]',
    ]

    for sel in transcript_selectors:
        try:
            el = page.locator(sel).first
            if not el.is_visible(timeout=1500):
                continue

            # Check if it's a toggle that's currently collapsed
            expanded = el.get_attribute("aria-expanded")
            if expanded == "false":
                log.info("Expanding transcript section")
                el.click()
                page.wait_for_timeout(1500)
            return True
        except PWTimeoutError:
            pass
        except Exception:
            pass

    # If we can already see transcript turns (speaker spans), we're good
    turns = page.locator('span[role="img"][aria-label]')
    try:
        if turns.first.is_visible(timeout=3000):
            return True
    except PWTimeoutError:
        pass

    log.warning("Could not find or expand transcript section")
    return False

def load_zoom_cookies_from_firefox():
    """Read Zoom session cookies from Firefox Nightly's profile."""
    tmp = "/tmp/ff_cookies_scraper.sqlite"
    try:
        shutil.copy(FF_COOKIES_DB, tmp)
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "SELECT host, path, name, value, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE host LIKE '%zoom%'"
        ).fetchall()
        con.close()
    except Exception as e:
        log.warning(f"Could not read Firefox cookies: {e}")
        return []

    SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
    cookies = []
    for host, path, name, value, expiry, secure, http_only, same_site in rows:
        # Playwright requires domain without leading dot for exact-match cookies
        cookies.append({
            "name":     name,
            "value":    value,
            "domain":   host,
            "path":     path,
            "expires":  (expiry // 1000) if expiry > 32503680000 else (expiry if expiry > 0 else -1),
            "httpOnly": bool(http_only),
            "secure":   bool(secure),
            "sameSite": SAME_SITE.get(same_site, "None"),
        })
    log.info(f"Loaded {len(cookies)} Zoom cookies from Firefox Nightly")
    return cookies


def scroll_to_load_all(page):
    """Scroll to the bottom to ensure lazy-loaded transcript content is rendered."""
    page.evaluate("""
        async function() {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            let prev = -1;
            while (document.body.scrollHeight !== prev) {
                prev = document.body.scrollHeight;
                window.scrollTo(0, document.body.scrollHeight);
                await sleep(600);
            }
        }
    """)
    page.wait_for_timeout(500)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    meetings = get_meetings_from_standup(TARGET)
    if not meetings:
        log.info("No meetings to process — done.")
        return

    folder = week_folder_for(TARGET)
    if not folder:
        log.error(f"Could not find week folder for {TARGET}")
        return

    date_str = TARGET.strftime("%Y-%m-%d")

    # Count duplicates to know when to append date
    from collections import Counter
    name_counts = Counter(meetings)

    zoom_cookies = load_zoom_cookies_from_firefox()

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=args.headless, slow_mo=200)
        context = browser.new_context()

        # Seed context with existing Zoom session cookies before first navigation
        if zoom_cookies:
            context.add_cookies(zoom_cookies)

        page = context.new_page()

        log.info(f"Navigating to {ZOOM_NOTES}")
        page.goto(ZOOM_NOTES, wait_until="networkidle", timeout=30000)

        # Fall back to interactive login if cookies didn't carry the session
        if not args.headless and "login" in page.url.lower():
            log.info("Cookies didn't carry session — please log in, then press Enter.")
            input("Press Enter after logging in...")
            page.goto(ZOOM_NOTES, wait_until="networkidle", timeout=30000)

        processed_names = Counter()  # track how many times we've processed each name

        for meeting_name in meetings:
            processed_names[meeting_name] += 1
            is_duplicate = name_counts[meeting_name] > 1

            # Navigate back to notes list for each meeting (except the first)
            current_url = page.url
            if current_url != ZOOM_NOTES and not current_url.startswith(ZOOM_NOTES + "/"):
                page.goto(ZOOM_NOTES, wait_until="networkidle", timeout=20000)
            elif "/notes/" in current_url:
                # We're on a specific note — go back to the list
                page.goto(ZOOM_NOTES, wait_until="networkidle", timeout=20000)

            if not find_and_open_note(page, meeting_name):
                log.warning(f"Skipping '{meeting_name}' — note not found")
                continue

            ensure_transcript_expanded(page)
            scroll_to_load_all(page)

            # Extract content using bookmarklet logic
            content = page.evaluate(EXTRACT_JS)
            if not content:
                log.warning(f"No transcript content found for '{meeting_name}'")
                continue

            # Determine filename — append date suffix for duplicates
            # For the Nth occurrence of a duplicate name, append date + occurrence number if >2
            if is_duplicate and processed_names[meeting_name] > 1:
                suffix = f"{date_str} ({processed_names[meeting_name]})"
            elif is_duplicate:
                suffix = date_str
            else:
                suffix = ""

            out_path = output_path(folder, meeting_name, date_str if suffix else "", bool(suffix))

            with open(out_path, 'w') as f:
                f.write(content)
            log.info(f"Saved: {out_path}")

        browser.close()

    log.info("Done.")


if __name__ == "__main__":
    main()
