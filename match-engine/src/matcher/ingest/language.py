"""Language detection without a model: stop-word profiles plus script detection (DECISIONS D-70).

Each line gets a language, or none:

- When letters of a non-Latin script are at least 30 % of the line's letters, the script names the
  language: Arabic ar, Hebrew he, Cyrillic ru, Greek el, Han zh (ja when the line has kana),
  Hangul ko, Devanagari hi, Thai th, and so on. Script to language is an approximation
  (Cyrillic could be uk or bg); ingest only needs to know the language is not supported.
- Otherwise the line's words are counted against the profiles of en, fr, es, de, pt, it and nl
  (function words plus distinctive résumé vocabulary such as section headings). The strictly
  highest count wins; no hit or a tie gives no language.

The document language is the language of the most lines (a tie goes to a supported language,
then to alphabetical order). `language_shares` is each language's share of the lines that have
one. Lines with no signal ("Python, SQL, Docker") are left out of both.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from typing import Optional

SUPPORTED_LANGUAGES = ("en", "fr")

SCRIPT_SHARE = 0.30  # share of a line's letters a non-Latin script needs to name the line

_WORD = re.compile(r"[^\W\d_]+")
# E-mail addresses, URLs and dotted names carry no language ("example.com" is not Portuguese "com").
_NOT_WORDS = re.compile(r"\S*(?:@|://|www\.)\S*|\S+\.[^\W\d_]\S*")

_PROFILES: dict[str, frozenset[str]] = {
    "en": frozenset("""
        a an the and or but of to in for with on at by from as into over under about across
        within without between through during after before per via is are was were be been
        being am this that these those it its my our your their his her he she we they i me us
        them which who whose what when where while than then also not no all any each both more
        most other such only own same so too very can could will would should may might must
        do does did have has had having using used including
        experience education skills projects summary certified responsibilities achievements
        present references objective employment university degree languages
        built led managed developed designed created implemented improved increased reduced
        delivered trained wrote
    """.split()),
    "fr": frozenset("""
        le la les un une des du de d l et ou à au aux en pour par avec sur dans chez est sont
        été ce cette ces qui que qu ne pas plus mon ma mes notre nos votre vos leur leurs son sa
        ses il elle nous vous ils elles être avoir depuis entre lors ainsi également auprès
        expérience expériences professionnelle professionnelles formation formations compétences
        langues projets profil sommaire diplôme stage poste aujourd présent actuellement
        loisirs intérêts centres données équipe gestion ingénieur développement entreprise
        université baccalauréat maîtrise ans mois janv févr avr juil sept déc juin
        français anglais
    """.split()),
    "es": frozenset("""
        el la los las un una unos unas de del y o en con por para que se su sus al es son fue
        como sobre entre desde hasta mi mis nuestro nuestra este esta estos estas también más
        muy sin
        experiencia educación formación habilidades competencias idiomas proyectos laboral
        profesional actualidad presente perfil desarrollo empresa años gestión equipo datos
        universidad licenciatura ingeniero ingeniera ene dic español inglés
    """.split()),
    "de": frozenset("""
        der die das den dem des ein eine einen einem einer und oder in im mit von vom zu zur
        zum für auf bei als ist sind war wurde werden nicht auch aus an am nach über durch
        sowie unter bis seit
        berufserfahrung erfahrung ausbildung kenntnisse fähigkeiten sprachen projekte studium
        heute entwicklung verantwortlich mitarbeiter unternehmen daten universität gmbh
        jahren deutsch englisch
    """.split()),
    "pt": frozenset("""
        o a os as um uma uns umas de do da dos das e em no na nos nas com por para que se seu
        sua seus suas é são foi como ao aos à pelo pela pelos pelas mais também entre sobre
        experiência educação formação habilidades competências idiomas projetos profissional
        atual desenvolvimento empresa anos gestão equipe dados universidade responsável
    """.split()),
    "it": frozenset("""
        il lo la i gli le un uno una di del dello della dei degli delle e ed o in nel nello
        nella nei negli con per su da dal dalla che è sono come anche più al allo alla ai agli
        l
        esperienza istruzione formazione competenze lingue progetti professionale sviluppo
        azienda anni gestione dati attuale università responsabile
    """.split()),
    "nl": frozenset("""
        de het een en of in op met voor van aan bij uit als is zijn was werd dat die deze dit
        ook niet naar door over tot om
        werkervaring ervaring opleiding vaardigheden talen projecten heden ontwikkeling bedrijf
        jaar gegevens verantwoordelijk
    """.split()),
}

# (first code point, last code point, script language). "latin" is the stop-word branch.
_RANGES = sorted([
    (0x0041, 0x005A, "latin"), (0x0061, 0x007A, "latin"), (0x00C0, 0x024F, "latin"),
    (0x1E00, 0x1EFF, "latin"),
    (0x0370, 0x03FF, "el"), (0x1F00, 0x1FFF, "el"),
    (0x0400, 0x052F, "ru"),
    (0x0530, 0x058F, "hy"),
    (0x0590, 0x05FF, "he"), (0xFB1D, 0xFB4F, "he"),
    (0x0600, 0x06FF, "ar"), (0x0750, 0x077F, "ar"), (0x08A0, 0x08FF, "ar"), (0xFB50, 0xFDFF, "ar"),
    (0xFE70, 0xFEFF, "ar"),
    (0x0900, 0x097F, "hi"), (0x0980, 0x09FF, "bn"), (0x0A00, 0x0A7F, "pa"), (0x0A80, 0x0AFF, "gu"),
    (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"), (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"),
    (0x0D80, 0x0DFF, "si"),
    (0x0E00, 0x0E7F, "th"), (0x0E80, 0x0EFF, "lo"), (0x1000, 0x109F, "my"), (0x10A0, 0x10FF, "ka"),
    (0x1100, 0x11FF, "ko"), (0x1200, 0x137F, "am"), (0x1780, 0x17FF, "km"),
    (0x3040, 0x309F, "kana"), (0x30A0, 0x30FF, "kana"), (0x31F0, 0x31FF, "kana"),
    (0x3130, 0x318F, "ko"), (0x3400, 0x4DBF, "zh"), (0x4E00, 0x9FFF, "zh"), (0xAC00, 0xD7AF, "ko"),
    (0xF900, 0xFAFF, "zh"), (0x20000, 0x2FA1F, "zh"),
])
_STARTS = [start for start, _, _ in _RANGES]


def _script(char: str) -> Optional[str]:
    code = ord(char)
    i = bisect.bisect_right(_STARTS, code) - 1
    if i >= 0 and code <= _RANGES[i][1]:
        return _RANGES[i][2]
    return None


def detect_line_language(line: str) -> Optional[str]:
    """The language of one line, or None when the line carries no clear signal."""
    line = _NOT_WORDS.sub(" ", line)
    scripts: Counter[str] = Counter()
    letters = 0
    for char in line:
        if char.isalpha():
            letters += 1
            script = _script(char)
            if script is not None:
                scripts[script] += 1
    if not letters:
        return None
    if scripts["kana"]:  # Japanese writes kanji (Han) next to kana
        scripts["ja"] += scripts.pop("kana") + scripts.pop("zh", 0)
    non_latin = {code: n for code, n in scripts.items() if code != "latin" and n > 0}
    if non_latin and sum(non_latin.values()) >= SCRIPT_SHARE * letters:
        return max(sorted(non_latin), key=lambda code: non_latin[code])
    words = _WORD.findall(line.lower())
    hits = {code: sum(1 for w in words if w in profile) for code, profile in _PROFILES.items()}
    best = max(hits.values(), default=0)
    if best == 0:
        return None
    winners = [code for code, n in hits.items() if n == best]
    return winners[0] if len(winners) == 1 else None


def _line_counts(text: str) -> Counter[str]:
    return Counter(code for line in text.splitlines() if (code := detect_line_language(line)))


def language_shares(text: str) -> dict[str, float]:
    """Share of the lines that have a language, per language, keys in alphabetical order."""
    counts = _line_counts(text)
    total = sum(counts.values())
    return {code: counts[code] / total for code in sorted(counts)} if total else {}


def detect_language(text: str) -> tuple[Optional[str], float]:
    """(ISO 639-1 code of the majority of lines, its share), or (None, 0.0) with no signal."""
    counts = _line_counts(text)
    total = sum(counts.values())
    if not total:
        return None, 0.0
    best = min(counts, key=lambda code: (-counts[code], code not in SUPPORTED_LANGUAGES, code))
    return best, counts[best] / total
