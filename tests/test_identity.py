"""Identity: SecondBrain roster parse, email resolution, manual-override wins."""
import pytest

from haven import identity
from haven import spine as spine_mod
from haven.spine import Spine

PAGE_GT = """---
type: person
---

# Garth Thompson

**Title:** Chief Information Officer
**Department:** [[it]] (IT)
**Manager:** [[mark-wade]] Mark Wade
**Work email:** garth@ayarlabs.com
"""

PAGE_REPORT = """---
type: person
---

# Jeff Cotter

**Title:** Director, Data Pillars
**Department:** [[it]] (IT)
**Manager:** [[garth-thompson]] Garth Thompson
**Work email:** jeff@ayarlabs.com
"""

PAGE_NOEMAIL = """# Pat Gelsinger

**Title:** Board Member
"""


@pytest.fixture
def sp(tmp_path, monkeypatch):
    s = Spine(tmp_path / "spine.sqlite")
    monkeypatch.setattr(spine_mod, "spine", s)
    monkeypatch.setattr(identity, "spine", s)
    # temp SecondBrain people dir
    people = tmp_path / "sb" / "wiki" / "entities" / "people"
    people.mkdir(parents=True)
    (people / "garth-thompson.md").write_text(PAGE_GT, encoding="utf-8")
    (people / "jeff-cotter.md").write_text(PAGE_REPORT, encoding="utf-8")
    (people / "pat-gelsinger.md").write_text(PAGE_NOEMAIL, encoding="utf-8")
    monkeypatch.setattr(identity, "_PEOPLE_DIR", people)
    return s


def test_roster_parse(sp):
    res = identity.load_roster()
    assert res["loaded"] == 3
    assert res["gt_reports"] == 1        # Jeff reports to Garth
    jeff = sp.person_by_email("jeff@ayarlabs.com")
    assert jeff["name"] == "Jeff Cotter"
    assert jeff["title"] == "Director, Data Pillars"
    assert jeff["department"] == "(IT)"  # wikilink stripped
    assert jeff["is_report"] == 1
    # no-email page still loads, just isn't email-resolvable
    assert sp.person_by_email("garth@ayarlabs.com")["name"] == "Garth Thompson"


def test_roster_reload_is_idempotent(sp):
    identity.load_roster()
    identity.load_roster()
    assert len(sp.list_people()) == 3     # upsert, not duplicate


def test_map_identity_and_coverage(sp):
    identity.load_roster()
    jeff = sp.person_by_email("jeff@ayarlabs.com")
    sp.map_identity(jeff["id"], "slack", "U123", provenance="email_match")
    cov = sp.identity_coverage()
    assert cov["people"] == 3 and cov["people_with_any_id"] == 1
    assert cov["by_system"]["slack"] == 1


def test_roster_drift_flags_joiners_and_stale(sp, monkeypatch):
    identity.load_roster()
    # roster people have no resolved slack id yet -> all "stale" (signal only)
    class FakeStore:
        def list_cached(self, src):
            if src == "gmail":
                return [{"sender": "New Person <newhire@ayarlabs.com>", "subject": "hi"},
                        {"sender": "vendor@outside.com", "subject": "pitch"}]
            return []
    monkeypatch.setattr(identity, "cursor_store", FakeStore())
    d = identity.roster_drift()
    joiner_emails = [j["email"] for j in d["candidate_joiners"]]
    assert "newhire@ayarlabs.com" in joiner_emails      # internal, no page
    assert "vendor@outside.com" not in joiner_emails     # external, ignored
    assert any(s["email"] == "jeff@ayarlabs.com" for s in d["roster_people_without_slack_id"])


def test_manual_override_not_clobbered(sp):
    identity.load_roster()
    jeff = sp.person_by_email("jeff@ayarlabs.com")
    sp.map_identity(jeff["id"], "slack", "U123", manual=True)
    # an automated pass tries to remap the same slack id to someone else — must not win
    gt = sp.person_by_email("garth@ayarlabs.com")
    sp.map_identity(gt["id"], "slack", "U123", provenance="email_match", manual=False)
    ids = sp.identities_for_person(jeff["id"])
    assert any(i["system_id"] == "U123" for i in ids)   # still Jeff's
    assert sp.identities_for_person(gt["id"]) == []
