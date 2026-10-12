"""Industry codes for an employer (SPEC §5.5; DECISIONS D-40; config/parse/industries.yaml).

The curated company table comes first (normalised names: legal suffixes, a trailing country or
"group" and an ATS tag in brackets are ignored), then keyword rules over the name, then the same
rules over the context (a role's first bullet, a posting's company description). Codes are NAICS
2022, the edition `data/taxonomy/naics.json` holds; an unknown employer gets `()`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matcher.parse.config import load_parse_config
from matcher.parse.industry import classify_industry, normalize_company
from matcher.parse.taxonomy_extras import load_naics_codes

REAL = Path(__file__).resolve().parents[1] / "data" / "taxonomy"


@pytest.fixture(scope="module")
def pc():
    return load_parse_config()


@pytest.mark.parametrize(
    "name, expected",
    [
        # Curated table: Tailrd's most frequent employers and well-known Canadian / US ones.
        ("Royal Bank of Canada", ("5221",)),
        ("RBC", ("5221",)),
        ("TD Bank Group", ("5221",)),
        ("Banque Nationale du Canada", ("5221",)),
        ("Shopify Inc.", ("5132",)),
        ("Shopify", ("5132",)),
        ("Google Canada", ("5192",)),
        ("Google LLC", ("5192",)),
        ("Postman (Greenhouse)", ("5132",)),  # an ATS tag from the job feed
        ("Notion (Ashby)", ("5132",)),
        ("Carvana", ("4411",)),
        ("SpaceX", ("3364",)),
        ("Jane Street", ("5231",)),
        ("Manulife", ("5241",)),
        ("Deloitte", ("5412", "5416")),
        ("CGI", ("5415",)),
        ("Hydro One", ("22",)),
        ("Bell Canada", ("517",)),
        ("Air Canada", ("4811",)),
        ("Loblaw Companies Limited", ("4451",)),
        ("Tim Hortons", ("7225",)),
        ("Amazon Web Services", ("5182",)),
        ("U.S. Army", ("9281",)),
        ("Canadian Armed Forces", ("9281",)),
        # Keyword rules over the name.
        ("City of Ottawa / Ville d'Ottawa", ("92",)),
        ("Ministry of Health", ("92",)),
        ("Ministère de la Santé et des Services sociaux", ("92",)),
        ("Government of Nova Scotia", ("92",)),
        ("Maple Leaf Credit Union", ("5221",)),
        ("Daily Bread Food Bank", ("6242",)),  # a food bank is no bank
        ("Toronto General Hospital", ("6221",)),
        ("Hôpital Sainte-Justine", ("6221",)),
        ("University of Waterloo", ("6113",)),
        ("Université de Montréal", ("6113",)),
        ("Ottawa-Carleton District School Board", ("6111",)),
        ("Centre de services scolaire de Montréal", ("6111",)),
        ("Northstar Consulting Group", ("5416",)),
        ("Prairie Mutual Insurance Co.", ("5241",)),
        ("Brightline Telecom", ("517",)),
        ("Corner Pizza Restaurant", ("7225",)),
        ("Westbuild Construction Ltd.", ("23",)),
        ("Acme Software Inc.", ("5132", "5415")),
        ("Nova Pharmaceuticals", ("3254", "5417")),
        ("Maritime Logistics", ("4931", "4841")),
        ("Smith & Lee LLP, Barristers and Solicitors", ("5411",)),
        ("Grant & Patel Chartered Professional Accountants", ("5412",)),
        ("Hope Foundation (non-profit)", ("8133",)),
        ("Riverside Dental Clinic", ("6212",)),
        ("Sunrise Daycare", ("6244",)),
        ("Northern Lights Hotel", ("7211",)),
        ("Main Street Grocery Store", ("4451",)),  # a specific rule is listed before the general one
        ("Riverside Book Store", ("44-45",)),
    ],
)
def test_classify_industry_by_name(pc, name: str, expected: tuple[str, ...]) -> None:
    assert classify_industry(name, "", pc) == expected


@pytest.mark.parametrize(
    "name, context, expected",
    [
        # The context decides only when the name says nothing.
        ("Northwind", "Northwind is a Toronto software company building SaaS tools for clinics.", ("5132", "5415")),
        ("Lumen Labs", "Processed claims for a regional insurance carrier.", ("5241",)),
        ("Toronto General Hospital", "Built a SaaS dashboard for the cardiology team.", ("6221",)),
        ("Shopify", "Supported the bank reconciliation team.", ("5132",)),
        ("", "Served customers at a busy downtown restaurant.", ("7225",)),
    ],
)
def test_classify_industry_with_context(pc, name: str, context: str, expected: tuple[str, ...]) -> None:
    assert classify_industry(name, context, pc) == expected


@pytest.mark.parametrize(
    "name, context",
    [
        ("Stealth Startup", ""),
        ("Self-employed", ""),
        ("Freelance", "Designed logos for local clients."),
        ("", ""),
        ("Bankside Partners", ""),  # "bank" only as a whole word
        ("Apple Tree Daycare and Learning", ""),  # never the company Apple by a prefix
    ],
)
def test_unknown_or_ambiguous_employers(pc, name: str, context: str) -> None:
    codes = classify_industry(name, context, pc)
    if name.startswith("Apple Tree"):
        assert codes == ("6244",)  # the daycare rule, not Apple Inc.
    else:
        assert codes == ()


def test_classify_industry_never_raises_on_odd_text(pc) -> None:
    for text in ("\x00\x01", "&&&", "()", "/ / /", "Inc.", "LLC", "a" * 5000, "Bank" * 300):
        assert isinstance(classify_industry(text, text, pc), tuple)


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("Shopify Inc.", "shopify"),
        ("The Home Depot", "home depot"),
        ("Postman (Greenhouse)", "postman"),
        ("Hôpital Sainte-Justine", "hopital sainte justine"),
        ("Smith & Lee LLP", "smith and lee"),
        ("Belron Canada inc", "belron canada"),
    ],
)
def test_normalize_company(pc, raw: str, normalized: str) -> None:
    assert normalize_company(raw, pc) == normalized


def test_every_configured_code_is_a_naics_2022_code(pc) -> None:
    naics = load_naics_codes(REAL)  # read through the MANIFEST
    configured = {code for company in pc.industries.companies for code in company.naics}
    configured |= {code for rule in pc.industries.rules for code in rule.naics}
    for code in sorted(configured):
        parts = code.split("-")
        assert all(part in naics for part in parts), f"{code} is not a NAICS 2022 code"


def test_the_table_covers_tailrds_most_frequent_employers(pc) -> None:
    assert len(pc.industries.companies) >= 150
    for name in ("Carvana", "SpaceX", "Toast", "Verkada", "Databricks", "Waymo", "Stripe", "Datadog", "Twilio",
                 "GitLab", "Scotiabank", "Kinaxis", "Wealthsimple"):
        assert classify_industry(name, "", pc) != (), name
