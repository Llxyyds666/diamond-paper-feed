from datetime import datetime, timezone
from pathlib import Path

import pytest

from diamond_feed.config import load_config
from diamond_feed.filtering import load_rules
from diamond_feed.models import PaperRecord


@pytest.fixture
def app_config():
    return load_config(Path("paper_feed_config.json"))


@pytest.fixture
def ai_config(app_config):
    return app_config.ai


@pytest.fixture
def query_rules():
    return load_rules(Path("config/queries.json"))


@pytest.fixture
def diamond_records():
    return [
        PaperRecord(
            title="Piezoelectric effect in a polycrystalline diamond membrane",
            abstract="A flexible diamond membrane produces a stable voltage.",
            authors=["A. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            doi="10.1000/diamond.1",
            url="https://doi.org/10.1000/diamond.1",
            sources=["rss"],
            source_ids=["rss:1"],
        ),
        PaperRecord(
            title="Boron-doped diamond electrode for electrochemistry",
            abstract="Electrochemical response of a boron-doped diamond electrode.",
            authors=["B. Author"],
            journal="Carbon Journal",
            published_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            doi="10.1000/diamond.2",
            url="https://doi.org/10.1000/diamond.2",
            sources=["crossref"],
            source_ids=["crossref:2"],
        ),
    ]
