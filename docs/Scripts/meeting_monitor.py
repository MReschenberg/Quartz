#!/opt/homebrew/bin/python3
"""
Meeting monitor: when a calendar meeting starts and Zoom is running within
5 minutes of that start time, log the meeting name to today's standup note
under a ### Meetings heading.

Intended to run every minute via LaunchAgent.
"""

import os, re, json, glob, subprocess, logging
from datetime import date, datetime

VAULT = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
STATE_FILE = os.path.join(VAULT, "Scripts", "meeting_monitor_state.json")
LOG_FILE   = os.path.join(VAULT, "Scripts", "meeting_monitor.log")
ZOOM_WINDOW_SECS = 5 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

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


# ── Calendar ───────────────────────────────────────────────────────────────────

APPLESCRIPT = """
tell application "Calendar"
    set today to current date
    set startOfDay to today - (time of today)
    set endOfDay to startOfDay + (24 * 60 * 60 - 1)
    set output to ""
    repeat with cal in every calendar
        try
            set calEvents to (every event of cal whose start date >= startOfDay and start date <= endOfDay)
            repeat with evt in calEvents
                set output to output & (summary of evt) & "|" & ((start date of evt) as string) & linefeed
            end repeat
        end try
    end repeat
    return output
end tell
"""

DT_FORMATS = [
    "%A, %B %d, %Y at %I:%M:%S %p",
    "%A, %B  %d, %Y at %I:%M:%S %p",  # double-space for single-digit days on some locales
]

def parse_applescript_date(s):
    s = s.strip()
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    # Fallback: try removing extra spaces
    s2 = re.sub(r'\s+', ' ', s)
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            pass
    return None

def get_today_events():
    try:
        out = subprocess.run(
            ['osascript', '-e', APPLESCRIPT],
            capture_output=True, text=True, timeout=15
        )
        if out.returncode != 0 or not out.stdout.strip():
            return []
        events = []
        for line in out.stdout.strip().splitlines():
            if '|' not in line:
                continue
            title, dt_str = line.split('|', 1)
            dt = parse_applescript_date(dt_str)
            if dt:
                events.append((title.strip(), dt))
        return events
    except Exception as e:
        log.warning(f"Calendar read failed: {e}")
        return []


# ── Zoom detection ─────────────────────────────────────────────────────────────

def is_zoom_running():
    try:
        r = subprocess.run(['pgrep', '-i', 'zoom'], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


# ── State ──────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"logged": []}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# ── Note update ────────────────────────────────────────────────────────────────

def add_meeting_to_note(meeting_name, d):
    wf = week_file_for(d)
    if not wf:
        log.warning(f"No week file found for {d}")
        return False

    with open(wf) as f:
        content = f.read()
    lines = content.split('\n')

    day_idx = find_day_idx(lines, d)
    if day_idx == -1:
        log.warning(f"No day entry found for {d} in {wf}")
        return False

    day_end = find_next_h2(lines, day_idx)

    # Find or create ### Meetings section
    meetings_idx = next(
        (i for i in range(day_idx, day_end) if lines[i].strip() == '### Meetings'),
        -1
    )

    if meetings_idx == -1:
        # Insert right after ### 🏁 Today: block (before the next ### heading)
        insert_at = day_idx + 1
        for i in range(day_idx, day_end):
            if '🏁 Today' in lines[i]:
                insert_at = i + 1
                while insert_at < day_end and not lines[insert_at].startswith('###'):
                    insert_at += 1
                break
        lines[insert_at:insert_at] = ['### Meetings', '']
        day_end += 2
        meetings_idx = insert_at

    # Find end of Meetings section
    meetings_end = meetings_idx + 1
    while meetings_end < day_end and not (
        lines[meetings_end].startswith('###') or lines[meetings_end].startswith('## ')
    ):
        meetings_end += 1

    # Idempotency: skip if already listed
    for i in range(meetings_idx + 1, meetings_end):
        if lines[i].strip() == f"- {meeting_name}":
            return True

    # Insert before the trailing blank line (if any) or at the end of the section
    insert_at = meetings_end
    while insert_at > meetings_idx + 1 and lines[insert_at - 1].strip() == '':
        insert_at -= 1
    lines.insert(insert_at, f"- {meeting_name}")

    with open(wf, 'w') as f:
        f.write('\n'.join(lines))

    log.info(f"Logged meeting: {meeting_name}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    now   = datetime.now()

    state   = load_state()
    logged  = set(state.get("logged", []))
    zoom_up = is_zoom_running()

    events    = get_today_events()
    new_keys  = []

    for title, start_dt in events:
        diff = (now - start_dt).total_seconds()
        if not (0 <= diff <= ZOOM_WINDOW_SECS):
            continue

        key = f"{today}:{title}"
        if key in logged:
            continue

        if zoom_up:
            if add_meeting_to_note(title, today):
                new_keys.append(key)
        else:
            log.info(f"Meeting '{title}' started but Zoom not running — skipping")

    if new_keys:
        state["logged"] = list(logged | set(new_keys))
        # Prune entries older than 7 days
        cutoff = today.toordinal() - 7
        state["logged"] = [
            k for k in state["logged"]
            if len(k) >= 10 and date.fromisoformat(k[:10]).toordinal() >= cutoff
        ]
        save_state(state)


if __name__ == "__main__":
    main()
