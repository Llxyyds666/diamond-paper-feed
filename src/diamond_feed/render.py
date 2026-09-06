"""RSS rendering for the raw, rules-filtered paper feed."""

from email.utils import format_datetime
from xml.etree import ElementTree

from diamond_feed.models import PaperRecord
from diamond_feed.normalize import record_key


DC_NS = "http://purl.org/dc/elements/1.1/"
ElementTree.register_namespace("dc", DC_NS)


def _element(parent: ElementTree.Element, name: str, value: str) -> None:
    ElementTree.SubElement(parent, name).text = _xml_text(value)


def _xml_text(value: str) -> str:
    """Remove code points forbidden by XML 1.0 while retaining legal whitespace."""
    return "".join(
        character
        for character in value
        if (code := ord(character)) in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def render_rss(records: list[PaperRecord], title: str, link: str, limit: int) -> str:
    """Render newest records first as an RSS 2.0 document, capped at 2,000 items."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    root = ElementTree.Element("rss", {"version": "2.0"})
    channel = ElementTree.SubElement(root, "channel")
    _element(channel, "title", title)
    _element(channel, "link", link)
    _element(channel, "description", title)

    for record in sorted(records, key=lambda item: item.published_at, reverse=True)[: min(limit, 2000)]:
        item = ElementTree.SubElement(channel, "item")
        _element(item, "title", record.title)
        _element(item, "link", record.url)
        _element(item, "description", record.abstract)
        _element(item, "guid", record_key(record))
        _element(item, "pubDate", format_datetime(record.published_at, usegmt=True))
        for author in record.authors:
            _element(item, f"{{{DC_NS}}}creator", author)
        if record.journal:
            _element(item, f"{{{DC_NS}}}source", record.journal)
        for source in record.sources:
            _element(item, f"{{{DC_NS}}}source", source)
        if record.doi:
            _element(item, f"{{{DC_NS}}}identifier", record.doi)

    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True)
