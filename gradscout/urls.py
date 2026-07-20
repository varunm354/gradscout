"""URL canonicalization for cross-source deduplication.

Two sources (e.g. a native ATS and a GitHub new-grad repo) frequently point at
the same posting via slightly different URLs. Canonicalization produces a stable
form so those collapse to one job, while we still preserve each source record.

Rules:
  - lowercase scheme + host, strip a leading "www."
  - drop default ports (80/443)
  - drop tracking query params (utm_*, ref, src, source, gh_src, ...)
  - KEEP meaningful params (e.g. greenhouse's gh_jid), sorted for stability
  - drop the fragment
  - strip a trailing slash (except the root path)
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Params that never identify a posting; safe to strip.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gh_src",
    "ref",
    "referrer",
    "source",
    "src",
    "trk",
    "trackingid",
    "mc_cid",
    "mc_eid",
}

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""

    parts = urlsplit(url)

    scheme = parts.scheme.lower()

    host = parts.hostname.lower() if parts.hostname else ""
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port is not None and _DEFAULT_PORTS.get(scheme) != str(parts.port):
        netloc = f"{host}:{parts.port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for (k, v) in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    kept.sort()
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))
