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
  * test_profile_facts_parity.py runs the same functions against the real TypeScript over a
    generated corpus (it needs Node >= 22.6 and skips without it).
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import json
import pickle
import re
import time
import typing
from collections.abc import Mapping
from typing import NamedTuple, Optional

import pytest

from backend.routers.fill import ApplicantProfile
from backend.routers.profile import ApplicationProfileOut
from backend.services import profile_facts as module
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

# Characters JavaScript and Python regular expressions treat differently (built with chr() so that this
# file holds no invisible characters): JS whitespace that is not Python's, and the reverse.
NBSP = chr(0xA0)  # whitespace to both; what pasted text carries
BOM = chr(0xFEFF)  # JavaScript whitespace, not Python's
NEL = chr(0x85)  # Python whitespace, not JavaScript's

# The text fields the facts read, under the names the profile schema gives them.
PROFILE_FIELDS = (
    "location", "addressStreet", "addressCity", "addressState", "postalCode", "country",
    "workAuthorization", "requiresSponsorship", "authorizedUS", "authorizedCanada",
)


@pytest.fixture(autouse=True)
def setup_db():
    """Override conftest's autouse fixture of the same name.

    That one creates and drops every table around each test (about 0.3 s each). These tests are
    pure functions of their arguments and never touch the database, so they skip it, which is
    what lets every table row below be a test of its own, with its own id.
    """
    yield


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
     {"street": "1055 W Georgia St", "city": "Vancouver", "region": "BC", "postal": ("V6E 3P3", "CA"),
      "country": "CA"}),
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


@pytest.mark.parametrize("text,expected", PARSE_CASES, ids=[repr(text) for text, _ in PARSE_CASES])
def test_parse_address_matches_the_typescript(text, expected):
    """Same input, same parts as the extension's parseAddress."""
    assert addr_shape(parse_address(text)) == expected


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
        dict(addressStreet="100 King St W", addressCity="Toronto", addressState="ON", postalCode="M5V 2T6",
             country="Canada"),
        street=("100 King St W", "high", "profile:addressStreet"),
        city=("Toronto", "high", "profile:addressCity"), region=("ON", "high", "profile:addressState"),
        postal_code=("M5V 2T6", "high", "profile:postalCode"), country=("CA", "high", "profile:country")),
    Loc("city-and-province-only", dict(addressCity="Toronto", addressState="ON"),
        city=("Toronto", "high", "profile:addressCity"), region=("ON", "high", "profile:addressState"),
        country=("CA", "high", "address:country")),
    Loc("city-alone-is-a-medium-country-hint", dict(addressCity="Toronto"),
        city=("Toronto", "high", "profile:addressCity"), country=("CA", "medium", "city-hint")),
    Loc("free-text-toronto-on", dict(location="Toronto, ON"), city=("Toronto", "high", "location:city"),
        region=("ON", "high", "location:region"), country=("CA", "high", "address:country")),
    Loc("free-text-city-province-country", dict(location="Toronto, ON, Canada"),
        city=("Toronto", "high", "location:city"), region=("ON", "high", "location:region"),
        country=("CA", "high", "address:country")),
    Loc("free-text-seattle-wa", dict(location="Seattle, WA"), city=("Seattle", "high", "location:city"),
        region=("WA", "high", "location:region"), country=("US", "high", "address:country")),
    Loc("bare-known-city", dict(location="Toronto"), city=("Toronto", "medium", "location:city"),
        country=("CA", "medium", "city-hint")),
    Loc("bare-us-city", dict(location="Seattle"), city=("Seattle", "medium", "location:city"),
        country=("US", "medium", "city-hint")),
    Loc("non-north-american-city-with-country", dict(location="Berlin, Germany"),
        city=("Berlin", "high", "location:city"), country=("DE", "high", "address:country")),
    Loc("non-north-american-city-without-country", dict(location="Mumbai"),
        city=("Mumbai", "medium", "location:city")),
    Loc("ambiguous-city-is-not-hinted", dict(location="London"), city=("London", "medium", "location:city")),
    Loc("uk-by-abbreviation", dict(location="London, UK"), city=("London", "high", "location:city"),
        country=("GB", "high", "address:country")),
    Loc("san-jose-ca-is-california-not-canada", dict(location="San Jose, CA"),
        city=("San Jose", "high", "location:city"), region=("CA", "high", "location:region"),
        country=("US", "high", "address:country")),
    Loc("default-united-states-alone", dict(location="United States"),
        country=("US", "high", "address:country")),
    Loc("remote-is-not-a-place", dict(location="Remote")),
    Loc("remote-with-a-country-keeps-only-the-country", dict(location="Remote, Canada"),
        country=("CA", "high", "address:country")),
    Loc("greater-toronto-area-is-not-a-city", dict(location="Greater Toronto Area")),
    Loc("one-line-street-address", dict(addressStreet="1055 W Georgia St, Vancouver, BC V6E 3P3, Canada"),
        street=("1055 W Georgia St", "high", "address:parsed-street"),
        city=("Vancouver", "high", "address:city"), region=("BC", "high", "address:region"),
        postal_code=("V6E 3P3", "high", "address:postal"), country=("CA", "high", "address:country")),
    Loc("stated-country-beats-the-default-location", dict(location="United States", country="Canada"),
        country=("CA", "high", "profile:country")),
    Loc("country-given-as-an-iso-code", dict(country="CA"), country=("CA", "high", "profile:country")),
    Loc("country-given-as-usa", dict(country="USA"), country=("US", "high", "profile:country")),
    Loc("nothing-stated", dict()),
]


@pytest.mark.parametrize("case", LOCATIONS, ids=lambda c: c.id)
def test_location_facts_matches_the_typescript(case):
    """location_facts on a structured address, city + province, free text, a non-North-American city,
    a bare city and the default "United States"."""
    want = {
        "street": case.street, "city": case.city, "region": case.region,
        "postal_code": case.postal_code, "country": case.country,
    }
    assert loc_shape(location_facts(profile(**case.inp))) == want


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
    Row("yes_sponsor-no_toronto", wa="Yes", rs="No", loc="Toronto, ON", resid="CA",
        auth_ca=(True, "work-auth:unscoped-residence"), spons_ca=(False, "sponsorship:stated")),
    Row("yes_sponsor-no_seattle", wa="Yes", rs="No", loc="Seattle, WA", resid="US",
        auth_us=(True, "work-auth:unscoped-residence"), spons_us=(False, "sponsorship:stated")),
    Row("yes_sponsor-no_unknown-residence", wa="Yes", rs="No", resid=None,
        spons_us=(False, "sponsorship:stated"), spons_ca=(False, "sponsorship:stated")),
    Row("yes_sponsor-no_bare-city-is-not-residence", wa="Yes", rs="No", loc="Toronto", resid=None,
        spons_us=(False, "sponsorship:stated"), spons_ca=(False, "sponsorship:stated")),
    Row("yes_sponsor-yes_toronto", wa="Yes", rs="Yes", loc="Toronto, ON", resid="CA",
        auth_ca=(True, "work-auth:unscoped-residence"), spons_ca=(True, "sponsorship:stated")),
    Row("yes_sponsor-blank_seattle", wa="Yes", loc="Seattle, WA", resid="US",
        auth_us=(True, "work-auth:unscoped-residence")),
    # --- citizens ---
    Row("canadian-citizen_sponsor-blank_toronto", wa="Canadian citizen", loc="Toronto, ON", resid="CA",
        auth_ca=(True, "work-auth:citizen"), spons_ca=(False, "sponsorship:citizen")),
    Row("canadian-citizen_sponsor-yes_toronto", wa="Canadian citizen", rs="Yes", loc="Toronto, ON",
        resid="CA", auth_ca=(True, "work-auth:citizen"), spons_ca=(False, "sponsorship:citizen")),
    Row("canadian-citizen_sponsor-no_seattle", wa="Canadian citizen", rs="No", loc="Seattle, WA", resid="US",
        auth_ca=(True, "work-auth:citizen"), spons_ca=(False, "sponsorship:citizen")),
    Row("us-citizen_explicit_seattle", wa="U.S. citizen", rs="No", au="Yes", ac="No", loc="Seattle, WA",
        resid="US", auth_us=(True, "work-auth:citizen"), auth_ca=(False, "work-auth:stated"),
        spons_us=(False, "sponsorship:citizen"), spons_ca=(True, "sponsorship:not-authorized-stated")),
    # --- a sentence naming the country ---
    Row("legally-authorized-canada_sponsor-no_toronto", wa="Yes, I am legally authorized to work in Canada",
        rs="No", loc="Toronto, ON", resid="CA", auth_ca=(True, "work-auth:statement"),
        spons_ca=(False, "sponsorship:stated")),
    Row("legally-authorized-canada_sponsor-blank_toronto",
        wa="Yes, I am legally authorized to work in Canada", loc="Toronto, ON", resid="CA",
        auth_ca=(True, "work-auth:statement")),
    Row("authorized-canada-and-us_toronto", wa="Authorized to work in Canada and the U.S.", rs="No",
        loc="Toronto, ON", resid="CA", auth_us=(True, "work-auth:statement"),
        auth_ca=(True, "work-auth:statement"), spons_us=(False, "sponsorship:stated"),
        spons_ca=(False, "sponsorship:stated")),
    Row("authorized-us-only_seattle", wa="Authorized to work in the US", rs="No", loc="Seattle, WA",
        resid="US", auth_us=(True, "work-auth:statement"), spons_us=(False, "sponsorship:stated")),
    # --- the per-country answers on their own ---
    Row("explicit-us-yes-only_seattle", au="Yes", loc="Seattle, WA", resid="US",
        auth_us=(True, "work-auth:stated")),
    Row("explicit-ca-yes-only_toronto", ac="Yes", loc="Toronto, ON", resid="CA",
        auth_ca=(True, "work-auth:stated")),
    Row("explicit-us-no_ca-yes_toronto", au="No", ac="Yes", loc="Toronto, ON", resid="CA",
        auth_us=(False, "work-auth:stated"), auth_ca=(True, "work-auth:stated"),
        spons_us=(True, "sponsorship:not-authorized-stated")),
    Row("explicit-us-yes_ca-no_seattle", au="Yes", ac="No", loc="Seattle, WA", resid="US",
        auth_us=(True, "work-auth:stated"), auth_ca=(False, "work-auth:stated"),
        spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("explicit-both-yes_toronto", rs="No", au="Yes", ac="Yes", loc="Toronto, ON", resid="CA",
        auth_us=(True, "work-auth:stated"), auth_ca=(True, "work-auth:stated"),
        spons_us=(False, "sponsorship:stated"), spons_ca=(False, "sponsorship:stated")),
    Row("explicit-both-no_unknown-residence", rs="Yes", au="No", ac="No", resid=None,
        auth_us=(False, "work-auth:stated"), auth_ca=(False, "work-auth:stated"),
        spons_us=(True, "sponsorship:not-authorized-stated"),
        spons_ca=(True, "sponsorship:not-authorized-stated")),
    # --- a per-country answer against the unscoped one: the per-country answer wins ---
    Row("conflict_unscoped-yes_us-no_ca-yes_toronto", wa="Yes", rs="No", au="No", ac="Yes", loc="Toronto, ON",
        resid="CA", auth_us=(False, "work-auth:stated"), auth_ca=(True, "work-auth:stated"),
        spons_us=(True, "sponsorship:not-authorized-stated"), spons_ca=(False, "sponsorship:stated")),
    Row("conflict_unscoped-yes_ca-no_toronto", wa="Yes", rs="No", ac="No", loc="Toronto, ON", resid="CA",
        auth_ca=(False, "work-auth:stated"), spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("conflict_canadian-citizen_ca-no", wa="Canadian citizen", ac="No", loc="Toronto, ON", resid="CA",
        auth_ca=(False, "work-auth:stated"), spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("conflict_authorized-us_us-no", wa="Authorized to work in the US", rs="No", au="No",
        loc="Seattle, WA", resid="US", auth_us=(False, "work-auth:stated"),
        spons_us=(True, "sponsorship:not-authorized-stated")),
    Row("conflict_text-denies-us_us-yes", wa="Not authorized to work in the US", au="Yes", loc="Seattle, WA",
        resid="US", auth_us=(True, "work-auth:denied")),
    Row("agree_canadian-citizen_ca-yes_us-no", wa="Canadian citizen", au="No", ac="Yes", loc="Toronto, ON",
        resid="CA", auth_us=(False, "work-auth:stated"), auth_ca=(True, "work-auth:citizen"),
        spons_us=(True, "sponsorship:not-authorized-stated"), spons_ca=(False, "sponsorship:citizen")),
    # --- visas and permits ---
    # "transfer required" and the F-1 row's words are outside the allow-list vocabulary (D-61
    # amendment): the text binds nothing, so the explicit per-country answers stand alone.
    Row("h1b_seattle", wa="H-1B visa (transfer required)", rs="Yes", au="Yes", ac="No", loc="Seattle, WA",
        resid="US", auth_us=(True, "work-auth:stated"), auth_ca=(False, "work-auth:stated"),
        spons_us=(True, "sponsorship:stated"), spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("green-card_san-jose", wa="U.S. permanent resident (green card)", rs="No", au="Yes", ac="No",
        loc="San Jose, CA", resid="US", auth_us=(True, "work-auth:permanent_resident"),
        auth_ca=(False, "work-auth:stated"), spons_us=(False, "sponsorship:permanent_resident"),
        spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("stem-opt_boston", wa="F-1 STEM OPT (EAD valid through June 2028)", rs="Yes", au="Yes", ac="No",
        loc="Boston, MA, USA", resid="US", auth_us=(True, "work-auth:stated"),
        auth_ca=(False, "work-auth:stated"), spons_us=(True, "sponsorship:stated"),
        spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("f1-student_no-explicit_cambridge", wa="F-1 student visa (OPT eligible)", rs="Yes",
        loc="Cambridge, MA 02139", resid="US", spons_us=(True, "sponsorship:stated")),
    Row("permanent-resident-canada_montreal", wa="Permanent resident of Canada", rs="No", au="No", ac="Yes",
        loc="Montréal, QC", resid="CA", auth_us=(False, "work-auth:stated"),
        auth_ca=(True, "work-auth:permanent_resident"), spons_us=(True, "sponsorship:not-authorized-stated"),
        spons_ca=(False, "sponsorship:permanent_resident")),
    Row("pgwp_toronto", wa="PGWP (open work permit)", rs="Yes", loc="Toronto, ON", resid="CA",
        auth_ca=(True, "work-auth:work_permit"), spons_ca=(True, "sponsorship:stated")),
    # --- blank means unanswered, never "no" ---
    Row("all-blank_toronto", loc="Toronto, ON", resid="CA"),
    Row("all-blank_unknown-residence", resid=None),
    Row("only-sponsor-yes_toronto", rs="Yes", loc="Toronto, ON", resid="CA",
        spons_ca=(True, "sponsorship:stated")),
    Row("whitespace-only-answers_toronto", wa="  ", rs=" ", au=" ", ac="  ", loc="Toronto, ON", resid="CA"),
    # --- living outside North America ---
    Row("german-citizen_berlin", wa="German citizen (EU)", rs="Yes", au="No", ac="No", loc="Berlin, Germany",
        resid="DE", auth_us=(False, "work-auth:stated"), auth_ca=(False, "work-auth:stated"),
        spons_us=(True, "sponsorship:not-authorized-stated"),
        spons_ca=(True, "sponsorship:not-authorized-stated")),
    Row("british-citizen_london", wa="British citizen", rs="Yes", au="No", ac="No",
        loc="London, United Kingdom", resid="GB", auth_us=(False, "work-auth:stated"),
        auth_ca=(False, "work-auth:stated"), spons_us=(True, "sponsorship:not-authorized-stated"),
        spons_ca=(True, "sponsorship:not-authorized-stated")),
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


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_saved_answers_read_the_same_from_application_profile_out(row):
    """The backend holds an ApplicationProfileOut (build_application_profile): every row of the table
    must read exactly like the dict the extension gets."""
    p = row_profile(row)
    model = ApplicationProfileOut(**p)
    check_row(row, model)
    assert work_auth_facts(model) == work_auth_facts(p)
    assert location_facts(model) == location_facts(p)


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
    _b("free-movement_german-citizen_asked-austria", {"workAuthorization": "German citizen"}, "AT", "DE",
       auth=(True, "high", "work-auth:free-movement"), spons=(False, "high", "sponsorship:free-movement")),
    _b("free-movement_german-citizen_asked-norway",
       {"workAuthorization": "German citizen", "requiresSponsorship": "Yes"}, "NO", "DE",
       auth=(True, "high", "work-auth:free-movement"), spons=(False, "high", "sponsorship:free-movement")),
    # "EU" is outside the allow-list vocabulary (D-61 amendment): the citizenship is unknown.
    _b("free-movement_needs-a-recognised-citizenship", {"workAuthorization": "German citizen (EU)"}, "AT", "DE"),
    _b("free-movement_stops-at-the-uk", {"workAuthorization": "German citizen (EU)"}, "GB", "DE"),
    _b("free-movement_stops-at-the-us", {"workAuthorization": "German citizen (EU)"}, "US", "DE"),
    _b("free-movement_needs-a-citizen-not-a-resident", {"workAuthorization": "Permanent resident of Germany"},
       "AT", "DE"),
    _b("free-movement_canadian-citizen_asked-france", {"workAuthorization": "Canadian citizen"}, "FR", "CA"),
    _b("free-movement_explicit-no-still-wins",
       {"workAuthorization": "German citizen (EU)", "authorizedCanada": "No"}, "CA", "DE",
       auth=(False, "high", "work-auth:stated"), spons=(True, "high", "sponsorship:not-authorized-stated")),
    # --- sponsorship that the visa itself settles ---
    _b("visa_h1b-no-stated-answer_needs-sponsorship", {"workAuthorization": "H-1B"}, "US", "US",
       auth=(True, "high", "work-auth:work_permit"), spons=(True, "high", "sponsorship:work_permit")),
    _b("visa_pgwp-no-stated-answer_unknown", {"workAuthorization": "PGWP (open work permit)"}, "CA", "CA",
       auth=(True, "high", "work-auth:work_permit")),
    _b("visa_f1-student-no-stated-answer_unknown", {"workAuthorization": "F-1 student visa (OPT eligible)"},
       "US", "US"),
    _b("text-denial_sponsorship",
       {"workAuthorization": "Authorized to work in Canada, not authorized in the US"}, "US", "CA",
       auth=(False, "high", "work-auth:denied"), spons=(True, "high", "sponsorship:not-authorized-denied")),
    # --- the question names no country ---
    _b("no-country_unscoped-yes", {"workAuthorization": "Yes"}, None, "CA",
       auth=(True, "high", "work-auth:unscoped")),
    _b("no-country_unscoped-yes_empty-string-is-no-country", {"workAuthorization": "Yes"}, "", "CA",
       auth=(True, "high", "work-auth:unscoped")),
    _b("no-country_unscoped-no", {"workAuthorization": "No"}, None, "CA",
       auth=(False, "high", "work-auth:unscoped")),
    _b("no-country_nothing-said", {}, None, "CA"),
    _b("no-country_stated-with-one-status",
       {"workAuthorization": "Canadian citizen", "requiresSponsorship": "Yes"}, None, "CA",
       spons=(True, "high", "sponsorship:stated-unscoped")),
    _b("no-country_stated-but-not-authorized-somewhere", {"authorizedUS": "No", "requiresSponsorship": "No"},
       None, "CA", spons=(False, "medium", "sponsorship:stated-unscoped-not-everywhere")),
    _b("no-country_stated-two-countries-residence-covered",
       {"workAuthorization": "Authorized to work in Canada and the U.S.", "requiresSponsorship": "No"}, None,
       "CA", spons=(False, "high", "sponsorship:stated-unscoped")),
    _b("no-country_stated-two-countries-residence-elsewhere",
       {"workAuthorization": "Authorized to work in Canada and the U.S.", "requiresSponsorship": "No"}, None,
       "GB", spons=(False, "medium", "sponsorship:stated-multi-country")),
    _b("no-country_stated-two-countries-residence-unknown",
       {"workAuthorization": "Authorized to work in Canada and the U.S.", "requiresSponsorship": "No"}, None,
       None, spons=(False, "medium", "sponsorship:stated-multi-country")),
    _b("no-country_single-citizen-no-stated", {"workAuthorization": "British citizen"}, None, "GB",
       spons=(False, "medium", "sponsorship:citizen-unscoped")),
    _b("no-country_single-resident-no-stated", {"workAuthorization": "Permanent resident of Germany"}, None,
       "DE", spons=(False, "medium", "sponsorship:permanent_resident-unscoped")),
    _b("no-country_single-statement-no-stated", {"workAuthorization": "Authorized to work in Canada"}, None,
       "CA"),
    _b("no-country_two-statuses-no-stated", {"workAuthorization": "Canadian citizen and US citizen"}, None,
       "CA"),
    # --- the applicant's own sponsorship answer, and where it applies ---
    _b("country_stated-answer-needs-covered-status",
       {"workAuthorization": "Authorized to work in Canada", "requiresSponsorship": "Yes"}, "CA", "CA",
       auth=(True, "high", "work-auth:statement"), spons=(True, "high", "sponsorship:stated")),
    _b("country_stated-answer-applies-with-no-status-and-matching-residence", {"requiresSponsorship": "Yes"},
       "CA", "CA", spons=(True, "high", "sponsorship:stated")),
    _b("country_stated-answer-not-applied-to-another-residence", {"requiresSponsorship": "Yes"}, "US", "CA"),
    _b("country_stated-answer-applies-when-residence-unknown", {"requiresSponsorship": "Yes"}, "US", None,
       spons=(True, "high", "sponsorship:stated")),
]


def fact3(fact):
    return None if fact is None else (fact.value, fact.confidence, fact.source)


@pytest.mark.parametrize("b", BRANCHES, ids=lambda b: b.id)
def test_every_branch_of_authorized_in_and_needs_sponsorship_in(b):
    """Free movement, visa-settled sponsorship, the no-country (medium) paths, and where the stated answer
    applies."""
    a = work_auth_facts(profile(**b.answers))
    assert fact3(authorized_in(a, b.country, b.residence)) == b.auth
    assert fact3(needs_sponsorship_in(a, b.country, b.residence)) == b.spons


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
    ('', None), ('   ', None), ('maybe', None), ('unsure', None), ('Never', None),
    # decide-b round 4: the informal yes and no words are answers.
    ('Nope', False), ('yep', True), ('Yeah', True), ('nah', False),
    # whitespace as JavaScript trims it: a byte-order mark, a non-breaking space, a line separator
    ('\ufeff Yes \u00a0', True), ('\u00a0no\u2028', False),
]


@pytest.mark.parametrize("text,expected", POLARITY, ids=[repr(text) for text, _ in POLARITY])
def test_polarity_of_matches_the_typescript(text, expected):
    """A short stored answer read as yes / no / unknown; blank is unknown, never no."""
    assert polarity_of(text) is expected


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


@pytest.mark.parametrize("text,codes", COUNTRIES_IN, ids=[repr(text) for text, _ in COUNTRIES_IN])
def test_countries_in_matches_the_typescript(text, codes):
    """Countries named by name, alias, demonym, or a status that only exists in one country."""
    assert [c.code for c in countries_in(text)] == codes


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
    # the per-country map refuses every way a dict can be changed
    new = CountryAuth(True, False, "citizen")
    changes = {
        "item assignment": lambda m: m.__setitem__("US", new),
        "del": lambda m: m.__delitem__("CA"),
        "pop": lambda m: m.pop("CA"),
        "popitem": lambda m: m.popitem(),
        "clear": lambda m: m.clear(),
        "update": lambda m: m.update({"US": new}),
        "setdefault": lambda m: m.setdefault("US", new),
        "in-place or": lambda m: m.__ior__({"US": new}),
    }
    for name, change in changes.items():
        with pytest.raises(TypeError):
            change(a.by_country)
    assert dict(a.by_country) == {"CA": CountryAuth(True, False, "citizen")}  # none of them got through
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
    assert list(b.by_country) == ["CA", "US"]  # the order the bound list names them


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


def test_the_facts_copy_pickle_serialise_and_hash():
    """The audit trace and the engine copy, store and key on these facts.

    `by_country` is a read-only dict, so asdict, json, deepcopy and pickle all work and a copy is
    as read-only as the original; every fact hashes.
    """
    auth = work_auth_facts(profile(workAuthorization="Canadian citizen", authorizedUS="No"))
    loc = location_facts(profile(location="Toronto, ON", addressStreet="100 King St W, Toronto, ON M5X 1A9"))
    for facts in (auth, loc, authorized_in(auth, "CA", "CA"), parse_address("Toronto, ON")):
        deep = copy.deepcopy(facts)
        assert deep == facts and deep is not facts
        assert copy.copy(facts) == facts
        assert pickle.loads(pickle.dumps(facts)) == facts
        assert hash(deep) == hash(facts)
        json.dumps(dataclasses.asdict(facts))  # plain data all the way down
    assert len({auth, copy.deepcopy(auth), pickle.loads(pickle.dumps(auth))}) == 1

    as_data = {
        "by_country": {
            "CA": {"authorized": True, "needs_sponsorship": False, "basis": "citizen"},
            "US": {"authorized": False, "needs_sponsorship": True, "basis": "stated"},
        },
        "stated_sponsorship": None,
        "unscoped_authorized": None,
        "sponsorship_unclear": False,
        "withheld": (),
    }
    assert dataclasses.asdict(auth) == as_data
    assert json.loads(json.dumps(dataclasses.asdict(auth))) == {**as_data, "withheld": []}

    for clone in (copy.deepcopy(auth), copy.copy(auth), pickle.loads(pickle.dumps(auth))):
        assert type(clone.by_country) is type(auth.by_country)
        with pytest.raises(TypeError):
            clone.by_country["BR"] = CountryAuth(True, False, "citizen")


def test_the_fields_the_port_reads_are_exactly_the_profile_fields_it_declares():
    """`ProfileFields` is the type of every `profile` argument. It must name exactly the fields the port
    reads, and both profile schemas the backend has (the one it holds and the /api/fill one) must carry
    every one of them: a renamed schema field fails here instead of silently reading as blank."""

    class Recorder(dict):
        def __init__(self):
            super().__init__()
            self.read = set()

        def get(self, key, default=None):
            self.read.add(key)
            return super().get(key, default)

    recorder = Recorder()
    location_facts(recorder)
    work_auth_facts(recorder)
    declared = set(module.ProfileFields.__annotations__)
    assert recorder.read == declared == set(PROFILE_FIELDS)
    assert declared <= set(ApplicationProfileOut.model_fields)
    assert declared <= set(ApplicantProfile.model_fields)
    # ProfileInput is a real union of "a mapping" and "something with those fields", not `object`
    assert set(typing.get_args(module.ProfileInput)) == {Mapping[str, typing.Any], module.ProfileFields}


def test_no_module_level_table_is_mutable_and_no_loop_variable_leaks():
    """Every lookup table is a tuple, a frozenset or a read-only mapping, and building them leaves no
    loop variable behind in the module namespace."""
    mutable = sorted(
        name for name, value in vars(module).items()
        if not name.startswith("__") and type(value) in (dict, list, set)
    )
    assert mutable == []
    assert [name for name in ("_c", "_alias", "_r", "_key") if hasattr(module, name)] == []
    for table in (module._CITY_COUNTRY, module._COUNTRY_BY_KEY, module._COUNTRY_BY_CODE, module._REGIONS_BY_KEY):
        with pytest.raises(TypeError):
            table["toronto"] = "US"
    assert isinstance(module.FREE_MOVEMENT, frozenset)


# =============================================================================================
# Hostile input: profile text has no length limit upstream
# =============================================================================================
# ApplicationProfileIn takes bare strings and the only cap is the request size (10 MiB), so a
# stored field can be enormous. Every function has to be linear in it.


def test_parse_address_is_linear_in_a_long_whitespace_run():
    """The part splitter was /\\s*[,;\\n]\\s*/, which is quadratic on one long whitespace run: 10,000
    spaces took 0.4 s, 40,000 took 6 s, and 200,000 would take minutes. It is a plain split on , ; and
    newline now (each part is trimmed afterwards): the same parts for every input, in one pass."""
    text = "a" + " " * 200_000 + "b"
    started = time.perf_counter()
    parsed = parse_address(text)
    elapsed = time.perf_counter() - started
    assert parsed.city == text  # one plain-word part: the result is unchanged, only the cost
    assert elapsed < 1.0, f"parse_address took {elapsed:.1f}s on 200,000 spaces"


def test_the_part_splitter_reads_every_input_as_the_old_padded_pattern_did():
    """Splitting on the bare separator and trimming each part gives exactly the parts the extension's
    /\\s*[,;\\n]\\s*/ gives: the whitespace that padded a separator used to be swallowed by the pattern
    and is now trimmed off the parts it touches. Checked on EVERY string of up to five characters over
    separators and the kinds of whitespace that JavaScript and Python disagree about."""
    space = "[" + module._JS_SPACE + "]*"
    padded = re.compile(space + "[,;\n]" + space)

    def padded_parts(raw):  # the extension's formulation: split on the padded pattern, trim, drop blanks
        return [part for part in (piece.strip(module._JS_SPACE) for piece in padded.split(raw)) if part]

    alphabet = ["a", ",", ";", "\n", " ", NBSP, BOM, NEL]
    checked = 0
    for length in range(6):
        for chars in itertools.product(alphabet, repeat=length):
            raw = "".join(chars)
            assert module._split_parts(raw) == padded_parts(raw), repr(raw)
            checked += 1
    assert checked == sum(len(alphabet) ** n for n in range(6))


HOSTILE_TEXT = {
    "whitespace between two words": lambda n: "a" + " " * n + "b",
    "a negation then whitespace": lambda n: "no" + " " * n + "x",
    "only separators": lambda n: "," * n,
    "many tiny parts": lambda n: "a, " * (n // 3),
    "one long digit run": lambda n: "1" * n,
    "one long word": lambda n: "a" * n,
    "a repeated negation": lambda n: "no " * (n // 3),
    "a repeated address": lambda n: "Toronto, ON, Canada, " * (n // 21),
    "a repeated denial": lambda n: "not authorized to work in the US " * (n // 33),
    "a repeated ZIP": lambda n: "12345 " * (n // 6),
    "a repeated country code": lambda n: "U.S. " * (n // 5),
    "mixed whitespace": lambda n: "\n \t " * (n // 4) + "x",
}


@pytest.mark.parametrize("shape", sorted(HOSTILE_TEXT))
def test_no_function_is_superlinear_on_hostile_text(shape):
    """Each shape is the worst case for one of the patterns the port runs on user text, read by every
    public function through all ten fields. The bound is generous (this takes about 0.1 s) and far below
    what a quadratic pattern costs (half a minute or more)."""
    text = HOSTILE_TEXT[shape](50_000)
    everywhere = dict.fromkeys(PROFILE_FIELDS, text)
    started = time.perf_counter()
    parse_address(text)
    polarity_of(text)
    countries_in(text)
    location_facts(everywhere)
    work_auth_facts(everywhere)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"{shape}: {elapsed:.1f}s on {len(text):,} characters"


# =============================================================================================
# PINNED TS BEHAVIOUR: looks like a bug, ported faithfully on purpose
# =============================================================================================
# The extension and the engine must agree. Each test below records what the TypeScript does,
# so the port cannot drift from it silently. If the extension is fixed, fix the port in the same
# change and update the test; do not "fix" only one side. All of these are listed in the task
# report with the likely consequence for the match engine.


@pytest.mark.parametrize("statement", ["Iranian citizen", "French citizen"])
def test_a_citizenship_the_country_table_lacks_is_unknown(statement):
    """Was PINNED TS BEHAVIOUR (probable bug): the demonym is missing from geo.ts (no "french" alias,
    no Iran), so "<nationality> citizen" fell into the unscoped "citizen" branch and read as
    "authorized wherever the applicant lives". The allow-list layer (D-61 amendment) fixes it: the
    unknown demonym is outside the closed vocabulary, so nothing positive is read.
    """
    a = work_auth_facts(profile(workAuthorization=statement))
    assert dict(a.by_country) == {}
    assert a.unscoped_authorized is None
    assert authorized_in(a, "CA", "CA") is None
    assert authorized_in(a, "US", "CA") is None
    assert authorized_in(a, None, "CA") is None


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


def test_a_negated_country_is_never_authorized():
    """Owner decisions 2026-10-13 (was a PINNED TS BEHAVIOUR): a negation belongs to the countries in its clause.

    "...Canada but not in the US" excludes the US outright: a No there, and Canada keeps the
    statement. "...Canada, not authorized in the US" denies the US only, not Canada. An explicit
    per-country answer that agrees with the text keeps the text's basis. Mirrors
    chrome-extension/test/workAuthScope.test.ts.
    """
    but = work_auth_facts(profile(workAuthorization="Authorized to work in Canada but not in the US"))
    assert authorized_in(but, "US", "CA") == Fact(False, "high", "work-auth:denied")
    # The allow-list layer (D-61 amendment): a word outside the closed vocabulary leaves it unknown.
    assert authorized_in(but, "CA", "CA") is None

    comma = work_auth_facts(profile(workAuthorization="Authorized to work in Canada, not authorized in the US"))
    assert authorized_in(comma, "CA", "CA") is None
    assert authorized_in(comma, "US", "CA") == Fact(False, "high", "work-auth:denied")
    assert needs_sponsorship_in(comma, "US", "CA") == Fact(True, "high", "sponsorship:not-authorized-denied")

    fixed = work_auth_facts(profile(
        workAuthorization="Authorized to work in Canada but not in the US", authorizedUS="No", authorizedCanada="Yes"))
    assert authorized_in(fixed, "US", "CA") == Fact(False, "high", "work-auth:denied")
    assert authorized_in(fixed, "CA", "CA") == Fact(True, "high", "work-auth:stated")


def test_a_country_only_named_stays_unknown():
    """Decide-b round 3 (binding model): "Canada" with no status bound to it is unknown."""
    a = work_auth_facts(profile(workAuthorization="Canada but not in the US"))
    assert authorized_in(a, "CA", "CA") is None
    assert authorized_in(a, "US", "CA") == Fact(False, "high", "work-auth:denied")


@pytest.mark.parametrize(("text", "us"), [
    ("Authorized in Canada, not the US", False),
    ("Authorized to work in Canada, except the US", False),
    ("Yes, in Canada but not the United States", False),
    ("Only in Canada", None),
])
def test_a_scoped_statement_counts_only_its_positive_countries(text, us):
    a = work_auth_facts(profile(workAuthorization=text))
    # Each holds a word outside the allow-list vocabulary ("not", "except", "but", "only", D-61
    # amendment): Canada is unknown, never a No.
    assert authorized_in(a, "CA", "CA") is None
    if us is None:
        assert authorized_in(a, "US", "CA") is None
    else:
        assert authorized_in(a, "US", "CA") == Fact(False, "high", "work-auth:denied")


def test_negation_scope_clauses():
    """"and" between two countries keeps both negated; "and" before a new statement ends it;
    a negated sponsorship clause and "not a ... citizen" never make a No."""
    both = work_auth_facts(profile(workAuthorization="Authorized to work in Canada, not in the US and the UK"))
    assert both.by_country["US"] == CountryAuth(False, None, "denied")
    assert both.by_country["GB"] == CountryAuth(False, None, "denied")
    split = work_auth_facts(profile(workAuthorization="Not authorized in the US and authorized to work in Canada"))
    assert split.by_country["US"] == CountryAuth(False, None, "denied")
    # The Canada clause shares a segment with "not": the veto (decide-b round 5) leaves it unknown,
    # never a No.
    assert "CA" not in split.by_country
    sponsor = work_auth_facts(profile(workAuthorization="US citizen, not requiring sponsorship in the US"))
    assert sponsor.by_country["US"] == CountryAuth(True, False, "citizen")
    weak = work_auth_facts(profile(workAuthorization="Canadian citizen, not a US citizen"))
    # The allow-list layer (D-61 amendment): a word outside the closed vocabulary leaves it unknown.
    assert "CA" not in weak.by_country
    assert "US" not in weak.by_country


def test_a_status_belongs_to_the_countries_of_its_own_clause():
    """Was PINNED TS BEHAVIOUR ("one status for every named country"); fixed with the extension.

    "Canadian citizen with H-1B in the US" is a Canadian citizen and an H-1B holder in the US.
    Mirrors chrome-extension/test/workAuthReview.test.ts.
    """
    a = work_auth_facts(profile(workAuthorization="Canadian citizen with H-1B in the US"))
    assert a.by_country["CA"] == CountryAuth(True, False, "citizen")
    assert a.by_country["US"] == CountryAuth(True, True, "work_permit")
    assert needs_sponsorship_in(a, "US", "CA") == Fact(True, "high", "sponsorship:work_permit")


@pytest.mark.parametrize("text", ["N/A", "n/a", "NA", "N.A.", "N / A", "Not applicable", "does not apply"])
def test_not_applicable_is_unanswered(text):
    """Owner decisions 2026-10-13 (was a PINNED TS BEHAVIOUR): "N/A" is unanswered, never a No."""
    assert polarity_of(text) is None


@pytest.mark.parametrize("text", ["No", "n", "None required", "Not required",
                                  "No, I will not need sponsorship in the future"])
def test_pin_other_n_words_still_read_as_no(text):
    """PINNED TS BEHAVIOUR: /^(no|n|false|non|none|not)\b/ still takes these for a No."""
    assert polarity_of(text) is False


def test_na_in_a_saved_answer_states_nothing():
    """The consequence of the N/A rule for the two saved answers."""
    unanswered = work_auth_facts(profile(workAuthorization="N/A"))
    assert unanswered.unscoped_authorized is None
    assert authorized_in(unanswered, "CA", "CA") is None

    no_sponsorship = work_auth_facts(profile(workAuthorization="Yes", requiresSponsorship="N/A"))
    assert no_sponsorship.stated_sponsorship is None
    assert needs_sponsorship_in(no_sponsorship, "CA", "CA") is None


# ---------------------------------------------------------------------------
# Decide B review round 1: each reviewer probe is a test (mirrors
# chrome-extension/test/workAuthReview.test.ts).
# ---------------------------------------------------------------------------

def _wa(text, **extra):
    return work_auth_facts(profile(workAuthorization=text, **extra))


def _auth(a, code):
    c = a.by_country.get(code)
    return None if c is None else c.authorized


@pytest.mark.parametrize("us", ["U.S.", "U.S", "U.S.A.", "u.s.", "U. S.", "USA", "United States", "America", "US"])
def test_review_c1_dotted_us_is_negated(us):
    text = f"Authorized to work in Canada but not in the {us}"
    assert authorized_in(_wa(text), "US", "CA") == Fact(False, "high", "work-auth:denied")
    assert authorized_in(_wa(text), "CA", "CA") is None  # allow-list (D-61 amendment): unknown
    assert needs_sponsorship_in(_wa(text, requiresSponsorship="No"), "US", None).value is True


def test_review_c1_dotted_collateral():
    assert _auth(_wa("Authorized to work in Canada, except the U.S."), "US") is False
    a = _wa("Canadian citizen, but not the U.S. (would need TN)")
    assert _auth(a, "US") is not True
    # The allow-list layer (D-61 amendment): a word outside the closed vocabulary leaves it unknown.
    assert _auth(a, "CA") is None
    b = _wa("Not authorized in the U.S. but authorized in Canada")
    assert _auth(b, "CA") is None and _auth(b, "US") is False
    c = _wa("not authorized to work in the U.S., Canadian citizen")
    assert _auth(c, "CA") is None and _auth(c, "US") is False
    d = _wa("Not authorized in the U.S. I am a Canadian citizen.")
    assert _auth(d, "CA") is None and _auth(d, "US") is False


@pytest.mark.parametrize("text", ["Canadian citizen. Not sure about the US.", "Not sure about the US; Canadian citizen"])
def test_review_i1_not_sure_is_unknown(text):
    assert authorized_in(_wa(text), "US", "CA") is None
    assert authorized_in(_wa(text), "CA", "CA") is None  # allow-list (D-61 amendment): unknown


@pytest.mark.parametrize("v", ["Not sure", "Unsure", "Not certain", "Unknown", "TBD", "Don't know",
                               "Not currently, but I will in the future", "Not now, but will need H-1B later",
                               "Not yet", "Not required now but will be after OPT"])
def test_review_i2_uncertain_or_future_answers_are_unanswered(v):
    assert polarity_of(v) is None
    assert needs_sponsorship_in(_wa("", requiresSponsorship=v), None, None) is None


@pytest.mark.parametrize("text", ["Not a US citizen but have a green card", "Green card holder (not a US citizen)",
                                  "Not a US citizen; I hold a green card"])
def test_review_i3_green_card_survives_not_a_citizen(text):
    # "not" is outside the allow-list vocabulary (D-61 amendment): unknown, never a No.
    assert _auth(_wa(text), "US") is None


@pytest.mark.parametrize("text", ["I am a US permanent resident, not a citizen", "Not a citizen, US green card holder"])
def test_review_i3_permanent_resident_not_a_citizen(text):
    # "not" is outside the allow-list vocabulary (D-61 amendment): unknown, never a No.
    assert _auth(_wa(text), "US") is None


def test_review_i3_not_a_citizen_alone_is_never_authorized():
    assert _auth(_wa("Not a citizen of the US"), "US") is not True


def test_review_i4_not_yet():
    assert _wa("Not yet authorized").unscoped_authorized is False
    a = _wa("Canadian citizen; not yet authorized in the US")
    assert _auth(a, "US") is False and _auth(a, "CA") is None  # allow-list (D-61 amendment): unknown
    assert _auth(_wa("Not yet authorized in the US; OPT pending"), "US") is False


@pytest.mark.parametrize("text", [
    "Canadian citizen, no US work authorization",
    "Canadian citizen without US work authorization",
    "Canadian citizen. Cannot work in the US.",
    "Canadian citizen, can't work in the US",
    "Canadian citizen, can’t work in the US",
    "Canadian citizen, unable to work in the US",
    "Canadian citizen, ineligible for US employment",
    "Unauthorized in the US, Canadian citizen",
    "Canadian citizen (I don't have US status)",
    "Canadian citizen, US work authorization: no",
    "Citizen of Canada, no authorization for the US",
])
def test_review_i5_other_negations(text):
    assert authorized_in(_wa(text), "US", "CA") == Fact(False, "high", "work-auth:denied")
    # Every phrasing holds a word outside the closed vocabulary, or a standalone "no" (the allow-list
    # layer, D-61 amendment): Canada is unknown, never a No.
    assert authorized_in(_wa(text), "CA", "CA") is None


def test_review_i5_sponsorship_clause():
    a = _wa("Canadian citizen; I would require sponsorship to work in the US")
    assert _auth(a, "US") is not True
    assert needs_sponsorship_in(a, "US", "CA").value is True
    assert _auth(a, "CA") is None  # allow-list (D-61 amendment): unknown
    assert _wa("I am a Canadian citizen and do not require sponsorship").by_country["CA"] == CountryAuth(
        True, False, "citizen")
    assert _auth(_wa("Authorized to work in the US without sponsorship"), "US") is True
    assert _auth(_wa("Canadian citizen, no sponsorship needed for the US"), "US") is not False


def test_review_probe_sponsorship_before_the_country_and_pending():
    a = _wa("Authorized in Canada; US requires sponsorship")
    assert _auth(a, "US") is not True
    assert needs_sponsorship_in(a, "US", "CA").value is True
    assert _auth(a, "CA") is None  # allow-list (D-61 amendment): unknown
    b = _wa("Canadian citizen, US authorization pending")
    assert "US" not in b.by_country
    assert _auth(b, "CA") is None


def test_review_i6_denial_after_the_country():
    a = _wa("Canadian PR; US not authorized")
    assert _auth(a, "CA") is None  # allow-list (D-61 amendment): unknown
    assert _auth(a, "US") is False


def test_review_m1_m2_demonym_and_not_only():
    a = _wa("Yes - US citizen (not Canadian)")
    assert _auth(a, "CA") is not False and _auth(a, "US") is None  # allow-list (D-61 amendment)
    b = _wa("Not only Canada, also the US")
    assert _auth(b, "CA") is not False and _auth(b, "US") is not False


@pytest.mark.parametrize("unit", ["Not sure about the U.S. ", "Canadian PR; US not authorized; ", "no U.S. status, ",
                                  "I would require sponsorship in the U. S.; ", "u.s.a.u.s.a."])
def test_review_timing_is_linear(unit):
    text = unit * (50000 // len(unit) + 1)
    start = time.perf_counter()
    _wa(text)
    polarity_of(text)
    assert time.perf_counter() - start < 2.0


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


def test_status_probe_tn_eligible_is_not_a_us_status():
    a = _wa("Canadian citizen; TN eligible for US")
    assert _auth(a, "CA") is None  # allow-list (D-61 amendment): unknown
    us = a.by_country.get("US")
    assert us is None or (us.basis != "citizen" and us.authorized is not True)
    assert (authorized_in(a, "US", "CA") or Fact(None, "high", "")).value is not True


def test_status_probe_us_citizen_and_canadian_pr():
    a = _wa("US citizen and Canadian permanent resident")
    assert a.by_country["US"] == CountryAuth(True, False, "citizen")
    assert a.by_country["CA"] == CountryAuth(True, False, "permanent_resident")


def test_status_probe_citizen_of_canada_h1b_in_the_us():
    # "via" is outside the allow-list vocabulary (D-61 amendment): both unknown, never a No.
    a = _wa("Citizen of Canada, authorized to work in the US via H-1B")
    assert _auth(a, "CA") is None
    assert _auth(a, "US") is None


def test_status_probe_canadian_and_american_citizen():
    a = _wa("Canadian and American citizen")
    assert a.by_country["CA"] == CountryAuth(True, False, "citizen")
    assert a.by_country["US"] == CountryAuth(True, False, "citizen")


@pytest.mark.parametrize("text", ["Canadian citizen not requiring sponsorship in the US", "Canadian citizen and US resident"])
def test_status_probe_no_us_status_borrowed(text):
    a = _wa(text)
    if text.endswith("US resident"):
        assert _auth(a, "CA") is None  # allow-list (D-61 amendment): unknown
    else:
        assert a.by_country["CA"] == CountryAuth(True, False, "citizen")
    assert (a.by_country.get("US") is None) or a.by_country["US"].basis != "citizen"


def test_status_probe_shared_statements_reach_every_listed_country():
    for text in ["Authorized to work in Canada and the U.S.", "Authorized to work in the US and Canada",
                 "I am a citizen of Canada and the United States"]:
        a = _wa(text)
        assert _auth(a, "CA") is True and _auth(a, "US") is True
    assert _wa("I am a citizen of Canada and the United States").by_country["US"].basis == "citizen"


@pytest.mark.parametrize("text", ["U.S. citizen", "U.S. Citizen", "U.S.A. citizen", "U. S. citizen"])
def test_status_probe_dotted_us_keeps_its_status(text):
    assert _wa(text).by_country["US"] == CountryAuth(True, False, "citizen")


def test_status_probe_f1_opt_eligible_is_still_a_student():
    # "F-1" and "eligible" are outside the allow-list vocabulary (D-61 amendment): still unknown.
    assert _auth(_wa("F-1 student visa (OPT eligible)"), "US") is None


def test_status_probe_timing():
    text = "Canadian citizen and US green card holder with H-1B via TN / " * 1000
    start = time.perf_counter()
    _wa(text)
    assert time.perf_counter() - start < 2.0
