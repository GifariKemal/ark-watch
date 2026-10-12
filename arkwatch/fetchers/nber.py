"""nber.py — US business cycle peak/trough dates (recession labels for backtests).

Official NBER chronology (preferred over inferring recessions from USREC).
Single table: column 0 = Peak, column 1 = Trough, cell format
'December 1854 (1854Q4)'; the first row has peak=&nbsp; (a cycle without a peak).
"""

from __future__ import annotations

import re

import requests

URL = "https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
}

_MONTHS = (
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December"
)
_DATE_RX = re.compile(rf"({_MONTHS})\s+(\d{{4}})")


def _clean(td_html: str) -> str:
    """Strip tags inside a cell and decode &nbsp;."""
    return re.sub(r"<[^>]+>", "", td_html).replace("&nbsp;", " ").strip()


def fetch_recession_dates() -> list[dict]:
    """Parse the NBER table -> [{'peak': 'February 2020', 'trough': 'April 2020'}, ...].

    Peak can be None (the first 1854 cycle, and ongoing recessions whose
    trough has not been announced yet).
    """
    r = requests.get(URL, headers=UA, timeout=(10, 30))
    if r.status_code != 200:
        raise RuntimeError(f"NBER: HTTP {r.status_code}")
    out: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 2:
            continue
        peak_cell = _clean(tds[0])
        trough_cell = _clean(tds[1])
        mp = _DATE_RX.search(peak_cell)
        mt = _DATE_RX.search(trough_cell)
        # valid data rows have at least a trough (or only a peak, for an
        # ongoing recession)
        if not mp and not mt:
            continue  # header / footnote rows
        out.append(
            {
                "peak": f"{mp.group(1)} {mp.group(2)}" if mp else None,
                "trough": f"{mt.group(1)} {mt.group(2)}" if mt else None,
            }
        )
    return out
