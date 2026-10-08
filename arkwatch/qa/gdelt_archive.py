"""GDELT 2.0 archive window discovery and download helpers."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

FEED_SUFFIXES = {
    "export": ".export.csv.zip",
    "mentions": ".mentions.csv.zip",
    "gkg": ".gkg.csv.zip",
}
WINDOW = timedelta(minutes=15)
MAX_FIELD_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True)
class Archive:
    feed: str
    latest_at: datetime
    url_template: str

    def url_at(self, timestamp: datetime) -> str:
        stamp = timestamp.strftime("%Y%m%d%H%M%S")
        return self.url_template.replace("{timestamp}", stamp)


class ArchiveUnavailable(RuntimeError):
    pass


def parse_lastupdate(text: str) -> dict[str, Archive]:
    archives = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        url = parts[2].replace("http://", "https://", 1)
        parsed_url = urlsplit(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "data.gdeltproject.org":
            raise ValueError("GDELT lastupdate contains a URL outside the official archive host")
        lowered = url.lower()
        for feed, suffix in FEED_SUFFIXES.items():
            if not lowered.endswith(suffix):
                continue
            pattern = re.compile(r"(\d{14})(?=" + re.escape(suffix) + r"$)", re.IGNORECASE)
            match = pattern.search(url)
            if match is None:
                raise ValueError(f"unrecognized GDELT {feed} archive URL")
            latest_at = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            if latest_at.minute % 15 or latest_at.second or latest_at.microsecond:
                raise ValueError(f"GDELT {feed} archive timestamp is not on a 15-minute boundary")
            if feed in archives:
                raise ValueError(f"GDELT lastupdate contains duplicate {feed} archives")
            archives[feed] = Archive(
                feed,
                latest_at,
                url[: match.start(1)] + "{timestamp}" + url[match.end(1) :],
            )
            break
    missing = set(FEED_SUFFIXES) - archives.keys()
    if missing:
        raise ValueError(f"GDELT lastupdate is missing feeds: {', '.join(sorted(missing))}")
    return archives


def windows_after(
    last_processed: datetime, latest_available: datetime, *, limit: int
) -> list[datetime]:
    if limit < 1:
        raise ValueError("window limit must be positive")
    if last_processed.tzinfo is None or latest_available.tzinfo is None:
        raise ValueError("GDELT archive timestamps must be timezone-aware")
    for timestamp in (last_processed.astimezone(UTC), latest_available.astimezone(UTC)):
        if timestamp.minute % 15 or timestamp.second or timestamp.microsecond:
            raise ValueError("GDELT archive watermarks must use 15-minute boundaries")
    if last_processed >= latest_available:
        return []
    next_window = last_processed.astimezone(UTC) + WINDOW
    latest_available = latest_available.astimezone(UTC)
    windows = []
    while next_window <= latest_available and len(windows) < limit:
        windows.append(next_window)
        next_window += WINDOW
    return windows


def download_rows(session, feed: str, url: str) -> list[list[str]]:
    response = session.get(url, timeout=(10, 120))
    if response.status_code == 404:
        raise ArchiveUnavailable(f"GDELT {feed} archive is not available yet")
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise RuntimeError(f"GDELT {feed} response is not a ZIP archive")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        if not names:
            raise RuntimeError(f"GDELT {feed} archive is empty")
        with archive.open(names[0]) as raw:
            rows = csv.reader(
                io.TextIOWrapper(raw, encoding="utf-8", errors="replace"), delimiter="\t"
            )
            previous_limit = csv.field_size_limit()
            csv.field_size_limit(max(previous_limit, MAX_FIELD_SIZE))
            try:
                return [row for row in rows if row]
            finally:
                csv.field_size_limit(previous_limit)
