"""Content-hash / material-change detection. No network, no DB."""

from gradscout.changes import compute_content_hash
from tests.conftest import make_job


def test_identical_content_hashes_equal():
    a = make_job("Software Engineer, New Grad", "Backend APIs.", location="NYC")
    b = make_job("Software Engineer, New Grad", "Backend APIs.", location="NYC")
    assert compute_content_hash(a) == compute_content_hash(b)


def test_title_change_changes_hash():
    a = make_job("Software Engineer, New Grad", "Backend APIs.")
    b = make_job("Senior Software Engineer", "Backend APIs.")
    assert compute_content_hash(a) != compute_content_hash(b)


def test_description_change_changes_hash():
    a = make_job("Software Engineer", "Backend APIs.")
    b = make_job("Software Engineer", "Backend APIs and infra.")
    assert compute_content_hash(a) != compute_content_hash(b)


def test_location_change_changes_hash():
    a = make_job("Software Engineer", "Backend APIs.", location="NYC")
    b = make_job("Software Engineer", "Backend APIs.", location="Remote")
    assert compute_content_hash(a) != compute_content_hash(b)


def test_sponsorship_and_degrees_hints_change_hash():
    a = make_job("Software Engineer", "Backend APIs.", raw_blob={"sponsorship": "Offers Sponsorship"})
    b = make_job(
        "Software Engineer", "Backend APIs.", raw_blob={"sponsorship": "Does Not Offer Sponsorship"}
    )
    assert compute_content_hash(a) != compute_content_hash(b)

    c = make_job("Software Engineer", "Backend APIs.", raw_blob={"degrees": ["Bachelor's"]})
    d = make_job("Software Engineer", "Backend APIs.", raw_blob={"degrees": ["Master's"]})
    assert compute_content_hash(c) != compute_content_hash(d)


def test_employment_hint_change_changes_hash():
    ashby_ft = make_job("Software Engineer", "Backend APIs.", raw_blob={"employmentType": "FullTime"})
    ashby_intern = make_job(
        "Software Engineer", "Backend APIs.", raw_blob={"employmentType": "Intern"}
    )
    assert compute_content_hash(ashby_ft) != compute_content_hash(ashby_intern)

    lever_ft = make_job("Software Engineer", "Backend APIs.", raw_blob={"categories": {"commitment": "Full-time"}})
    lever_intern = make_job(
        "Software Engineer", "Backend APIs.", raw_blob={"categories": {"commitment": "Intern"}}
    )
    assert compute_content_hash(lever_ft) != compute_content_hash(lever_intern)


def test_raw_blob_key_order_does_not_affect_hash():
    a = make_job("Software Engineer", "Backend APIs.", raw_blob={"a": 1, "b": 2})
    b = make_job("Software Engineer", "Backend APIs.", raw_blob={"b": 2, "a": 1})
    assert compute_content_hash(a) == compute_content_hash(b)


def test_volatile_fields_are_not_part_of_the_job_model_input():
    """apply_url differing (e.g. tracking params) must not affect the hash --
    only analysis-relevant content does."""
    a = make_job(
        "Software Engineer", "Backend APIs.", apply_url="https://x.test/jobs/1?utm_source=a"
    )
    b = make_job(
        "Software Engineer", "Backend APIs.", apply_url="https://x.test/jobs/1?utm_source=b"
    )
    assert compute_content_hash(a) == compute_content_hash(b)
