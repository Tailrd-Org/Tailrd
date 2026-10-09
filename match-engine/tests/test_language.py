"""Language detection: stop-word profiles plus script detection, no model (DECISIONS D-70)."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from matcher.ingest import SUPPORTED_LANGUAGES, detect_language
from matcher.ingest.language import detect_line_language, language_shares

LATIN = {
    "en": "Built the reporting pipeline for the finance team and trained two analysts in the use of the new tools.",
    "fr": "Responsable de la conception des tableaux de bord pour l'équipe des finances et de la formation des "
          "analystes.",
    "es": "Responsable del diseño de los paneles para el equipo de finanzas y de la formación de los analistas.",
    "de": "Verantwortlich für die Entwicklung der Berichte und die Schulung der Mitarbeiter in der "
          "Finanzabteilung.",
    "pt": "Responsável pelo desenvolvimento dos relatórios para a equipe de finanças e pela formação dos "
          "analistas.",
    "it": "Responsabile dello sviluppo dei report per il team finanziario e della formazione degli analisti.",
    "nl": "Verantwoordelijk voor de ontwikkeling van de rapporten voor het financiële team en de opleiding van "
          "analisten.",
}

SCRIPTS = {
    "ar": "تطوير تطبيقات الويب لفريق المالية",
    "he": "פיתוח דוחות עבור צוות הכספים",
    "ru": "Разработка отчётов для финансового отдела",
    "el": "Ανάπτυξη αναφορών για την ομάδα οικονομικών",
    "zh": "负责财务团队的报表开发",
    "ja": "財務チームのためのレポートを開発しました",
    "ko": "재무팀을 위한 보고서를 개발했습니다",
    "hi": "वित्त टीम के लिए रिपोर्ट विकसित की",
    "th": "พัฒนารายงานสำหรับทีมการเงิน",
}


def test_supported_languages() -> None:
    assert SUPPORTED_LANGUAGES == ("en", "fr")


@pytest.mark.parametrize("code", sorted(LATIN))
def test_stop_word_profiles(code: str) -> None:
    assert detect_language(LATIN[code]) == (code, 1.0)
    assert detect_line_language(LATIN[code]) == code


@pytest.mark.parametrize("code", sorted(SCRIPTS))
def test_scripts(code: str) -> None:
    assert detect_language(SCRIPTS[code]) == (code, 1.0)


@pytest.mark.parametrize("text", ["", "   ", "Python, SQL, Docker", "2019 - 2021", "!!! ???", "en"])
def test_undetermined(text: str) -> None:
    assert detect_language(text) == (None, 0.0)
    assert language_shares(text) == {}


def test_document_language_is_the_majority_of_lines() -> None:
    text = "\n".join([
        "Expérience professionnelle",
        "Responsable de la conception des tableaux de bord",
        "Formation en statistique à l'Université",
        "Built the reporting pipeline for the finance team",
        "Python, SQL, Tableau",  # no language signal: not counted
    ])
    assert language_shares(text) == {"en": 0.25, "fr": 0.75}
    assert detect_language(text) == ("fr", 0.75)


def test_resume_section_words_carry_signal() -> None:
    assert detect_line_language("EXPERIENCE") == "en"
    assert detect_line_language("EXPÉRIENCE PROFESSIONNELLE") == "fr"
    assert detect_line_language("BERUFSERFAHRUNG") == "de"
    assert detect_line_language("EXPERIENCIA PROFESIONAL") == "es"


def test_addresses_and_urls_carry_no_language() -> None:
    # "example.com" once made a line Portuguese ("com" = "with").
    assert detect_line_language("Alex Example | alex.example@example.com") is None
    assert detect_line_language("Portfolio: https://example.com/de/la") is None
    assert detect_line_language("www.example.com/en") is None


def test_a_tie_between_languages_is_undetermined() -> None:
    assert detect_line_language("in") is None  # en, de, it and nl all use "in"


def test_scripts_beat_latin_noise_on_a_line() -> None:
    assert detect_line_language("מהנדס תוכנה at Example Corp") == "he"
    assert detect_line_language("Python and SQL with " + "日本語") == "en"


@settings(max_examples=300)
@given(st.text())
def test_never_raises_and_shares_are_a_distribution(text: str) -> None:
    code, share = detect_language(text)
    shares = language_shares(text)
    assert 0.0 <= share <= 1.0
    if code is None:
        assert share == 0.0 and shares == {}
    else:
        assert len(code) == 2 and code.islower() and code in shares
        assert math.isclose(sum(shares.values()), 1.0, abs_tol=1e-9)
        assert list(shares) == sorted(shares)
