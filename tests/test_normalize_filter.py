from datetime import datetime, timezone
from pathlib import Path

import pytest

from diamond_feed.filtering import load_rules, matches_rules
from diamond_feed.models import PaperRecord
from diamond_feed.normalize import merge_records, normalize_doi, record_key


def paper(title: str, abstract: str = "", doi: str | None = None) -> PaperRecord:
    return PaperRecord(
        title=title,
        abstract=abstract,
        authors=["A. Author"],
        journal="Test Journal",
        published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        doi=doi,
        url="https://example.test/paper",
        sources=["test"],
        source_ids=["id-1"],
    )


def test_doi_and_title_keys_are_canonical():
    assert normalize_doi("https://doi.org/10.1000/ABC.1") == "10.1000/abc.1"
    assert record_key(paper("A title", doi="doi:10.1000/ABC.1")) == "doi:10.1000/abc.1"


def test_title_fallback_key_collapses_all_non_alphanumeric_runs():
    assert record_key(paper("A_title—study")) == "title:a title study:2026"


def test_merge_prefers_complete_metadata_and_keeps_provenance():
    merged = merge_records(
        paper("CVD diamond", doi="10.1/x"),
        PaperRecord(
            title="CVD diamond",
            abstract="A complete abstract",
            authors=["A. Author", "B. Author"],
            journal="Diamond Journal",
            published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            doi="10.1/x",
            url="https://publisher.test/x",
            sources=["crossref"],
            source_ids=["cr-x"],
        ),
    )
    assert merged.abstract == "A complete abstract"
    assert merged.sources == ["test", "crossref"]


def test_rules_keep_material_research_and_drop_obvious_semantic_noise():
    rules = load_rules(Path("config/queries.json"))
    assert matches_rules(paper("Piezoelectric effect in polycrystalline diamond membranes"), rules)
    assert matches_rules(paper("Boron-doped diamond electrodes for electrochemistry"), rules)
    assert not matches_rules(paper("A diamond graph algorithm for network routing"), rules)
    assert not matches_rules(paper("Baseball diamond geometry for player tracking"), rules)


@pytest.mark.parametrize(
    "title",
    [
        "Neuropsychological outcomes in Shwachman-Diamond syndrome",
        "Quantum fields inside a causal diamond",
        "Entanglement across spacetime diamonds",
        "India's diamond crossroads: challenges and shifting markets",
        "Global diamond trade and market outlook",
        "Diamond Cut Veneers in restorative dentistry",
        "Iwasawa Continued Fractions via Diamond Lattice — E8 Intelligence Research",
        "Hyperbolic 3-Space via Diamond Lattice",
        "DART-SD: Diamond-topology Aware Retrieval for Tool-Calling Agents",
        "Diamond topology for tool calling",
        "The Diamond Covenant and religious identity",
        "The Art of Indian Jewelry: Kundan, Jadau & Contemporary Elegance",
        "Diamond-Blackfan anemia syndrome patient outcomes",
        "Mapping Schwarzschild Spacetime: From Kruskal to Diamond Representations",
        "Reasoning accuracy on the GPQA Diamond benchmark",
        "Voices of Indigenous Youth as diamond mines begin closing",
        "Porous Ti6Al4V scaffolds with a hexagonal diamond structure",
    ],
)
def test_explicit_noise_senses_are_unconditional_even_with_material_context(title):
    rules = load_rules(Path("config/queries.json"))

    assert not matches_rules(
        paper(title, "quantum carbon diamond crystal film surface sensor research"), rules
    )


@pytest.mark.parametrize(
    "title",
    [
        "Quantum transport in the cubic diamond crystal lattice",
        "Hyperbolic phonon polaritons in a diamond crystal",
        "Defect topology in boron-doped diamond lattice materials",
    ],
)
def test_noise_regressions_keep_genuine_diamond_crystal_lattice_research(title):
    rules = load_rules(Path("config/queries.json"))

    assert matches_rules(paper(title, "Carbon crystal defects and phonon transport"), rules)


@pytest.mark.parametrize(
    "title",
    [
        "Thermal transport across diamond-graphene heterostructures",
        "Diamond graphene field-effect transistors",
        "Diamond-coated Ti6Al4V scaffolds for biomedical implants",
    ],
)
def test_noise_phrases_do_not_swallow_diamond_devices_or_coatings(title):
    rules = load_rules(Path("config/queries.json"))

    assert matches_rules(
        paper(title, "CVD diamond semiconductor carbon film surface"), rules
    )


def test_record_round_trip_uses_json_native_values_and_utc_timestamp():
    value = paper("Diamond sensor", doi="10.1000/sensor")
    value.published_at = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    data = value.to_dict()

    assert data["published_at"] == "2026-09-01T08:00:00+00:00"
    assert PaperRecord.from_dict(data) == value
