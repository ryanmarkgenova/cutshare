"""Mac dialogs for CutShare, so editors never need Terminal.

The Finder Quick Actions installed by `Install CutShare.command` call:
    python3 -m cutshare ui submit <exported .fcpxml>
    python3 -m cutshare ui pull|assemble|status [<folder>]
Every question is a native macOS dialog (osascript). Errors show as alerts
and are also written to ~/.cutshare/ui.log.
"""
import contextlib
import posixpath
import datetime
import io
import os
import subprocess
import traceback

from . import cli
from . import fcpxml as fx
from .diff import one_line
from .store import Config, Project, StoreError
from .timecode import human, parse_clock

CANCEL = "__CANCEL__"
OUT_ROOT = os.path.expanduser("~/Movies/CutShare")


# ---------------------------------------------------------------- dialogs

class Dialogs:
    """Native macOS dialogs. Every method returns None if the user cancels."""

    def _osa(self, script, *args):
        r = subprocess.run(["osascript", "-", *[str(a) for a in args]], input=script,
                           capture_output=True, text=True)
        if r.returncode != 0 and ("-1713" in r.stderr or "user interaction" in r.stderr.lower()):
            # Some macOS versions block dialogs from Quick Action scripts; ask Finder to show them
            wrapped = script.replace("on run argv\n", 'on run argv\ntell application "Finder"\nactivate\n', 1)
            wrapped = wrapped[::-1].replace("end run"[::-1], ("end tell\nend run")[::-1], 1)[::-1]
            r = subprocess.run(["osascript", "-", *[str(a) for a in args]], input=wrapped,
                               capture_output=True, text=True)
        if r.returncode != 0:
            if "-128" in r.stderr:
                return None
            raise RuntimeError(r.stderr.strip() or "osascript failed")
        out = r.stdout.rstrip("\n")
        return None if out == CANCEL else out

    def choose(self, prompt, items, default=None):
        return self._osa("""on run argv
  set p to item 1 of argv
  set d to item 2 of argv
  set xs to items 3 thru -1 of argv
  if d is "" then
    set r to choose from list xs with title "CutShare" with prompt p
  else
    set r to choose from list xs with title "CutShare" with prompt p default items {d}
  end if
  if r is false then return "__CANCEL__"
  return item 1 of r
end run""", prompt, default or "", *items)

    def ask(self, prompt, default=""):
        return self._osa("""on run argv
  set r to display dialog (item 1 of argv) default answer (item 2 of argv) with title "CutShare" buttons {"Cancel", "OK"} default button "OK"
  return text returned of r
end run""", prompt, default)

    def buttons(self, prompt, names):
        return self._osa("""on run argv
  set bs to items 2 thru -1 of argv
  set r to display dialog (item 1 of argv) with title "CutShare" buttons bs default button (count of bs)
  return button returned of r
end run""", prompt, *names)

    def choose_folder(self, prompt):
        return self._osa("""on run argv
  return POSIX path of (choose folder with prompt (item 1 of argv))
end run""", prompt)

    def alert(self, title, message):
        self._osa("""on run argv
  display alert (item 1 of argv) message (item 2 of argv) as critical
end run""", title, message)

    def notify(self, title, message):
        try:
            self._osa("""on run argv
  display notification (item 2 of argv) with title "CutShare" subtitle (item 1 of argv)
end run""", title, message)
        except RuntimeError:
            pass

    def open(self, path, app=None, reveal=False):
        if reveal:
            subprocess.run(["open", "-R", path], check=False)
            return
        if app and subprocess.run(["open", "-a", app, path], check=False).returncode == 0:
            return
        subprocess.run(["open", path], check=False)


# ---------------------------------------------------------------- helpers

def _log(text):
    path = os.path.expanduser("~/.cutshare/ui.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n--- {datetime.datetime.now().isoformat(timespec='seconds')}\n{text}")


def resolve_project(paths, explicit, cfg, dlg, prefer_paths=True):
    """Find the project: given -p, the selected item (or a parent), the last
    project used on this Mac, or ask."""
    if explicit:
        proj = Project(explicit)
    else:
        proj = None
        if prefer_paths:
            for p in paths:
                proj = Project.find_from(p)
                if proj:
                    break
        if proj is None and cfg.data.get("last_project") and \
                os.path.exists(os.path.join(cfg.data["last_project"], "cutshare.json")):
            proj = Project(cfg.data["last_project"])
        if proj is None:
            folder = dlg.choose_folder("Pick the shared CutShare project folder (the one ending in .cutshare)")
            if not folder:
                return None
            proj = Project.find_from(folder)
            if proj is None:
                raise StoreError(f"{folder} isn't a CutShare project folder")
    cfg.data["last_project"] = proj.path
    cfg.save()
    return proj


def ensure_media_root(proj, cfg, dlg):
    if cfg.media_root(proj.id):
        return True
    folder = dlg.choose_folder(f"First time on this Mac for {proj.name}. "
                               "Where is the project's footage? (pick the shared footage folder)")
    if not folder:
        return False
    cfg.set_media_root(proj.id, folder.rstrip("/"))
    return True


def _seg_label(seg, me):
    v = seg.latest()
    lock = seg.lock()
    bits = [f"v{v['number']}" if v else "empty"]
    if lock:
        bits.append("you're editing" if lock["user"] == me else f"{lock['user']} is editing")
    return f"{seg.order}. {seg.name}  ({', '.join(bits)})"


def guess_segment(segs, me, hint=""):
    mine = [s for s in segs if (s.lock() or {}).get("user") == me]
    if len(mine) == 1:
        return mine[0]
    hint = hint.lower()
    named = [s for s in segs if hint.endswith(s.name.lower()) or f"- {s.name.lower()}" in hint]
    if len(named) == 1:
        return named[0]
    return None


def _quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = fn(*a, **k)
    _log(buf.getvalue())
    return result, buf.getvalue()


def _out_dir(proj):
    d = os.path.join(OUT_ROOT, proj.name)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- actions

def ui_submit(paths, explicit, cfg, dlg):
    files = [p for p in paths if p.lower().rstrip("/").endswith((".fcpxml", ".fcpxmld"))]
    if not files:
        dlg.alert("Nothing to submit", "Right-click an exported .fcpxml (File > Export XML in Final Cut Pro).")
        return
    path = files[0]
    proj = resolve_project([path], explicit, cfg, dlg)
    if not proj or not ensure_media_root(proj, cfg, dlg):
        return
    segs = proj.segments()
    if not segs:
        raise StoreError(f"{proj.name} has no segments yet")
    me = cfg.user
    doc = fx.load(path)
    guess = guess_segment(segs, me, doc.name)
    labels = [_seg_label(s, me) for s in segs]
    pick = dlg.choose(f"Save “{os.path.basename(path.rstrip('/'))}” as the next version of which segment?",
                      labels, _seg_label(guess, me) if guess else None)
    if pick is None:
        return
    seg = segs[labels.index(pick)]
    note = dlg.ask(f"What changed in {seg.name}? (a short note for the team)")
    if note is None:
        return
    force = False
    lock = seg.lock()
    if lock and lock["user"] != me:
        b = dlg.buttons(f"{lock['user']} is editing {seg.name} right now. Saving will put your version on top of theirs.",
                        ["Cancel", "Save anyway"])
        if b != "Save anyway":
            return
        force = True
    (rec, page), _ = _quiet(cli._submit, proj, cfg, seg, path, note, force=force)
    if page:
        dlg.open(page)
    summary = one_line(rec["changes"]) if rec.get("changes") else "First version"
    dlg.notify(f"Saved {seg.name} v{rec['number']}", summary)


def ui_pull(paths, explicit, cfg, dlg):
    proj = resolve_project(paths, explicit, cfg, dlg)
    if not proj or not ensure_media_root(proj, cfg, dlg):
        return
    me = cfg.user
    segs = [s for s in proj.segments() if s.latest()]
    if not segs:
        raise StoreError(f"{proj.name} has no segments with versions yet")
    labels = [_seg_label(s, me) for s in segs]
    pick = dlg.choose("Which segment do you want to open in Final Cut Pro?", labels,
                      next((l for s, l in zip(segs, labels) if (s.lock() or {}).get("user") == me), None))
    if pick is None:
        return
    seg = segs[labels.index(pick)]
    lock = seg.lock()
    claim = False
    if lock and lock["user"] != me:
        b = dlg.buttons(f"{lock['user']} is editing {seg.name}. You can open it to watch, "
                        "but your changes won't be saved over theirs.", ["Cancel", "Open read-only"])
        if b is None or b == "Cancel":
            return
    elif not lock:
        b = dlg.buttons(f"Lock {seg.name} so nobody else edits it while you work?",
                        ["Cancel", "Just look", "Lock it for me"])
        if b is None or b == "Cancel":
            return
        claim = b == "Lock it for me"
    rec = seg.version()
    out = os.path.join(_out_dir(proj), f"{proj.name} - {seg.name} v{rec['number']:03d}.fcpxml")
    _quiet(cli._export_for_me, seg.file_of(rec), out, proj, cfg)
    if claim:
        seg.claim(me)
    dlg.open(out, app="Final Cut Pro")
    dlg.notify(f"Opened {seg.name} v{rec['number']}",
               "Locked for you. Right-click your export > Submit when done" if claim else "Not locked")


def ui_assemble(paths, explicit, cfg, dlg):
    proj = resolve_project(paths, explicit, cfg, dlg)
    if not proj or not ensure_media_root(proj, cfg, dlg):
        return
    n_next = max((r["number"] for r in proj.assembled()), default=0) + 1
    out = os.path.join(_out_dir(proj), f"{proj.name} - master v{n_next:03d}.fcpxml")
    (out, n, used, dur), log = _quiet(cli.assemble_project, proj, cfg, out)
    busy = [line.split("note: ", 1)[1] for line in log.splitlines() if "still being edited" in line]
    msg = (f"Master v{n} is ready ({human(dur)}).\n\n"
           + "\n".join(f"{k}: v{v}" for k, v in used.items())
           + ("\n\nHeads up:\n" + "\n".join(busy) if busy else ""))
    b = dlg.buttons(msg, ["Show in Finder", "Open in Final Cut Pro"])
    if b == "Open in Final Cut Pro":
        dlg.open(out, app="Final Cut Pro")
    elif b == "Show in Finder":
        dlg.open(out, reveal=True)


def ui_status(paths, explicit, cfg, dlg):
    proj = resolve_project(paths, explicit, cfg, dlg)
    if not proj:
        return
    me = cfg.user
    lines = [_seg_label(s, me) for s in proj.segments()] or ["No segments yet"]
    masters = proj.assembled()
    lines.append("")
    lines.append(f"Last master: v{masters[-1]['number']} by {masters[-1]['by']}" if masters
                 else "Not assembled yet")
    dlg.buttons(f"{proj.name}\n\n" + "\n".join(lines), ["OK"])


def footage_folder(doc):
    """The folder that holds all of this timeline's media, if they share one."""
    from urllib.parse import unquote, urlparse
    dirs = []
    for el in doc.root.iter():
        src = el.get("src") if el.tag in ("media-rep", "asset") else None
        if src and src.startswith("file://") and not src.startswith(fx.MEDIA_TOKEN):
            dirs.append(posixpath.dirname(unquote(urlparse(src).path)))
    if not dirs:
        return None
    common = posixpath.commonpath(dirs)
    return common if common not in ("", "/") else None


def _suggest_cuts(doc, parts=3):
    """Even cut points (seconds), snapped later to clip edges by split()."""
    total = float(doc.duration())
    return [round(total * k / parts, 1) for k in range(1, parts)]


def _fmt_clock(sec):
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}"


def ui_new(paths, explicit, cfg, dlg):
    files = [p for p in paths if p.lower().rstrip("/").endswith((".fcpxml", ".fcpxmld"))]
    if not files:
        dlg.alert("Pick an export first", "Right-click a rough cut exported from Final Cut Pro (File > Export XML).")
        return
    path = files[0]
    doc = fx.load(path)
    name = dlg.ask("Name this project (the video's name)", doc.name or "New video")
    if not name:
        return
    name = name.strip().replace("/", "-")
    where = dlg.choose_folder("Where should the shared project live? Pick a Google Drive or Dropbox "
                              "folder everyone on the team can open")
    if not where:
        return
    cut_text = dlg.ask(f"Your cut is {human(doc.duration())} long. Where should it be split into segments? "
                       "Times separated by commas. Cuts snap to the nearest clip. Leave empty to keep it whole.",
                       ", ".join(_fmt_clock(c) for c in _suggest_cuts(doc)))
    if cut_text is None:
        return
    cuts = [parse_clock(t) for t in cut_text.replace(";", ",").split(",") if t.strip()]
    default_names = ", ".join(f"Part {k}" for k in range(1, len(cuts) + 2))
    names_text = dlg.ask(f"Names for the {len(cuts) + 1} segments, in order, separated by commas",
                         default_names)
    if names_text is None:
        return
    names = [n.strip() for n in names_text.split(",") if n.strip()]
    if len(names) != len(cuts) + 1:
        raise StoreError(f"Got {len(names)} names for {len(cuts) + 1} segments. Try again with one name per segment.")

    target = os.path.join(where.rstrip("/"), f"{name}.cutshare")
    proj = Project.init(target, name)
    footage = footage_folder(doc)
    if footage:
        b = dlg.buttons(f"Is this the footage folder everyone shares?\n\n{footage}",
                        ["Pick another", "Yes, use it"])
        if b == "Yes, use it":
            cfg.set_media_root(proj.id, footage)
    if not ensure_media_root(proj, cfg, dlg):
        return
    (made, _), _ = _quiet(cli.split_into, proj, cfg, path, cuts, names)
    cfg.data["last_project"] = proj.path
    cfg.save()
    summary = "\n".join(f"{seg.order}. {seg.name}  starts {human(start)}, {human(dur)} long"
                        for seg, start, dur in made)
    b = dlg.buttons(f"Created {name} with {len(made)} segments:\n\n{summary}\n\n"
                    "Share that folder with your editors. They right-click it > "
                    "CutShare – Open a segment.", ["Show in Finder"])
    if b == "Show in Finder":
        dlg.open(proj.path, reveal=True)


ACTIONS = {"new": ui_new, "submit": ui_submit, "pull": ui_pull, "assemble": ui_assemble, "status": ui_status}


def run(action, paths, explicit=None, dlg=None):
    dlg = dlg or Dialogs()
    cfg = Config()
    try:
        ACTIONS[action](paths, explicit, cfg, dlg)
    except (StoreError, fx.FCPXMLError) as e:
        _log(f"error: {e}")
        dlg.alert("CutShare", str(e))
        return 1
    except Exception as e:  # show anything unexpected instead of failing silently
        _log(traceback.format_exc())
        dlg.alert("CutShare hit a problem", f"{e}\n\nDetails are in ~/.cutshare/ui.log")
        return 1
    return 0
