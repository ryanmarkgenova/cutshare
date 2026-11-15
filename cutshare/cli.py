"""cutshare: parallel editing for Final Cut Pro through FCPXML.

Typical loop for each editor:
    cutshare claim intro          # lock your segment
    cutshare pull intro           # get the latest .fcpxml, import into FCP
    ...edit in FCP, File > Export XML...
    cutshare submit intro ~/Desktop/intro.fcpxml -m "tightened the open"
Then anyone can run:
    cutshare assemble             # stitch every segment into one master timeline
"""
import argparse
import os
import subprocess
import sys

from . import __version__
from . import fcpxml as fx
from . import notify, viz
from .diff import diff, one_line
from .store import Config, Project, StoreError
from .timecode import human, parse_clock


def warn(msg):
    print(msg, file=sys.stderr)


def _ctx(args):
    return Project.find(args.project), Config()


def _media_root_or_warn(cfg, proj, action):
    root = cfg.media_root(proj.id)
    if not root:
        warn(f"note: no media folder set for this project, so media paths were left as-is while {action}. "
             "Run `cutshare media-root /path/to/your/footage` once so clips relink on every Mac.")
    return root


def _ingest(path, proj, cfg, project_name=None):
    """Load an FCPXML export and return a clean, shareable ET root + Doc."""
    doc = fx.load(path, project_name=project_name)
    if doc.other_projects:
        warn(f"note: {path} has {doc.other_projects + 1} projects; using '{doc.name}'. "
             "Use --project-name to pick another.")
    return doc


def _store_root(doc, proj, cfg, seg_name):
    root = fx.project_root(doc, proj.event_name, f"{proj.name} - {seg_name}")
    media = _media_root_or_warn(cfg, proj, "saving")
    if media:
        done, outside = fx.canonicalize_paths(root, media)
        if outside:
            warn(f"warning: {outside} clip(s) use media outside your media folder ({media}). "
                 "Teammates won't be able to relink them. Move that footage into the shared folder.")
    return root


def _export_for_me(src_path, out_path, proj, cfg):
    root = fx.load(src_path).root
    media = cfg.media_root(proj.id)
    if media:
        fx.localize_paths(root, media)
    elif fx.count_placeholders(root):
        warn("warning: this file uses shared media paths but you haven't set your media folder, "
             "so clips will show as missing. Run `cutshare media-root /path/to/footage` and pull again.")
    fx.write(root, out_path)


# ---------------------------------------------------------------- commands

def cmd_init(args):
    base = args.dir or os.getcwd()
    path = os.path.join(base, f"{args.name}.cutshare")
    proj = Project.init(path, args.name)
    print(f"Created {path}")
    print("Next: put this folder somewhere everyone can reach (Google Drive, Dropbox, a NAS),")
    print("then each editor runs `cutshare whoami NAME` and `cutshare media-root /path/to/footage`.")
    return proj


def cmd_whoami(args):
    cfg = Config()
    if args.name:
        cfg.set_user(args.name)
    print(cfg.user)


def cmd_media_root(args):
    proj, cfg = _ctx(args)
    if args.path:
        raw = args.path.strip().replace("\\ ", " ")
        path = os.path.abspath(os.path.expanduser(raw)).rstrip("/")
        if not os.path.isdir(path):
            warn(f"note: {path} doesn't exist on this computer yet")
        cfg.set_media_root(proj.id, path)
    print(cfg.media_root(proj.id) or "(not set)")


def cmd_add(args):
    proj, cfg = _ctx(args)
    seg = proj.add_segment(args.name, order=args.order, user=cfg.user)
    print(f"Added segment {seg.order}: {seg.name}")
    if args.source:
        _submit(proj, cfg, seg, args.source, "first version", force=True, keep_lock=False)


def cmd_split(args):
    proj, cfg = _ctx(args)
    cuts = [parse_clock(t) for t in args.at.split(",") if t.strip()]
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    made, doc = split_into(proj, cfg, args.file, cuts, names, args.project_name)
    for seg, seg_start, dur in made:
        print(f"  {seg.order}. {seg.name:<20} starts {human(seg_start)}  length {human(dur)}")
    print(f"Split '{doc.name}' into {len(made)} segments. Editors can now claim one each.")


def split_into(proj, cfg, path, cuts, names, project_name=None):
    """Split an exported timeline into new segments, each with a version 1."""
    doc = _ingest(path, proj, cfg, project_name)
    pieces = fx.split(doc, cuts, names, proj.event_name, proj.name, warn=warn)
    media = _media_root_or_warn(cfg, proj, "splitting")
    start_order = max((s.order for s in proj.segments()), default=0)
    made = []
    for k, (name, root, seg_start) in enumerate(pieces, 1):
        seg = proj.add_segment(name, order=start_order + k, user=cfg.user)
        if media:
            fx.canonicalize_paths(root, media)
        n, out = seg.new_version_path(cfg.user)
        fx.write(root, out)
        d = fx.load(out)
        seg.record_version(n, out, cfg.user, f"split from {os.path.basename(path.rstrip('/'))}",
                           None, _stats(d))
        made.append((seg, seg_start, d.duration()))
    return made, doc


def _stats(doc):
    s = doc.stats()
    s["duration"] = float(s["duration"])
    return s


def cmd_status(args):
    proj, cfg = _ctx(args)
    segs = proj.segments()
    print(f"{proj.name}   (you are {cfg.user}; media folder: {cfg.media_root(proj.id) or 'not set'})")
    if not segs:
        print("No segments yet. Add some with `cutshare add NAME` or `cutshare split`.")
        return
    total = 0.0
    print(f"{'#':>2}  {'segment':<22} {'ver':>4}  {'length':>7}  {'last change':<28} editing now")
    for s in segs:
        v = s.latest()
        lock = s.lock()
        dur = v["stats"]["duration"] if v else 0
        total += dur
        who = f"{lock['user']} (since {lock['since'][5:16].replace('T', ' ')})" if lock else "-"
        last = f"{v['author']}, {v['time'][5:16].replace('T', ' ')}" if v else "no versions"
        print(f"{s.order:>2}  {s.name[:22]:<22} {('v' + str(v['number'])) if v else '-':>4}  "
              f"{human(dur):>7}  {last:<28} {who}")
    masters = proj.assembled()
    print(f"Total length {human(total)}. "
          + (f"Last master: v{masters[-1]['number']} by {masters[-1]['by']}." if masters else "Not assembled yet."))


def cmd_claim(args):
    proj, cfg = _ctx(args)
    seg = proj.segment(args.segment)
    seg.claim(cfg.user, force=args.force)
    print(f"You're editing {seg.name}. Others see it as locked until you submit or release.")


def cmd_release(args):
    proj, cfg = _ctx(args)
    seg = proj.segment(args.segment)
    print(f"Released {seg.name}" if seg.release(cfg.user, force=args.force) else f"{seg.name} wasn't locked")


def cmd_pull(args):
    proj, cfg = _ctx(args)
    seg = proj.segment(args.segment)
    rec = seg.version(args.version)
    out = args.output or f"{proj.name} - {seg.name} v{rec['number']:03d}.fcpxml"
    _export_for_me(seg.file_of(rec), out, proj, cfg)
    lock = seg.lock()
    print(f"Wrote {out}  (v{rec['number']} by {rec['author']})")
    print("In Final Cut Pro: File > Import > XML, pick this file and your library.")
    if not lock:
        print(f"Tip: run `cutshare claim {seg.slug}` before you start so nobody edits it at the same time.")
    elif lock["user"] != cfg.user:
        print(f"Heads up: {lock['user']} is editing this segment right now. Treat your copy as read-only.")


def _submit(proj, cfg, seg, path, message, force=False, keep_lock=False, project_name=None):
    lock = seg.lock()
    if lock and lock["user"] != cfg.user and not force:
        raise StoreError(f"{seg.name} is locked by {lock['user']}. Your edit wasn't saved. "
                         "Ask them to release it, or use --force.")
    doc = _ingest(path, proj, cfg, project_name)
    root = _store_root(doc, proj, cfg, seg.name)
    prev = seg.latest()
    n, out = seg.new_version_path(cfg.user)
    fx.write(root, out)
    new_doc = fx.load(out)
    summary = None
    if prev:
        lines, summary = diff(fx.load(seg.file_of(prev)), new_doc)
    rec = seg.record_version(n, out, cfg.user, message, summary, _stats(new_doc))
    print(f"Saved {seg.name} v{rec['number']}" + (f": {one_line(summary)}" if summary else ""))
    page = None
    if prev:
        page = out[:-len(".fcpxml")] + ".diff.html"
        viz.write_html(fx.load(seg.file_of(prev)), new_doc, page, f"{proj.name}: {seg.name}",
                       f"v{prev['number']} by {prev['author']}", f"v{n} by {cfg.user}")
        print(f"Visual diff: {page}")
    if lock and lock["user"] == cfg.user and not keep_lock:
        seg.release(cfg.user)
        print("Released your lock.")
    notify.post(proj, f"**{cfg.user}** saved **{seg.name}** v{rec['number']}"
                + (f": {one_line(summary)}" if summary else "")
                + (f"\n> {message}" if message else ""))
    return rec, page


def cmd_submit(args):
    proj, cfg = _ctx(args)
    seg = proj.segment(args.segment)
    if not seg.lock() and not args.force:
        warn(f"note: you didn't claim {seg.name} first; saving anyway since nobody else has it.")
    _submit(proj, cfg, seg, args.file, args.message or "", force=args.force,
            keep_lock=args.keep_lock, project_name=args.project_name)


def cmd_diff(args):
    if args.a and args.b and os.path.exists(args.a) and os.path.exists(args.b):
        a, b = fx.load(args.a), fx.load(args.b)
        title = f"{args.a} -> {args.b}"
    else:
        proj, cfg = _ctx(args)
        seg = proj.segment(args.a)
        vs = seg.versions()
        if len(vs) < 2 and args.frm is None:
            print(f"{seg.name} only has {len(vs)} version(s); nothing to compare yet.")
            return
        to = seg.version(args.to)
        frm = seg.version(args.frm) if args.frm is not None else \
            [v for v in vs if v["number"] < to["number"]][-1]
        a, b = fx.load(seg.file_of(frm)), fx.load(seg.file_of(to))
        title = f"{seg.name}: v{frm['number']} ({frm['author']}) -> v{to['number']} ({to['author']})"
        labels = (f"v{frm['number']} by {frm['author']}", f"v{to['number']} by {to['author']}")
    if args.html is not None or args.open:
        if not (args.a and args.b and os.path.exists(args.a)):
            out = args.html or f"{proj.name} - {seg.name} v{frm['number']}-v{to['number']} diff.html"
            page_title = f"{proj.name}: {seg.name}"
        else:
            out = args.html or "timeline diff.html"
            labels = (os.path.basename(args.a), os.path.basename(args.b))
            page_title = b.name or "Timeline diff"
        viz.write_html(a, b, out, page_title, *labels)
        print(f"Wrote visual diff: {out}")
        if args.open:
            _open(out)
        return
    lines, _ = diff(a, b)
    print(title)
    for line in lines:
        print("  " + line)


def _open(path):
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        print(f"(open {path} in your browser)")


def cmd_log(args):
    proj, cfg = _ctx(args)
    segs = [proj.segment(args.segment)] if args.segment else proj.segments()
    rows = []
    for s in segs:
        for v in s.versions():
            rows.append((v["time"], f"{s.name} v{v['number']}", v))
    for r in proj.assembled() if not args.segment else []:
        rows.append((r["time"], f"MASTER v{r['number']}", {"author": r["by"], "message": "assembled "
                     + ", ".join(f"{k} v{n}" for k, n in r["segments"].items()), "changes": None}))
    for t, what, v in sorted(rows):
        ch = f"  [{one_line(v['changes'])}]" if v.get("changes") else ""
        msg = f"  \"{v['message']}\"" if v.get("message") else ""
        print(f"{t[:16].replace('T', ' ')}  {what:<26} {v['author']:<10}{msg}{ch}")


INSTALL_URL = "https://raw.githubusercontent.com/ryanmarkgenova/cutshare/main/install.sh"


def cmd_update(args):
    print("Updating CutShare from GitHub...")
    return subprocess.call(["/bin/bash", "-c", f"curl -fsSL {INSTALL_URL} | bash"])


def cmd_discord(args):
    proj, cfg = _ctx(args)
    if args.off:
        proj.set_meta("discord_webhook", None)
        print("Discord pings are off.")
        return
    if args.url:
        if not args.url.startswith("https://discord.com/api/webhooks/"):
            raise StoreError("that doesn't look like a Discord webhook URL "
                             "(Server settings > Integrations > Webhooks > Copy URL)")
        proj.set_meta("discord_webhook", args.url)
        sent = notify.post(proj, f"CutShare is connected to **{proj.name}**. Submits and masters will show up here.")
        print("Discord pings are on." + ("" if sent else " (Test message didn't send; check the URL.)"))
        return
    print("on" if proj.meta.get("discord_webhook") else "off")


def cmd_assemble(args):
    proj, cfg = _ctx(args)
    out, n, used, dur = assemble_project(proj, cfg, args.output, args.allow_mixed)
    print(f"Assembled master v{n} from {len(used)} segments ({human(dur)}): "
          + ", ".join(f"{k} v{v}" for k, v in used.items()))
    print(f"Wrote {out}. In Final Cut Pro: File > Import > XML.")


def assemble_project(proj, cfg, output=None, allow_mixed=False):
    parts, used = [], {}
    for s in proj.segments():
        v = s.latest()
        if not v:
            warn(f"note: skipping {s.name} (no versions yet)")
            continue
        lock = s.lock()
        if lock:
            warn(f"note: {s.name} is still being edited by {lock['user']}; using their last submitted v{v['number']}")
        parts.append(fx.Segment(s.name, fx.load(s.file_of(v))))
        used[s.name] = v["number"]
    if not parts:
        raise StoreError("no segments have versions yet")
    n, master = proj.next_master_path()
    root = fx.assemble(parts, f"{proj.name} - master v{n:03d}", proj.event_name,
                       warn=warn, allow_mixed=allow_mixed)
    fx.write(root, master)
    proj.record_master(n, master, cfg.user, used)
    notify.post(proj, f"**{cfg.user}** assembled **{proj.name}** master v{n}: "
                + ", ".join(f"{k} v{v}" for k, v in used.items()))
    out = output or f"{proj.name} - master v{n:03d}.fcpxml"
    _export_for_me(master, out, proj, cfg)
    return out, n, used, fx.load(out).duration()


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="cutshare", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"cutshare {__version__}")
    p.add_argument("-p", "--project", help="path to the .cutshare folder (default: search from here)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a shared project folder")
    s.add_argument("name")
    s.add_argument("--dir", help="where to create it (default: here)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("whoami", help="show or set your name")
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("media-root", help="show or set where this project's footage lives on your Mac")
    s.add_argument("path", nargs="?")
    s.set_defaults(fn=cmd_media_root)

    s = sub.add_parser("add", help="add a segment (optionally with a first version)")
    s.add_argument("name")
    s.add_argument("--order", type=int)
    s.add_argument("--from", dest="source", help="FCPXML to use as version 1")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("split", help="split an existing timeline into segments at clip edges")
    s.add_argument("file")
    s.add_argument("--at", required=True, help="cut times, e.g. 0:45,2:10")
    s.add_argument("--names", required=True, help="segment names, e.g. Intro,Show,Outro")
    s.add_argument("--project-name", help="which project inside the file")
    s.set_defaults(fn=cmd_split)

    s = sub.add_parser("status", help="who is editing what")
    s.set_defaults(fn=cmd_status)

    for name, fn, hlp in (("claim", cmd_claim, "lock a segment so only you edit it"),
                          ("release", cmd_release, "unlock a segment without submitting")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("segment")
        s.add_argument("--force", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("pull", help="write a segment's latest version for import into FCP")
    s.add_argument("segment")
    s.add_argument("-o", "--output")
    s.add_argument("--version", type=int)
    s.set_defaults(fn=cmd_pull)

    s = sub.add_parser("submit", help="save your exported FCPXML as the segment's next version")
    s.add_argument("segment")
    s.add_argument("file")
    s.add_argument("-m", "--message")
    s.add_argument("--keep-lock", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--project-name", help="which project inside the file")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("diff", help="what changed (segment versions, or two files)")
    s.add_argument("a", help="segment, or first file")
    s.add_argument("b", nargs="?", help="second file")
    s.add_argument("--from", dest="frm", type=int)
    s.add_argument("--to", type=int)
    s.add_argument("--html", nargs="?", const="", default=None,
                   help="write a visual diff page (optionally give a file name)")
    s.add_argument("--open", action="store_true", help="write the visual diff and open it")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("log", help="history of versions and masters")
    s.add_argument("segment", nargs="?")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("update", help="update CutShare to the latest version")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("discord", help="post submits and masters to a Discord channel (optional)")
    s.add_argument("url", nargs="?", help="the channel's webhook URL")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_discord)

    s = sub.add_parser("ui", help="Mac dialogs, used by the Finder Quick Actions")
    s.add_argument("action", choices=["new", "submit", "pull", "assemble", "status"])
    s.add_argument("paths", nargs="*")
    s.set_defaults(fn=lambda a: __import__("cutshare.macui", fromlist=["run"]).run(a.action, a.paths, a.project))

    s = sub.add_parser("assemble", help="stitch the latest of every segment into one master timeline")
    s.add_argument("-o", "--output")
    s.add_argument("--allow-mixed", action="store_true", help="allow different frame sizes/rates")
    s.set_defaults(fn=cmd_assemble)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (StoreError, fx.FCPXMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
