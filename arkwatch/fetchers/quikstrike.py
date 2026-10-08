"""quikstrike.py — official FedWatch probability matrix via Referer + menu POST.

Anonymous working flow (no login; confirmed against
fixtures/quikstrike/menu_post_FedWatch.html):
 1. GET  QuikStrikeView.aspx?viewitemid=IntegratedTreasuryWatch&userId=lwolf + Referer
    -> WebForms shell (either a hydrated ~100KB view or a small shell; both
    carry __VIEWSTATE + the navigation menu)
 2. Find the 'FedWatch Tool' menu item in the HTML -> its EVENTTARGET (ctl00$...$lbMenuItem)
 3. POST all hidden fields + __EVENTTARGET -> the FedWatch page rendered server-side
 4. Table: 'Meeting Date|Contract|Expires|Mid Price|Prior Volume|Prior OI' rows per
    meeting, EACH FOLLOWED by a probability row 'E%|H%|X%' (ease|hold|hike)
NOTE: QuikStrikeTools.aspx?viewitemid=IntegratedFedWatchTool (the CME iframe URL)
cannot be hydrated anonymously (PageMethod 401) — do not use that route.
"""

from __future__ import annotations

import contextlib
import re

from curl_cffi import requests as creq

# Referer must be pinned to the cmegroup.com FedWatch page.
REFERER = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
QS_BASE = "https://cmegroup-tools.quikstrike.net/User/QuikStrikeView.aspx"
PARAMS = {"viewitemid": "IntegratedTreasuryWatch", "userId": "lwolf"}

_MONTH = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


def _hidden_fields(text: str) -> dict[str, str]:
    out = {}
    for name, val in re.findall(r'<input type="hidden" name="([^"]+)"[^>]*value="([^"]*)"', text):
        out[name] = val
    return out


def _form_action(text: str) -> str:
    """The WebForms action URL carries insid/qsid session params — POST must target it.

    Posting without them is silently ignored by the server (the response is
    identical to the GET).
    """
    from urllib.parse import urljoin, urlparse

    m = re.search(r'<form[^>]*action="([^"]+)"', text)
    if not m:
        return QS_BASE
    url = urljoin("https://cmegroup-tools.quikstrike.net/User/", m.group(1).replace("&amp;", "&"))
    # scraped action: never POST the form (session fields) off-host
    u = urlparse(url)
    return url if (u.scheme, u.hostname) == ("https", "cmegroup-tools.quikstrike.net") else QS_BASE


def _fedwatch_eventtarget(text: str) -> str | None:
    """Find the EVENTTARGET of the menu item labeled 'FedWatch Tool'.

    The href uses HTML entities: javascript:__doPostBack(&#39;ctl00$...&#39;,&#39;&#39;)
    """
    pat = re.compile(r"__doPostBack\((?:&#39;|')([a-zA-Z0-9_$]+)(?:&#39;|')\s*,")
    for m in pat.finditer(text):
        tail = text[m.end() : m.end() + 400]
        if re.search(r">\s*FedWatch\s*Tool", tail):
            return m.group(1)
    return None


def _cells(tr: str) -> list[str]:
    clean = re.sub(r"<[^>]+>", "|", tr)
    clean = re.sub(r"\s*\|\s*", "|", clean).strip("|")
    return [c.strip() for c in clean.split("|") if c.strip()]


def fetch_fedwatch_official() -> list[dict] | None:
    """Fetch the official FedWatch probability matrix anonymously.

    Returns [{meeting, contract, ease, hold, hike}] or None when the fetch fails.
    """
    s = creq.Session(impersonate="chrome")
    r = s.get(QS_BASE, params=PARAMS, headers={"Referer": REFERER}, timeout=(10, 30))
    if r.status_code != 200 or "ErrorPage" in str(r.url):
        return None
    target = _fedwatch_eventtarget(r.text)
    if not target:
        return None
    form = _hidden_fields(r.text)
    form.update({"__EVENTTARGET": target, "__EVENTARGUMENT": ""})
    r2 = s.post(_form_action(r.text), data=form, headers={"Referer": REFERER}, timeout=(10, 30))
    if r2.status_code != 200 or len(r2.text) < 20000:
        return None

    # parse: a meeting row 'DD Mon YYYY|CONTRACT|...' followed by an 'E%|H%|X%' row
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", r2.text, re.S)
    meetings: list[dict] = []
    pending_meeting: dict | None = None
    for tr in rows:
        cells = _cells(tr)
        if not cells:
            continue
        # meeting row: starts with e.g. '16 Sep 2026'
        m = re.match(rf"(\d{{1,2}})\s+({_MONTH})\s+(\d{{4}})", cells[0])
        if m and len(cells) >= 2 and re.match(r"[A-Z]{2,3}[FGHJKMNQUVXZ]\d", cells[1]):
            mid = None
            if len(cells) >= 4:
                with contextlib.suppress(ValueError):
                    mid = float(cells[3].replace(",", ""))
            pending_meeting = {
                "meeting": f"{m.group(2)} {m.group(3)}",
                "meeting_date": cells[0],
                "contract": cells[1],
                "mid": mid,
            }
            continue
        # probability row: exactly 3 percent values
        pcts = [c for c in cells if c.endswith("%")]
        if pending_meeting and len(pcts) == 3:
            try:
                pending_meeting["ease"] = float(pcts[0][:-1])
                pending_meeting["hold"] = float(pcts[1][:-1])
                pending_meeting["hike"] = float(pcts[2][:-1])
                meetings.append(pending_meeting)
            except ValueError:
                pass
            pending_meeting = None
    return meetings if meetings else None


def compare_diy_vs_official(diy_probs: list[dict], official: list[dict]) -> list[dict]:
    """Compare DIY probabilities against official FedWatch; returns deltas.

    Acceptance gate: hike difference <= 3pp. Matching on month alone would
    pair 'Sep 2027' (long DIY strip) with 'Sep 2026' (official) and compare
    across years, so keys are the full 'Mon YY'.
    """
    import re as _re

    def _key(m):
        mm = _re.match(r"\s*([A-Za-z]{3})\w*\s+(\d{2,4})", str(m))
        return f"{mm.group(1).upper()} {mm.group(2)[-2:]}" if mm else ""

    comparisons = []
    for diy in diy_probs:
        diy_k = _key(diy.get("meeting", ""))
        if not diy_k:
            continue
        for off in official:
            if diy_k == _key(off.get("meeting", "")):
                d_hike = abs(diy.get("hike", 0) * 100 - off.get("hike", 0))
                comparisons.append(
                    {
                        "meeting": diy.get("meeting"),
                        "diy_hike_pct": round(diy.get("hike", 0) * 100, 1),
                        "official_hike_pct": off.get("hike", 0),
                        "delta_hike_pp": round(d_hike, 1),
                        "pass": d_hike <= 3.0,
                    }
                )
                break
    return comparisons
