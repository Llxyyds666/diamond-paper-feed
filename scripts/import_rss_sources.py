"""Import and normalize journal RSS URLs into the active registry."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import io
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

from diamond_feed.atomic import atomic_write_text


SOURCE_COLUMNS = ("name", "category", "url")
_MDPI_PATTERN = re.compile(
    r"^https?://www\.mdpi\.com/(?:journal/([^/?#]+)/rss|rss/journal/([^/?#]+))/?$",
    re.IGNORECASE,
)
_APS_PATTERN = re.compile(
    r"^https?://journals\.aps\.org/([^/?#]+)/rss/?$", re.IGNORECASE
)
_MDPI_ALIASES = {"condmat": "condensedmatter", "microwaves": "microwave"}


@dataclass(frozen=True, slots=True)
class SourceRow:
    name: str
    category: str
    url: str


def normalize_url(url: str) -> str:
    """Return canonical spellings only for publisher patterns we have verified."""
    value = url.strip()
    mdpi = _MDPI_PATTERN.fullmatch(value)
    if mdpi:
        slug = (mdpi.group(1) or mdpi.group(2)).lower()
        slug = _MDPI_ALIASES.get(slug, slug)
        return f"https://www.mdpi.com/rss/journal/{slug}"

    aps = _APS_PATTERN.fullmatch(value)
    if aps:
        code = aps.group(1).lower()
        if code == "prm":
            code = "prmaterials"
        return f"https://feeds.aps.org/rss/recent/{code}.xml"

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid HTTP(S) URL: {value!r}")
    # Scheme/host casing and a bare trailing slash are spelling, not endpoint guesses.
    host = parsed.netloc.lower()
    path = parsed.path[:-1] if parsed.path.endswith("/") and parsed.path != "/" else parsed.path
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, parsed.fragment))


def import_urls(paths: list[Path]) -> list[SourceRow]:
    """Read URL-only lines and preserve the first occurrence of each canonical URL."""
    rows: list[SourceRow] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(encoding="utf-8-sig") as handle:
            for raw_line in handle:
                if not raw_line.startswith(("http://", "https://")):
                    continue
                url = normalize_url(raw_line.rstrip("\r\n"))
                if url in seen:
                    continue
                seen.add(url)
                rows.append(SourceRow("unknown", "unknown", url))
    return rows


def _render_sources(rows: list[SourceRow]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(SOURCE_COLUMNS)
    for row in sorted(rows, key=lambda item: (item.name.casefold(), item.category.casefold(), item.url)):
        writer.writerow((row.name, row.category, row.url))
    return output.getvalue()


def write_sources(path: Path, rows: list[SourceRow]) -> None:
    atomic_write_text(path, _render_sources(rows))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import normalized journal RSS URLs.")
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = import_urls(args.input)
    write_sources(args.output, rows)
    print(f"imported {len(rows)} unique RSS candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
