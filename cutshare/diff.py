"""Human-readable differences between two versions of a timeline."""
import difflib
from collections import Counter

from .timecode import human, parse_time


def _rows(doc):
    rows = []
    pos = 0
    for el in doc.items():
        if el.tag == "transition":
            continue
        pos += 1
        connected = Counter(doc.identity(c) for c in el.iter() if c is not el and c.get("lane"))
        rows.append({
            "ident": doc.identity(el),
            "label": doc.label(el),
            "tag": el.tag,
            "start": parse_time(el.get("start")),
            "dur": parse_time(el.get("duration")),
            "pos": pos,
            "connected": connected,
        })
    return rows


def _sec(x):
    return f"{float(x):.1f}s"


def diff(a, b):
    """Return (lines, summary_dict) describing how timeline b differs from a."""
    ra, rb = _rows(a), _rows(b)
    sm = difflib.SequenceMatcher(a=[r["ident"] for r in ra], b=[r["ident"] for r in rb],
                                 autojunk=False)
    lines, added, removed = [], [], []
    counts = Counter()

    def compare(x, y):
        if x["tag"] in ("asset-clip", "clip", "ref-clip", "mc-clip", "sync-clip") and (
                x["start"] != y["start"] or x["dur"] != y["dur"]):
            head = y["start"] - x["start"]
            tail = (y["start"] + y["dur"]) - (x["start"] + x["dur"])
            bits = []
            if head:
                bits.append(f"in point {'+' if head > 0 else ''}{_sec(head)}")
            if tail:
                bits.append(f"out point {'+' if tail > 0 else ''}{_sec(tail)}")
            lines.append(f"~ Trimmed {y['label']}: {_sec(x['dur'])} -> {_sec(y['dur'])} ({', '.join(bits)})")
            counts["trimmed"] += 1
        elif x["dur"] != y["dur"]:
            lines.append(f"~ Resized {y['label']}: {_sec(x['dur'])} -> {_sec(y['dur'])}")
            counts["trimmed"] += 1
        plus = y["connected"] - x["connected"]
        minus = x["connected"] - y["connected"]
        for ident, n in plus.items():
            lines.append(f"  + on {y['label']}: connected {ident.split(':', 1)[-1] or ident}" + (f" x{n}" if n > 1 else ""))
            counts["connected_added"] += n
        for ident, n in minus.items():
            lines.append(f"  - on {y['label']}: removed connected {ident.split(':', 1)[-1] or ident}" + (f" x{n}" if n > 1 else ""))
            counts["connected_removed"] += n

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for x, y in zip(ra[i1:i2], rb[j1:j2]):
                compare(x, y)
            continue
        if op == "replace" and i2 - i1 == 1 and j2 - j1 == 1 \
                and ra[i1]["tag"] == "title" and rb[j1]["tag"] == "title":
            lines.append(f"~ Title text {ra[i1]['label'][6:]} -> {rb[j1]['label'][6:]}")
            counts["retitled"] += 1
            continue
        if op in ("delete", "replace"):
            removed.extend(ra[i1:i2])
        if op in ("insert", "replace"):
            added.extend(rb[j1:j2])

    # an item that disappears in one place and appears in another was moved
    for r in list(removed):
        match = next((x for x in added if x["ident"] == r["ident"] and x["dur"] == r["dur"]), None) \
            or next((x for x in added if x["ident"] == r["ident"]), None)
        if match:
            lines.append(f"> Moved {match['label']} from #{r['pos']} to #{match['pos']}")
            counts["moved"] += 1
            if match["dur"] != r["dur"] or match["start"] != r["start"]:
                compare(r, match)
            removed.remove(r)
            added.remove(match)
    for r in added:
        lines.append(f"+ Added {r['label']} at #{r['pos']} ({_sec(r['dur'])})")
        counts["added"] += 1
    for r in removed:
        lines.append(f"- Removed {r['label']} (was #{r['pos']}, {_sec(r['dur'])})")
        counts["removed"] += 1

    sa, sb = a.stats(), b.stats()
    if sa["markers"] != sb["markers"]:
        lines.append(f"* Markers: {sa['markers']} -> {sb['markers']}")
    if sa["transitions"] != sb["transitions"]:
        lines.append(f"* Transitions: {sa['transitions']} -> {sb['transitions']}")
    da, db = sa["duration"], sb["duration"]
    delta = db - da
    lines.append(f"= Length {human(da)} -> {human(db)} ({'+' if delta >= 0 else ''}{float(delta):.1f}s)")
    summary = dict(counts)
    summary["duration_before"] = float(da)
    summary["duration_after"] = float(db)
    return lines, summary


def one_line(summary):
    parts = []
    for key, word in (("added", "added"), ("removed", "removed"), ("trimmed", "trimmed"),
                      ("moved", "moved"), ("retitled", "titles edited"),
                      ("connected_added", "connected clips added")):
        if summary.get(key):
            parts.append(f"{summary[key]} {word}")
    parts.append(f"{human(summary['duration_before'])} -> {human(summary['duration_after'])}")
    return ", ".join(parts)
