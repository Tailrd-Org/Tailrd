"""Résumé entries: dated header lines with their head lines and bullets (SPEC §14.2-§14.3).

Shared by the section fallback (`sections.segment` when no header exists anywhere) and by
`roles.split_roles`. A line is classified once (`classify`):

* bullet: a bullet glyph starts it ("- ", "•", "▪", ...), or, for raw text, an indented line;
* anchor: a line that is not a bullet and holds a date range, read with `find_date_ranges` on
  that line only (never on a bullet: "Rolled out Microsoft Office 2019" is no role date). Two-ended
  ranges ("May 2023 – Aug 2023", "2016 – Present") win; a lone period ("Summer 2024", "2021") counts
  only when it is named or set apart by a separator ("Shopify, 2021", never "Office 2019"). When a
  section has no such line, bulleted lines with a two-ended range are the anchors ("- Cashier,
  Loblaws | 2021 – 2022");
* head: a short line that could name a title, an employer or a place (at most 12 words, no final
  sentence period, not starting in lower case, no e-mail, URL or phone number);
* label: "Technologies: React" (never a head line).

An entry is an anchor plus the head lines it needs: a header line holds two pieces at most (title
and employer), so an anchor with fewer pieces takes the head lines right above it, then right
below it ("Employer / Title / dates", "Title  dates / Employer  Location"); places and employment
types right below always join ("Ottawa, ON", "Full-time"). A dated line whose only piece names no
job, directly followed by another anchor, is an employer heading its promotions (`group`). Without
an anchor, a run of head lines naming a job or an organisation and followed by bullets is an
undated entry (role parsing only). The body is the lines up to the next entry; in strict mode
(the fallback) it ends at the first line that is neither a bullet, a bullet's continuation nor a
sentence. Standard library only (plus the date parser), total on any input.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from matcher.parse._text import fold, words
from matcher.parse.dates import DateRange, find_date_ranges

# ─── vocabulary (grammar for reading header lines, not weights) ─────────────────────────────

# Words that make a piece of a header line look like a job title.
TITLE_NOUNS: frozenset[str] = frozenset("""
intern interns internship student students assistant associate analyst engineer developer programmer
designer scientist researcher consultant manager director coordinator specialist officer administrator
representative rep agent technician technologist architect lead leader head supervisor president vp ceo
cto cfo coo cio cmo founder cofounder owner partner advisor adviser counsellor counselor tutor teacher
instructor lecturer professor fellow ta ra gta volunteer member captain treasurer secretary chair
chairperson editor writer accountant auditor bookkeeper clerk cashier server barista cook chef baker
nurse therapist pharmacist physician doctor dentist receptionist driver operator mechanic electrician
plumber carpenter welder labourer laborer worker crew staff attendant host hostess bartender lifeguard
guard mentor ambassador organizer organiser facilitator trainer coach strategist marketer recruiter buyer
planner estimator inspector surveyor paralegal lawyer solicitor attorney stagiaire apprentice trainee
merchandiser salesperson banker teller underwriter actuary economist statistician librarian curator
photographer videographer journalist reporter producer animator illustrator artist musician dispatcher
courier picker packer stocker janitor custodian housekeeper nanny caregiver aide paramedic medic
firefighter soldier rifleman infantryman corpsman sergeant corporal lieutenant sailor airman
demonstrator moderator investigator entrepreneur
developpeur developpeuse ingenieur ingenieure analyste conseiller conseillere gestionnaire directeur
directrice adjoint adjointe technicien technicienne coordonnateur coordonnatrice coordinateur
coordinatrice prepose preposee caissier caissiere serveur serveuse commis vendeur vendeuse representant
representante agente charge chargee responsable assistante enseignant enseignante tuteur tutrice
moniteur monitrice animateur animatrice benevole superviseur superviseure president presidente fondateur
fondatrice infirmier infirmiere etudiant etudiante apprenti apprentie comptable chercheur chercheuse
redacteur redactrice traducteur traductrice educateur educatrice professeure mecanicien electricien
soudeur cuisinier cuisiniere livreur chauffeur auxiliaire
""".split())

# Seniority and student words: a piece holding one reads as a title too.
_TITLE_MODIFIERS: tuple[tuple[str, ...], ...] = tuple(
    tuple(phrase.split()) for phrase in (
        "intern", "co op", "coop", "senior", "junior", "sr", "jr", "lead", "principal", "staff", "entry level",
        "graduate", "trainee", "apprentice", "stagiaire", "stage", "summer", "incoming", "freelance",
    )
)

# Words that make a piece look like an organisation.
EMPLOYER_CUES: frozenset[str] = frozenset("""
inc incorporated ltd limited llc llp lp corp corporation co company plc gmbh ltee university universite
college cegep polytechnique institute institut school ecole academy bank banque group groupe
technologies technology labs laboratory laboratories solutions systems services software consulting
consultants partners associates hospital hopital clinic clinique health sante ministry ministere
government gouvernement agency agence department foundation fondation society club association council
conseil centre center board city ville municipality county region province network capital financial
insurance assurance assurances communications media studio studios ventures holdings industries
enterprises international global canada army navy forces corps guard library bibliotheque museum church
hotel restaurant cafe store stores shop market foods airlines airways motors energy hydro
pharmaceuticals therapeutics freelance
""".split())

_SELF_EMPLOYED = (("self", "employed"), ("freelance",), ("freelancer",), ("travailleur", "autonome"))

# Employment types written as their own piece ("Shopify · Internship"). The internship-like ones
# are kept on the title ("Software Developer (Internship)"); the others are dropped.
_EMPLOYMENT_TYPES: dict[tuple[str, ...], bool] = {
    tuple(words(text)): keep for text, keep in (
        ("full-time", False), ("full time", False), ("fulltime", False), ("part-time", False),
        ("part time", False), ("parttime", False), ("contract", False), ("contractor", False),
        ("temporary", False), ("temp", False), ("permanent", False), ("seasonal", False), ("casual", False),
        ("temps plein", False), ("temps partiel", False), ("contractuel", False), ("contractuelle", False),
        ("saisonnier", False), ("saisonniere", False), ("permanent full-time", False),
        ("internship", True), ("co-op", True), ("coop", True), ("co-op term", True), ("apprenticeship", True),
        ("volunteer", True), ("stage", True), ("benevole", True), ("work term", True),
    )
}

# Words a date leaves behind on its line ("Expected May 2027", "From 2019").
_DATE_WORDS = frozenset({"expected", "anticipated", "est", "prevu", "prevue", "from", "since", "starting",
                         "ongoing", "depuis", "present", "current"})

# ─── places ─────────────────────────────────────────────────────────────────────────────────

_CA_CODES = "AB BC MB NB NL NS NT NU ON PE QC SK YT"
_US_CODES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY")
_REGION_NAMES = (
    "Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland and Labrador|Newfoundland|Nova Scotia|"
    "Northwest Territories|Nunavut|Ontario|Prince Edward Island|Qu[ée]bec|Saskatchewan|Yukon|Alabama|Alaska|"
    "Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|"
    "Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|"
    "Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
    "Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|"
    "Virginia|Washington|West Virginia|Wisconsin|Wyoming"
)
_COUNTRIES = (
    "Canada|USA|U\\.S\\.A\\.?|US|U\\.S\\.?|United States(?: of America)?|UK|U\\.K\\.?|United Kingdom|England|"
    "France|Germany|India|China|Mexico|Ireland|Netherlands|Singapore|Japan|Australia|Brazil|Spain|Italy|"
    "Switzerland|Sweden|Israel|Belgium|Portugal|Poland|Nigeria|Pakistan|Philippines|Vietnam|South Korea|Korea"
)
_CITIES = (
    "Toronto|Montr[ée]al|Vancouver|Ottawa|Calgary|Edmonton|Waterloo|Kitchener|Mississauga|Winnipeg|Halifax|"
    "Qu[ée]bec City|Victoria|Hamilton|London|Markham|Burnaby|Laval|Gatineau|Saskatoon|Regina|Brampton|Oakville|"
    "Guelph|Kingston|Sherbrooke|St\\.? John'?s|Fredericton|Moncton|Charlottetown|Surrey|Richmond|Vaughan|"
    "Windsor|Barrie|Oshawa|Longueuil|L[ée]vis|Trois-Rivi[èe]res|Kelowna|Nanaimo|Lethbridge|Red Deer|Sudbury|"
    "Thunder Bay|Saint John|Whitehorse|Yellowknife|Iqaluit|New York(?: City)?|NYC|San Francisco|Seattle|"
    "Boston|Chicago|Austin|Los Angeles|Denver|Atlanta|Miami|Dallas|Houston|Washington|Philadelphia|Pittsburgh|"
    "Palo Alto|Mountain View|Menlo Park|Redmond|Cupertino|San Jose|Sunnyvale|Santa Clara|San Diego|Portland|"
    "Detroit|Minneapolis|Phoenix|Salt Lake City|Raleigh|Bay Area|Silicon Valley|Greater Toronto Area|GTA|"
    "Paris|Berlin|Bangalore|Bengaluru|Hyderabad|Mumbai|Delhi|Singapore|Dublin|Amsterdam|Tokyo|Sydney"
)
_REMOTE = (r"(?i:remote|hybrid|on-?site|in-?person|work from home|wfh|virtual|t[ée]l[ée]travail|"
           r"[àa] distance|en ligne|hybride|sur place|pr[ée]sentiel)")
_CODES = "|".join(f"{_CA_CODES} {_US_CODES}".split())
_PLACE_WORD = r"[A-ZÀ-ÖØ-Þ][\w.'’\-]*"
# Single spaces only: two spaces or a tab separate columns ("DESJARDINS    Montreal, QC").
_CITY = rf"{_PLACE_WORD}(?: (?:de |du |of |la )?{_PLACE_WORD}){{0,3}}"
_REGION = rf"(?:(?:{_CODES})(?![\w])|(?i:{_REGION_NAMES})(?![\w]))"
_COUNTRY = rf"(?i:{_COUNTRIES})(?![\w])"
_PLACE = (rf"(?:{_CITY}\s*,\s*{_REGION}(?:\s*,\s*{_COUNTRY})?|{_CITY}\s*,\s*{_COUNTRY}|"
          rf"(?i:{_CITIES})(?:\s*,\s*{_COUNTRY})?|{_REGION}(?:\s*,\s*{_COUNTRY})?|{_COUNTRY})")
_WHOLE_PLACE = re.compile(rf"\s*\(?\s*(?:{_REMOTE}(?:\s*[(,\-–]\s*(?:{_PLACE})\s*\)?)?|(?:{_PLACE})"
                          rf"(?:\s*[(,\-–]\s*{_REMOTE}\s*\)?)?)\s*\)?\s*")
_TAIL_PLACE = re.compile(rf"(?:^|(?<=[\s,(|]))(?P<place>{_CITY}\s*,\s*{_REGION}(?:\s*,\s*{_COUNTRY})?|"
                         rf"{_CITY}\s*,\s*{_COUNTRY})\s*\)?\s*$")
_KNOWN_CITY_END = re.compile(rf"(?:^| )(?i:{_CITIES})$")


def _known_city_start(city: str) -> int:
    """Where a known city starts inside a would-be city with words before it ("Shopify Ottawa"
    -> 8), else 0."""
    found = _KNOWN_CITY_END.search(city)
    if found is None or found.start() == 0:
        return 0
    return found.start() + 1


def place_of(text: str) -> str:
    """`text` itself when the whole of it names a place or a work arrangement ("Ottawa, ON",
    "Remote", "Toronto, Ontario, Canada", "Canada"), else ""."""
    stripped = text.strip()
    if not stripped or len(stripped) > 80 or _WHOLE_PLACE.fullmatch(stripped) is None:
        return ""
    if _names_a_job(stripped):
        return ""
    tail = _TAIL_PLACE.search(stripped)
    if tail is not None and _known_city_start(tail.group("place").split(",", 1)[0]):
        return ""  # "Shopify Ottawa, ON": an employer, then the place
    return stripped.strip("()").strip()


def split_place(text: str) -> tuple[str, str]:
    """(the rest, the place) for a piece ending in "City, Region[, Country]" ("Shopify, Ottawa, ON")."""
    found = _TAIL_PLACE.search(text)
    if found is None:
        return text, ""
    place = found.group("place").strip()
    city = place.split(",", 1)[0]
    if _names_a_job(city):  # "Software Engineer, ON" is no place
        return text, ""
    shift = _known_city_start(city)
    return text[: found.start("place") + shift], place[shift:]


def _names_a_job(text: str) -> bool:
    return any(word in TITLE_NOUNS for word in words(text))


# ─── pieces of a header line ────────────────────────────────────────────────────────────────

_STRONG = re.compile(
    r"\s*\|\s*|\s*[•·]\s*|\t+\s*|\s{2,}|\s+[–—\-]+\s+|\s+@\s+"
    r"|\s+(?:at|chez)\s+(?=(?:the\s+)?[A-Z0-9À-ÖØ-Þ])"
)
_BRACKET = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_EMPTY_BRACKETS = re.compile(r"\(\s*[,;:|/\-–—]*\s*\)|\[\s*[,;:|/\-–—]*\s*\]")
_EDGE_CHARS = " \t,;:|/·•–—-~"
_DURATION = re.compile(
    r"[\s·•|,(\[–—-]{0,8}\b\d+\s*(?:\+\s*)?(?:yrs?|years?|ans?|mos?|months?|mois)\b\.?"
    r"(?:\s*\d+\s*(?:mos?|months?|mois)\b\.?)?\s*[)\]]?",
    re.IGNORECASE,
)


def tidy(text: str) -> str:
    """Spaces collapsed, empty brackets and stray separators gone, edges stripped."""
    text = " ".join(text.split())
    previous = None
    while previous != text:
        previous = text
        text = _EMPTY_BRACKETS.sub(" ", text)
    text = re.sub(r"\( ?[,;|/] ?", "(", text)
    text = re.sub(r" ?[,;|/] ?\)", ")", text)
    text = re.sub(r" ([,;)\]])", r"\1", text)
    text = re.sub(r"([(\[]) ", r"\1", text)
    text = " ".join(text.split())
    text = text.strip(_EDGE_CHARS)
    if text.count("(") > text.count(")"):
        text = text.rstrip("(").strip(_EDGE_CHARS)
    if text.startswith(")"):
        text = text.lstrip(")").strip(_EDGE_CHARS)
    return text


class HeadParts(NamedTuple):
    pieces: tuple[str, ...]  # title or employer candidates, in order
    place: str
    kinds: tuple[str, ...]  # internship-like employment types written as a piece


def _strip_date_words(piece: str) -> str:
    parts = piece.split()
    while parts and fold(parts[0]).strip(".:") in _DATE_WORDS:
        parts.pop(0)
    while parts and fold(parts[-1]).strip(".:") in _DATE_WORDS:
        parts.pop()
    return " ".join(parts)


def head_parts(text: str) -> HeadParts:
    """Split a header line (dates already removed) into title/employer pieces, a place and the
    employment types written as their own piece."""
    place = ""
    kinds: list[str] = []

    def lift_place(found: re.Match[str]) -> str:
        nonlocal place
        inner = found.group(1) if found.group(1) is not None else found.group(2)
        here = place_of(inner or "")
        if here:
            place = place or here
            return " "
        return found.group(0)

    text = _BRACKET.sub(lift_place, text)
    out: list[str] = []
    for raw_piece in _STRONG.split(text):
        piece = _strip_date_words(tidy(raw_piece))
        if not piece:
            continue
        here = place_of(piece)
        if here:
            place = place or here
            continue
        key = tuple(words(piece))
        if key in _EMPLOYMENT_TYPES:
            if _EMPLOYMENT_TYPES[key]:
                kinds.append(piece)
            continue
        rest, tail = split_place(piece)
        if tail:
            place = place or tail
            piece = _strip_date_words(tidy(rest))
            if not piece:
                continue
        out.append(piece)
    if len(out) == 1 and "," in out[0]:
        parts = [p for p in (_strip_date_words(tidy(part)) for part in out[0].split(",")) if p]
        while len(parts) >= 2 and place_of(parts[-1]):  # "Barista, Starbucks, Toronto"
            place = place or place_of(parts[-1])
            parts.pop()
        if len(parts) >= 2:
            out = _comma_pieces(parts)
        elif parts:
            out = parts
    return HeadParts(tuple(out), place, tuple(kinds))


def _comma_pieces(parts: list[str]) -> list[str]:
    """"Title, Employer" -> two pieces; with three or more, the last one is the employer unless it
    names a job ("Manager, Business Development, RBC" -> title "Manager, Business Development")."""
    if len(parts) == 2:
        return parts
    if title_score(parts[-1]) == 0:
        return [", ".join(parts[:-1]), parts[-1]]
    return [parts[0], ", ".join(parts[1:])]


def title_score(piece: str) -> int:
    tokens = words(piece)
    score = sum(1 for word in tokens if word in TITLE_NOUNS)
    for phrase in _TITLE_MODIFIERS:
        n = len(phrase)
        score += sum(1 for i in range(len(tokens) - n + 1) if tuple(tokens[i:i + n]) == phrase)
    return score


def employer_score(piece: str) -> int:
    tokens = words(piece)
    score = sum(1 for word in tokens if word in EMPLOYER_CUES)
    for phrase in _SELF_EMPLOYED:
        n = len(phrase)
        score += sum(1 for i in range(len(tokens) - n + 1) if tuple(tokens[i:i + n]) == phrase)
    return score


def names_a_role(text: str) -> bool:
    """Whether a head line names a job or an organisation (an undated entry starts there)."""
    return title_score(text) > 0 or employer_score(text) > 0


# ─── lines ──────────────────────────────────────────────────────────────────────────────────

_GLYPHS = "•▪◦●‣∙➢➤►▶✓✔○■◆□➔→·"
_BULLET = re.compile(rf"^\s*(?:[{_GLYPHS}–—]\s*|[-*]\s+)")
_LABEL = re.compile(r"^\s*([^\W\d_][^\d:]{0,40}?)\s*:\s*\S")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}(?!\d)")
_ABBREVIATION_END = re.compile(
    r"(?:\b(?:inc|corp|ltd|co|jr|sr|st|ltee|llc|llp|plc|dept|univ|assn|bros|mfg|intl)|(?:\b[A-Za-z]\.){2,})\.$",
    re.IGNORECASE,
)
_RANGE_SEPARATOR = re.compile(
    r"[–—‒―−~\-]|\d\s*/\s*\d|\b(?:to|until|till|through|thru|au|jusqu|since|depuis|d[èe]s)\b"
    r"|pr[ée]sent|current|now|ongoing|aujourd|actuel|en cours|ce jour|today|date",
    re.IGNORECASE,
)
_SEPARATED = re.compile(r"(?:^|[,|(\[–—\-·•:;]\s*|\s{2,}|\t)$")
_MAX_HEAD_WORDS = 12


@dataclass(frozen=True)
class Line:
    index: int
    raw: str
    text: str  # stripped; a bullet without its glyph
    blank: bool
    bullet: bool
    indented: bool
    label: bool
    head: bool
    sentence: bool
    place: bool  # the whole line names a place or a work arrangement
    ranges: tuple[tuple[int, int, DateRange], ...] = ()  # offsets into `text`; anchors only


def _two_ended(found: tuple[int, int, DateRange]) -> bool:
    return found[2].is_current or _RANGE_SEPARATOR.search(found[2].raw) is not None


def anchor_ranges(text: str, *, as_of: dt.date, lang: str) -> tuple[tuple[int, int, DateRange], ...]:
    """The ranges that make `text` a dated header line (see the module docstring)."""
    found = find_date_ranges(text, as_of=as_of, lang="fr" if lang == "fr" else "en")
    if not found:
        return ()
    two_ended = tuple(item for item in found if _two_ended(item))
    if two_ended:
        return two_ended
    singles = []
    for item in found:
        named = any(ch.isalpha() for ch in item[2].raw)
        if named or _SEPARATED.search(text[: item[0]]) is not None:
            singles.append(item)
    return tuple(singles)


def is_sentence(text: str) -> bool:
    if len(text.split()) > 20:
        return True
    return text.endswith((".", "!", "?")) and _ABBREVIATION_END.search(text) is None


def is_head(text: str) -> bool:
    tokens = text.split()
    if not tokens or len(tokens) > _MAX_HEAD_WORDS or is_sentence(text):
        return False
    first = next((ch for ch in text if ch.isalnum()), "")
    if not first or first.islower():
        return False
    lowered = text.casefold()
    if "@" in text or "http" in lowered or "www." in lowered or _PHONE.search(text) is not None:
        return False
    return any(ch.isalpha() for ch in text)


def classify(raw_lines: Sequence[str], *, as_of: dt.date, lang: str = "en") -> list[Line]:
    out: list[Line] = []
    for index, raw_line in enumerate(raw_lines):
        raw = str(raw_line).replace("\r", "").replace("\n", " ").rstrip()
        if not raw.strip():
            out.append(Line(index, raw, "", True, False, False, False, False, False, False))
            continue
        glyph = _BULLET.match(raw)
        text = (raw[glyph.end():] if glyph else raw).strip()
        bullet = glyph is not None and bool(text)
        indented = not bullet and raw[:1].isspace()
        if not text:  # a lone glyph
            out.append(Line(index, raw, "", True, False, False, False, False, False, False))
            continue
        label_match = _LABEL.match(text)
        label = label_match is not None and len(label_match.group(1).split()) <= 4
        ranges = () if bullet else anchor_ranges(text, as_of=as_of, lang=lang)
        out.append(Line(
            index=index, raw=raw, text=text, blank=False, bullet=bullet, indented=indented, label=label,
            head=not bullet and not label and is_head(text), sentence=is_sentence(text),
            place=not bullet and bool(place_of(text)), ranges=ranges,
        ))
    if not any(line.ranges for line in out):  # one bulleted line per role
        out = [_bullet_anchor(line, as_of, lang) for line in out]
    return out


def _bullet_anchor(line: Line, as_of: dt.date, lang: str) -> Line:
    if not line.bullet or line.sentence:
        return line
    ranges = tuple(item for item in anchor_ranges(line.text, as_of=as_of, lang=lang) if _two_ended(item))
    if not ranges:
        return line
    return Line(line.index, line.raw, line.text, False, False, False, False, False, False, False, ranges)


def remainder(line: Line) -> str:
    """A dated header line without its ranges and the duration LinkedIn adds ("· 4 mos")."""
    text = line.text
    for start, end, _ in sorted(line.ranges, reverse=True):
        text = text[:start] + " | " + text[end:]
    text = _DURATION.sub(" | ", text)
    previous = None
    while previous != text:
        previous = text
        text = _EMPTY_BRACKETS.sub(" ", text)
    return text.strip(_EDGE_CHARS)  # inner spacing kept: two spaces separate pieces


# ─── entries ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Entry:
    start: int
    end: int
    anchor: Optional[int]
    before: tuple[int, ...]
    after: tuple[int, ...]
    body: tuple[int, ...]
    group: bool = False
    pieces: dict[int, HeadParts] = field(default_factory=dict, compare=False)


_DANGLING_WORDS = frozenset({"and", "or", "the", "a", "an", "of", "for", "to", "with", "in", "on", "at", "by",
                             "from", "using", "via", "into", "across", "over", "under", "as", "including",
                             "such", "plus", "et", "de", "des", "du", "la", "le", "les", "pour", "avec"})


def continues(previous: str, line: Line) -> bool:
    """Whether `line` carries on the text of the item before it (a soft-wrapped bullet)."""
    if line.bullet or not previous:
        return False
    first = line.text[:1]
    if first.islower() or first.isdigit() or first in "(&,;:%$+/":
        return True
    tail = previous.rstrip()
    if tail.endswith((",", "-", "–", "/", "&", "+", "(")):
        return True
    last = tail.split()[-1].casefold() if tail.split() else ""
    return last in _DANGLING_WORDS


def entries(lines: Sequence[Line], *, strict: bool) -> list[Entry]:
    """Entries in document order (see the module docstring)."""
    anchors = [line.index for line in lines if line.ranges]
    claimed: set[int] = set(anchors)
    specs: list[Entry] = []
    pieces: dict[int, HeadParts] = {}

    def parts_of(index: int) -> HeadParts:
        if index not in pieces:
            line = lines[index]
            pieces[index] = head_parts(remainder(line) if line.ranges else line.text)
        return pieces[index]

    for position, anchor in enumerate(anchors):
        need = 2 - min(2, len(parts_of(anchor).pieces))
        before: list[int] = []
        i = anchor - 1
        while need > 0 and i >= 0:
            line = lines[i]
            if i in claimed or line.blank or line.bullet or line.ranges or line.label or not line.head or line.place:
                break
            before.append(i)
            need -= max(1, len(parts_of(i).pieces))
            i -= 1
        before.reverse()
        after: list[int] = []
        j = anchor + 1
        while j < len(lines):
            line = lines[j]
            if j in claimed or line.blank or line.bullet or line.ranges or line.label:
                break
            if line.place or (line.head and not parts_of(j).pieces):
                after.append(j)
            elif need > 0 and line.head:
                after.append(j)
                need -= max(1, len(parts_of(j).pieces))
            else:
                break
            j += 1
        claimed.update(before)
        claimed.update(after)
        group = _heads_promotions(lines, anchors, position, parts_of)
        specs.append(Entry(start=before[0] if before else anchor, end=anchor, anchor=anchor, before=tuple(before),
                           after=tuple(after), body=(), group=group))

    if not strict:
        specs.extend(_undated(lines, claimed, parts_of))
    specs.sort(key=lambda entry: entry.start)

    out: list[Entry] = []
    for k, spec in enumerate(specs):
        heads = [*spec.before, *([spec.anchor] if spec.anchor is not None else []), *spec.after]
        last_head = max(heads)
        limit = specs[k + 1].start if k + 1 < len(specs) else len(lines)
        body = _body(lines, last_head + 1, limit, strict=strict)
        end = body[-1] if body else last_head
        out.append(Entry(start=spec.start, end=end, anchor=spec.anchor, before=spec.before, after=spec.after,
                         body=tuple(body), group=spec.group, pieces={i: parts_of(i) for i in heads}))
    return out


def _heads_promotions(lines: Sequence[Line], anchors: list[int], position: int, parts_of) -> bool:
    """A dated employer line whose next line is another anchor naming a job."""
    anchor = anchors[position]
    own = parts_of(anchor).pieces
    if not own or any(title_score(piece) for piece in own):
        return False
    j = anchor + 1
    while j < len(lines) and (lines[j].blank or lines[j].place):
        j += 1
    if j >= len(lines) or not lines[j].ranges:
        return False
    return any(title_score(piece) for piece in parts_of(j).pieces)


def _undated(lines: Sequence[Line], claimed: set[int], parts_of) -> list[Entry]:
    out: list[Entry] = []
    i = 0
    while i < len(lines):
        run: list[int] = []
        j = i
        while j < len(lines) and len(run) < 2:
            line = lines[j]
            if j in claimed or line.blank or line.bullet or line.ranges or line.label or not line.head or line.place:
                break
            run.append(j)
            j += 1
        follows = j < len(lines) and lines[j].bullet
        previous = lines[i - 1] if i > 0 else None
        wrapped = previous is not None and previous.bullet and run and continues(previous.text, lines[run[0]])
        if run and follows and not wrapped and any(names_a_role(lines[r].text) for r in run):
            out.append(Entry(start=run[0], end=run[-1], anchor=None, before=tuple(run), after=(), body=()))
            for r in run:
                parts_of(r)
            i = j
            continue
        i += 1
    return out


def _body(lines: Sequence[Line], start: int, limit: int, *, strict: bool) -> list[int]:
    body: list[int] = []
    previous: Optional[Line] = None
    for index in range(start, limit):
        line = lines[index]
        if line.blank:
            previous = None
            continue
        if strict:
            keep = line.bullet or line.indented or line.sentence or (
                previous is not None and continues(previous.text, line))
            if not keep:
                break
        body.append(index)
        previous = line
    return body


def items(lines: Sequence[Line], body: Sequence[int]) -> tuple[str, ...]:
    """Bullet texts of an entry body: soft-wrapped lines joined to the item they continue."""
    out: list[str] = []
    previous_index = -2
    for index in body:
        line = lines[index]
        adjacent = index == previous_index + 1
        if out and adjacent and not line.bullet and continues(out[-1], line):
            out[-1] = f"{out[-1]} {line.text}".strip()
        else:
            out.append(line.text)
        previous_index = index
    return tuple(" ".join(item.split()) for item in out if item.strip())
