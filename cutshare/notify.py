"""Optional Discord pings. Off until someone runs `cutshare discord <webhook URL>`.

The webhook lives in the shared project file, so the whole team posts to the
same channel. Failures never block an edit: a ping that can't send is skipped.
"""
import json
import urllib.request

TIMEOUT = 5


def post(proj, text, opener=urllib.request.urlopen):
    url = proj.meta.get("discord_webhook")
    if not url:
        return False
    body = json.dumps({"username": "CutShare", "content": text[:1900]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "CutShare"})
    try:
        opener(req, timeout=TIMEOUT)
        return True
    except Exception:
        return False
