"""Deterministic Bay Area / Northern California location classification (Phase 5.2).

Produces exactly one of four outcomes for a job's location text:
    preferred          -- Bay Area / Northern California onsite or hybrid.
    remote_acceptable  -- clearly U.S.-remote (acceptable anywhere in the U.S., since the
                           candidate can work remotely from the Bay Area).
    out_of_region      -- clearly onsite/hybrid outside Northern California, or remote
                           explicitly restricted to another country/region.
    unclear            -- cannot be determined safely (missing, bare/ambiguous "remote",
                           or an unrecognized/ambiguous location such as plain "California").

This module NEVER affects eligibility -- see gradscout.eligibility, which has no
location awareness at all. It is consulted only by gradscout.analyze (to compute
location_classification/location_reason and apply the optional remote priority
penalty) and gradscout.pipeline (to gate normal individual alerts). A job is always
stored regardless of its classification.

Classification precedence (first match wins -- this is what makes a multi-location
string like "Bay Area, CA, New York, NY" resolve to ``preferred``, and what makes an
explicitly U.S.-remote listing resolve to ``remote_acceptable`` even if it also lists
other supported countries):

    1. Any configured preferred (Bay Area / NorCal) location mentioned anywhere.
    2. A "remote" signal (the word "remote" in the text, or a structured remote flag)
       combined with an explicit U.S. indication -> remote_acceptable, regardless of
       any other country also mentioned.
    3. A "remote" signal combined with an explicit non-U.S. country/region indication
       (and no U.S. indication) -> out_of_region.
    4. A bare/ambiguous "remote" signal with neither a U.S. nor a foreign indication
       -> unclear.
    5. A recognized onsite/hybrid location outside Northern California (Southern
       California, another named U.S. city, or a foreign country/city) -> out_of_region.
    6. Anything else unrecognized (including bare "California") -> unclear.
"""

from __future__ import annotations

from dataclasses import dataclass

from gradscout.models import CandidateProfile, LocationClassification
from gradscout.textmatch import contains_any, find_first, normalize

REMOTE_TOKENS = ("remote", "work from home", "wfh", "distributed team", "distributed")

# Explicit U.S. indication. "us" alone is safe as a word-boundary-only match (see
# gradscout.textmatch._boundary_pattern): it can never match inside another word like
# "focus" or "campus", only a standalone "us" token.
US_INDICATOR_TOKENS = (
    "united states", "usa", "u.s.a.", "u.s.", "us",
    "us-based", "us based", "anywhere in the us", "anywhere in the united states",
    "us only", "us residents",
)

# Explicit non-U.S. country/city/region indication.
FOREIGN_TOKENS = (
    "india", "canada", "toronto", "vancouver", "montreal",
    "united kingdom", "london", "manchester",
    "germany", "berlin", "munich", "france", "paris",
    "poland", "warsaw", "ireland", "dublin", "philippines", "manila",
    "mexico", "mexico city", "brazil", "sao paulo", "singapore",
    "australia", "sydney", "melbourne", "china", "beijing", "shanghai",
    "japan", "tokyo", "netherlands", "amsterdam", "spain", "madrid",
    "italy", "rome", "emea", "apac", "latam", "international",
)

# Explicit "not the U.S." phrasing. Checked BEFORE US_INDICATOR_TOKENS so a phrase like
# "outside the US" (which contains the literal word "us") is never misread as a U.S.
# indicator -- this is a negation of the U.S., not a mention of it.
US_NEGATION_TOKENS = (
    "outside the united states", "outside the us", "outside of the us",
    "non-us", "non us", "not available in the us", "excluding the us",
)

# Southern California -- explicitly out_of_region per spec, never preferred just for
# being "in California".
SOCAL_TOKENS = (
    "los angeles", "irvine", "san diego", "orange county", "santa monica",
    "long beach", "anaheim", "pasadena", "burbank", "culver city", "el segundo",
    "socal", "southern california", "san bernardino", "riverside", "costa mesa",
    "newport beach",
)

# A representative sample of other well-known non-NorCal U.S. metros, so common
# postings resolve to out_of_region without needing a full city gazetteer. Anything
# not covered here safely falls through to "unclear" rather than a wrong guess.
OTHER_US_OUT_OF_REGION_TOKENS = (
    "new york", "nyc", "brooklyn", "manhattan", "dallas", "austin", "seattle",
    "boston", "chicago", "atlanta", "miami", "denver", "houston", "philadelphia",
    "phoenix", "portland", "san antonio", "raleigh", "durham", "charlotte",
    "nashville", "salt lake city", "minneapolis", "detroit", "columbus",
    "pittsburgh", "baltimore", "washington dc", "washington, dc", "reston",
    "new jersey", "jersey city",
)


@dataclass
class LocationResult:
    classification: LocationClassification
    reason: str
    matched: str | None = None


def classify_location(
    location: str | None,
    remote: bool | None,
    preferred_locations: list[str] | tuple[str, ...],
) -> LocationResult:
    """Classify a job's location text (see module docstring for the precedence)."""
    norm = normalize(location)
    preferred_norm = [normalize(p) for p in preferred_locations if p]

    if not norm:
        return LocationResult(
            LocationClassification.unclear,
            "No location information provided" if not remote else
            "Marked remote but no location/country information provided",
        )

    # 1) Preferred (Bay Area / NorCal) anywhere in the text -- wins outright, so a
    # multi-location listing that offers even one NorCal option is preferred.
    preferred_hit = find_first(norm, preferred_norm)
    if preferred_hit:
        return LocationResult(
            LocationClassification.preferred,
            f"Matches a preferred Bay Area / Northern California location: '{preferred_hit}'",
            preferred_hit,
        )

    # 2-4) A remote signal (textual or structured) is resolved by country indication.
    if contains_any(norm, REMOTE_TOKENS) or remote:
        # Negation ("outside the US") must be checked before the bare "us" indicator,
        # since it literally contains the word "us" but means the opposite.
        negation_hit = find_first(norm, US_NEGATION_TOKENS)
        if negation_hit:
            return LocationResult(
                LocationClassification.out_of_region,
                f"Remote explicitly restricted outside the U.S.: '{negation_hit}'",
                negation_hit,
            )
        us_hit = find_first(norm, US_INDICATOR_TOKENS)
        if us_hit:
            return LocationResult(
                LocationClassification.remote_acceptable,
                f"Explicitly U.S.-remote: '{us_hit}'",
                us_hit,
            )
        foreign_hit = find_first(norm, FOREIGN_TOKENS)
        if foreign_hit:
            return LocationResult(
                LocationClassification.out_of_region,
                f"Remote explicitly restricted outside the U.S.: '{foreign_hit}'",
                foreign_hit,
            )
        return LocationResult(
            LocationClassification.unclear,
            "Bare or ambiguous remote wording; no U.S. or foreign indication to confirm",
        )

    # 5) Recognized onsite/hybrid location outside Northern California.
    socal_hit = find_first(norm, SOCAL_TOKENS)
    if socal_hit:
        return LocationResult(
            LocationClassification.out_of_region,
            f"Southern California location outside the Bay Area/NorCal: '{socal_hit}'",
            socal_hit,
        )
    other_us_hit = find_first(norm, OTHER_US_OUT_OF_REGION_TOKENS)
    if other_us_hit:
        return LocationResult(
            LocationClassification.out_of_region,
            f"U.S. location outside Northern California: '{other_us_hit}'",
            other_us_hit,
        )
    foreign_hit = find_first(norm, FOREIGN_TOKENS)
    if foreign_hit:
        return LocationResult(
            LocationClassification.out_of_region,
            f"Location outside the United States: '{foreign_hit}'",
            foreign_hit,
        )

    # 6) Unrecognized (including bare "California") -- conservative: never guess preferred.
    return LocationResult(
        LocationClassification.unclear,
        f"Location could not be confidently classified: '{location}'",
    )


def location_permits_alert(
    classification: LocationClassification, candidate: CandidateProfile
) -> bool:
    """Whether this location classification allows a normal individual alert.

    Never gates the eligibility-review digest (see gradscout.pipeline) -- only the
    ordinary/baseline eligible-job alert path.
    """
    if not candidate.location_required_for_alert:
        return True
    if classification == LocationClassification.preferred:
        return True
    if classification == LocationClassification.remote_acceptable:
        return candidate.allow_us_remote
    return False  # out_of_region / unclear


_LOCATION_LABELS = {
    LocationClassification.preferred: "Bay Area / NorCal",
    LocationClassification.remote_acceptable: "US Remote",
    LocationClassification.out_of_region: "Out of region",
    LocationClassification.unclear: "Location unclear",
}


def location_label(classification: LocationClassification) -> str:
    """Short human-readable label for Discord formatting."""
    return _LOCATION_LABELS.get(classification, classification.value)
