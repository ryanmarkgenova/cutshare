"""End-to-end and unit tests. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from cutshare import fcpxml as fx  # noqa: E402
from cutshare import viz  # noqa: E402
from cutshare.cli import main  # noqa: E402
from cutshare.diff import diff  # noqa: E402
from cutshare.timecode import fmt_time, parse_clock, parse_time  # noqa: E402
import samples  # noqa: E402

RYAN = "/Users/ryan/Movies/FASA Show"
ALAN = "/Volumes/Alan SSD/FASA"
MIA = "/Users/mia/Desktop/fasa footage"

MASTER = [("Arrival", 0, 150), ("Crowd", 300, 150), ("Dance_A", 0, 300),
          ("Dance_B", 600, 300), ("Crowd", 1200, 120), ("Bows", 0, 240)]


def check_integrity(tc, root):
    """Things FCP's importer insists on."""
    ids = [el.get("id") for el in root.iter() if el.get("id")]
    tc.assertEqual(len(ids), len(set(ids)), f"duplicate ids: {sorted(ids)}")
    res_ids = {el.get("id") for el in root.find("resources")}
    style_ids = {el.get("id") for el in root.iter("text-style-def")}
    for el in root.iter():
        for a in ("ref", "format"):
            v = el.get(a)
            if v and el.tag != "text-style":
                tc.assertIn(v, res_ids, f"<{el.tag} {a}={v}> points at nothing")
        if el.tag == "text-style" and el.get("ref"):
            tc.assertIn(el.get("ref"), style_ids)
    seq = root.find(".//sequence")
    spine = seq.find("spine")
    pos = parse_time(seq.get("tcStart"))
    fmt = next(f for f in root.find("resources") if f.get("id") == seq.get("format"))
    fd = parse_time(fmt.get("frameDuration"))
    for el in spine:
        off = parse_time(el.get("offset"))
        tc.assertEqual((off / fd).denominator, 1, "offset off a frame boundary")
        if el.tag == "transition":
            continue
        tc.assertEqual(off, pos, f"gap or overlap at {el.get('name')}")
        pos += parse_time(el.get("duration"))
    tc.assertEqual(parse_time(seq.get("duration")), pos - parse_time(seq.get("tcStart")))


class Team:
    """Simulates several editors, each with their own Mac config."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.cwd = os.path.join(tmp, "shared")
        os.makedirs(self.cwd)

    def run(self, user, *argv, ok=True):
        os.environ["CUTSHARE_CONFIG"] = os.path.join(self.tmp, f"{user}.json")
        os.environ["CUTSHARE_USER"] = user
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(self.cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        finally:
            os.chdir(old)
        if ok and code != 0:
            raise AssertionError(f"{user}: cutshare {' '.join(argv)} failed:\n{err.getvalue()}")
        if not ok and code == 0:
            raise AssertionError(f"{user}: cutshare {' '.join(argv)} should have failed")
        return out.getvalue() + err.getvalue()

    def path(self, *p):
        return os.path.join(self.cwd, *p)

    def write(self, name, text):
        with open(self.path(name), "w") as f:
            f.write(text)
        return self.path(name)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.team = Team(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        for k in ("CUTSHARE_CONFIG", "CUTSHARE_USER"):
            os.environ.pop(k, None)

    def test_three_editors(self):
        T = self.team
        T.run("ryan", "init", "FASA Recap")
        T.run("ryan", "media-root", RYAN)
        T.run("alan", "media-root", ALAN)
        T.run("mia", "media-root", MIA)
        T.write("master.fcpxml", samples.project(
            MASTER, root=RYAN, titles={0: "FASA 2026", 3: "Dance B"}, transition_after=2))

        out = T.run("ryan", "split", "master.fcpxml", "--at", "0:10,0:33",
                    "--names", "Intro,Performances,Outro")
        self.assertIn("Split 'FASA Recap' into 3 segments", out)
        status = T.run("ryan", "status")
        self.assertIn("Performances", status)

        # stored copies never contain anyone's personal paths
        with open(T.path("FASA Recap.cutshare", "segments", "02-performances", "versions",
                         "v001_ryan.fcpxml")) as f:
            stored = f.read()
        self.assertNotIn("/Users/ryan", stored)
        self.assertIn(fx.MEDIA_TOKEN, stored)
        self.assertNotIn("<bookmark>", stored)

        # Alan takes Performances and gets paths for HIS drive
        T.run("alan", "claim", "performances")
        T.run("alan", "pull", "performances", "-o", "alan_perf.fcpxml")
        pulled = fx.load(T.path("alan_perf.fcpxml"))
        srcs = [mr.get("src") for mr in pulled.root.iter("media-rep")]
        self.assertTrue(srcs and all(s.startswith("file:///Volumes/Alan%20SSD/FASA/") for s in srcs), srcs)
        check_integrity(self, pulled.root)

        # Mia can't grab it while Alan has it
        err = T.run("mia", "claim", "performances", ok=False)
        self.assertIn("being edited by alan", err)

        # Alan edits in FCP: trims Dance_A, drops the Crowd shot, adds Dance_C, exports
        alan_edit = T.write("alan_export.fcpxml", samples.project(
            [("Dance_A", 30, 240), ("Dance_B", 600, 300), ("Dance_C", 0, 200)],
            root=ALAN, titles={1: "Dance B"}, project_uid="ALAN-PROJ"))
        out = T.run("alan", "submit", "performances", alan_edit, "-m", "tightened dances")
        self.assertIn("Saved Performances v2", out)
        self.assertIn("Visual diff:", out)
        T.run("ryan", "diff", "performances", "--html", "perf.html")
        with open(T.path("perf.html")) as f:
            page = f.read()
        self.assertIn("Zoom to changes", page)
        self.assertIn("Dance_C", page)
        self.assertIn("Released your lock", out)

        d = T.run("ryan", "diff", "performances")
        self.assertIn('Trimmed "Dance_A"', d)
        self.assertIn('Added "Dance_C"', d)
        self.assertIn('Removed "Crowd"', d)

        # Mia does the outro at the same time on her own Mac
        T.run("mia", "claim", "outro")
        mia_edit = T.write("mia_export.fcpxml", samples.project(
            [("Bows", 0, 200), ("Arrival", 0, 60)], root=MIA, titles={1: "Thanks for coming"},
            project_uid="MIA-PROJ"))
        T.run("mia", "submit", "outro", mia_edit, "-m", "added thank-you card")

        log = T.run("ryan", "log")
        self.assertIn("tightened dances", log)

        out = T.run("ryan", "assemble", "-o", "master_out.fcpxml")
        self.assertIn("Assembled master v1 from 3 segments", out)
        m = fx.load(T.path("master_out.fcpxml"))
        check_integrity(self, m.root)
        names = [el.get("name") for el in m.items() if el.tag != "transition"]
        self.assertEqual(names, ["Arrival", "Crowd", "Dance_A", "Dance_B", "Dance_C", "Bows", "Arrival"])
        # same footage used by two editors is listed once
        assets = [a.get("name") for a in m.root.find("resources").iter("asset")]
        self.assertEqual(len(assets), len(set(assets)), assets)
        # paths are Ryan's again
        for mr in m.root.iter("media-rep"):
            self.assertTrue(mr.get("src").startswith("file:///Users/ryan/Movies/FASA%20Show/"))
        expected = (150 + 150) + (240 + 300 + 200) + (200 + 60)
        self.assertEqual(m.duration(), expected * Fraction(1001, 30000))
        titles = [fx.title_text(t) for t in m.spine.iter("title")]
        self.assertEqual(titles, ["FASA 2026", "Dance B", "Thanks for coming"])

        # Ryan re-imports, someone updates the outro, assemble again -> master v2
        T.run("mia", "submit", "outro", mia_edit, "-m", "same")
        out = T.run("ryan", "assemble", "-o", "master2.fcpxml")
        self.assertIn("master v2", out)

    def test_submit_blocked_by_other_lock(self):
        T = self.team
        T.run("ryan", "init", "Clip")
        T.run("ryan", "add", "Intro")
        T.run("ryan", "claim", "intro")
        f = T.write("x.fcpxml", samples.project(MASTER[:2]))
        err = T.run("alan", "submit", "intro", f, ok=False)
        self.assertIn("locked by ryan", err)
        T.run("alan", "submit", "intro", f, "--force")


class DiscordPings(unittest.TestCase):
    def test_posts_only_when_configured(self):
        from cutshare import notify
        sent = []

        class P:
            meta = {}
        self.assertFalse(notify.post(P, "hi", opener=lambda req, timeout: sent.append(req)))
        P.meta = {"discord_webhook": "https://discord.com/api/webhooks/1/x"}
        self.assertTrue(notify.post(P, "Alan saved B v3", opener=lambda req, timeout: sent.append(req)))
        self.assertIn(b"Alan saved B v3", sent[0].data)

        def boom(req, timeout):
            raise OSError("offline")
        self.assertFalse(notify.post(P, "x", opener=boom))  # never blocks an edit

    def test_cli_rejects_bad_url(self):
        with tempfile.TemporaryDirectory() as d:
            T = Team(d)
            T.run("ryan", "init", "X")
            self.assertIn("doesn't look like", T.run("ryan", "discord", "https://example.com", ok=False))
            T.run("ryan", "discord", "--off")


class Units(unittest.TestCase):
    def test_time(self):
        self.assertEqual(parse_time("1001/30000s"), Fraction(1001, 30000))
        self.assertEqual(parse_time("5s"), 5)
        self.assertEqual(fmt_time(Fraction(3003, 30000), 30000), "3003/30000s")
        self.assertEqual(fmt_time(Fraction(60), 30000), "60s")
        self.assertEqual(parse_clock("1:05.5"), Fraction(131, 2))

    def test_split_snaps_to_clip_edges_and_handles_transitions(self):
        doc = fx.loads(samples.project(MASTER, transition_after=1))
        notes = []
        pieces = fx.split(doc, [Fraction(9)], ["A", "B"], "E", "P", warn=notes.append)
        a, b = [fx.Doc(r) for _, r, _ in pieces]
        self.assertEqual(len(a.items()), 2)  # Arrival, Crowd
        # the dissolve sat right on the cut, so it was dropped with a note
        self.assertTrue(any("transition" in n for n in notes), notes)
        self.assertEqual(a.duration() + b.duration(), doc.duration())
        for _, r, _ in pieces:
            check_integrity(self, r)

    def test_mixed_formats_refused(self):
        a = fx.loads(samples.project(MASTER[:2]))
        b_text = samples.project(MASTER[2:3]).replace('width="1920" height="1080"', 'width="1080" height="1920"')
        b = fx.loads(b_text)
        with self.assertRaises(fx.FCPXMLError):
            fx.assemble([fx.Segment("a", a), fx.Segment("b", b)], "P", "E", warn=lambda m: None)

    def test_bundle_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            bundle = os.path.join(d, "Thing.fcpxmld")
            os.makedirs(bundle)
            with open(os.path.join(bundle, "Info.fcpxml"), "w") as f:
                f.write(samples.project(MASTER[:2]))
            self.assertEqual(len(fx.load(bundle).items()), 2)
            bad = os.path.join(d, "events_only.fcpxml")
            with open(bad, "w") as f:
                f.write('<fcpxml version="1.13"><resources/><event name="x"/></fcpxml>')
            with self.assertRaises(fx.FCPXMLError):
                fx.load(bad)

    def test_diff_reorder(self):
        a = fx.loads(samples.project(MASTER))
        b = fx.loads(samples.project([MASTER[i] for i in (0, 1, 3, 2, 4, 5)]))
        lines, s = diff(a, b)
        self.assertEqual(s.get("moved"), 1, lines)
        self.assertFalse(s.get("added"))

    def test_paths_roundtrip(self):
        root = ET.fromstring(samples.project(MASTER[:1], root=RYAN))
        self.assertEqual(fx.canonicalize_paths(root, RYAN), (1, 0))
        fx.localize_paths(root, MIA)
        src = next(root.iter("media-rep")).get("src")
        self.assertEqual(src, "file:///Users/mia/Desktop/fasa%20footage/Footage/Arrival.mov")


FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class RealFCPExports(unittest.TestCase):
    """Exports from a real Final Cut Pro 12 project (59.94p, 11 lanes, compound
    clips, titles, adjustment clips, retimes, keyframed masks)."""

    def setUp(self):
        self.a = fx.load(os.path.join(FIX, "real_A.fcpxml"))
        self.b = fx.load(os.path.join(FIX, "real_A_edit.fcpxml"))

    def test_visual_diff_finds_the_real_edit(self):
        ba, bb = viz.compare(self.a, self.b)
        removed = [x["name"] for x in ba if x["status"] == "removed"]
        trimmed = [y["name"] for y in bb if y["status"] == "trimmed"]
        self.assertEqual(sorted(removed), ["C0686"] + ["ktpfifa- a short film Clip 2"] * 3)
        self.assertEqual(trimmed, ["C0686"])
        self.assertFalse([y for y in bb if y["status"] in ("added", "moved")])
        self.assertIn("Zoom to changes", viz.render(self.a, self.b, "t", "a", "b"))

    def test_split_and_assemble_roundtrip(self):
        start, end = self.a.span()
        pieces = fx.split(self.a, [Fraction(10), Fraction(20)], ["x", "y", "z"], "E", "P",
                          warn=lambda m: None)
        segs = [fx.Segment(n, fx.Doc(r)) for n, r, _ in pieces]
        for s in segs:
            check_integrity(self, s.doc.root)
        merged = fx.Doc(fx.assemble(segs, "P", "E", warn=lambda m: None))
        check_integrity(self, merged.root)
        self.assertEqual(merged.duration(), self.a.duration())
        ba, bb = viz.compare(self.a, merged)
        self.assertFalse([x for x in ba + bb if x["status"] != "same"])


if __name__ == "__main__":
    unittest.main()


class FakeDialogs:
    """Scripted answers instead of real macOS dialogs."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen, self.opened, self.alerts, self.notes = [], [], [], []

    def _next(self, kind, prompt, options=None, default=None):
        self.seen.append((kind, prompt, options, default))
        return self.answers.pop(0)

    def choose(self, prompt, items, default=None):
        ans = self._next("choose", prompt, items, default)
        return next((i for i in items if i.startswith(ans)), None) if ans else None

    def ask(self, prompt, default=""):
        return self._next("ask", prompt)

    def buttons(self, prompt, names):
        return self._next("buttons", prompt, names)

    def choose_folder(self, prompt):
        return self._next("folder", prompt)

    def alert(self, title, message):
        self.alerts.append((title, message))

    def notify(self, title, message):
        self.notes.append((title, message))

    def open(self, path, app=None, reveal=False):
        self.opened.append((path, app, reveal))


class MacQuickActions(unittest.TestCase):
    def setUp(self):
        from cutshare import macui
        self.macui = macui
        self._tmp = tempfile.TemporaryDirectory()
        self.team = Team(self._tmp.name)
        macui.OUT_ROOT = os.path.join(self._tmp.name, "Movies")
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name  # keep ui.log out of the real home

    def tearDown(self):
        self._tmp.cleanup()
        os.environ["HOME"] = self._home
        for k in ("CUTSHARE_CONFIG", "CUTSHARE_USER"):
            os.environ.pop(k, None)

    def as_user(self, user):
        os.environ["CUTSHARE_CONFIG"] = os.path.join(self._tmp.name, f"{user}.json")
        os.environ["CUTSHARE_USER"] = user

    def test_no_terminal_loop(self):
        T = self.team
        T.run("ryan", "init", "FASA Recap")
        T.run("ryan", "media-root", RYAN)
        T.write("master.fcpxml", samples.project(MASTER, root=RYAN))
        T.run("ryan", "split", "master.fcpxml", "--at", "0:10,0:33", "--names", "Intro,Performances,Outro")
        proj = T.path("FASA Recap.cutshare")

        # Alan's first time: right-clicks the project folder > Open a segment
        self.as_user("alan")
        d = FakeDialogs([ALAN, "2.", "Lock it for me"])
        self.assertEqual(self.macui.run("pull", [proj], dlg=d), 0, d.alerts)
        out, app, _ = d.opened[0]
        self.assertEqual(app, "Final Cut Pro")
        self.assertTrue(os.path.exists(out))
        self.assertIn("Locked for you", d.notes[0][1])

        # exports from FCP to his Desktop, right-clicks it > Submit (project remembered)
        export = T.write("Performances edit.fcpxml", samples.project(
            [("Dance_A", 30, 240), ("Dance_B", 600, 300)], root=ALAN, name="FASA Recap - Performances"))
        d = FakeDialogs(["2.", "tightened the dances"])
        self.assertEqual(self.macui.run("submit", [export], dlg=d), 0, d.alerts)
        self.assertEqual(d.seen[0][3].split("  ")[0], "2. Performances")  # guessed his locked segment
        self.assertTrue(d.opened[0][0].endswith(".diff.html"))
        self.assertIn("Saved Performances v2", d.notes[0][0])

        # Mia tries to submit over a lock she doesn't hold and backs out
        self.as_user("mia")
        T.run("mia", "media-root", MIA)
        self.as_user("alan")
        self.macui.run("pull", [proj], dlg=FakeDialogs(["2.", "Lock it for me"]))
        self.as_user("mia")
        d = FakeDialogs(["2.", "note", "Cancel"])
        self.macui.run("submit", [export], explicit=proj, dlg=d)
        self.assertEqual(len(d.notes), 0)

        # Ryan assembles from the folder and opens it in FCP
        self.as_user("ryan")
        d = FakeDialogs(["Open in Final Cut Pro"])
        self.assertEqual(self.macui.run("assemble", [proj], dlg=d), 0, d.alerts)
        self.assertIn("Master v1 is ready", d.seen[0][1])
        self.assertIn("Performances is still being edited by alan", d.seen[0][1])
        self.assertTrue(d.opened[0][0].endswith("master v001.fcpxml"))

    def test_new_project_from_export(self):
        self.as_user("ryan")
        T = self.team
        export = T.write("rough cut.fcpxml", samples.project(MASTER, root=RYAN, name="FASA Recap"))
        drive = os.path.join(self._tmp.name, "Google Drive")
        os.makedirs(drive)
        d = FakeDialogs(["FASA Recap", drive, "0:10, 0:33", "Intro, Performances, Outro",
                         "Yes, use it", "Show in Finder"])
        self.assertEqual(self.macui.run("new", [export], dlg=d), 0, d.alerts)
        self.assertEqual(d.seen[2][1].split(" long")[0], "Your cut is 0:42.0")
        proj = os.path.join(drive, "FASA Recap.cutshare")
        self.assertTrue(os.path.exists(os.path.join(proj, "cutshare.json")))
        self.assertIn(RYAN + "/Footage", d.seen[4][1])          # footage folder guessed from the clips
        self.assertIn("Created FASA Recap with 3 segments", d.seen[5][1])
        self.assertEqual(d.opened, [(proj, None, True)])
        # editors can now pick it up straight away
        d = FakeDialogs(["1.", "Just look"])
        self.assertEqual(self.macui.run("pull", [proj], dlg=d), 0, d.alerts)

    def test_new_project_name_count_mismatch(self):
        self.as_user("ryan")
        export = self.team.write("x.fcpxml", samples.project(MASTER, root=RYAN))
        drive = os.path.join(self._tmp.name, "Drive")
        os.makedirs(drive)
        d = FakeDialogs(["X", drive, "0:10", "Only one name"])
        self.macui.run("new", [export], dlg=d)
        self.assertIn("2 segments", d.alerts[0][1])

    def test_errors_become_alerts(self):
        self.as_user("ryan")
        d = FakeDialogs([])
        self.assertEqual(self.macui.run("submit", ["/nope/movie.mov"], dlg=d), 0)
        self.assertEqual(d.alerts[0][0], "Nothing to submit")

    def test_quick_action_bundles(self):
        import plistlib
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "macos"))
        import make_quick_actions as mqa
        launcher = "/Users/x/Library/Application Support/CutShare/bin/cutshare"
        made = mqa.write_all(launcher, self._tmp.name)
        with open(os.path.join(os.path.dirname(__file__), "..", "install.sh")) as f:
            installer = f.read()
        for title in mqa.menu_titles():  # installer switches on exactly these
            self.assertIn(f'"{title}"', installer)
        self.assertEqual(len(made), 5)
        for b in made:
            with open(os.path.join(b, "Contents", "document.wflow"), "rb") as f:
                doc = plistlib.load(f)
            cmd = doc["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
            self.assertIn(f'"{launcher}" ui', cmd)
            with open(os.path.join(b, "Contents", "Info.plist"), "rb") as f:
                self.assertEqual(plistlib.load(f)["NSServices"][0]["NSMessage"], "runWorkflowAsService")
