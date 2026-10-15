from app.domain.scrubbing import scrub
from tests.conftest import inquiry


def test_clean_inquiry_passes():
    assert scrub(inquiry(), is_duplicate=False).ok


def test_no_consent_is_rejected():
    result = scrub(inquiry(consent=False), is_duplicate=False)
    assert "no consent to be contacted" in result.reasons


def test_honeypot_catches_bots():
    result = scrub(inquiry(website="http://spam.example"), is_duplicate=False)
    assert any("honeypot" in r for r in result.reasons)


def test_disposable_email_is_rejected():
    result = scrub(inquiry(email="someone@mailinator.com"), is_duplicate=False)
    assert "disposable email address" in result.reasons


def test_impossible_phone_is_rejected():
    result = scrub(inquiry(phone="012-345-6789"), is_duplicate=False)
    assert "phone number is not a valid US number" in result.reasons


def test_link_spam_is_rejected():
    spam = "Buy now http://a.example and http://b.example cheap cheap cheap"
    result = scrub(inquiry(description=spam), is_duplicate=False)
    assert any("links" in r for r in result.reasons)


def test_duplicate_is_rejected():
    result = scrub(inquiry(), is_duplicate=True)
    assert any(r.startswith("duplicate") for r in result.reasons)


def test_every_problem_is_reported_not_just_the_first():
    result = scrub(inquiry(consent=False, email="x@yopmail.com"), is_duplicate=True)
    assert len(result.reasons) == 3
