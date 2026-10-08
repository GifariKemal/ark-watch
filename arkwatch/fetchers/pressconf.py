"""pressconf.py — FOMC press conference transcript fetcher + NLP analysis.

PDF source (live-verified 2026-09-19): the transcript PDF is linked from the
presconf video page at /mediacenter/files/FOMCpresconf{date}.pdf (NOT at
/monetarypolicy/files/ — that path 404s). Text extraction via PyMuPDF.

The press conference happens 30 min after the statement (14:30 ET); the
transcript PDF appears on the Fed website later the same day.
"""

from __future__ import annotations

import re

import requests

PDF_BASE = "https://www.federalreserve.gov/mediacenter/files/FOMCpresconf"
CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


class PressConfError(RuntimeError):
    pass


def fetch_transcript_text(date_iso: str) -> str:
    """Download the press conference transcript PDF → clean text.

    date_iso: the meeting's END date (same as minutes), e.g. '2026-07-29'.
    """
    r = requests.get(f"{PDF_BASE}{date_iso.replace('-', '')}.pdf", timeout=(10, 60))
    if r.status_code != 200:
        raise PressConfError(f"pressconf {date_iso}: HTTP {r.status_code}")
    if r.content[:4] != b"%PDF":
        raise PressConfError(f"pressconf {date_iso}: not a PDF ({r.content[:20]})")

    import pymupdf

    doc = pymupdf.open(stream=r.content, filetype="pdf")
    text = " ".join(page.get_text() for page in doc)
    doc.close()

    # normalize: collapse whitespace, strip page markers
    text = re.sub(r"Page \d+ of \d+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 1000:
        raise PressConfError(f"pressconf {date_iso}: suspiciously short ({len(text)} chars)")
    return text


def available_dates() -> list[str]:
    """All press conference dates (meeting end-dates) from the calendar."""
    r = requests.get(CALENDAR_URL, timeout=(10, 60))
    if r.status_code != 200:
        raise PressConfError(f"FOMC calendar: HTTP {r.status_code}")
    dates = sorted(set(re.findall(r"fomcpresconf(\d{8})\.htm", r.text)), reverse=True)
    return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]


def analyze_pressconf(date_iso: str) -> dict:
    """Fetch transcript + structural parse + NLP tone analysis.

    Returns the full analysis dict including the text for further processing.
    """
    text = fetch_transcript_text(date_iso)

    # structural: count Q&A exchanges, check for key phrases
    qa_count = text.count("QUESTION:") + text.count("Q:") - text.count("CHAIRMAN WARSH")
    has_qa = qa_count > 0

    # NLP analysis (multi-provider via nlp.py)
    from .nlp import analyze_tone

    try:
        tone = analyze_tone(text, source_type="press_conference")
    except Exception as ex:
        tone = {"error": str(ex)[:100]}

    return {
        "meeting_date": date_iso,
        "text": text,
        "char_count": len(text),
        "has_qa": has_qa,
        "tone": tone,
    }
