from gradscout.urls import canonicalize_url


def test_strips_tracking_params_and_fragment():
    url = "https://boards.greenhouse.io/stripe/jobs/123?utm_source=x&gh_src=abc#apply"
    assert canonicalize_url(url) == "https://boards.greenhouse.io/stripe/jobs/123"


def test_keeps_meaningful_params_sorted():
    url = "https://job-boards.greenhouse.io/embed?gh_jid=999&utm_medium=email&a=1"
    # gh_jid and a are meaningful and kept; utm_medium dropped; params sorted
    assert canonicalize_url(url) == "https://job-boards.greenhouse.io/embed?a=1&gh_jid=999"


def test_lowercases_host_strips_www_and_trailing_slash():
    url = "HTTPS://WWW.Example.com/Careers/Job/42/"
    assert canonicalize_url(url) == "https://example.com/Careers/Job/42"


def test_drops_default_port():
    assert canonicalize_url("https://example.com:443/x") == "https://example.com/x"
    assert canonicalize_url("http://example.com:80/x") == "http://example.com/x"


def test_two_sources_same_posting_canonicalize_equal():
    ats = "https://boards.greenhouse.io/acme/jobs/555"
    repo_link = "https://www.boards.greenhouse.io/acme/jobs/555/?utm_source=github&ref=repo"
    assert canonicalize_url(ats) == canonicalize_url(repo_link)


def test_empty_url_is_empty():
    assert canonicalize_url("") == ""
    assert canonicalize_url("   ") == ""
