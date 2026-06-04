#!/opt/homebrew/bin/python3
"""
Interactive Zoom meeting diagnostic.
Captures file system and process state at each stage of back-to-back meetings.
Press Enter at each prompt to record a snapshot.
"""
import os, subprocess
from datetime import datetime

ZOOM_APP_SUPPORT = os.path.expanduser("~/Library/Application Support/zoom.us")
ZOOM_DOCS        = os.path.expanduser("~/Documents/Zoom")
WATCH_DIRS       = [d for d in [ZOOM_APP_SUPPORT, ZOOM_DOCS] if os.path.exists(d)]

# ── Snapshot helpers ────────────────────────────────────────────────────────────

def file_mtimes(dirs):
    out = {}
    for d in dirs:
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [s for s in subdirs if s not in ('Cache','cache','GPUCache')]
            for f in files:
                fp = os.path.join(root, f)
                try:
                    out[fp] = os.stat(fp).st_mtime
                except OSError:
                    pass
    return out

def diff(before, after, label):
    new  = sorted(k for k in after if k not in before)
    mod  = sorted(k for k in after if k in before and after[k] != before[k])
    gone = sorted(k for k in before if k not in after)
    print(f"\n  {'─'*50}")
    print(f"  File changes since '{label}':")
    if new:
        print(f"  NEW ({len(new)}):")
        for f in new:   print(f"    + {f.replace(os.path.expanduser('~'), '~')}")
    if mod:
        print(f"  MODIFIED ({len(mod)}):")
        for f in mod[:20]: print(f"    ~ {f.replace(os.path.expanduser('~'), '~')}")
        if len(mod) > 20: print(f"    ... and {len(mod)-20} more")
    if gone:
        print(f"  DELETED ({len(gone)}):")
        for f in gone:  print(f"    - {f.replace(os.path.expanduser('~'), '~')}")
    if not new and not mod and not gone:
        print("  (no changes)")

def zoom_processes():
    r = subprocess.run(['ps', '-axo', 'pid,comm'], capture_output=True, text=True)
    return {line.strip() for line in r.stdout.splitlines()
            if 'zoom' in line.lower() and 'grep' not in line.lower()}

def proc_diff(before, after, label):
    appeared = after - before
    died     = before - after
    print(f"\n  Process changes since '{label}':")
    if appeared:
        for p in sorted(appeared): print(f"    + {p}")
    if died:
        for p in sorted(died):     print(f"    - {p}")
    if not appeared and not died:
        print("    (no changes)")

def audio_snapshot():
    r = subprocess.run(['lsof', '-c', 'zoom'], capture_output=True, text=True, timeout=8)
    lines = [l for l in r.stdout.splitlines()
             if any(x in l for x in ('audio','Audio','AppleH','CoreAudio','VDC','vdc','mic','Mic'))]
    return lines

def audio_diff(before, after, label):
    b, a = set(before), set(after)
    appeared = a - b
    died     = b - a
    print(f"\n  Audio/mic fd changes since '{label}':")
    if appeared:
        for l in sorted(appeared): print(f"    + {l}")
    if died:
        for l in sorted(died):     print(f"    - {l}")
    if not appeared and not died:
        print("    (no changes)")

def stamp(label):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] === {label} ===")
    files = file_mtimes(WATCH_DIRS)
    procs = zoom_processes()
    audio = audio_snapshot()
    print(f"  {len(files)} watched files  |  {len(procs)} zoom processes  |  {len(audio)} audio fds")
    return files, procs, audio


# ── Main ────────────────────────────────────────────────────────────────────────

print("\n╔══════════════════════════════════════════╗")
print("║      Zoom Back-to-Back Diagnostic        ║")
print("╚══════════════════════════════════════════╝")
print("\nMake sure Zoom is open but you are NOT in any meeting.")
input("\nPress Enter to capture baseline... ")
bf, bp, ba = stamp("BASELINE — Zoom open, no meeting")

print("\n─────────────────────────────────────────────")
print("JOIN MEETING 1 now (camera + mic on if possible).")
input("Press Enter once you're fully in the meeting... ")
m1f, m1p, m1a = stamp("MEETING 1 — joined")
diff(bf, m1f,   "baseline")
proc_diff(bp, m1p, "baseline")
audio_diff(ba, m1a, "baseline")

print("\n─────────────────────────────────────────────")
print("LEAVE MEETING 1 — keep Zoom open, do NOT quit.")
input("Press Enter once you've left meeting 1... ")
lf, lp, la = stamp("BETWEEN MEETINGS — Zoom open, no meeting")
diff(m1f, lf,   "meeting 1")
proc_diff(m1p, lp, "meeting 1")
audio_diff(m1a, la, "meeting 1")

print("\n─────────────────────────────────────────────")
print("JOIN MEETING 2 now (camera + mic on if possible).")
input("Press Enter once you're fully in meeting 2... ")
m2f, m2p, m2a = stamp("MEETING 2 — joined")
diff(lf, m2f,   "between meetings")
proc_diff(lp, m2p, "between meetings")
audio_diff(la, m2a, "between meetings")

print("\n─────────────────────────────────────────────")
print("LEAVE MEETING 2.")
input("Press Enter once you've left meeting 2... ")
ef, ep, ea = stamp("END — Zoom open, no meeting")
diff(m2f, ef,   "meeting 2")
proc_diff(m2p, ep, "meeting 2")
audio_diff(m2a, ea, "meeting 2")

print("\n\n╔══════════════════════════════════════════╗")
print("║            Diagnostic complete!         ║")
print("╚══════════════════════════════════════════╝\n")
