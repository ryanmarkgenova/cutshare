"""Build FCPXML that looks like what Final Cut Pro 12 writes with File > Export XML."""
from fractions import Fraction
from urllib.parse import quote

FD = Fraction(1001, 30000)  # 29.97 fps


def t(frames):
    """frames -> FCP-style time string."""
    if frames == 0:
        return "0s"
    return f"{frames * 1001}/30000s"


def asset(rid, name, root, frames=9000, uid=None):
    src = "file://" + quote(f"{root}/Footage/{name}.mov", safe="/")
    return (f'<asset id="{rid}" name="{name}" uid="{uid or name.upper() + "UID"}" start="0s" '
            f'duration="{t(frames)}" hasVideo="1" format="r1" videoSources="1" hasAudio="1" '
            f'audioSources="1" audioChannels="2" audioRate="48000">'
            f'<media-rep kind="original-media" sig="SIG{name}" src="{src}">'
            f'<bookmark>Ym9va1Rlc3Q=</bookmark></media-rep>'
            f'<metadata><md key="com.apple.proapps.studio.rawToLogConversion" value="0"/></metadata>'
            f'</asset>')


def project(clips, root="/Users/ryan/Movies/FASA Show", name="FASA Recap", titles=None,
            transition_after=None, version="1.13", project_uid="PROJ-UID-1"):
    """clips: list of (asset_name, source_start_frames, length_frames)."""
    names = []
    for c in clips:
        if c[0] not in names:
            names.append(c[0])
    rid = {n: f"r{k + 2}" for k, n in enumerate(names)}
    res = ['<format id="r1" name="FFVideoFormat1080p2997" frameDuration="1001/30000s" '
           'width="1920" height="1080" colorSpace="1-1-1 (Rec. 709)"/>']
    res += [asset(rid[n], n, root) for n in names]
    title_rid = f"r{len(names) + 2}"
    dissolve_rid = f"r{len(names) + 3}"
    res.append(f'<effect id="{title_rid}" name="Basic Title" uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>')
    res.append(f'<effect id="{dissolve_rid}" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>')

    spine, off, ts = [], 0, 0
    titles = titles or {}
    for k, (n, start, length) in enumerate(clips):
        inner = ""
        if k in titles:
            ts += 1
            inner += (f'<title ref="{title_rid}" lane="1" offset="{t(start + 30)}" name="{titles[k]}" '
                      f'start="{t(108108)}" duration="{t(90)}"><text><text-style ref="ts{ts}">{titles[k]}</text-style></text>'
                      f'<text-style-def id="ts{ts}"><text-style font="Helvetica" fontSize="63" fontColor="1 1 1 1"/></text-style-def></title>')
        inner += f'<marker start="{t(start + 10)}" duration="{t(1)}" value="beat {k}"/>'
        spine.append(f'<asset-clip ref="{rid[n]}" offset="{t(off)}" name="{n}" start="{t(start)}" '
                     f'duration="{t(length)}" format="r1" tcFormat="NDF" audioRole="dialogue">{inner}</asset-clip>')
        if transition_after == k:
            spine.append(f'<transition name="Cross Dissolve" offset="{t(off + length - 15)}" duration="{t(30)}">'
                         f'<filter-video ref="{dissolve_rid}" name="Cross Dissolve"/></transition>')
        off += length
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>

<fcpxml version="{version}">
    <resources>
        {"".join(res)}
    </resources>
    <library location="file:///Users/ryan/Movies/FASA.fcpbundle/">
        <event name="FASA" uid="EVENT-UID">
            <project name="{name}" uid="{project_uid}" modDate="2026-11-14 15:00:00 -0800">
                <sequence format="r1" duration="{t(off)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
                    <spine>
                        {"".join(spine)}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
'''
