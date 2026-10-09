"""
The Python port of the extension's profile facts (work authorisation + location).

Source of truth: chrome-extension/src/content/profileFacts.ts (and the geo tables in
geo.ts that it reads). The extension answers application forms from these facts; the match
engine has to read a saved profile the same way, or a job the extension would call
"authorised" scores as "needs sponsorship" here (and the reverse).

How the expectations were made:
  * The mirrored cases come from chrome-extension/test/profileFacts.test.ts. Each docstring
    cites the TypeScript test it mirrors.
  * Everything else (the answer table, the branch table, the polarity / country / address
    tables) was produced by running the REAL TypeScript (profileFacts.ts under Node) and
    reading what it returned, not by reading this port.
  * The "PINNED TS BEHAVIOUR" tests record behaviour that looks like a bug but is ported
    faithfully on purpose. The extension and the engine must keep agreeing, so a fix has to
    land on both sides at once: when it does, these tests change with it.
"""

from __future__ import annotations

import dataclasses
from typing import NamedTuple, Optional

import pytest

from backend.routers.fill import ApplicantProfile
from backend.routers.profile import ApplicationProfileOut
from backend.services.profile_facts import (
    FREE_MOVEMENT,
    CountryAuth,
    Fact,
    LocationFacts,
    ParsedAddress,
    authorized_in,
    countries_in,
    country_of,
    location_facts,
    needs_sponsorship_in,
    parse_address,
    polarity_of,
    residence_of,
    work_auth_facts,
)


def profile(**over):
    """The extension test's profile() helper: a full camelCase profile, every text field blank.

    A plain dict with the extension's JSON field names is one of the two accepted inputs.
    `authorizedUS` / `authorizedCanada` are left absent, as they are there.
    """
    base = {
        "firstName": "", "lastName": "", "email": "", "phone": "", "location": "",
        "addressStreet": "", "addressCity": "", "addressState": "", "postalCode": "", "country": "",
        "linkedin": "", "github": "", "portfolio": "", "currentCompany": "", "currentTitle": "",
        "workAuthorization": "", "requiresSponsorship": "", "dateOfBirth": "",
        "education": [], "experience": [], "skills": [], "coverLetter": "",
    }
    base.update(over)
    return base


def vc(fact):
    """(value, confidence) of a Fact, or None: what the TypeScript tests match on."""
    return None if fact is None else (fact.value, fact.confidence)


def loc_shape(loc: LocationFacts) -> dict:
    """A LocationFacts as {field: (value, confidence, source) | None}; region/country by code."""

    def one(fact, value=lambda v: v):
        return None if fact is None else (value(fact.value), fact.confidence, fact.source)

    return {
        "street": one(loc.street),
        "city": one(loc.city),
        "region": one(loc.region, lambda r: r.code),
        "postal_code": one(loc.postal_code),
        "country": one(loc.country, lambda c: c.code),
    }


def addr_shape(a: ParsedAddress) -> dict:
    """A ParsedAddress as plain values (only the parts that were identified)."""
    out = {}
    if a.street is not None:
        out["street"] = a.street
    if a.city is not None:
        out["city"] = a.city
    if a.region is not None:
        out["region"] = a.region.code
    if a.postal is not None:
        out["postal"] = (a.postal.code, a.postal.country)
    if a.country is not None:
        out["country"] = a.country.code
    return out


# =============================================================================================
# parse_address: mirrors of profileFacts.test.ts > parseAddress
# =============================================================================================


def test_parse_address_splits_a_full_canadian_address():
    """TS: profileFacts.test.ts > parseAddress > splits a full Canadian address"""
    a = parse_address("1055 W Georgia St, Vancouver, BC V6E 3P3, Canada")
    assert a.street == "1055 W Georgia St"
    assert a.city == "Vancouver"
    assert a.region is not None and a.region.code == "BC"
    assert a.postal is not None and a.postal.code == "V6E 3P3"
    assert a.country is not None and a.country.code == "CA"


def test_parse_address_splits_a_us_address_with_an_apartment_and_a_zip_after_the_state():
    """TS: profileFacts.test.ts > parseAddress > splits a US address with an apartment and a ZIP after the state"""
    a = parse_address("77 Massachusetts Ave, Apt 4B, Cambridge, MA 02139")
    assert a.street == "77 Massachusetts Ave, Apt 4B"
    assert a.city == "Cambridge"
    assert a.region is not None and a.region.name == "Massachusetts"
    assert a.postal is not None and a.postal.code == "02139"


def test_parse_address_does_not_take_a_five_digit_street_number_for_the_zip():
    """TS: profileFacts.test.ts > parseAddress > does not take a five-digit street number for the ZIP"""
    a = parse_address("12345 Main St, Springfield, IL 62701")
    assert a.postal is not None and a.postal.code == "62701"


def test_parse_address_reads_a_city_province_country_location_string():
    """TS: profileFacts.test.ts > parseAddress > reads a city, province, country location string"""
    a = parse_address("Toronto, ON, Canada")
    assert a.city == "Toronto"
    assert a.region is not None and a.region.name == "Ontario"
    assert a.country is not None and a.country.name == "Canada"


# Beyond the mirrored cases: (text, what the TypeScript parseAddress returned for it).
PARSE_CASES = [
    ("1055 W Georgia St, Vancouver, BC V6E 3P3, Canada",
     {"street": "1055 W Georgia St", "city": "Vancouver", "region": "BC", "postal": ("V6E 3P3", "CA"), "country": "CA"}),
    ("77 Massachusetts Ave, Apt 4B, Cambridge, MA 02139",
     {"street": "77 Massachusetts Ave, Apt 4B", "city": "Cambridge", "region": "MA", "postal": ("02139", "US")}),
    ("12345 Main St, Springfield, IL 62701",
     {"street": "12345 Main St", "city": "Springfield", "region": "IL", "postal": ("62701", "US")}),
    ("Toronto, ON, Canada", {"city": "Toronto", "region": "ON", "country": "CA"}),
    ("12 Bermondsey Street, London SE1 3TQ, United Kingdom",
     {"street": "12 Bermondsey Street", "postal": ("SE1 3TQ", "GB"), "country": "GB"}),
    ("4520 rue Saint-Denis, app. 3, Montréal, QC H2J 2L3",
     {"street": "4520 rue Saint-Denis", "city": "Montréal", "region": "QC", "postal": ("H2J 2L3", "CA")}),
    ("United States", {"country": "US"}),
    ("Ottawa", {"city": "Ottawa"}),
    ("New York, NY", {"city": "New York", "region": "NY"}),
    ("Washington, DC", {"city": "Washington", "region": "DC"}),
    ("Remote", {"city": "Remote"}),  # parse_address only parses; location_facts is what refuses "Remote"
    ("Unit 5, 100 King St W, Toronto, ON M5X 1A9",
     {"street": "Unit 5", "city": "Toronto", "region": "ON", "postal": ("M5X 1A9", "CA")}),
    ("Toronto\nON", {"city": "Toronto", "region": "ON"}),
    ("Toronto; ON; Canada", {"city": "Toronto", "region": "ON", "country": "CA"}),
    ("", {}),
    ("   ", {}),
    (",,;;", {}),
]


def test_parse_address_matches_the_typescript():
    """Same input, same parts as the extension's parseAddress (every mismatching case is reported)."""
    mismatches = [
        f"{text!r}: expected {expected}, got {got}"
        for text, expected in PARSE_CASES
        if (got := addr_shape(parse_address(text))) != expected
    ]
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_parse_address_tolerates_none():
    """TS: `(text || "")`: a missing address is an empty address."""
    assert parse_address(None) == ParsedAddress()


def test_country_of_prefers_the_named_country_then_the_region_then_the_postcode():
    """country_of (TS countryOf): named country, else the country its region exists in, else its postcode shape."""
    assert country_of(parse_address("Toronto, ON, Canada")).code == "CA"
    assert country_of(parse_address("Ottawa, ON")).code == "CA"  # ON only exists in Canada
    assert country_of(parse_address("Cambridge, MA 02139")).code == "US"
    assert country_of(parse_address("SE1 3TQ")).code == "GB"  # postcode shape alone
    assert country_of(parse_address("Springfield 62701")).code == "US"
    assert country_of(parse_address("Ottawa")) is None
    assert country_of(ParsedAddress()) is None


# =============================================================================================
# location_facts: mirrors of profileFacts.test.ts > locationFacts
# =============================================================================================


def test_location_facts_derives_city_province_country_from_the_location_string_alone_at_high_confidence():
    """TS: profileFacts.test.ts > locationFacts > derives city/province/country from the location string alone, at high confidence"""
    f = location_facts(profile(location="Toronto, ON, Canada"))
    assert vc(f.city) == ("Toronto", "high")
    assert f.region is not None and f.region.value.code == "ON"
    assert f.country is not None and f.country.confidence == "high"
    assert f.country.value.name == "Canada"


def test_location_facts_derives_the_country_from_a_province_code():
    """TS: profileFacts.test.ts > locationFacts > derives the country from a province code ('Ottawa, ON')"""
    f = location_facts(profile(location="Ottawa, ON"))
    assert f.country is not None
    assert f.country.value.code == "CA"
    assert f.country.confidence == "high"


def test_location_facts_a_bare_city_is_only_a_medium_hint_for_its_country_and_a_medium_city():
    """TS: profileFacts.test.ts > locationFacts > a bare city is only a MEDIUM hint for its country, and a medium city"""
    f = location_facts(profile(location="Toronto"))
    assert f.country is not None and f.country.confidence == "medium"
    assert f.city is not None and f.city.confidence == "medium"


def test_location_facts_splits_a_one_line_street_address_into_its_parts():
    """TS: profileFacts.test.ts > locationFacts > splits a one-line street address into its parts"""
    f = location_facts(profile(addressStreet="1055 W Georgia St, Vancouver, BC V6E 3P3, Canada"))
    assert f.street is not None and f.street.value == "1055 W Georgia St"
    assert f.city is not None and f.city.value == "Vancouver"
    assert f.region is not None and f.region.value.code == "BC"
    assert f.postal_code is not None and f.postal_code.value == "V6E 3P3"
    assert f.country is not None and f.country.value.code == "CA"


def test_location_facts_stated_structured_fields_win_over_parsed_ones():
    """TS: profileFacts.test.ts > locationFacts > stated structured fields win over parsed ones"""
    f = location_facts(profile(location="Toronto, ON, Canada", addressCity="Mississauga", country="Canada"))
    assert f.city is not None and f.city.value == "Mississauga"
    assert f.country is not None and f.country.source == "profile:country"


def test_location_facts_never_reads_remote_as_a_city():
    """TS: profileFacts.test.ts > locationFacts > never reads 'Remote' as a city"""
    assert location_facts(profile(location="Remote")).city is None


class Loc(NamedTuple):
    """One profile location and the facts the TypeScript derived from it."""

    id: str
    inp: dict
    street: Optional[tuple] = None
    city: Optional[tuple] = None
    region: Optional[tuple] = None
    postal_code: Optional[tuple] = None
    country: Optional[tuple] = None


# (value, confidence, source); region and country values are their codes.
LOCATIONS = [
    Loc("full-structured-address",
        dict(addressStreet="100 King St W", addressCity="Toronto", addressState="ON", postalCode="M5V 2T6", country="Canada"),
        street=("100 King St W", "high", "profile:addressStreet"), city=("Toronto", "high", "profile:addressCity"),
        region=("ON", "high", "profile:addressState"), postal_code=("M5V 2T6", "high", "profile:postalCode"),
        country=("CA", "high", "profile:country")),
    Loc("city-and-province-only",
        dict(addressCity="Toronto", addressState="ON"),
        city=("Toronto", "high", "profile:addressCity"), region=("ON", "high", "profile:addressState"),
        country=("CA", "high", "address:country")),
    Loc("city-alone-is-a-medium-country-hint",
        dict(addressCity="Toronto"),
        city=("Toronto", "high", "profile:addressCity"), country=("CA", "medium", "city-hint")),
    Loc("free-text-toronto-on",
        dict(location="Toronto, ON"),
        city=("Toronto", "high", "location:city"), region=("ON", "high", "location:region"),
        country=("CA", "high", "address:country")),
    Loc("free-text-city-province-country",
        dict(location="Toronto, ON, Canada"),
        city=("Toronto", "high", "location:city"), region=("ON", "high", "location:region"),
        country=("CA", "high", "address:country")),
    Loc("free-text-seattle-wa",
        dict(location="Seattle, WA"),
        city=("Seattle", "high", "location:city"), region=("WA", "high", "location:region"),
        country=("US", "high", "address:country")),
    Loc("bare-known-city",
        dict(location="Toronto"),
        city=("Toronto", "medium", "location:city"), country=("CA", "medium", "city-hint")),
    Loc("bare-us-city",
        dict(location="Seattle"),
        city=("Seattle", "medium", "location:city"), country=("US", "medium", "city-hint")),
    Loc("non-north-american-city-with-country",
        dict(location="Berlin, Germany"),
        city=("Berlin", "high", "location:city"), country=("DE", "high", "address:country")),
    Loc("non-north-american-city-without-country",
        dict(location="Mumbai"),
        city=("Mumbai", "medium", "location:city")),
    Loc("ambiguous-city-is-not-hinted",
        dict(location="London"),
        city=("London", "medium", "location:city")),
    Loc("uk-by-abbreviation",
        dict(location="London, UK"),
        city=("London", "high", "location:city"), country=("GB", "high", "address:country")),
    Loc("san-jose-ca-is-california-not-canada",
        dict(location="San Jose, CA"),
        city=("San Jose", "high", "location:city"), region=("CA", "high", "location:region"),
        country=("US", "high", "address:country")),
    Loc("default-united-states-alone",
        dict(location="United States"),
        country=("US", "high", "address:country")),
    Loc("remote-is-not-a-place",
        dict(location="Remote")),
    Loc("remote-with-a-country-keeps-only-the-country",
        dict(location="Remote, Canada"),
        country=("CA", "high", "address:country")),
    Loc("greater-toronto-area-is-not-a-city",
        dict(location="Greater Toronto Area")),
    Loc("one-line-street-address",
        dict(addressStreet="1055 W Georgia St, Vancouver, BC V6E 3P3, Canada"),
        street=("1055 W Georgia St", "high", "address:parsed-street"), city=("Vancouver", "high", "address:city"),
        region=("BC", "high", "address:region"), postal_code=("V6E 3P3", "high", "address:postal"),
        country=("CA", "high", "address:country")),
    Loc("stated-country-beats-the-default-location",
        dict(location="United States", country="Canada"),
        country=("CA", "high", "profile:country")),
    Loc("country-given-as-an-iso-code",
        dict(country="CA"),
        country=("CA", "high", "profile:country")),
    Loc("country-given-as-usa",
        dict(country="USA"),
        country=("US", "high", "profile:country")),
    Loc("nothing-stated", dict()),
]


def test_location_facts_matches_the_typescript():
    """location_facts on a structured address, city + province, free text, a non-North-American city,
    a bare city and the default "United States" (every mismatching case is reported)."""
    mismatches = []
    for case in LOCATIONS:
        got = loc_shape(location_facts(profile(**case.inp)))
        want = {
            "street": case.street, "city": case.city, "region": case.region,
            "postal_code": case.postal_code, "country": case.country,
        }
        if got != want:
            mismatches.append(f"{case.id}: expected {want}, got {got}")
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_residence_is_the_country_only_when_it_is_settled():
    """residence_of (TS questionResolver residenceOf): the country code, and only at high confidence."""
    assert residence_of(location_facts(profile(location="Toronto, ON"))) == "CA"
    assert residence_of(location_facts(profile(location="Seattle, WA"))) == "US"
    assert residence_of(location_facts(profile(location="Berlin, Germany"))) == "DE"
    assert residence_of(location_facts(profile(location="Toronto"))) is None  # only a medium hint
    assert residence_of(location_facts(profile(location="Mumbai"))) is None
    assert residence_of(location_facts(profile())) is None


# =============================================================================================
# work_auth_facts / authorized_in / needs_sponsorship_in: mirrors of profileFacts.test.ts
# =============================================================================================


def test_a_canadian_citizen_is_authorized_in_canada_with_no_sponsorship_and_unknown_for_the_us():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > a Canadian citizen is authorized in Canada with no sponsorship, and UNKNOWN for the US"""
    a = work_auth_facts(profile(workAuthorization="Canadian citizen"))
    assert vc(authorized_in(a, "CA", "CA")) == (True, "high")
    assert vc(needs_sponsorship_in(a, "CA", "CA")) == (False, "high")
    assert authorized_in(a, "US", "CA") is None
    assert needs_sponsorship_in(a, "US", "CA") is None


def test_authorized_to_work_in_canada_says_nothing_about_the_united_states():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > 'Authorized to work in Canada' says nothing about the United States"""
    a = work_auth_facts(profile(workAuthorization="Authorized to work in Canada", requiresSponsorship="No"))
    assert authorized_in(a, "US", "CA") is None
    assert authorized_in(a, "CA", "CA").value is True
    # The applicant's own "No" covers the country their status names, and an
    # unscoped question, but not a country they never mentioned.
    assert needs_sponsorship_in(a, "CA", "CA").value is False
    assert vc(needs_sponsorship_in(a, None, "CA")) == (False, "high")
    assert needs_sponsorship_in(a, "US", "CA") is None


def test_it_reads_a_two_country_statement():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > reads a two-country statement"""
    a = work_auth_facts(profile(workAuthorization="Authorized to work in Canada and the U.S."))
    assert authorized_in(a, "US", "CA").value is True
    assert authorized_in(a, "CA", "CA").value is True


def test_a_green_card_holder_needs_no_us_sponsorship():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > a green card holder needs no US sponsorship"""
    a = work_auth_facts(profile(workAuthorization="Green card holder"))
    assert authorized_in(a, "US", "US").value is True
    assert needs_sponsorship_in(a, "US", "US").value is False


def test_an_f1_students_authorization_is_unknown_but_their_stated_sponsorship_answer_stands():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > an F-1 student's authorization is unknown, but their stated sponsorship answer stands"""
    a = work_auth_facts(profile(workAuthorization="F-1 student visa (OPT eligible)", requiresSponsorship="Yes"))
    assert authorized_in(a, "US", "US") is None
    assert vc(needs_sponsorship_in(a, "US", "US")) == (True, "high")


def test_an_h1b_holder_is_authorized_and_needs_sponsorship():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > an H-1B holder is authorized and needs sponsorship"""
    a = work_auth_facts(profile(workAuthorization="H-1B"))
    assert authorized_in(a, "US", "US").value is True
    assert needs_sponsorship_in(a, "US", "US").value is True


def test_a_bare_yes_applies_to_the_applicants_own_country_and_to_unscoped_questions_only():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > a bare 'yes' applies to the applicant's own country and to unscoped questions only"""
    a = work_auth_facts(profile(workAuthorization="yes"))
    assert authorized_in(a, None, "CA").value is True
    assert authorized_in(a, "CA", "CA").value is True
    assert authorized_in(a, "US", "CA") is None


def test_a_negative_statement_is_a_no():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > a negative statement is a No"""
    a = work_auth_facts(profile(workAuthorization="Not authorized to work in the US"))
    assert authorized_in(a, "US", "CA").value is False


def test_north_america_is_not_the_united_states():
    """TS: profileFacts.test.ts > workAuthFacts / authorizedIn / needsSponsorshipIn > 'North America' is not the United States"""
    a = work_auth_facts(profile(workAuthorization="Authorized to work in North America"))
    assert "US" not in a.by_country


# =============================================================================================
# The saved answers Tailrd's Profile page stores, read the way the extension reads them
# =============================================================================================

Cell = Optional[tuple]  # (value, source) of a high-confidence fact, or None for "the profile does not say"


class Row(NamedTuple):
    """One combination of saved answers and the four facts the extension derives from it.

    wa  = "Are you authorized to work in this country?"          (workAuthorization)
    rs  = "Do you now or in the future require sponsorship?"      (requiresSponsorship)
    au  = "Authorized to work in the US"                          (authorizedUS)
    ac  = "Authorized to work in Canada"                          (authorizedCanada)
    loc = the location, which decides the residence               (location)
    resid = the residence that location yields (country, high confidence only)
    """

    id: str
    wa: str = ""
    rs: str = ""
    au: str = ""
    ac: str = ""
    loc: str = ""
    resid: Optional[str] = None
    auth_us: Cell = None
    auth_ca: Cell = None
    spons_us: Cell = None
    spons_ca: Cell = None


ROWS = [
    # --- the unscoped "Yes": it is about the country the applicant lives in ---
    Row('yes_sponsor-no_toronto', wa='Yes', rs='No', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:unscoped-residence'), spons_ca=(False, 'sponsorship:stated')),
    Row('yes_sponsor-no_seattle', wa='Yes', rs='No', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:unscoped-residence'), spons_us=(False, 'sponsorship:stated')),
    Row('yes_sponsor-no_unknown-residence', wa='Yes', rs='No', resid=None, spons_us=(False, 'sponsorship:stated'), spons_ca=(False, 'sponsorship:stated')),
    Row('yes_sponsor-no_bare-city-is-not-residence', wa='Yes', rs='No', loc='Toronto', resid=None, spons_us=(False, 'sponsorship:stated'), spons_ca=(False, 'sponsorship:stated')),
    Row('yes_sponsor-yes_toronto', wa='Yes', rs='Yes', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:unscoped-residence'), spons_ca=(True, 'sponsorship:stated')),
    Row('yes_sponsor-blank_seattle', wa='Yes', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:unscoped-residence')),
    # --- citizens ---
    Row('canadian-citizen_sponsor-blank_toronto', wa='Canadian citizen', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:citizen'), spons_ca=(False, 'sponsorship:citizen')),
    Row('canadian-citizen_sponsor-yes_toronto', wa='Canadian citizen', rs='Yes', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:citizen'), spons_ca=(False, 'sponsorship:citizen')),
    Row('canadian-citizen_sponsor-no_seattle', wa='Canadian citizen', rs='No', loc='Seattle, WA', resid='US', auth_ca=(True, 'work-auth:citizen'), spons_ca=(False, 'sponsorship:citizen')),
    Row('us-citizen_explicit_seattle', wa='U.S. citizen', rs='No', au='Yes', ac='No', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:citizen'), auth_ca=(False, 'work-auth:stated'), spons_us=(False, 'sponsorship:citizen'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    # --- a sentence naming the country ---
    Row('legally-authorized-canada_sponsor-no_toronto', wa='Yes, I am legally authorized to work in Canada', rs='No', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:statement'), spons_ca=(False, 'sponsorship:stated')),
    Row('legally-authorized-canada_sponsor-blank_toronto', wa='Yes, I am legally authorized to work in Canada', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:statement')),
    Row('authorized-canada-and-us_toronto', wa='Authorized to work in Canada and the U.S.', rs='No', loc='Toronto, ON', resid='CA', auth_us=(True, 'work-auth:statement'), auth_ca=(True, 'work-auth:statement'), spons_us=(False, 'sponsorship:stated'), spons_ca=(False, 'sponsorship:stated')),
    Row('authorized-us-only_seattle', wa='Authorized to work in the US', rs='No', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:statement'), spons_us=(False, 'sponsorship:stated')),
    # --- the per-country answers on their own ---
    Row('explicit-us-yes-only_seattle', au='Yes', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:stated')),
    Row('explicit-ca-yes-only_toronto', ac='Yes', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:stated')),
    Row('explicit-us-no_ca-yes_toronto', au='No', ac='Yes', loc='Toronto, ON', resid='CA', auth_us=(False, 'work-auth:stated'), auth_ca=(True, 'work-auth:stated'), spons_us=(True, 'sponsorship:not-authorized-stated')),
    Row('explicit-us-yes_ca-no_seattle', au='Yes', ac='No', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:stated'), auth_ca=(False, 'work-auth:stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('explicit-both-yes_toronto', rs='No', au='Yes', ac='Yes', loc='Toronto, ON', resid='CA', auth_us=(True, 'work-auth:stated'), auth_ca=(True, 'work-auth:stated'), spons_us=(False, 'sponsorship:stated'), spons_ca=(False, 'sponsorship:stated')),
    Row('explicit-both-no_unknown-residence', rs='Yes', au='No', ac='No', resid=None, auth_us=(False, 'work-auth:stated'), auth_ca=(False, 'work-auth:stated'), spons_us=(True, 'sponsorship:not-authorized-stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    # --- a per-country answer against the unscoped one: the per-country answer wins ---
    Row('conflict_unscoped-yes_us-no_ca-yes_toronto', wa='Yes', rs='No', au='No', ac='Yes', loc='Toronto, ON', resid='CA', auth_us=(False, 'work-auth:stated'), auth_ca=(True, 'work-auth:stated'), spons_us=(True, 'sponsorship:not-authorized-stated'), spons_ca=(False, 'sponsorship:stated')),
    Row('conflict_unscoped-yes_ca-no_toronto', wa='Yes', rs='No', ac='No', loc='Toronto, ON', resid='CA', auth_ca=(False, 'work-auth:stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('conflict_canadian-citizen_ca-no', wa='Canadian citizen', ac='No', loc='Toronto, ON', resid='CA', auth_ca=(False, 'work-auth:stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('conflict_authorized-us_us-no', wa='Authorized to work in the US', rs='No', au='No', loc='Seattle, WA', resid='US', auth_us=(False, 'work-auth:stated'), spons_us=(True, 'sponsorship:not-authorized-stated')),
    Row('conflict_text-denies-us_us-yes', wa='Not authorized to work in the US', au='Yes', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:denied')),
    Row('agree_canadian-citizen_ca-yes_us-no', wa='Canadian citizen', au='No', ac='Yes', loc='Toronto, ON', resid='CA', auth_us=(False, 'work-auth:stated'), auth_ca=(True, 'work-auth:citizen'), spons_us=(True, 'sponsorship:not-authorized-stated'), spons_ca=(False, 'sponsorship:citizen')),
    # --- visas and permits ---
    Row('h1b_seattle', wa='H-1B visa (transfer required)', rs='Yes', au='Yes', ac='No', loc='Seattle, WA', resid='US', auth_us=(True, 'work-auth:work_permit'), auth_ca=(False, 'work-auth:stated'), spons_us=(True, 'sponsorship:stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('green-card_san-jose', wa='U.S. permanent resident (green card)', rs='No', au='Yes', ac='No', loc='San Jose, CA', resid='US', auth_us=(True, 'work-auth:permanent_resident'), auth_ca=(False, 'work-auth:stated'), spons_us=(False, 'sponsorship:permanent_resident'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('stem-opt_boston', wa='F-1 STEM OPT (EAD valid through June 2028)', rs='Yes', au='Yes', ac='No', loc='Boston, MA, USA', resid='US', auth_us=(True, 'work-auth:student'), auth_ca=(False, 'work-auth:stated'), spons_us=(True, 'sponsorship:stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('f1-student_no-explicit_cambridge', wa='F-1 student visa (OPT eligible)', rs='Yes', loc='Cambridge, MA 02139', resid='US', spons_us=(True, 'sponsorship:stated')),
    Row('permanent-resident-canada_montreal', wa='Permanent resident of Canada', rs='No', au='No', ac='Yes', loc='Montréal, QC', resid='CA', auth_us=(False, 'work-auth:stated'), auth_ca=(True, 'work-auth:permanent_resident'), spons_us=(True, 'sponsorship:not-authorized-stated'), spons_ca=(False, 'sponsorship:permanent_resident')),
    Row('pgwp_toronto', wa='PGWP (open work permit)', rs='Yes', loc='Toronto, ON', resid='CA', auth_ca=(True, 'work-auth:work_permit'), spons_ca=(True, 'sponsorship:stated')),
    # --- blank means unanswered, never "no" ---
    Row('all-blank_toronto', loc='Toronto, ON', resid='CA'),
    Row('all-blank_unknown-residence', resid=None),
    Row('only-sponsor-yes_toronto', rs='Yes', loc='Toronto, ON', resid='CA', spons_ca=(True, 'sponsorship:stated')),
    Row('whitespace-only-answers_toronto', wa='  ', rs=' ', au=' ', ac='  ', loc='Toronto, ON', resid='CA'),
    # --- living outside North America ---
    Row('german-citizen_berlin', wa='German citizen (EU)', rs='Yes', au='No', ac='No', loc='Berlin, Germany', resid='DE', auth_us=(False, 'work-auth:stated'), auth_ca=(False, 'work-auth:stated'), spons_us=(True, 'sponsorship:not-authorized-stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
    Row('british-citizen_london', wa='British citizen', rs='Yes', au='No', ac='No', loc='London, United Kingdom', resid='GB', auth_us=(False, 'work-auth:stated'), auth_ca=(False, 'work-auth:stated'), spons_us=(True, 'sponsorship:not-authorized-stated'), spons_ca=(True, 'sponsorship:not-authorized-stated')),
]


def row_profile(row: Row) -> dict:
    return profile(
        workAuthorization=row.wa, requiresSponsorship=row.rs,
        authorizedUS=row.au, authorizedCanada=row.ac, location=row.loc,
    )


def assert_cell(name: str, fact, cell: Cell) -> None:
    """A country-scoped answer is either not stated, or stated at high confidence by the named rule."""
    if cell is None:
        assert fact is None, f"{name}: expected no fact, got {fact}"
        return
    assert fact is not None, f"{name}: expected {cell}, got no fact"
    assert (fact.value, fact.source) == cell, f"{name}: expected {cell}, got {(fact.value, fact.source)}"
    assert fact.confidence == "high", f"{name}: expected high confidence, got {fact.confidence}"


def check_row(row: Row, p) -> None:
    residence = residence_of(location_facts(p))
    assert residence == row.resid
    auth = work_auth_facts(p)
    assert_cell("authorized_in US", authorized_in(auth, "US", residence), row.auth_us)
    assert_cell("authorized_in CA", authorized_in(auth, "CA", residence), row.auth_ca)
    assert_cell("needs_sponsorship_in US", needs_sponsorship_in(auth, "US", residence), row.spons_us)
    assert_cell("needs_sponsorship_in CA", needs_sponsorship_in(auth, "CA", residence), row.spons_ca)


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_saved_answers_read_like_the_extension_reads_them(row):
    """authorized_in / needs_sponsorship_in for US and CA over realistic saved answers (dict input)."""
    check_row(row, row_profile(row))


def test_saved_answers_read_the_same_from_application_profile_out():
    """The backend holds an ApplicationProfileOut (build_application_profile): every row of the table
    must read exactly like the dict the extension gets."""
    for row in ROWS:
        p = row_profile(row)
        model = ApplicationProfileOut(**p)
        try:
            check_row(row, model)
        except AssertionError as err:
            raise AssertionError(f"{row.id} (ApplicationProfileOut): {err}") from err
        assert work_auth_facts(model) == work_auth_facts(p), row.id
        assert location_facts(model) == location_facts(p), row.id


def test_a_per_country_answer_beats_the_unscoped_one_for_that_country_only():
    """The 2026-10-03 live case (profileFacts.ts applyExplicitCountries): unscoped Yes + sponsorship No + US No."""
    a = work_auth_facts(profile(workAuthorization="Yes", requiresSponsorship="No", authorizedUS="No", authorizedCanada="Yes"))
    # The employer's question for the US is answered by the US answer, not by the general "No sponsorship".
    assert authorized_in(a, "US", "CA") == Fact(False, "high", "work-auth:stated")
    assert needs_sponsorship_in(a, "US", "CA") == Fact(True, "high", "sponsorship:not-authorized-stated")
    # Canada keeps the general sponsorship answer.
    assert authorized_in(a, "CA", "CA") == Fact(True, "high", "work-auth:stated")
    assert needs_sponsorship_in(a, "CA", "CA") == Fact(False, "high", "sponsorship:stated")


# =============================================================================================
# Every branch of authorized_in / needs_sponsorship_in
# =============================================================================================


class Branch(NamedTuple):
    id: str
    answers: dict
    country: Optional[str]
    residence: Optional[str]
    auth: Optional[tuple]  # (value, confidence, source)
    spons: Optional[tuple]


def _b(id, answers, country, residence, auth=None, spons=None):
    return Branch(id, answers, country, residence, auth, spons)


BRANCHES = [
    # --- free movement: a citizen of the EU / EEA / Switzerland may work in any other ---
    _b('free-movement_german-citizen_asked-austria', {'workAuthorization': 'German citizen (EU)'}, 'AT', 'DE',
       auth=(True, 'high', 'work-auth:free-movement'), spons=(False, 'high', 'sponsorship:free-movement')),
    _b('free-movement_german-citizen_asked-norway', {'workAuthorization': 'German citizen (EU)', 'requiresSponsorship': 'Yes'}, 'NO', 'DE',
       auth=(True, 'high', 'work-auth:free-movement'), spons=(False, 'high', 'sponsorship:free-movement')),
    _b('free-movement_stops-at-the-uk', {'workAuthorization': 'German citizen (EU)'}, 'GB', 'DE'),
    _b('free-movement_stops-at-the-us', {'workAuthorization': 'German citizen (EU)'}, 'US', 'DE'),
    _b('free-movement_needs-a-citizen-not-a-resident', {'workAuthorization': 'Permanent resident of Germany'}, 'AT', 'DE'),
    _b('free-movement_canadian-citizen_asked-france', {'workAuthorization': 'Canadian citizen'}, 'FR', 'CA'),
    _b('free-movement_explicit-no-still-wins', {'workAuthorization': 'German citizen (EU)', 'authorizedCanada': 'No'}, 'CA', 'DE',
       auth=(False, 'high', 'work-auth:stated'), spons=(True, 'high', 'sponsorship:not-authorized-stated')),
    # --- sponsorship that the visa itself settles ---
    _b('visa_h1b-no-stated-answer_needs-sponsorship', {'workAuthorization': 'H-1B'}, 'US', 'US',
       auth=(True, 'high', 'work-auth:work_permit'), spons=(True, 'high', 'sponsorship:work_permit')),
    _b('visa_pgwp-no-stated-answer_unknown', {'workAuthorization': 'PGWP (open work permit)'}, 'CA', 'CA',
       auth=(True, 'high', 'work-auth:work_permit')),
    _b('visa_f1-student-no-stated-answer_unknown', {'workAuthorization': 'F-1 student visa (OPT eligible)'}, 'US', 'US'),
    _b('text-denial_sponsorship', {'workAuthorization': 'Authorized to work in Canada, not authorized in the US'}, 'US', 'CA',
       auth=(False, 'high', 'work-auth:denied'), spons=(True, 'high', 'sponsorship:not-authorized-denied')),
    # --- the question names no country ---
    _b('no-country_unscoped-yes', {'workAuthorization': 'Yes'}, None, 'CA',
       auth=(True, 'high', 'work-auth:unscoped')),
    _b('no-country_unscoped-yes_empty-string-is-no-country', {'workAuthorization': 'Yes'}, '', 'CA',
       auth=(True, 'high', 'work-auth:unscoped')),
    _b('no-country_unscoped-no', {'workAuthorization': 'No'}, None, 'CA',
       auth=(False, 'high', 'work-auth:unscoped')),
    _b('no-country_nothing-said', {}, None, 'CA'),
    _b('no-country_stated-with-one-status', {'workAuthorization': 'Canadian citizen', 'requiresSponsorship': 'Yes'}, None, 'CA',
       spons=(True, 'high', 'sponsorship:stated-unscoped')),
    _b('no-country_stated-but-not-authorized-somewhere', {'authorizedUS': 'No', 'requiresSponsorship': 'No'}, None, 'CA',
       spons=(False, 'medium', 'sponsorship:stated-unscoped-not-everywhere')),
    _b('no-country_stated-two-countries-residence-covered', {'workAuthorization': 'Authorized to work in Canada and the U.S.', 'requiresSponsorship': 'No'}, None, 'CA',
       spons=(False, 'high', 'sponsorship:stated-unscoped')),
    _b('no-country_stated-two-countries-residence-elsewhere', {'workAuthorization': 'Authorized to work in Canada and the U.S.', 'requiresSponsorship': 'No'}, None, 'GB',
       spons=(False, 'medium', 'sponsorship:stated-multi-country')),
    _b('no-country_stated-two-countries-residence-unknown', {'workAuthorization': 'Authorized to work in Canada and the U.S.', 'requiresSponsorship': 'No'}, None, None,
       spons=(False, 'medium', 'sponsorship:stated-multi-country')),
    _b('no-country_single-citizen-no-stated', {'workAuthorization': 'British citizen'}, None, 'GB',
       spons=(False, 'medium', 'sponsorship:citizen-unscoped')),
    _b('no-country_single-resident-no-stated', {'workAuthorization': 'Permanent resident of Germany'}, None, 'DE',
       spons=(False, 'medium', 'sponsorship:permanent_resident-unscoped')),
    _b('no-country_single-statement-no-stated', {'workAuthorization': 'Authorized to work in Canada'}, None, 'CA'),
    _b('no-country_two-statuses-no-stated', {'workAuthorization': 'Canadian citizen and US citizen'}, None, 'CA'),
    # --- the applicant's own sponsorship answer, and where it applies ---
    _b('country_stated-answer-needs-covered-status', {'workAuthorization': 'Authorized to work in Canada', 'requiresSponsorship': 'Yes'}, 'CA', 'CA',
       auth=(True, 'high', 'work-auth:statement'), spons=(True, 'high', 'sponsorship:stated')),
    _b('country_stated-answer-applies-with-no-status-and-matching-residence', {'requiresSponsorship': 'Yes'}, 'CA', 'CA',
       spons=(True, 'high', 'sponsorship:stated')),
    _b('country_stated-answer-not-applied-to-another-residence', {'requiresSponsorship': 'Yes'}, 'US', 'CA'),
    _b('country_stated-answer-applies-when-residence-unknown', {'requiresSponsorship': 'Yes'}, 'US', None,
       spons=(True, 'high', 'sponsorship:stated')),
]


def fact3(fact):
    return None if fact is None else (fact.value, fact.confidence, fact.source)


def test_every_branch_of_authorized_in_and_needs_sponsorship_in():
    """Free movement, visa-settled sponsorship, the no-country (medium) paths, and where the stated answer
    applies (every mismatching case is reported)."""
    mismatches = []
    for b in BRANCHES:
        a = work_auth_facts(profile(**b.answers))
        got_auth = fact3(authorized_in(a, b.country, b.residence))
        got_spons = fact3(needs_sponsorship_in(a, b.country, b.residence))
        if got_auth != b.auth:
            mismatches.append(f"{b.id}: authorized_in expected {b.auth}, got {got_auth}")
        if got_spons != b.spons:
            mismatches.append(f"{b.id}: needs_sponsorship_in expected {b.spons}, got {got_spons}")
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_free_movement_group_is_the_eu_the_eea_and_switzerland():
    """TS: FREE_MOVEMENT in profileFacts.ts (31 codes); the UK left in 2020 and is not in it."""
    assert len(FREE_MOVEMENT) == 31
    assert {"DE", "AT", "FR", "IE", "NO", "IS", "LI", "CH"} <= FREE_MOVEMENT
    assert not ({"GB", "US", "CA", "TR", "UA"} & FREE_MOVEMENT)


# =============================================================================================
# polarity_of and countries_in
# =============================================================================================

# (stored answer, what the TypeScript polarityOf returned)
POLARITY = [
    ('Yes', True), ('yes', True), ('YES', True), ('Yes.', True), (' yes ', True), ('y', True), ('Y', True),
    ('true', True), ('True', True), ('oui', True), ('Oui', True), ('si', True),
    ('No', False), ('no', False), ('NO', False), ('n', False), ('N', False), ('false', False), ('non', False),
    ('None', False), ('none', False), ('Not required', False), ('not needed', False),
    ('I do not require sponsorship', False), ("I don't need sponsorship", False), ('Will not require', False),
    ('no longer require sponsorship', False),
    ('I will require sponsorship', True), ('Do need sponsorship', True),
    ('Sponsorship required', True), ('Sponsorship needed', True),
    ('', None), ('   ', None), ('maybe', None), ('unsure', None), ('Nope', None), ('yep', None), ('Never', None),
    # whitespace as JavaScript trims it: a byte-order mark, a non-breaking space, a line separator
    ('\ufeff Yes \u00a0', True), ('\u00a0no\u2028', False),
]


def test_polarity_of_matches_the_typescript():
    """A short stored answer read as yes / no / unknown; blank is unknown, never no."""
    mismatches = [
        f"{text!r}: expected {expected}, got {got}"
        for text, expected in POLARITY
        if (got := polarity_of(text)) is not expected
    ]
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_polarity_of_tolerates_none():
    """TS: `(text || "")`."""
    assert polarity_of(None) is None


# (free text, the country codes the TypeScript countriesIn found, in order)
COUNTRIES_IN = [
    ('Canadian citizen', ["CA"]), ('Canada', ["CA"]),
    ('Authorized to work in Canada and the U.S.', ["US", "CA"]),
    ('US Citizen', ["US"]), ('U.S. citizen', ["US"]), ('Authorized to work in the US', ["US"]),
    ('authorized to work in us', ["US"]), ('work for us', []),
    ('Authorized to work in North America', []), ('Authorized to work in America', ["US"]),
    ('American citizen', ["US"]), ('Latin America', []), ('Central America', []), ('South American', []),
    ('United States of America', ["US"]), ('USA', ["US"]), ('U.S.A.', ["US"]), ('the States', ["US"]),
    ('H-1B', ["US"]), ('F-1 STEM OPT (EAD valid through June 2028)', ["US"]), ('TN visa', ["US"]),
    ('Green card holder', ["US"]), ('L-1A', []),
    ('PR card', ["CA"]), ('PGWP', ["CA"]), ('Post-Graduation Work Permit', ["CA"]),
    ('study permit', ["CA"]), ('landed immigrant', ["CA"]),
    ('British citizen', ["GB"]), ('UK', ["GB"]), ('u.k.', ["GB"]), ('Great Britain', ["GB"]), ('England', ["GB"]),
    ('German citizen (EU)', ["DE"]), ('Germany', ["DE"]), ('Irish passport', ["IE"]),
    ('Citoyen canadien', []), ('France', ["FR"]), ('Indian citizen', ["IN"]), ('India', ["IN"]),
    ('Mexican', ["MX"]), ('Mexico', ["MX"]), ('polish', ["PL"]),
    ('', []), ('   ', []), ('Yes', []), ('No', []),
]


def test_countries_in_matches_the_typescript():
    """Countries named by name, alias, demonym, or a status that only exists in one country."""
    mismatches = [
        f"{text!r}: expected {codes}, got {got}"
        for text, codes in COUNTRIES_IN
        if (got := [c.code for c in countries_in(text)]) != codes
    ]
    assert not mismatches, "\n" + "\n".join(mismatches)


def test_countries_in_tolerates_none():
    assert countries_in(None) == []


# =============================================================================================
# Input shapes and immutability (the port's own contract)
# =============================================================================================


def test_every_accepted_input_shape_reads_the_same():
    """A dict (the extension's JSON), an ApplicationProfileOut (the backend), and an /api/fill ApplicantProfile."""
    answers = dict(
        location="Toronto, ON", addressStreet="100 King St W", addressCity="Toronto", addressState="ON",
        postalCode="M5V 2T6", country="Canada", workAuthorization="Canadian citizen",
        requiresSponsorship="No", authorizedUS="No", authorizedCanada="Yes",
    )
    as_dict = profile(**answers)
    as_model = ApplicationProfileOut(**answers)
    as_fill = ApplicantProfile(**answers)
    assert location_facts(as_model) == location_facts(as_dict)
    assert location_facts(as_fill) == location_facts(as_dict)
    assert work_auth_facts(as_model) == work_auth_facts(as_dict)
    assert work_auth_facts(as_fill) == work_auth_facts(as_dict)
    # ...and they read something, so the equalities above are not two blanks agreeing.
    assert vc(authorized_in(work_auth_facts(as_model), "CA", "CA")) == (True, "high")
    assert loc_shape(location_facts(as_fill))["postal_code"] == ("M5V 2T6", "high", "profile:postalCode")


def test_absent_null_and_non_string_fields_read_as_blank():
    """Blank means unanswered, never no: a missing key, null, or junk in a field is a blank, not a crash."""
    blank = work_auth_facts(profile())
    assert work_auth_facts({}) == blank
    assert work_auth_facts({"workAuthorization": None, "requiresSponsorship": None, "authorizedUS": None}) == blank
    assert work_auth_facts({"workAuthorization": 5, "requiresSponsorship": True, "authorizedCanada": ["Yes"]}) == blank
    assert work_auth_facts(None) == blank
    blank_loc = location_facts(profile())
    assert location_facts({}) == blank_loc
    assert location_facts({"location": None, "addressStreet": 7, "country": ["Canada"]}) == blank_loc
    assert location_facts(None) == blank_loc
    assert loc_shape(blank_loc) == {"street": None, "city": None, "region": None, "postal_code": None, "country": None}
    assert blank.by_country == {} and blank.stated_sponsorship is None and blank.unscoped_authorized is None


def test_facts_are_frozen_and_the_per_country_map_is_read_only():
    """Frozen dataclasses mirror the TypeScript types; nothing a caller holds can be edited in place."""
    a = work_auth_facts(profile(workAuthorization="Canadian citizen"))
    fact = authorized_in(a, "CA", "CA")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.value = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.stated_sponsorship = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        a.by_country["US"] = CountryAuth(True, False, "citizen")  # type: ignore[index]
    loc = location_facts(profile(location="Toronto, ON"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        loc.city = None  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parse_address("Toronto, ON").city = "x"  # type: ignore[misc]


def test_the_per_country_map_keeps_the_order_the_countries_were_named():
    """TS byCountry is a Map (insertion order); the explicit per-country answers append after the text's countries."""
    a = work_auth_facts(profile(workAuthorization="Canadian citizen", authorizedUS="No"))
    assert list(a.by_country) == ["CA", "US"]
    assert a.by_country["CA"] == CountryAuth(True, False, "citizen")
    assert a.by_country["US"] == CountryAuth(False, True, "stated")
    b = work_auth_facts(profile(workAuthorization="Authorized to work in Canada and the U.S."))
    assert list(b.by_country) == ["US", "CA"]  # countries_in order: the US patterns are tried first


def test_javascript_regex_semantics_survive_the_port():
    """The patterns are JavaScript's: \\b and \\d are ASCII-only, \\s is the Unicode whitespace set.

    Python's defaults differ on all three, so each of these would flip if the port dropped the
    translation (see _js_regex): pasted text carries non-breaking spaces, and a word boundary
    next to an accented letter is a boundary in JavaScript.
    """
    # A non-breaking space is whitespace: "Not\xa0authorized" is still a denial.
    nbsp = work_auth_facts(profile(workAuthorization="Not\u00a0authorized to work in the US"))
    assert authorized_in(nbsp, "US", "CA") == Fact(False, "high", "work-auth:denied")
    assert polarity_of("I do not\u00a0require sponsorship") is False
    # U+0085 (NEL) is not JavaScript whitespace, although Python's str.isspace() says it is.
    assert polarity_of("I do not\u0085require sponsorship") is None
    # "y" / "n" followed by an accented letter still end at a word boundary.
    assert polarity_of("yés") is True
    assert polarity_of("né") is False
    # Surrounding non-breaking spaces do not hide the status.
    a = work_auth_facts(profile(workAuthorization="\u00a0Canadian\u00a0citizen\u00a0"))
    assert a.by_country["CA"] == CountryAuth(True, False, "citizen")


# =============================================================================================
# PINNED TS BEHAVIOUR: looks like a bug, ported faithfully on purpose
# =============================================================================================
# The extension and the engine must agree. Each test below records what the TypeScript does,
# so the port cannot drift from it silently. If the extension is fixed, fix the port in the same
# change and update the test; do not "fix" only one side. All of these are listed in the task
# report with the likely consequence for the match engine.


@pytest.mark.parametrize("statement", ["Iranian citizen", "French citizen"])
def test_pin_a_citizenship_the_country_table_lacks_reads_as_an_unscoped_yes(statement):
    """PINNED TS BEHAVIOUR (probable bug): the demonym is missing from geo.ts (no "french" alias, no Iran).

    No country is found, so "<nationality> citizen" falls into the unscoped "citizen" branch
    and becomes "authorized wherever the applicant lives", at high confidence. A French citizen
    living in Toronto reads as authorized to work in Canada.
    """
    a = work_auth_facts(profile(workAuthorization=statement))
    assert dict(a.by_country) == {}
    assert a.unscoped_authorized is True
    assert authorized_in(a, "CA", "CA") == Fact(True, "high", "work-auth:unscoped-residence")
    assert authorized_in(a, "US", "CA") is None
    assert authorized_in(a, None, "CA") == Fact(True, "high", "work-auth:unscoped")


def test_pin_a_citizenship_the_table_knows_stays_scoped_to_its_country():
    """The contrast to the pin above: "Indian citizen" is scoped to India, so it says nothing about Canada."""
    a = work_auth_facts(profile(workAuthorization="Indian citizen"))
    assert a.by_country["IN"] == CountryAuth(True, False, "citizen")
    assert a.unscoped_authorized is None
    assert authorized_in(a, "CA", "CA") is None


def test_pin_a_french_canadian_statement_reads_as_nothing_at_all():
    """PINNED TS BEHAVIOUR (limitation): only English wording is read. "Citoyen canadien" yields no fact."""
    a = work_auth_facts(profile(workAuthorization="Citoyen canadien"))
    assert dict(a.by_country) == {}
    assert a.unscoped_authorized is None
    assert authorized_in(a, "CA", "CA") is None


def test_pin_a_negation_is_not_attributed_to_one_country():
    """PINNED TS BEHAVIOUR (probable bug): one reading is applied to EVERY country the sentence names.

    "...Canada but not in the US" has no "not authorized" phrase, so it is a plain affirmative
    for both countries: authorized in the US. "...Canada, not authorized in the US" matches the
    denial pattern, so it is a denial for both: NOT authorized in Canada. The per-country
    answers (authorizedUS / authorizedCanada) repair either, see the last assertions.
    """
    but = work_auth_facts(profile(workAuthorization="Authorized to work in Canada but not in the US"))
    assert authorized_in(but, "US", "CA") == Fact(True, "high", "work-auth:statement")
    assert authorized_in(but, "CA", "CA") == Fact(True, "high", "work-auth:statement")

    comma = work_auth_facts(profile(workAuthorization="Authorized to work in Canada, not authorized in the US"))
    assert authorized_in(comma, "CA", "CA") == Fact(False, "high", "work-auth:denied")
    assert needs_sponsorship_in(comma, "CA", "CA") == Fact(True, "high", "sponsorship:not-authorized-denied")

    fixed = work_auth_facts(profile(
        workAuthorization="Authorized to work in Canada but not in the US", authorizedUS="No", authorizedCanada="Yes"))
    assert authorized_in(fixed, "US", "CA") == Fact(False, "high", "work-auth:stated")
    assert authorized_in(fixed, "CA", "CA") == Fact(True, "high", "work-auth:statement")


def test_pin_one_status_is_applied_to_every_named_country():
    """PINNED TS BEHAVIOUR (probable bug): "Canadian citizen with H-1B in the US" is a citizen of the US too.

    The status words are read once for the whole sentence, then stamped on each country found.
    """
    a = work_auth_facts(profile(workAuthorization="Canadian citizen with H-1B in the US"))
    assert a.by_country["CA"] == CountryAuth(True, False, "citizen")
    assert a.by_country["US"] == CountryAuth(True, False, "citizen")
    assert needs_sponsorship_in(a, "US", "CA") == Fact(False, "high", "sponsorship:citizen")


@pytest.mark.parametrize("text", ["N/A", "n/a", "Not sure", "Not applicable"])
def test_pin_a_non_answer_that_starts_with_n_reads_as_no(text):
    """PINNED TS BEHAVIOUR (probable bug): /^(no|n|false|non|none|not)\\b/ takes the "N" of "N/A" for a No."""
    assert polarity_of(text) is False


def test_pin_na_in_a_saved_answer_becomes_a_high_confidence_no():
    """PINNED TS BEHAVIOUR (probable bug): the consequence of the pin above for the two saved answers."""
    unauthorised = work_auth_facts(profile(workAuthorization="N/A"))
    assert unauthorised.unscoped_authorized is False
    assert authorized_in(unauthorised, "CA", "CA") == Fact(False, "high", "work-auth:unscoped-residence")

    no_sponsorship = work_auth_facts(profile(workAuthorization="Yes", requiresSponsorship="N/A"))
    assert no_sponsorship.stated_sponsorship is False
    assert needs_sponsorship_in(no_sponsorship, "CA", "CA") == Fact(False, "high", "sponsorship:stated")


def test_pin_a_leading_yes_wins_over_a_later_negation():
    """PINNED TS BEHAVIOUR (ambiguity): "Yes, I will not require sponsorship" is a Yes: the prefix is read first."""
    assert polarity_of("Yes, I will not require sponsorship") is True
    assert polarity_of("Y/N") is True


def test_pin_the_default_united_states_location_is_a_high_confidence_residence():
    """PINNED TS BEHAVIOUR (probable bug for the engine): location "United States" alone is a settled country.

    build_application_profile falls back to UserSettings.location, whose column default is
    "United States" (the LinkedIn bot's search region). The TypeScript reads that as: lives in
    the US, high confidence, no city, no region. For a user who never typed a city, an unscoped
    "Yes" therefore reads as "authorized in the US".
    """
    f = location_facts(profile(location="United States"))
    assert loc_shape(f) == {
        "street": None, "city": None, "region": None, "postal_code": None,
        "country": ("US", "high", "address:country"),
    }
    assert residence_of(f) == "US"
    a = work_auth_facts(profile(location="United States", workAuthorization="Yes"))
    assert authorized_in(a, "US", residence_of(f)) == Fact(True, "high", "work-auth:unscoped-residence")


def test_pin_the_default_location_outranks_a_structured_province():
    """PINNED TS BEHAVIOUR (probable bug): the country comes from the location string before the stated state.

    {location: "United States", addressState: "ON"} is read as the US, and the province is then
    dropped as inconsistent with it.
    """
    f = location_facts(profile(location="United States", addressState="ON"))
    assert loc_shape(f)["country"] == ("US", "high", "address:country")
    assert f.region is None
    g = location_facts(profile(location="United States", addressState="Ontario", addressCity="Ottawa"))
    assert loc_shape(g)["country"] == ("US", "high", "address:country")
    assert g.region is None
    assert g.city == Fact("Ottawa", "high", "profile:addressCity")


def test_pin_an_explicit_yes_over_a_text_denial_keeps_the_denied_basis():
    """PINNED TS BEHAVIOUR (cosmetic): "Not authorized in the US" + authorizedUS "Yes" is True with source work-auth:denied.

    The Yes wins the value, but the basis is carried over from the text it overrode.
    """
    a = work_auth_facts(profile(workAuthorization="Not authorized to work in the US", authorizedUS="Yes"))
    assert a.by_country["US"] == CountryAuth(True, None, "denied")
    assert authorized_in(a, "US", "US") == Fact(True, "high", "work-auth:denied")


def test_pin_a_state_that_is_also_a_city_is_consumed_as_the_region():
    """PINNED TS BEHAVIOUR (limitation): "New York" is the state; no city is left over. "New York, NY" has both."""
    only = location_facts(profile(location="New York"))
    assert only.city is None
    assert loc_shape(only)["region"] == ("NY", "high", "location:region")
    both = location_facts(profile(location="New York, NY"))
    assert both.city == Fact("New York", "high", "location:city")
    assert loc_shape(both)["region"] == ("NY", "high", "location:region")


def test_pin_a_five_digit_postcode_is_a_us_zip():
    """PINNED TS BEHAVIOUR (limitation): a German "10119 Berlin" is a ZIP, and with no country named it makes the US.

    The street is also mis-picked ("10119 Berlin" is the part that opens with a number).
    """
    a = parse_address("Torstraße 109, 10119 Berlin, Germany")
    assert addr_shape(a) == {"street": "10119 Berlin", "postal": ("10119", "US"), "country": "DE"}
    f = location_facts(profile(addressStreet="Torstraße 109, 10119 Berlin"))
    assert loc_shape(f)["country"] == ("US", "high", "address:country")
    stated = location_facts(profile(addressStreet="Torstraße 109, 10119 Berlin", country="Germany"))
    assert loc_shape(stated)["country"] == ("DE", "high", "profile:country")
