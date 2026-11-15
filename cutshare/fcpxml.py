"""Reading, splitting, merging and rewriting FCPXML timelines.

Only the stdlib is used so this runs on any Mac with Python 3.9+.
Works on what Final Cut Pro writes with File > Export XML (.fcpxml or
.fcpxmld bundles) and what DaVinci Resolve writes as FCPXML.
"""
import copy
import os
import posixpath
import xml.etree.ElementTree as ET
from fractions import Fraction
from urllib.parse import quote, unquote, urlparse

from .timecode import fmt_time, parse_time

# Attributes that point at a resource id.
REF_ATTRS = ("ref", "format")
# Stored copies use this placeholder in place of each person's media folder.
MEDIA_TOKEN = "file:///__CUTSHARE_MEDIA__/"
RESOURCE_ORDER = ("format", "asset", "effect", "locator", "media")
# Elements whose text is user content; never touch whitespace inside them.
TEXTY = {"title", "text", "text-style", "note", "caption", "marker", "keyword", "metadata", "md"}


class FCPXMLError(Exception):
    pass


# ---------------------------------------------------------------- loading

def load(path, project_name=None):
    real = path
    if os.path.isdir(path):  # .fcpxmld bundle (FCP 10.6+)
        real = os.path.join(path, "Info.fcpxml")
        if not os.path.exists(real):
            raise FCPXMLError(f"{path}: bundle has no Info.fcpxml inside")
    try:
        root = ET.parse(real).getroot()
    except ET.ParseError as e:
        raise FCPXMLError(f"{path}: not valid XML ({e})")
    except FileNotFoundError:
        raise FCPXMLError(f"{path}: file not found")
    if root.tag != "fcpxml":
        raise FCPXMLError(f"{path}: not an FCPXML file (root is <{root.tag}>)")
    return Doc(root, source=path, project_name=project_name)


def loads(text, project_name=None):
    return Doc(ET.fromstring(text), source="<string>", project_name=project_name)


class Doc:
    """One FCPXML document focused on one project (timeline)."""

    def __init__(self, root, source="", project_name=None):
        self.root = root
        self.source = source
        self.version = root.get("version", "1.11")
        self.resources = root.find("resources")
        if self.resources is None:
            self.resources = ET.Element("resources")
            root.insert(0, self.resources)
        self.res = {el.get("id"): el for el in self.resources if el.get("id")}

        projects = [p for p in root.iter("project") if p.find("sequence") is not None]
        if project_name:
            projects = [p for p in projects if p.get("name") == project_name]
        if not projects:
            raise FCPXMLError(
                f"{source}: no project timeline found. In Final Cut Pro, select the "
                "project in the browser, then File > Export XML.")
        self.other_projects = len(projects) - 1
        self.project = projects[0]
        self.sequence = self.project.find("sequence")
        self.spine = self.sequence.find("spine")
        if self.spine is None:
            self.spine = ET.SubElement(self.sequence, "spine")

        fmt = self.res.get(self.sequence.get("format"))
        self.format_el = fmt
        fd = fmt.get("frameDuration") if fmt is not None else None
        self.frame_duration = parse_time(fd) if fd else None
        self.base_den = None
        if fd and "/" in fd:
            self.base_den = int(fd.rstrip("s").split("/")[1])
        elif self.frame_duration:
            self.base_den = self.frame_duration.denominator
        self.tc_start = parse_time(self.sequence.get("tcStart"))

    # -- basic facts
    @property
    def name(self):
        return self.project.get("name", "")

    def items(self):
        return list(self.spine)

    def span(self, items=None):
        items = self.items() if items is None else items
        real = [i for i in items if i.tag != "transition"]
        if not real:
            return self.tc_start, self.tc_start
        start = min(parse_time(i.get("offset")) for i in real)
        end = max(parse_time(i.get("offset")) + parse_time(i.get("duration")) for i in real)
        return start, end

    def duration(self):
        s, e = self.span()
        return e - s

    def format_signature(self):
        f = self.format_el
        if f is None:
            return None
        return (f.get("frameDuration"), f.get("width"), f.get("height"))

    def stats(self):
        items = self.items()
        connected = sum(1 for top in items for d in top.iter() if d is not top and d.get("lane"))
        return {
            "clips": sum(1 for i in items if i.tag not in ("transition", "gap")),
            "connected": connected,
            "titles": sum(1 for _ in self.spine.iter("title")),
            "markers": sum(1 for d in self.spine.iter() if d.tag in ("marker", "chapter-marker")),
            "transitions": sum(1 for i in items if i.tag == "transition"),
            "duration": self.duration(),
        }

    # -- describing a clip for diffs and status
    def media_src(self, asset):
        if asset is None:
            return None
        mr = asset.find("media-rep")
        if mr is not None and mr.get("src"):
            return mr.get("src")
        return asset.get("src")

    def identity(self, el):
        tag = el.tag
        ref = el.get("ref")
        if tag in ("asset-clip", "video", "audio"):
            return "asset:" + self._asset_label(self.res.get(ref))
        if tag == "clip":
            inner = next((c for c in el if c.tag in ("video", "audio") and c.get("ref")), None)
            if inner is not None:
                return "asset:" + self._asset_label(self.res.get(inner.get("ref")))
            return "clip:" + el.get("name", "")
        if tag == "ref-clip":
            m = self.res.get(ref)
            return "compound:" + (m.get("name", "") if m is not None else el.get("name", ""))
        if tag == "mc-clip":
            m = self.res.get(ref)
            return "multicam:" + (m.get("name", "") if m is not None else el.get("name", ""))
        if tag == "title":
            return "title:" + title_text(el)[:40]
        if tag == "gap":
            return "gap"
        return f"{tag}:{el.get('name', '')}"

    def _asset_label(self, asset):
        if asset is None:
            return "?"
        src = self.media_src(asset)
        if src:
            return posixpath.basename(unquote(urlparse(src).path))
        return asset.get("name", "?")

    def label(self, el):
        if el.tag == "title":
            return f'title "{title_text(el)[:30]}"'
        if el.tag == "gap":
            return "gap"
        return f'"{el.get("name") or self.identity(el).split(":", 1)[-1]}"'


def title_text(el):
    t = el.find("text")
    if t is None:
        return el.get("name", "")
    return "".join(t.itertext()).strip()


# ---------------------------------------------------------------- writing

def build_root(version, resources, event_name, project_name, seq_template, items,
               seq_format, duration, tc_start, base_den):
    root = ET.Element("fcpxml", {"version": version})
    res_el = ET.SubElement(root, "resources")
    res_el.extend(resources)
    event = ET.SubElement(root, "event", {"name": event_name})
    project = ET.SubElement(event, "project", {"name": project_name})
    attrs = {k: v for k, v in seq_template.attrib.items()}
    attrs["format"] = seq_format
    attrs["duration"] = fmt_time(duration, base_den)
    attrs["tcStart"] = fmt_time(tc_start, base_den)
    seq = ET.SubElement(project, "sequence", attrs)
    spine = ET.SubElement(seq, "spine")
    spine.extend(items)
    return root


def _indent(el, level=0):
    pad = "\n" + "    " * level
    if el.tag in TEXTY:
        if not el.tail or not el.tail.strip():
            el.tail = pad
        return
    if len(el):
        if not el.text or not el.text.strip():
            el.text = pad + "    "
        for child in el:
            _indent(child, level + 1)
        last = el[-1]
        if not last.tail or not last.tail.strip():
            last.tail = pad
    if level and (not el.tail or not el.tail.strip()):
        el.tail = pad


def write(root, path):
    _indent(root)
    root.tail = None
    body = ET.tostring(root, encoding="unicode")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
        f.write(body + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- resources

def _refs_in(el):
    out = set()
    for d in el.iter():
        for a in REF_ATTRS:
            v = d.get(a)
            if v:
                out.add(v)
    return out


def _rewrite_refs(elements, mapping):
    for el in elements:
        for d in el.iter():
            for a in REF_ATTRS:
                v = d.get(a)
                if v in mapping:
                    d.set(a, mapping[v])


def used_resources(doc, items):
    """Resources (in document order) that the given items actually need,
    following compound clips into the assets they use."""
    need = set(_refs_in_list(items))
    need.add(doc.sequence.get("format"))
    frontier = list(need)
    while frontier:
        rid = frontier.pop()
        el = doc.res.get(rid)
        if el is None:
            continue
        for r in _refs_in(el):
            if r != rid and r not in need:
                need.add(r)
                frontier.append(r)
    return [el for el in doc.resources if el.get("id") in need]


def _refs_in_list(items):
    out = set()
    for i in items:
        out |= _refs_in(i)
    return out


def _canon(el):
    c = copy.deepcopy(el)
    c.attrib.pop("id", None)
    for d in c.iter():
        d.tail = None
        if d.text and not d.text.strip():
            d.text = None
    return ET.tostring(c, encoding="unicode")


def _dedupe_key(el, doc_src):
    tag = el.tag
    if tag == "format":
        return ("format", tuple(sorted((k, v) for k, v in el.attrib.items() if k != "id")))
    if tag == "asset":
        if el.get("uid"):
            return ("asset-uid", el.get("uid"))
        return ("asset-src", doc_src(el), el.get("start"), el.get("duration"))
    if tag == "effect":
        return ("effect", el.get("uid") or el.get("name"))
    return (tag, _canon(el))


# ---------------------------------------------------------------- assemble

class Segment:
    def __init__(self, label, doc):
        self.label = label
        self.doc = doc


def assemble(segments, project_name, event_name, warn=print, allow_mixed=False):
    """Stitch several segment timelines end to end into one project.
    Returns an ET root ready to write."""
    if not segments:
        raise FCPXMLError("nothing to assemble")
    sigs = {s.label: s.doc.format_signature() for s in segments}
    first_sig = sigs[segments[0].label]
    odd = [k for k, v in sigs.items() if v != first_sig]
    if odd and not allow_mixed:
        raise FCPXMLError(
            "segments use different frame rates/sizes: "
            + ", ".join(f"{k}={sigs[k]}" for k in sigs)
            + ". Make every project the same format (or pass --allow-mixed).")
    versions = sorted({s.doc.version for s in segments}, key=_vkey)
    if len(versions) > 1:
        warn(f"note: segments came from different FCPXML versions {versions}; "
             f"writing {versions[-1]}")

    all_res, all_items, origins, spans = [], [], [], []
    seq_formats = []
    for i, seg in enumerate(segments):
        d = seg.doc
        items = trim_edge_transitions(d.items(), seg.label, warn)
        res = [copy.deepcopy(r) for r in used_resources(d, items)]
        items = [copy.deepcopy(x) for x in items]
        mapping = {r.get("id"): f"s{i}_{r.get('id')}" for r in res}
        for r in res:
            r.set("id", mapping[r.get("id")])
        _rewrite_refs(res, mapping)
        _rewrite_refs(items, mapping)
        _uniquify_local_ids(items, f"s{i}")
        seq_formats.append(mapping.get(d.sequence.get("format")))
        start, end = d.span(items)
        origins.append(start)
        spans.append(end - start)
        all_res.extend(res)
        all_items.append(items)

    # merge identical resources (same media, same effect, same format)
    def src_of(el):
        mr = el.find("media-rep")
        return mr.get("src") if mr is not None else el.get("src")

    alias, keep, seen, media_uids = {}, [], {}, set()
    ordered = sorted(all_res, key=lambda e: RESOURCE_ORDER.index(e.tag)
                     if e.tag in RESOURCE_ORDER else len(RESOURCE_ORDER))
    for el in ordered:
        _rewrite_refs([el], alias)
        key = _dedupe_key(el, src_of)
        if key in seen:
            alias[el.get("id")] = seen[key]
            continue
        if el.tag == "media" and el.get("uid"):
            if el.get("uid") in media_uids:
                # same compound clip, edited differently by two people: keep both
                del el.attrib["uid"]
            else:
                media_uids.add(el.get("uid"))
        seen[key] = el.get("id")
        keep.append(el)

    final = {el.get("id"): f"r{n}" for n, el in enumerate(keep, 1)}
    for el in keep:
        el.set("id", final[el.get("id")])
    # kept ids are renamed; now point every old id (kept or merged) at its final id
    inner_map = {}
    for old in list(alias) + list(final):
        tgt = alias.get(old, old)
        inner_map[old] = final.get(tgt, tgt)
    _rewrite_refs(keep, inner_map)
    for items in all_items:
        _rewrite_refs(items, inner_map)

    first = segments[0].doc
    cursor = first.tc_start
    spine_items = []
    for items, origin, span in zip(all_items, origins, spans):
        for el in items:
            off = parse_time(el.get("offset"))
            el.set("offset", fmt_time(off - origin + cursor, first.base_den))
            spine_items.append(el)
        cursor += span

    seq_format = inner_map.get(seq_formats[0], seq_formats[0])
    return build_root(versions[-1], keep, event_name, project_name, first.sequence,
                      spine_items, seq_format, cursor - first.tc_start,
                      first.tc_start, first.base_den)


def _uniquify_local_ids(items, prefix):
    """Title styles (<text-style-def id="ts1">) use document-wide XML ids, and
    every FCP export starts at ts1, so merged segments would clash."""
    local = {}
    for el in items:
        for d in el.iter():
            if d.get("id"):
                local[d.get("id")] = f"{prefix}{d.get('id')}"
                d.set("id", local[d.get("id")])
    for el in items:
        for d in el.iter("text-style"):
            if d.get("ref") in local:
                d.set("ref", local[d.get("ref")])


def trim_edge_transitions(items, label, warn):
    items = list(items)
    while items and items[0].tag == "transition":
        warn(f"note: dropped a transition at the start of {label} (it would overlap the previous segment)")
        items.pop(0)
    while items and items[-1].tag == "transition":
        warn(f"note: dropped a transition at the end of {label}")
        items.pop()
    return items


def _vkey(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


# ---------------------------------------------------------------- split

def split(doc, cuts, names, event_name, project_prefix, warn=print):
    """Split a timeline at clip boundaries nearest the given times (seconds
    from the start). Returns [(name, root, snapped_start_seconds)]."""
    items = doc.items()
    real = [i for i in items if i.tag != "transition"]
    if not real:
        raise FCPXMLError("timeline is empty")
    bounds = sorted({parse_time(i.get("offset")) for i in real})
    start, end = doc.span()
    inner = [b for b in bounds if start < b < end]
    if not inner and cuts:
        raise FCPXMLError("timeline has only one clip, nothing to split at")
    snapped = []
    for c in cuts:
        target = start + c
        best = min(inner, key=lambda b: abs(b - target))
        if abs(best - target) >= (doc.frame_duration or Fraction(1, 24)):
            warn(f"note: cut at {float(c):.2f}s moved to nearest clip edge {float(best - start):.2f}s")
        if best not in snapped:
            snapped.append(best)
    snapped.sort()
    edges = [start] + snapped + [end]
    if len(names) != len(edges) - 1:
        raise FCPXMLError(f"need {len(edges) - 1} segment names, got {len(names)}")

    groups = [[] for _ in names]
    pending = []
    for el in items:
        if el.tag == "transition":
            pending.append(el)
            continue
        off = parse_time(el.get("offset"))
        idx = max(k for k in range(len(names)) if off >= edges[k])
        if pending:
            if groups[idx] or idx == 0:
                groups[idx].extend(pending)
            else:
                warn(f"note: dropped a transition on the cut into {names[idx]}")
            pending = []
        groups[idx].append(el)

    out = []
    for name, group, seg_start in zip(names, groups, edges):
        group = trim_edge_transitions(group, name, warn)
        res = [copy.deepcopy(r) for r in used_resources(doc, group)]
        seg_items = [copy.deepcopy(x) for x in group]
        s0, s1 = doc.span(seg_items)
        for el in seg_items:
            off = parse_time(el.get("offset"))
            el.set("offset", fmt_time(off - s0 + doc.tc_start, doc.base_den))
        root = build_root(doc.version, res, event_name, f"{project_prefix} - {name}",
                          doc.sequence, seg_items, doc.sequence.get("format"),
                          s1 - s0, doc.tc_start, doc.base_den)
        out.append((name, root, seg_start - start))
    return out


def project_root(doc, event_name, project_name):
    """Re-wrap a doc's single project as a clean import file."""
    items = [copy.deepcopy(x) for x in doc.items()]
    res = [copy.deepcopy(r) for r in used_resources(doc, doc.items())]
    s0, s1 = doc.span(items)
    for el in items:
        off = parse_time(el.get("offset"))
        el.set("offset", fmt_time(off - s0 + doc.tc_start, doc.base_den))
    return build_root(doc.version, res, event_name, project_name, doc.sequence, items,
                      doc.sequence.get("format"), s1 - s0, doc.tc_start, doc.base_den)


# ---------------------------------------------------------------- media paths

def _src_holders(root):
    for el in root.iter():
        if el.tag == "media-rep" and el.get("src"):
            yield el
        elif el.tag == "asset" and el.get("src"):
            yield el


def _drop_bookmarks(el):
    for bm in list(el.findall("bookmark")):
        el.remove(bm)


def canonicalize_paths(root, media_root):
    """Local media paths under media_root -> shared placeholder.
    Returns (rewritten, outside) counts; 'outside' clips won't relink for others."""
    base = media_root.rstrip("/")
    done = outside = 0
    for el in _src_holders(root):
        src = el.get("src")
        if src.startswith(MEDIA_TOKEN) or not src.startswith("file://"):
            continue
        path = unquote(urlparse(src).path)
        if path == base or path.startswith(base + "/"):
            rel = path[len(base) + 1:]
            el.set("src", MEDIA_TOKEN + quote(rel, safe="/"))
            _drop_bookmarks(el)
            done += 1
        else:
            outside += 1
    return done, outside


def localize_paths(root, media_root):
    """Shared placeholder -> this person's media folder."""
    base = media_root.rstrip("/")
    done = 0
    for el in _src_holders(root):
        src = el.get("src")
        if src.startswith(MEDIA_TOKEN):
            rel = unquote(src[len(MEDIA_TOKEN):])
            el.set("src", "file://" + quote(posixpath.join(base, rel), safe="/"))
            _drop_bookmarks(el)
            done += 1
    return done


def count_placeholders(root):
    return sum(1 for el in _src_holders(root) if el.get("src").startswith(MEDIA_TOKEN))
