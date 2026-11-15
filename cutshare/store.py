"""The shared project folder.

Everything lives in plain files inside one folder you put on a shared drive
(Google Drive, Dropbox, OneDrive, a NAS, or a git repo). Each version,
lock and segment is its own small file, so two people syncing at once
never overwrite each other's file.

  My Video.cutshare/
    cutshare.json                    project id + name
    segments/01-intro/segment.json   name, order
    segments/01-intro/lock.json      who is editing it right now
    segments/01-intro/versions/v003_ryan.fcpxml  (+ .json notes)
    assembled/master_v002.fcpxml     stitched full timelines
"""
import datetime
import getpass
import json
import os
import re
import socket
import uuid

PROJECT_FILE = "cutshare.json"


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "segment"


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


class StoreError(Exception):
    pass


# ---------------------------------------------------------------- local config

class Config:
    """Per-computer settings: your name and where your media lives."""

    def __init__(self):
        self.path = os.environ.get("CUTSHARE_CONFIG") or os.path.expanduser("~/.cutshare/config.json")
        self.data = _read(self.path, {}) or {}

    @property
    def user(self):
        return os.environ.get("CUTSHARE_USER") or self.data.get("user") or getpass.getuser()

    def set_user(self, name):
        self.data["user"] = name
        self.save()

    def media_root(self, project_id):
        return self.data.get("media_roots", {}).get(project_id)

    def set_media_root(self, project_id, path):
        self.data.setdefault("media_roots", {})[project_id] = path
        self.save()

    def save(self):
        _write(self.path, self.data)


# ---------------------------------------------------------------- project

class Project:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        meta = _read(os.path.join(self.path, PROJECT_FILE))
        if meta is None:
            raise StoreError(f"{path} is not a CutShare project (no {PROJECT_FILE})")
        self.meta = meta

    @property
    def id(self):
        return self.meta["id"]

    @property
    def name(self):
        return self.meta["name"]

    @classmethod
    def init(cls, path, name):
        if os.path.exists(os.path.join(path, PROJECT_FILE)):
            raise StoreError(f"{path} is already a CutShare project")
        os.makedirs(os.path.join(path, "segments"), exist_ok=True)
        _write(os.path.join(path, PROJECT_FILE),
               {"id": uuid.uuid4().hex[:12], "name": name, "created": now(), "tool": "cutshare/1"})
        return cls(path)

    @classmethod
    def find(cls, explicit=None):
        if explicit:
            return cls(explicit)
        env = os.environ.get("CUTSHARE_PROJECT")
        if env:
            return cls(env)
        found = cls.find_from(os.getcwd())
        if found is None:
            raise StoreError("no CutShare project here. cd into the shared folder, "
                             "pass -p PATH, or run `cutshare init`.")
        return found

    @classmethod
    def find_from(cls, start):
        """Look in start, its .cutshare children, then each parent. None if nothing."""
        d = os.path.abspath(start)
        if os.path.isfile(d):
            d = os.path.dirname(d)
        while True:
            if os.path.exists(os.path.join(d, PROJECT_FILE)):
                return cls(d)
            try:
                entries = os.listdir(d)
            except OSError:
                entries = []
            for entry in sorted(entries):
                if entry.endswith(".cutshare") and os.path.exists(os.path.join(d, entry, PROJECT_FILE)):
                    return cls(os.path.join(d, entry))
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent

    def set_meta(self, key, value):
        if value is None:
            self.meta.pop(key, None)
        else:
            self.meta[key] = value
        _write(os.path.join(self.path, PROJECT_FILE), self.meta)

    @property
    def event_name(self):
        return f"CutShare - {self.name}"

    def segments(self):
        root = os.path.join(self.path, "segments")
        out = []
        for entry in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            p = os.path.join(root, entry)
            if os.path.exists(os.path.join(p, "segment.json")):
                out.append(Segment(self, p))
        return sorted(out, key=lambda s: (s.order, s.name))

    def segment(self, query):
        q = str(query).strip().lower()
        segs = self.segments()
        for s in segs:
            if q in (s.slug, s.name.lower(), str(s.order)):
                return s
        hits = [s for s in segs if q in s.slug or q in s.name.lower()]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(f"{s.order}:{s.name}" for s in segs) or "none yet"
        raise StoreError(f"no segment matches '{query}' (segments: {names})")

    def add_segment(self, name, order=None, user=None):
        segs = self.segments()
        if any(s.name.lower() == name.lower() for s in segs):
            raise StoreError(f"segment '{name}' already exists")
        if order is None:
            order = max((s.order for s in segs), default=0) + 1
        slug = f"{order:02d}-{slugify(name)}"
        path = os.path.join(self.path, "segments", slug)
        _write(os.path.join(path, "segment.json"),
               {"name": name, "order": order, "created": now(), "created_by": user})
        os.makedirs(os.path.join(path, "versions"), exist_ok=True)
        return Segment(self, path)

    # assembled masters
    def assembled(self):
        d = os.path.join(self.path, "assembled")
        if not os.path.isdir(d):
            return []
        recs = [_read(os.path.join(d, f)) for f in os.listdir(d) if f.endswith(".json")]
        return sorted([r for r in recs if r], key=lambda r: r["number"])

    def next_master_path(self):
        n = max((r["number"] for r in self.assembled()), default=0) + 1
        d = os.path.join(self.path, "assembled")
        os.makedirs(d, exist_ok=True)
        return n, os.path.join(d, f"master_v{n:03d}.fcpxml")

    def record_master(self, n, path, user, used):
        _write(path[:-len(".fcpxml")] + ".json",
               {"number": n, "file": os.path.basename(path), "by": user, "time": now(), "segments": used})


class Segment:
    def __init__(self, project, path):
        self.project = project
        self.path = path
        self.meta = _read(os.path.join(path, "segment.json"), {})

    @property
    def name(self):
        return self.meta.get("name", os.path.basename(self.path))

    @property
    def order(self):
        return int(self.meta.get("order", 0))

    @property
    def slug(self):
        return os.path.basename(self.path)

    # -- locks
    @property
    def lock_path(self):
        return os.path.join(self.path, "lock.json")

    def lock(self):
        return _read(self.lock_path)

    def claim(self, user, force=False):
        cur = self.lock()
        if cur and cur["user"] != user and not force:
            raise StoreError(f"{self.name} is being edited by {cur['user']} since {cur['since']}. "
                             "Ask them to release it (or use --force if they're done).")
        _write(self.lock_path, {"user": user, "host": socket.gethostname(), "since": now()})

    def release(self, user, force=False):
        cur = self.lock()
        if not cur:
            return False
        if cur["user"] != user and not force:
            raise StoreError(f"{self.name} is locked by {cur['user']}, not you (use --force to break it)")
        os.remove(self.lock_path)
        return True

    # -- versions
    @property
    def versions_dir(self):
        return os.path.join(self.path, "versions")

    def versions(self):
        if not os.path.isdir(self.versions_dir):
            return []
        recs = []
        for f in os.listdir(self.versions_dir):
            if f.endswith(".json"):
                r = _read(os.path.join(self.versions_dir, f))
                if r and os.path.exists(os.path.join(self.versions_dir, r["file"])):
                    recs.append(r)
        return sorted(recs, key=lambda r: (r["number"], r["time"]))

    def latest(self):
        v = self.versions()
        return v[-1] if v else None

    def version(self, number=None):
        vs = self.versions()
        if not vs:
            raise StoreError(f"{self.name} has no versions yet")
        if number is None:
            return vs[-1]
        for r in vs:
            if r["number"] == int(number):
                return r
        raise StoreError(f"{self.name} has no version {number} (latest is {vs[-1]['number']})")

    def file_of(self, rec):
        return os.path.join(self.versions_dir, rec["file"])

    def new_version_path(self, user):
        n = max((r["number"] for r in self.versions()), default=0) + 1
        os.makedirs(self.versions_dir, exist_ok=True)
        return n, os.path.join(self.versions_dir, f"v{n:03d}_{slugify(user)}.fcpxml")

    def record_version(self, n, path, user, message, summary, stats):
        rec = {"number": n, "file": os.path.basename(path), "author": user, "time": now(),
               "message": message, "changes": summary, "stats": stats}
        _write(path[:-len(".fcpxml")] + ".json", rec)
        return rec
