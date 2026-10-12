"""Scope features from the quantities in bullets (ADVANCED §2.3 as quoted in the Task 6.3 brief).

"QUANTITY PATTERNS -> normalized scope features: '40k req/s', '2M users', '$12M budget', 'team of
14', 'petabyte' -> scale_tier in {personal, team, org, internet} (log-bucketed); people_managed:
int; budget_managed: normalized currency."

* Scale: a count of users or requests (users, customers, subscribers, visitors, requests, req/s,
  queries, transactions, events, messages, downloads, sessions, MAU, QPS, ...) written as a number
  with a magnitude ("40k", "2M", "1.5 million", "10,000+") or as "thousands / millions / tens of
  millions / billions of ...". The largest count decides: below 1e3 personal, below 1e5 team, below
  1e7 org, from 1e7 internet; petabyte scale ("petabyte", "PB", "exabyte") is internet. People
  ("40 analysts") and data sizes in terabytes are not users or requests.
* People managed: a management verb (led, managed, supervised, directed, oversaw, headed; fr géré,
  dirigé, supervisé) before "a team of N" or before N people ("12 volunteers"), or "N direct
  reports". Working in a team, mentoring and a bare "team of 8" are not managing.
* Budget: an amount with a currency ("$12M", "US$1.5 million", "CAD $2M", "€400k", French "50 000 $",
  "2 M$") next to
  budget / P&L / spend / portfolio / fund in the same clause. "$" alone reads as US dollars; CAD,
  EUR and GBP convert at fixed approximate rates (no clock, no network). Revenue and savings are not
  a budget managed.

The largest of each figure is kept; `evidence` lists the matched snippets in bullet order.
Extraction only: parse diagnostics report it, scoring never reads it. Pure and total.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Literal, Optional

from pydantic import Field

from matcher.models import Frozen

__all__ = ["ScopeFeatures", "extract_scope"]

ScaleTier = Literal["personal", "team", "org", "internet"]


class ScopeFeatures(Frozen):
    scale_tier: Optional[ScaleTier]
    people_managed: Optional[int] = Field(default=None, ge=1)
    budget_usd: Optional[float] = Field(default=None, gt=0.0)
    evidence: tuple[str, ...] = ()


# Fixed approximate rates to US dollars (scope is a diagnostic; no live rates, no clock).
_USD_RATES = {"USD": 1.0, "CAD": 0.73, "EUR": 1.08, "GBP": 1.27}
_MAX_EVIDENCE = 20
_MAX_PEOPLE = 1_000_000
_MAX_BUDGET = 1e13

_MAGNITUDES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "millions": 1e6, "b": 1e9,
               "bn": 1e9, "billion": 1e9, "billions": 1e9, "t": 1e12, "trillion": 1e12, "g": 1e9,
               "milliard": 1e9, "milliards": 1e9}
_NUMBER = r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
_MAGNITUDE = r"(?:\s?(?P<mag>k|K|M|MM|B|bn|BN|thousand|million|billion|trillion)\b)?"
_SCALE_NOUN = (r"(?:users?|customers?|clients|subscribers?|visitors?|viewers?|readers?|players?|members|"
               r"requests?|reqs?|queries|transactions?|events?|messages?|calls|api\s+calls|downloads?|installs?|"
               r"sessions?|page\s*views|pageviews|impressions|records|rows|documents|devices|accounts|mau|dau|"
               r"qps|rps|tps|utilisateurs|clients|abonnés|requêtes)")
_SCALE = re.compile(
    rf"(?<![\w.$€£]){_NUMBER}{_MAGNITUDE}\s?\+?\s*(?:(?:[a-z\-]+\s+){{0,2}}){_SCALE_NOUN}(?:\s?/\s?(?:s|sec|second|min|day))?\b",
    re.IGNORECASE,
)
_SCALE_WORDS = re.compile(
    r"\b(?P<words>hundreds\s+of\s+thousands|tens\s+of\s+thousands|thousands|tens\s+of\s+millions|"
    r"hundreds\s+of\s+millions|millions|billions)\s+of\s+(?:(?:[a-z\-]+\s+){0,2})" + _SCALE_NOUN + r"\b",
    re.IGNORECASE,
)
_WORD_COUNTS = {"thousands": 1e3, "tens of thousands": 1e4, "hundreds of thousands": 1e5, "millions": 1e6,
                "tens of millions": 1e7, "hundreds of millions": 1e8, "billions": 1e9}
_PETABYTE = re.compile(r"\b(?:peta-?bytes?|petabyte-scale|exa-?bytes?|\d+(?:\.\d+)?\s?(?:PB|EB))\b", re.IGNORECASE)

_MANAGE = r"(?:led|lead|leading|managed|manage|managing|supervised|supervise|supervising|directed|oversaw|" \
          r"oversee|overseeing|headed|ran|hired\s+and\s+managed|built\s+and\s+led|g[ée]r[ée]|dirig[ée]|supervis[ée])"
_PEOPLE = (r"(?:engineers?|developers?|people|staff|employees|reports|direct\s+reports|volunteers|members|analysts|"
           r"interns|designers|students|technicians|associates|agents|representatives|nurses|workers|"
           r"contractors|scientists|researchers|managers|cashiers|servers|teammates|personnes|employ[ée]s|"
           r"analystes|b[ée]n[ée]voles|stagiaires)")
_TEAM_OF = re.compile(
    rf"\b{_MANAGE}\b(?:\s+(?!and\b|or\b)[\w\-]+){{0,3}}?\s+(?:a\s+|an\s+|une\s+|the\s+)?"
    rf"(?:(?:cross-functional|global|remote|distributed|small|large)\s+)?(?P<snippet>(?:team|squad|crew|group|"
    rf"[ée]quipe)\s+(?:of|de)\s+(?P<n>\d{{1,6}}))\b",
    re.IGNORECASE,
)
_MANAGED_COUNT = re.compile(rf"\b{_MANAGE}\s+(?P<snippet>(?P<n>\d{{1,6}})\s+(?:[\w\-]+\s+)?{_PEOPLE})\b", re.IGNORECASE)
_DIRECT_REPORTS = re.compile(r"\b(?P<snippet>(?P<n>\d{1,6})\s+direct\s+reports)\b", re.IGNORECASE)

_CURRENCY = r"(?P<cur>US\$|USD\s?\$?|CAD\s?\$?|C\$|CA\$|\$|EUR\s?|€|GBP\s?|£)"
_AMOUNT = re.compile(rf"{_CURRENCY}\s?(?P<num>\d{{1,3}}(?:,\d{{3}})+|\d+(?:\.\d+)?){_MAGNITUDE}", re.IGNORECASE)
# French order: the currency after the amount, spaces between thousands ("50 000 $", "2 M$").
_AMOUNT_AFTER = re.compile(
    r"(?<![\w.,])(?P<num>\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?)\s?"
    r"(?P<mag>k|K|M|MM|G|millions?|milliards?)?\s?(?P<cur>\$|€|£|CAD|USD|EUR|GBP)(?![\w$])"
)
_BUDGET_NOUN = re.compile(r"\b(?:budgets?|p\s?&\s?l|spend|spending|portfolio|fund|funds|budg[ée]t)\b", re.IGNORECASE)
_CLAUSE = re.compile(r"[;!?]|\.(?!\d)|\s(?:and|but)\s")


def _number(text: str) -> Optional[float]:
    try:
        value = float(re.sub(r"[ ,\u00a0\u202f]", "", text))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _scaled(num: str, mag: Optional[str]) -> Optional[float]:
    value = _number(num)
    if value is None:
        return None
    if mag:
        value *= _MAGNITUDES.get(mag.lower(), 1.0)
    return value if math.isfinite(value) else None


def _tier(count: float) -> ScaleTier:
    if count < 1e3:
        return "personal"
    if count < 1e5:
        return "team"
    if count < 1e7:
        return "org"
    return "internet"


def _currency(symbol: str) -> str:
    folded = symbol.upper().replace(" ", "").rstrip("$") if symbol not in ("$",) else "$"
    if folded in ("", "$", "US", "USD", "US$"):
        return "USD"
    if folded in ("CAD", "C", "CA"):
        return "CAD"
    if folded in ("EUR", "€"):
        return "EUR"
    if folded in ("GBP", "£"):
        return "GBP"
    return "USD"


def _clause_around(text: str, start: int, end: int) -> tuple[int, int]:
    """The bounds of the clause holding text[start:end] (split at . ; ! ? and "and" / "but")."""
    left = max((m.end() for m in _CLAUSE.finditer(text, 0, start)), default=0)
    right = next((m.start() for m in _CLAUSE.finditer(text, end)), len(text))
    return left, right


def extract_scope(bullets: Sequence[str]) -> ScopeFeatures:
    """Scope features of a role's bullets (see the module docstring)."""
    scale: Optional[float] = None
    petabyte = False
    people: Optional[int] = None
    budget: Optional[float] = None
    evidence: list[tuple[int, int, str]] = []  # (bullet, offset, snippet)
    for b, bullet in enumerate(bullets):
        if not isinstance(bullet, str) or not bullet.strip():
            continue
        for match in _SCALE.finditer(bullet):
            count = _scaled(match.group("num"), match.group("mag"))
            if count is not None and count >= 1:
                scale = count if scale is None else max(scale, count)
                evidence.append((b, match.start(), match.group().strip()))
        for match in _SCALE_WORDS.finditer(bullet):
            count = _WORD_COUNTS.get(" ".join(match.group("words").lower().split()))
            if count is not None:
                scale = count if scale is None else max(scale, count)
                evidence.append((b, match.start(), match.group().strip()))
        for match in _PETABYTE.finditer(bullet):
            petabyte = True
            evidence.append((b, match.start(), match.group().strip()))
        for pattern in (_TEAM_OF, _MANAGED_COUNT, _DIRECT_REPORTS):
            for match in pattern.finditer(bullet):
                n = int(match.group("n"))
                if 1 <= n <= _MAX_PEOPLE:
                    people = n if people is None else max(people, n)
                    evidence.append((b, match.start("snippet"), match.group("snippet").strip()))
        amounts = [*_AMOUNT.finditer(bullet), *_AMOUNT_AFTER.finditer(bullet)]
        for match in sorted(amounts, key=lambda m: m.start()):
            left, right = _clause_around(bullet, match.start(), match.end())
            nouns = list(_BUDGET_NOUN.finditer(bullet, left, right))
            if not nouns:
                continue
            value = _scaled(match.group("num"), match.group("mag"))
            if value is None or value <= 0:
                continue
            usd = value * _USD_RATES[_currency(match.group("cur"))]
            if not math.isfinite(usd) or usd > _MAX_BUDGET:
                continue
            budget = usd if budget is None else max(budget, usd)
            after = [noun for noun in nouns if noun.start() >= match.end()]
            before = [noun for noun in nouns if noun.end() <= match.start()]
            if after and after[0].start() - match.end() <= 25:  # "$12M budget", "$1.5M marketing budget"
                start, end = match.start(), after[0].end()
            elif before:  # "a budget of $250K"
                start, end = before[-1].start(), match.end()
            else:
                start, end = match.start(), match.end()
            evidence.append((b, start, bullet[start:end].strip()))
    tier: Optional[ScaleTier] = "internet" if petabyte else (_tier(scale) if scale is not None else None)
    ordered = [snippet for _, _, snippet in sorted(evidence, key=lambda e: (e[0], e[1]))]
    unique = list(dict.fromkeys(ordered))[:_MAX_EVIDENCE]
    return ScopeFeatures(scale_tier=tier, people_managed=people, budget_usd=budget, evidence=tuple(unique))
