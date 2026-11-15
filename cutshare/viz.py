"""Visual timeline diff: two versions drawn as colored timeline bars.

    green  = added        red    = removed
    yellow = trimmed      purple = moved
    grey   = unchanged

Writes one self-contained HTML file (no internet needed) you can open in
any browser, screenshot, or drop in a portfolio.
"""
import difflib
import html
from collections import defaultdict

from .timecode import human, parse_time

STATUS_LABEL = {"same": "Unchanged", "added": "Added", "removed": "Removed",
                "trimmed": "Trimmed", "moved": "Moved"}


# ---------------------------------------------------------------- layout

def _kind(doc, el):
    tag = el.tag
    if tag == "title":
        return "title"
    if tag == "gap":
        return "gap"
    if tag in ("ref-clip", "sync-clip", "mc-clip"):
        return "compound"
    ref = el.get("ref")
    if tag == "clip":
        inner = [c for c in el if c.tag in ("video", "audio")]
        if any(c.tag == "video" for c in inner):
            return "video"
        return "audio" if inner else "video"
    if tag == "audio":
        return "audio"
    res = doc.res.get(ref)
    if res is not None and res.tag == "effect":
        return "adjust"
    if res is not None and res.tag == "asset" and res.get("hasVideo") != "1":
        return "audio"
    return "video"


def _label(doc, el):
    name = el.get("name")
    if name:
        return name
    ident = doc.identity(el)
    return ident.split(":", 1)[-1] or el.tag


def blocks(doc):
    """Every clip on the timeline with an absolute position (seconds from start)."""
    out = []
    origin = doc.tc_start
    for pos, top in enumerate(i for i in doc.items() if i.tag != "transition"):
        off = parse_time(top.get("offset"))
        start = parse_time(top.get("start"))
        dur = parse_time(top.get("duration"))
        out.append(_block(doc, top, 0, off - origin, dur, start, pos + 1, None))
        # connected clips: their offset is in the parent's source time
        for child in top:
            if child.get("lane") is None or child.tag in ("marker", "chapter-marker"):
                continue
            c_off = parse_time(child.get("offset"))
            c_dur = parse_time(child.get("duration"))
            abs_start = off - origin + (c_off - start)
            out.append(_block(doc, child, int(child.get("lane")), abs_start, c_dur,
                              parse_time(child.get("start")), None, pos + 1))
    return out


def _block(doc, el, lane, at, dur, src_in, pos, parent):
    return {
        "lane": lane, "at": at, "dur": dur, "src_in": src_in,
        "name": _label(doc, el), "ident": doc.identity(el), "kind": _kind(doc, el),
        "enabled": el.get("enabled") != "0", "pos": pos, "parent": parent,
        "status": "same", "note": "",
    }


# ---------------------------------------------------------------- matching

def compare(a, b):
    """Return (blocks_a, blocks_b) with status filled in on both sides."""
    ba, bb = blocks(a), blocks(b)
    pa = [x for x in ba if x["lane"] == 0]
    pb = [x for x in bb if x["lane"] == 0]
    sm = difflib.SequenceMatcher(a=[x["ident"] for x in pa], b=[x["ident"] for x in pb],
                                 autojunk=False)
    left, right = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for x, y in zip(pa[i1:i2], pb[j1:j2]):
                _pair(x, y)
        else:
            left.extend(pa[i1:i2])
            right.extend(pb[j1:j2])
    _match_leftovers(left, right, moved=True)

    # connected clips travel with their parent, so match them across the whole timeline
    # Pass 1: identical clip (same media, lane, in point, length), nearest in time.
    # Pass 2: same media + lane + in point, so only the length changed (trimmed).
    # Pass 3: same media anywhere (trimmed or slipped).
    rest_a = [x for x in ba if x["lane"] != 0]
    rest_b = [y for y in bb if y["lane"] != 0]
    keys = (lambda c: (c["ident"], c["lane"], c["src_in"], c["dur"]),
            lambda c: (c["ident"], c["lane"], c["src_in"]),
            lambda c: (c["ident"],))
    for key in keys:
        rest_a, rest_b = _nearest_pairs(rest_a, rest_b, key)
    for x in rest_a:
        x["status"] = "removed"
    for y in rest_b:
        y["status"] = "added"
    return ba, bb


def _nearest_pairs(left, right, key):
    """Pair items with equal keys, closest timeline position first (greedy)."""
    pool = defaultdict(list)
    for y in right:
        pool[key(y)].append(y)
    cands = []
    for x in left:
        for y in pool.get(key(x), ()):
            cands.append((abs(x["at"] - y["at"]), id(x), id(y), x, y))
    cands.sort(key=lambda c: c[:3])
    used_l, used_r = set(), set()
    for _, ix, iy, x, y in cands:
        if ix in used_l or iy in used_r:
            continue
        used_l.add(ix)
        used_r.add(iy)
        _pair(x, y)
    return ([x for x in left if id(x) not in used_l], [y for y in right if id(y) not in used_r])


def _pair(x, y):
    if x["dur"] != y["dur"] or x["src_in"] != y["src_in"]:
        x["status"] = y["status"] = "trimmed"
        delta = float(y["dur"] - x["dur"])
        y["note"] = f"{float(x['dur']):.2f}s \u2192 {float(y['dur']):.2f}s ({delta:+.2f}s)"
        x["note"] = y["note"]


def _match_leftovers(left, right, moved):
    for x in list(left):
        y = next((y for y in right if y["ident"] == x["ident"] and y["dur"] == x["dur"]), None) \
            or next((y for y in right if y["ident"] == x["ident"]), None)
        if y is None:
            continue
        left.remove(x)
        right.remove(y)
        if moved and x["pos"] != y["pos"]:
            x["status"] = y["status"] = "moved"
            note = f"#{x['pos']} \u2192 #{y['pos']}"
            if x["dur"] != y["dur"]:
                note += f", {float(x['dur']):.2f}s \u2192 {float(y['dur']):.2f}s"
            x["note"] = y["note"] = note
        else:
            _pair(x, y)
    for x in left:
        x["status"] = "removed"
    for y in right:
        y["status"] = "added"


# ---------------------------------------------------------------- html

# Design follows the unslop process (Damir Sokolovsky, @heydamir, CC BY 4.0):
# quiet graphite chrome, the changed clips are the only color on the page.
CSS = """
:root{
--bg:#141415;--deep:#0f0f10;--card:#19191b;--raised:#232325;
--line:rgba(237,237,237,.08);--line2:rgba(237,237,237,.14);
--t1:#ededed;--t2:#a1a1a6;--t3:#85858b;
--added:#4cc38a;--removed:#e5484d;--trimmed:#e2b340;--moved:#5b8def;
--ease:cubic-bezier(.22,1,.36,1);
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--t1);
font:14px/20px -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1600px;margin:0 auto;padding:32px}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap}
h1{font-size:28px;line-height:1.15;font-weight:500;margin:0}
h1 span{color:var(--t3)}
.sub{color:var(--t2);margin-top:8px}
.zoom{display:flex;gap:8px;align-items:center}
.seg{display:flex;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px;gap:4px}
button{font:inherit;font-size:13px;height:32px;padding:0 12px;border-radius:6px;border:0;cursor:pointer;
color:var(--t2);background:transparent;transition:background-color .15s var(--ease),color .15s var(--ease)}
button:hover{color:var(--t1);background:var(--raised)}
button.on{color:var(--t1);background:var(--raised);box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}
button:focus-visible{outline:2px solid var(--t2);outline-offset:2px}
button.primary{height:40px;padding:0 16px;border-radius:10px;background:var(--t1);color:#141415;font-weight:500}
button.primary:hover{background:#ffffff}
button.primary:active{transform:scale(.98)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) minmax(0,1.4fr);gap:12px;margin:24px 0 16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px}
.stat .k{display:flex;align-items:center;gap:8px;color:var(--t2);font-size:13px}
.dot{width:8px;height:8px;border-radius:2px;flex:0 0 8px}
.stat .v{font-size:28px;line-height:1;font-weight:500;margin-top:12px;font-variant-numeric:tabular-nums}
.stat .v.zero{color:var(--t3)}
.stat .c{font-family:var(--mono);font-size:12px;color:var(--t3);margin-top:8px}
.stat .c b{font-weight:400;color:var(--t2)}
.board{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.tl{display:flex}
.inner{padding-left:0}
.labels{flex:0 0 88px;border-right:1px solid var(--line)}
.scroller{flex:1;overflow-x:auto;overflow-y:hidden;scrollbar-width:thin;scrollbar-color:#3a3a3d transparent}
.inner{width:calc(var(--zoom,1)*100%);min-width:100%;padding:0 16px 0 0}
.ruler{position:relative;height:32px;border-bottom:1px solid var(--line)}
.lab-ruler{height:32px;border-bottom:1px solid var(--line)}
.tick{position:absolute;top:0;bottom:0;border-left:1px solid var(--line)}
.tick span{position:absolute;left:6px;top:9px;font:12px/16px var(--mono);color:var(--t3);font-variant-numeric:tabular-nums;white-space:nowrap}
.flag{position:absolute;bottom:0;width:8px;height:10px;margin-left:-4px;clip-path:polygon(0 0,100% 0,100% 60%,50% 100%,0 60%)}
.group{position:relative;height:40px;display:flex;align-items:center;gap:8px;padding:0 16px;color:var(--t2);font-size:13px}
.group b{font-weight:500;color:var(--t1)}
.inner .group{padding:0;position:sticky;left:16px;width:max-content;z-index:4}
.sep{border-top:1px solid var(--line)}
.lbl{height:24px;margin-bottom:4px;padding-right:12px;text-align:right;font:12px/24px var(--mono);color:var(--t3)}
.lbl.p{height:40px;line-height:40px;color:var(--t2)}
.lane{position:relative;height:24px;margin-bottom:4px;border-radius:4px;background:rgba(237,237,237,.025)}
.lane.p{height:40px;background:rgba(237,237,237,.045)}
.pad{height:12px}
.blk{position:absolute;top:2px;bottom:2px;border-radius:4px;overflow:hidden;white-space:nowrap;
font-size:11px;line-height:20px;padding:0 6px;color:var(--t2);min-width:2px;
box-shadow:inset 0 0 0 1px rgba(0,0,0,.35);transition:background-color .15s var(--ease),color .15s var(--ease)}
.lane.p .blk{line-height:36px;font-size:12px}
.blk.k-video,.blk.k-compound{background:#38383c}
.blk.k-title{background:#323236}.blk.k-adjust{background:#2b2b2e}
.blk.k-audio{background:#2a2c2b}
.blk.k-gap{background:repeating-linear-gradient(135deg,rgba(237,237,237,.05) 0 4px,transparent 4px 8px)}
.blk:hover{background:#4a4a4f;color:var(--t1);z-index:3}
.blk.s-added,.blk.s-removed,.blk.s-trimmed,.blk.s-moved{color:#111113;font-weight:500;box-shadow:none;z-index:2}
.blk.s-added{background:var(--added)}.blk.s-removed{background:var(--removed)}
.blk.s-trimmed{background:var(--trimmed)}.blk.s-moved{background:var(--moved)}
.blk.ghost{background:transparent!important;border:1px dashed;opacity:.6;color:transparent;z-index:1}
.blk.ghost.s-added{border-color:var(--added)}.blk.ghost.s-removed{border-color:var(--removed)}
.blk.off{opacity:.5;background-image:repeating-linear-gradient(135deg,rgba(0,0,0,.3) 0 3px,transparent 3px 6px)}
.note{color:var(--t3);font-size:13px;margin:12px 0 32px}
.note i{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-2px;margin:0 6px 0 16px}
.note i:first-child{margin-left:0}
h2{font-size:16px;font-weight:500;margin:0 0 12px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
td{height:40px;padding:0 16px;border-top:1px solid var(--line);font-size:13px;color:var(--t2);white-space:nowrap}
tr:first-child td{border-top:0}
td.st{width:120px;color:var(--t1)}
td.st .dot{display:inline-block;margin-right:8px}
td.tc,td.ln{font-family:var(--mono);font-size:12px;color:var(--t3);font-variant-numeric:tabular-nums;width:96px}
td.nm{color:var(--t1);white-space:normal}
td.dt{font-family:var(--mono);font-size:12px;text-align:right}
.empty{color:var(--t3);padding:16px}
footer{color:var(--t3);font-size:12px;margin-top:32px}
"""


def _ticks(total):
    t = float(total)
    step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if t / s <= 12), 900)
    out, x = [], 0.0
    while x < t - step * 0.4:
        out.append(x)
        x += step
    return out


def _lane_name(lane):
    return "Primary" if lane == 0 else (f"+{lane}" if lane > 0 else str(lane))


def _blk_html(b, t, ghost=False):
    left = min(max(0.0, float(b["at"]) / t * 100), 100.0)
    right = min(100.0, (float(b["at"]) + float(b["dur"])) / t * 100)
    width = max(0.12, right - left)
    tip = (f"{b['name']}\n{STATUS_LABEL[b['status']]}"
           + (f", {b['note']}" if b["note"] else "")
           + f"\n{_tc(b['at'])} · {float(b['dur']):.2f}s · {_lane_name(b['lane'])}"
           + ("" if b["enabled"] else "\nDisabled"))
    cls = f"blk k-{b['kind']} s-{b['status']}" + ("" if b["enabled"] else " off") + (" ghost" if ghost else "")
    label = "" if (b["kind"] == "gap" or ghost) else html.escape(b["name"])
    return (f'<div class="{cls}" style="left:{left:.4f}%;width:{width:.4f}%" '
            f'title="{html.escape(tip)}">{label}</div>')


def _tc(sec):
    """Seconds -> m:ss.ff-ish mono timecode for people."""
    return human(sec)


def _lanes_html(blist, ghosts, lanes, t):
    labels, rows = [], []
    for lane in lanes:
        cells = [_blk_html(g, t, ghost=True) for g in ghosts if g["lane"] == lane]
        cells += [_blk_html(b, t) for b in sorted((b for b in blist if b["lane"] == lane),
                                                  key=lambda b: b["at"])]
        p = " p" if lane == 0 else ""
        labels.append(f'<div class="lbl{p}">{_lane_name(lane)}</div>')
        rows.append(f'<div class="lane{p}">{"".join(cells)}</div>')
    return "".join(labels), "".join(rows)


def change_lines(ba, bb):
    """One line per change, in timeline order, from the matched blocks."""
    return [f"{sym} {status.capitalize()} \"{c['name']}\" ({_lane_name(c['lane'])}, {_tc(c['at'])})"
            + (f": {c['note']}" if c["note"] else f", {float(c['dur']):.2f}s")
            for sym, status, c in _changes(ba, bb)]


def _changes(ba, bb):
    sym = {"removed": "-", "added": "+", "trimmed": "~", "moved": ">"}
    rows = [(sym[c["status"]], c["status"], c) for c in ba if c["status"] == "removed"]
    rows += [(sym[c["status"]], c["status"], c) for c in bb if c["status"] in ("added", "trimmed", "moved")]
    return sorted(rows, key=lambda r: r[2]["at"])


JS = """
(function(){
  var sc=document.querySelector('.scroller');
  var btns=[].slice.call(document.querySelectorAll('.seg button'));
  function zoom(z,center){
    document.documentElement.style.setProperty('--zoom',z);
    btns.forEach(function(b){b.classList.toggle('on',+b.dataset.z===z)});
    requestAnimationFrame(function(){
      sc.scrollLeft=center==null?0:Math.max(0,center*sc.scrollWidth-sc.clientWidth/2);});
  }
  btns.forEach(function(b){b.onclick=function(){zoom(+b.dataset.z,null)}});
  var f=document.querySelector('button.primary');
  if(f){f.onclick=function(){
    var a=+f.dataset.a,b=+f.dataset.b,span=Math.max(b-a,0.02);
    zoom(Math.round(Math.max(1,Math.min(40,0.6/span))*10)/10,(a+b)/2);};}
})();
"""


def render(a, b, title, before_label, after_label):
    ba, bb = compare(a, b)
    lanes = sorted({x["lane"] for x in ba + bb}, reverse=True)
    total = max(a.duration(), b.duration())
    t = float(total) or 1.0

    counts = defaultdict(int)
    for x in ba:
        if x["status"] == "removed":
            counts["removed"] += 1
    for y in bb:
        if y["status"] in ("added", "trimmed", "moved"):
            counts[y["status"]] += 1
    delta = float(b.duration() - a.duration())

    stats = ""
    for k in ("added", "removed", "trimmed", "moved"):
        n = counts[k]
        stats += (f'<div class="stat"><div class="k"><span class="dot" style="background:var(--{k})"></span>'
                  f'{STATUS_LABEL[k]}</div><div class="v{" zero" if not n else ""}">{n}</div>'
                  f'<div class="c">{"clips" if n != 1 else "clip"}</div></div>')
    stats += (f'<div class="stat"><div class="k">Length</div><div class="v">{_tc(b.duration())}</div>'
              f'<div class="c"><b>{delta:+.2f}s</b> vs {html.escape(before_label)}</div></div>')

    changed = [c for c in ba + bb if c["status"] != "same"]
    zoom = ('<div class="zoom"><div class="seg">'
            + "".join(f'<button data-z="{z}"{" class=on" if z == 1 else ""}>{"Fit" if z == 1 else f"{z}x"}</button>'
                      for z in (1, 2, 4, 8)) + "</div>")
    if changed:
        lo = min(float(c["at"]) for c in changed) / t
        hi = max(float(c["at"] + c["dur"]) for c in changed) / t
        zoom += f'<button class="primary" data-a="{lo:.5f}" data-b="{min(hi, 1):.5f}">Zoom to changes</button>'
    zoom += "</div>"

    ticks = "".join(f'<div class="tick" style="left:{x / t * 100:.3f}%"><span>{_tc(x)}</span></div>'
                    for x in _ticks(total))
    flags = "".join(f'<div class="flag" style="left:{min(float(c["at"]) / t * 100, 99.6):.3f}%;'
                    f'background:var(--{st})"></div>' for _, st, c in _changes(ba, bb))
    removed = [x for x in ba if x["status"] == "removed"]
    added = [y for y in bb if y["status"] == "added"]
    lab_a, rows_a = _lanes_html(ba, added, lanes, t)
    lab_b, rows_b = _lanes_html(bb, removed, lanes, t)
    board = (
        '<div class="board"><div class="tl">'
        f'<div class="labels"><div class="lab-ruler"></div>'
        f'<div class="group"></div>{lab_a}<div class="pad"></div>'
        f'<div class="sep"></div><div class="group"></div>{lab_b}<div class="pad"></div></div>'
        '<div class="scroller"><div class="inner">'
        f'<div class="ruler">{ticks}{flags}</div>'
        f'<div class="group">Before <b>{html.escape(before_label)}</b></div>{rows_a}<div class="pad"></div>'
        f'<div class="sep"></div><div class="group">After <b>{html.escape(after_label)}</b></div>{rows_b}<div class="pad"></div>'
        '</div></div></div></div>')

    rows = ""
    for _, st, c in _changes(ba, bb):
        detail = c["note"] or f"{float(c['dur']):.2f}s"
        rows += (f'<tr><td class="st"><span class="dot" style="background:var(--{st})"></span>{STATUS_LABEL[st]}</td>'
                 f'<td class="tc">{_tc(c["at"])}</td><td class="ln">{_lane_name(c["lane"])}</td>'
                 f'<td class="nm">{html.escape(c["name"])}</td><td class="dt">{html.escape(detail)}</td></tr>')
    table = f"<table>{rows}</table>" if rows else '<div class="empty">No clip changes between these versions</div>'
    n = len(changed and _changes(ba, bb))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<div class="top"><div><h1>{html.escape(title)}</h1>
<div class="sub">{html.escape(before_label)} to {html.escape(after_label)}</div></div>{zoom}</div>
<div class="stats">{stats}</div>
{board}
<div class="note"><i style="background:#38383c"></i>Unchanged
<i style="border:1px dashed var(--removed)"></i>Where a clip used to be
<i style="background:repeating-linear-gradient(135deg,#555 0 3px,#222 3px 6px)"></i>Disabled
<span style="margin-left:16px">Hover a clip for its timing</span></div>
<h2>Changes <span style="color:var(--t3);font-weight:400">{n}</span></h2>
{table}
<footer>CutShare. Connected clips are matched across the whole timeline, so a clip that rides along with a moved parent counts as unchanged. Design pass based on unslop by @heydamir (CC BY 4.0).</footer>
</div><script>{JS}</script></body></html>
"""


def write_html(a, b, path, title, before_label, after_label):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(a, b, title, before_label, after_label))
    return path
