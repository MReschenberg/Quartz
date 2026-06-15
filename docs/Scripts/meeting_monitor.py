#!/opt/homebrew/bin/python3
"""
Meeting monitor: when a calendar meeting starts and Zoom is running within
5 minutes of that start time, log the meeting name to today's standup note
under a ### Meetings heading.

Intended to run every minute via LaunchAgent.
"""

import os, re, json, glob, subprocess, logging
from datetime import date, datetime, time as dtime

VAULT = os.path.expanduser("~/Library/Mobile Documents/iCloud~md~obsidian/Documents")
STATE_FILE = os.path.join(VAULT, "Scripts", "meeting_monitor_state.json")
LOG_FILE   = os.path.join(VAULT, "Scripts", "meeting_monitor.log")
ZOOM_WINDOW_SECS = 10 * 60

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

CALENDAR_NAME = "Work"
ICAL_BUDDY    = "/opt/homebrew/bin/icalBuddy"
ANSI_RE       = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def get_today_events():
    """Return list of (title, start_datetime) for today's Work calendar events."""
    try:
        out = subprocess.run(
            [ICAL_BUDDY, "--noColorCodes", "--noPropNames", "eventsToday"],
            capture_output=True, text=True, timeout=15,
        )
        raw = ANSI_RE.sub('', out.stdout)
    except Exception as e:
        log.warning(f"icalBuddy failed: {e}")
        return []

    events = []
    today  = date.today()
    current_title    = None
    current_calendar = None

    for line in raw.splitlines():
        # New event header: "• Title (Calendar Name)"
        m_header = re.match(r'^[•\*]\s+(.+?)\s+\(([^)]+)\)\s*$', line)
        if m_header:
            current_title    = m_header.group(1).strip()
            current_calendar = m_header.group(2).strip()
            continue

        # Time line: "    9:30 AM - 10:00 AM"  or "    9:30 AM"
        if current_title and current_calendar == CALENDAR_NAME:
            m_time = re.match(r'^\s+(\d{1,2}:\d{2}\s*[AP]M)', line)
            if m_time:
                try:
                    t = datetime.strptime(m_time.group(1).strip(), "%I:%M %p")
                    dt = datetime.combine(today, dtime(t.hour, t.minute))
                    events.append((current_title, dt))
                except ValueError:
                    pass
                current_title = current_calendar = None

    return events


# ── Zoom detection ─────────────────────────────────────────────────────────────

def is_zoom_in_meeting():
    """True only when ZoomHybridConf is running, i.e. actively in a meeting.
    Retries for up to ~10s to handle the race where WatchPaths fires just before
    ZoomHybridConf finishes spawning."""
    import time
    for _ in range(5):
        try:
            r = subprocess.run(['pgrep', 'ZoomHybridConf'], capture_output=True)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(2)
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
    zoom_up = is_zoom_in_meeting()

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
