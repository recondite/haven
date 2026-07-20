"""SecondBrain search ranking + traversal guard (temp wiki dir)."""
import pytest

from haven import config, knowledge

PAGE_A = """---
type: concept
---

# Co-packaged optics

CPO integrates optical engines beside the compute die to break the copper I/O wall.
Ayar Labs TeraPHY is a co-packaged optics product.
"""

PAGE_B = """---
type: company
---

# Alchip

Alchip is an ASIC design-services partner and Series E investor in Ayar Labs.
"""


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "SecondBrain"
    wdir = root / "wiki" / "concepts"
    wdir.mkdir(parents=True)
    (wdir / "co-packaged-optics.md").write_text(PAGE_A, encoding="utf-8")
    (root / "wiki" / "entities").mkdir(parents=True)
    (root / "wiki" / "entities" / "alchip.md").write_text(PAGE_B, encoding="utf-8")
    monkeypatch.setattr(config, "SECONDBRAIN_DIR", root)
    monkeypatch.setattr(knowledge, "_WIKI_DIR", root / "wiki")
    return root


def test_search_ranks_title_hits_first(wiki):
    hits = knowledge.search("co-packaged optics")
    assert hits[0]["title"] == "Co-packaged optics"
    assert hits[0]["path"] == "wiki/concepts/co-packaged-optics.md"
    assert "CPO" in hits[0]["excerpt"] or "optical" in hits[0]["excerpt"]


def test_search_finds_by_content(wiki):
    hits = knowledge.search("Series E investor")
    assert any(h["title"] == "Alchip" for h in hits)


def test_search_empty_query(wiki):
    assert knowledge.search("") == []


def test_similar_pages_flags_near_dup(wiki):
    hits = knowledge.similar_pages("Co-packaged optics overview")
    assert hits and hits[0]["path"].endswith("co-packaged-optics.md")
    assert knowledge.similar_pages("quarterly travel budget analysis") == []


def test_curation_backlog_counts_haven_ingests(wiki, tmp_path):
    root = wiki
    p = root / "wiki" / "sources"
    p.mkdir(parents=True)
    (p / "ingested.md").write_text(
        "---\ntype: source\nsources: [haven-ingest-2026-07-19]\n---\n\n# Ingested\n\nx\n",
        encoding="utf-8")
    bl = knowledge.curation_backlog()
    assert bl["count"] == 1 and bl["pages"][0].endswith("ingested.md")


def test_get_page_and_traversal_guard(wiki):
    assert "Co-packaged" in knowledge.get_page("wiki/concepts/co-packaged-optics.md")
    assert knowledge.get_page("../../../etc/passwd") is None
    assert knowledge.get_page("wiki/nope.md") is None
