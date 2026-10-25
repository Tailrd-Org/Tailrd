"""scripts/ats_detect.py: the ATS behind a careers URL, from the URL alone or
from the page's links, iframes and script tags, as a registry seed entry.

No network: pages are the offline fixtures in fixtures/ingest/ats_detect, and
the one fetch the command makes is replaced."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from backend.services.ats_scraper import workday_public_base


@pytest.fixture(autouse=True)
def setup_db():
    """No database here: this module skips the root conftest's per-test
    create_all/drop_all."""
    yield


REPO = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ingest" / "ats_detect"

# What this checkout can crawl in these tests, so the answers do not move when
# an adapter lands.
CRAWLABLE = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "workable", "recruitee",
             "teamtailor", "bamboohr"}


@pytest.fixture(scope="module")
def ats():
    spec = importlib.util.spec_from_file_location("ats_detect_under_test", REPO / "scripts" / "ats_detect.py")
    module = importlib.util.module_from_spec(spec)
    # The script's dataclasses look their module up by name while they are built.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def page(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# ─── From the URL alone ──────────────────────────────────────────────────────

@pytest.mark.parametrize("url, platform, slug, careers_url", [
    ("https://jobs.lever.co/acme-labs/0f2a1c3e-1111-4222-8333-444455556666/apply",
     "lever", "acme-labs", "https://jobs.lever.co/acme-labs"),
    ("https://jobs.eu.lever.co/acme-labs", "lever", "acme-labs", "https://jobs.eu.lever.co/acme-labs"),
    ("https://boards.greenhouse.io/acmerobotics/jobs/4285367",
     "greenhouse", "acmerobotics", "https://boards.greenhouse.io/acmerobotics"),
    ("https://job-boards.greenhouse.io/acmerobotics", "greenhouse", "acmerobotics",
     "https://boards.greenhouse.io/acmerobotics"),
    ("https://boards.greenhouse.io/embed/job_board?for=acmerobotics&b=https%3A%2F%2Facme.example",
     "greenhouse", "acmerobotics", "https://boards.greenhouse.io/acmerobotics"),
    ("https://boards.greenhouse.io/embed/job_app?for=acmerobotics&token=4285367",
     "greenhouse", "acmerobotics", "https://boards.greenhouse.io/acmerobotics"),
    ("https://boards-api.greenhouse.io/v1/boards/acmerobotics/jobs",
     "greenhouse", "acmerobotics", "https://boards.greenhouse.io/acmerobotics"),
    ("https://jobs.ashbyhq.com/northwind/5d1c2b7e-0000-4111-9222-333344445555",
     "ashby", "northwind", "https://jobs.ashbyhq.com/northwind"),
    ("https://api.ashbyhq.com/posting-api/job-board/northwind?includeCompensation=true",
     "ashby", "northwind", "https://jobs.ashbyhq.com/northwind"),
    # SmartRecruiters company ids are case sensitive.
    ("https://jobs.smartrecruiters.com/Accenture1/743999999-analyst",
     "smartrecruiters", "Accenture1", "https://jobs.smartrecruiters.com/Accenture1"),
    ("https://api.smartrecruiters.com/v1/companies/Accenture1/postings",
     "smartrecruiters", "Accenture1", "https://jobs.smartrecruiters.com/Accenture1"),
    ("https://apply.workable.com/norfolk-county/j/ABC123DEF4/",
     "workable", "norfolk-county", "https://apply.workable.com/norfolk-county/"),
    ("https://apply.workable.com/api/v1/widget/accounts/norfolk-county?details=true",
     "workable", "norfolk-county", "https://apply.workable.com/norfolk-county/"),
    ("https://ottawasafetycouncil.recruitee.com/o/software-intern",
     "recruitee", "ottawasafetycouncil", "https://ottawasafetycouncil.recruitee.com"),
    ("https://cityofhamilton.bamboohr.com/careers/123",
     "bamboohr", "cityofhamilton", "https://cityofhamilton.bamboohr.com/careers"),
    ("https://owlco.teamtailor.com/jobs/123-welder", "teamtailor", "owlco", "https://owlco.teamtailor.com"),
    ("https://jobs.jobvite.com/ornge", "jobvite", "ornge", "https://jobs.jobvite.com/ornge"),
    ("https://jobs.jobvite.com/careers/ornge/jobs", "jobvite", "ornge", "https://jobs.jobvite.com/ornge"),
])
def test_a_board_or_posting_link_names_its_board(ats, url, platform, slug, careers_url):
    found = ats.detect(url, supported=CRAWLABLE)

    assert (found.entry["ats_platform"], found.entry["board_slug"]) == (platform, slug)
    assert found.entry["careers_url"] == careers_url
    assert found.hit.source == "url" and found.hit.board


def test_the_north_american_teamtailor_host_goes_in_config(ats):
    entry = ats.detect("https://owlco.na.teamtailor.com/jobs/123-welder", supported=CRAWLABLE).entry

    assert entry["board_slug"] == "owlco" and entry["config"] == {"host": "owlco.na.teamtailor.com"}
    assert "config" not in ats.detect("https://owlco.teamtailor.com/", supported=CRAWLABLE).entry


@pytest.mark.parametrize("url, template, careers_url", [
    ("https://acmebank.wd3.myworkdayjobs.com/en-US/AcmeBankCareers/job/Toronto-ON/Analyst_R-1001",
     "https://acmebank.wd3.myworkdayjobs.com/wday/cxs/acmebank/AcmeBankCareers",
     "https://acmebank.wd3.myworkdayjobs.com/AcmeBankCareers"),
    ("https://acmebank.wd3.myworkdayjobs.com/AcmeBankCareers",
     "https://acmebank.wd3.myworkdayjobs.com/wday/cxs/acmebank/AcmeBankCareers",
     "https://acmebank.wd3.myworkdayjobs.com/AcmeBankCareers"),
    ("https://wd1.myworkdaysite.com/en-US/recruiting/upenn/careers-at-penn/job/HUP/Clinician_JR1",
     "https://wd1.myworkdaysite.com/wday/cxs/upenn/careers-at-penn",
     "https://wd1.myworkdaysite.com/recruiting/upenn/careers-at-penn"),
])
def test_a_workday_link_gives_the_cxs_template_and_the_public_base(ats, url, template, careers_url):
    entry = ats.detect(url, supported=CRAWLABLE).entry

    assert entry["workday_url_template"] == template and entry["careers_url"] == careers_url
    assert entry["board_slug"] == template.split("/wday/cxs/")[1].split("/")[0]
    assert entry["enabled"] is True
    # The same derivation the crawler uses for the posting URLs it stores.
    assert workday_public_base(template) == careers_url


def test_a_workday_tenant_without_a_site_is_found_but_not_enabled(ats):
    for url in ("https://acmebank.wd3.myworkdayjobs.com/", "https://acmebank.wd3.myworkdayjobs.com/en-US"):
        found = ats.detect(url, supported=CRAWLABLE)

        assert found.entry["ats_platform"] == "workday" and found.entry["board_slug"] == "acmebank"
        assert "workday_url_template" not in found.entry and found.entry["enabled"] is False
        assert any("site name" in note for note in found.notes)


def test_icims_and_oracle_boards_carry_the_config_their_connectors_read(ats):
    icims = ats.detect("https://careers-guelph.icims.com/jobs/search?ss=1", supported={"icims"}).entry
    assert icims["board_slug"] == "careers-guelph" and icims["config"] == {"host": "careers-guelph.icims.com"}
    assert icims["careers_url"] == "https://careers-guelph.icims.com/jobs/search" and icims["enabled"] is True

    oracle = ats.detect(
        "https://fa-eyjj-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/123",
        supported={"oracle_hcm"}).entry
    assert oracle["config"] == {"host": "fa-eyjj-saasfaprod1.fa.ocs.oraclecloud.com", "site": "CX_1"}
    assert oracle["careers_url"] == ("https://fa-eyjj-saasfaprod1.fa.ocs.oraclecloud.com"
                                     "/hcmUI/CandidateExperience/en/sites/CX_1")


def test_a_platform_without_a_connector_gets_a_disabled_entry_with_a_reason(ats):
    found = ats.detect("https://heartandstroke.applytojob.com/apply", supported=CRAWLABLE)

    assert found.supported is False
    assert found.entry["ats_platform"] == "jazzhr" and found.entry["board_slug"] == "heartandstroke"
    assert found.entry["enabled"] is False
    assert "no connector for jazzhr" in found.entry["disabled_reason"]


def test_the_same_url_is_enabled_once_the_platform_is_crawlable(ats):
    url = "https://careers-guelph.icims.com/jobs/search"

    assert ats.detect(url, supported=CRAWLABLE).entry["enabled"] is False
    assert ats.detect(url, supported=CRAWLABLE | {"icims"}).entry["enabled"] is True


@pytest.mark.parametrize("url", [
    "https://www.recruitee.com/pricing",           # the vendor's own marketing site
    "https://boards.greenhouse.io/embed/job_app?token=4285367",
    "https://jobs.smartrecruiters.com/oneclick-ui/company/Accenture1/publication/abc",
])
def test_a_vendors_own_page_is_not_an_employers_board(ats, url):
    found = ats.detect(url, supported=CRAWLABLE)

    assert found is None or not found.hit.board


@pytest.mark.parametrize("url", [
    "https://jobs.lever.co/", "https://boards.greenhouse.io/", "https://jobs.lever.co/%E4%B8%AD%E6%96%87",
])
def test_a_vendors_own_address_leaves_the_slug_and_name_empty(ats, url):
    # "lever" would look like a slug and be wrong; an empty one is skipped by
    # the registry loader and cannot be pasted in unseen.
    found = ats.detect(url, supported=CRAWLABLE)

    assert found.entry["board_slug"] == "" and found.entry["company_name"] == ""
    assert found.entry["enabled"] is False and "board name was not found" in found.entry["disabled_reason"]
    assert any("board_slug is empty" in note for note in found.notes)
    assert any("company_name is empty" in note for note in found.notes)


def test_a_myworkdaysite_address_without_a_site_still_names_the_tenant(ats):
    found = ats.detect("https://wd5.myworkdaysite.com/en-US/recruiting/acmecare", supported=CRAWLABLE)

    assert found.entry["ats_platform"] == "workday" and found.entry["board_slug"] == "acmecare"
    assert "workday_url_template" not in found.entry and found.entry["enabled"] is False
    assert any("site name" in note for note in found.notes)


@pytest.mark.parametrize("url, platform", [
    ("https://www.acme.example/careers/platform-engineer?gh_jid=4285367", "greenhouse"),
    ("https://www.acme.example/jobs?ashby_jid=5d1c2b7e-0000-4111-9222-333344445555", "ashby"),
])
def test_a_job_id_in_the_query_names_the_vendor_but_no_board(ats, url, platform):
    found = ats.detect(url, supported=CRAWLABLE)

    assert found.entry["ats_platform"] == platform and not found.hit.board
    assert any("board_slug" in note and "guess" in note for note in found.notes)


@pytest.mark.parametrize("url", [
    "https://www.acme.com/careers", "https://example.org/", "https://www.linkedin.com/company/acme/jobs",
    "https://www.indeed.com/cmp/Acme", "not a url", "",
])
def test_an_employers_own_url_names_no_ats(ats, url):
    assert ats.detect(url, supported=CRAWLABLE) is None


# ─── From the page ───────────────────────────────────────────────────────────

def detect_page(ats, fixture, url="https://www.acme.example/careers", **kwargs):
    return ats.detect(url, page(fixture), supported=CRAWLABLE, **kwargs)


def test_a_greenhouse_embed_script_names_the_board(ats):
    found = detect_page(ats, "greenhouse_embed.html")

    assert found.entry == {
        "company_name": "Acme Robotics Inc.",       # og:site_name beats the title
        "ats_platform": "greenhouse", "board_slug": "acmerobotics",
        "careers_url": "https://boards.greenhouse.io/acmerobotics", "enabled": True}
    assert found.hit.source == "link" and found.others == []


def test_lever_posting_links_name_the_board(ats):
    found = detect_page(ats, "lever_postings.html")

    assert found.entry["ats_platform"] == "lever" and found.entry["board_slug"] == "acme-labs"
    assert found.entry["company_name"] == "Acme Labs"  # "Acme Labs - Careers" without the suffix
    assert found.others == []


def test_a_protocol_relative_script_and_escaped_json_are_read(ats):
    found = detect_page(ats, "ashby_embed.html")

    assert (found.entry["ats_platform"], found.entry["board_slug"]) == ("ashby", "northwind")
    # The title "Jobs" names no employer: the name comes from the board.
    assert found.entry["company_name"] == "Northwind"


def test_a_smartrecruiters_iframe_names_the_board(ats):
    found = detect_page(ats, "smartrecruiters_iframe.html")

    assert (found.entry["ats_platform"], found.entry["board_slug"]) == ("smartrecruiters", "AcmeInc1")


def test_workday_links_on_a_page_give_the_template(ats):
    found = detect_page(ats, "workday_site_links.html")

    assert found.entry["board_slug"] == "acmebank"
    assert found.entry["workday_url_template"] == "https://acmebank.wd3.myworkdayjobs.com/wday/cxs/acmebank/AcmeBankCareers"
    assert found.entry["company_name"] == "Acme Bank"


def test_myworkdaysite_links_on_a_page_give_the_template(ats):
    found = detect_page(ats, "myworkdaysite_links.html")

    assert found.entry["board_slug"] == "lakesidehealth"
    assert found.entry["workday_url_template"] == "https://wd5.myworkdaysite.com/wday/cxs/lakesidehealth/LakesideCareers"
    assert found.entry["careers_url"] == "https://wd5.myworkdaysite.com/recruiting/lakesidehealth/LakesideCareers"
    assert found.entry["company_name"] == "Lakeside Health"


def test_the_teamtailor_script_marks_a_custom_domain_career_site(ats):
    found = detect_page(ats, "teamtailor_widget.html", url="https://careers.usgboral.example/jobs")

    assert found.hit.source == "marker" and found.hit.evidence == "teamtailor-cdn.com"
    assert found.entry["ats_platform"] == "teamtailor" and found.entry["board_slug"] == "usgboral"
    assert found.entry["config"] == {"host": "careers.usgboral.example"}
    assert found.entry["company_name"] == "USG Boral"
    assert any("guess" in note and "board_slug" in note for note in found.notes)


def test_a_teamtailor_link_on_a_custom_domain_names_the_host_not_the_posting(ats):
    found = ats.detect("https://careers.blueorange.digital/jobs/123-welder", supported=CRAWLABLE)

    assert found.entry["ats_platform"] == "teamtailor" and not found.hit.board
    # "123-welder" is a posting, not the employer: the slug comes from the host.
    assert found.entry["board_slug"] == "blueorange"
    assert found.entry["config"] == {"host": "careers.blueorange.digital"}


def test_a_script_marker_of_a_vendor_without_a_connector_gives_a_disabled_entry(ats):
    found = detect_page(ats, "phenom_marker.html", url="https://careers.airexample.example/search")

    assert found.hit.source == "marker"
    assert found.entry["ats_platform"] == "phenom" and found.entry["board_slug"] == "airexample"
    assert found.entry["company_name"] == "Air Example" and found.entry["enabled"] is False
    assert "no connector for phenom" in found.entry["disabled_reason"]


def test_the_board_the_page_mentions_most_wins_and_the_other_is_reported(ats):
    found = detect_page(ats, "two_boards.html")

    assert (found.entry["ats_platform"], found.entry["board_slug"]) == ("lever", "harbourgroup")
    [(other, count)] = found.others
    assert (other.platform, other.slug, count) == ("greenhouse", "harbourtech", 1)


def test_a_page_with_no_ats_names_none(ats):
    assert detect_page(ats, "no_ats.html") is None


def test_the_url_beats_the_page_and_the_final_url_follows_a_redirect(ats):
    # The URL names Lever: a Greenhouse embed on the page does not matter.
    found = ats.detect("https://jobs.lever.co/acme-labs", page("greenhouse_embed.html"), supported=CRAWLABLE)
    assert found.entry["ats_platform"] == "lever"

    # careers.acme.example redirected to the Workday site: that is the answer.
    found = ats.detect("https://careers.acme.example/", "<html></html>", supported=CRAWLABLE,
                       final_url="https://acmebank.wd3.myworkdayjobs.com/en-US/AcmeBankCareers")
    assert found.entry["ats_platform"] == "workday" and found.entry["board_slug"] == "acmebank"


def test_the_page_names_the_board_behind_a_vendor_only_url(ats):
    url = "https://www.acmerobotics.example/careers/platform-engineer?gh_jid=4285367"
    found = ats.detect(url, page("greenhouse_embed.html"), supported=CRAWLABLE)

    assert found.entry["board_slug"] == "acmerobotics" and found.hit.board and found.hit.source == "link"
    assert [(other.platform, other.slug) for other, _count in found.others] == [("greenhouse", "")]


def test_a_vendor_only_url_stands_when_the_page_shows_nothing_better(ats):
    url = "https://www.acmerobotics.example/careers/platform-engineer?gh_jid=4285367"
    found = ats.detect(url, page("no_ats.html"), supported=CRAWLABLE)

    assert found.hit.source == "url" and found.entry["ats_platform"] == "greenhouse"
    assert found.entry["board_slug"] == "acmerobotics"  # guessed from the employer's own host


def test_a_name_given_on_the_command_line_wins(ats):
    found = detect_page(ats, "greenhouse_embed.html", name="Acme Robotics")

    assert found.entry["company_name"] == "Acme Robotics"
    assert not any("company_name" in note for note in found.notes)


def test_the_entry_leaves_hq_country_and_sector_to_a_person(ats):
    found = detect_page(ats, "lever_postings.html")

    assert "hq_country" not in found.entry and "sector" not in found.entry
    assert any("hq_country" in note for note in found.notes)


def test_a_huge_page_is_read_to_its_limit(ats):
    body = "<p>filler</p>" * 400_000 + '<a href="https://jobs.lever.co/late-board/abc">x</a>'

    assert len(body) > ats.MAX_PAGE_CHARS
    assert ats.detect("https://www.acme.example/careers", body, supported=CRAWLABLE) is None


# ─── The command ─────────────────────────────────────────────────────────────

def run(ats, capsys, *argv):
    code = ats.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_board_url_is_answered_without_fetching(ats, capsys, monkeypatch):
    def refuse(url):
        raise AssertionError("a board URL must not be fetched")

    monkeypatch.setattr(ats, "fetch_page", refuse)
    code, out, err = run(ats, capsys, "https://jobs.lever.co/acme-labs/0f2a1c3e-1111-4222-8333-444455556666")

    assert code == 0
    entry = json.loads(out)  # stdout is the entry and nothing else
    assert entry["ats_platform"] == "lever" and entry["board_slug"] == "acme-labs"
    assert "found Lever (lever) from url" in err and "hq_country" in err


def test_an_employers_url_is_fetched_once_and_its_page_read(ats, capsys, monkeypatch):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return url, page("greenhouse_embed.html")

    monkeypatch.setattr(ats, "fetch_page", fetch)
    code, out, _err = run(ats, capsys, "https://www.acmerobotics.example/careers")

    assert code == 0 and fetched == ["https://www.acmerobotics.example/careers"]
    assert json.loads(out)["board_slug"] == "acmerobotics"


def test_a_vendor_only_url_is_fetched_for_its_board(ats, capsys, monkeypatch):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return url, page("greenhouse_embed.html")

    monkeypatch.setattr(ats, "fetch_page", fetch)
    url = "https://www.acmerobotics.example/careers/platform-engineer?gh_jid=4285367"
    code, out, err = run(ats, capsys, url)

    assert code == 0 and fetched == [url]
    assert json.loads(out)["board_slug"] == "acmerobotics" and "from link" in err


def test_an_unreadable_page_keeps_the_answer_the_url_gave(ats, capsys, monkeypatch):
    def blocked(url):
        raise ats.FetchError("robots.txt does not allow TailrdBot to read " + url)

    monkeypatch.setattr(ats, "fetch_page", blocked)
    code, out, err = run(ats, capsys, "https://www.acme.example/jobs?ashby_jid=5d1c2b7e-0000-4111-9222-333344445555")

    assert code == 0 and json.loads(out)["ats_platform"] == "ashby"
    assert "cannot read the page" in err


def test_a_redirect_to_an_ats_is_followed(ats, capsys, monkeypatch):
    monkeypatch.setattr(ats, "fetch_page", lambda url: (
        "https://acmebank.wd3.myworkdayjobs.com/en-US/AcmeBankCareers", "<html></html>"))
    code, out, _err = run(ats, capsys, "https://careers.acmebank.example/")

    assert code == 0 and json.loads(out)["workday_url_template"].endswith("/wday/cxs/acmebank/AcmeBankCareers")


def test_a_saved_page_is_read_instead_of_fetching(ats, capsys, monkeypatch):
    monkeypatch.setattr(ats, "fetch_page", lambda url: pytest.fail("a saved page must not be fetched"))
    code, out, err = run(ats, capsys, "https://www.acme.example/careers",
                         "--html", str(FIXTURES / "lever_postings.html"), "--name", "Acme Labs Inc")

    assert code == 0 and json.loads(out)["company_name"] == "Acme Labs Inc"
    assert "from link" in err


def test_no_fetch_reads_the_url_only(ats, capsys, monkeypatch):
    monkeypatch.setattr(ats, "fetch_page", lambda url: pytest.fail("--no-fetch must not fetch"))
    code, out, err = run(ats, capsys, "https://www.acme.example/careers", "--no-fetch")

    assert (code, out) == (1, "") and "no ATS found" in err


def test_a_page_that_cannot_be_read_exits_2(ats, capsys, monkeypatch):
    def blocked(url):
        raise ats.FetchError("robots.txt does not allow TailrdBot to read " + url)

    monkeypatch.setattr(ats, "fetch_page", blocked)
    code, out, err = run(ats, capsys, "https://www.acme.example/careers")

    assert (code, out) == (2, "") and "cannot read the page" in err and "robots.txt" in err


def test_a_page_with_no_ats_exits_1(ats, capsys, monkeypatch):
    monkeypatch.setattr(ats, "fetch_page", lambda url: (url, page("no_ats.html")))
    code, out, err = run(ats, capsys, "https://www.plainco.example/careers")

    assert (code, out) == (1, "") and "no ATS found" in err


def test_the_second_board_is_named_on_stderr(ats, capsys, monkeypatch):
    monkeypatch.setattr(ats, "fetch_page", lambda url: (url, page("two_boards.html")))
    code, _out, err = run(ats, capsys, "https://www.harbour.example/careers")

    assert code == 0 and "also seen: greenhouse board harbourtech x1" in err


# ─── The vendor table is the extension's ─────────────────────────────────────

def _site_registry():
    """(id, tier, domains, pattern hosts) of every entry of siteRegistry.ts."""
    source = (REPO / "chrome-extension" / "src" / "content" / "siteRegistry.ts").read_text(encoding="utf-8")
    entries = []
    for match in re.finditer(r'\{\s*id:\s*"(\w+)",\s*label:\s*"[^"]*",\s*tier:\s*"(vendor|portal)",(.*?)\n  \},',
                             source, re.DOTALL):
        body = match.group(3)

        def strings(key):
            listed = re.search(rf"\b{key}:\s*\[(.*?)\]", body, re.DOTALL)
            return re.findall(r'"([^"]+)"', listed.group(1)) if listed else []

        hosts = []
        for pattern in strings("patterns"):
            host = re.match(r"\*://(?:\*\.)?([^/*]+)/", pattern)
            if host:
                hosts.append(host.group(1))
        entries.append((match.group(1), match.group(2), strings("domains"), hosts))
    return entries


# The extension's ids that the registry names differently, and the one vendor
# that is a job board rather than an ATS.
RENAMED = {"oraclecloud": "oracle_hcm", "ultipro": "ukg", "pinpointhq": "pinpoint"}
NOT_AN_ATS = {"welcometothejungle"}


def test_every_vendor_of_the_extension_is_in_the_table(ats):
    registry = _site_registry()
    vendors = [entry for entry in registry if entry[1] == "vendor" and entry[0] not in NOT_AN_ATS]
    assert len(vendors) >= 45  # the parse found the table

    platforms = {vendor.platform for vendor in ats.VENDORS}
    suffixes = {host.lower() for vendor in ats.VENDORS for host in vendor.hosts}
    for vendor_id, _tier, domains, pattern_hosts in vendors:
        assert RENAMED.get(vendor_id, vendor_id) in platforms, vendor_id
        for host in domains + pattern_hosts:
            host = host.lower()
            # The extension's host is the table's, or a host under one of its suffixes.
            assert any(host == suffix or host.endswith("." + suffix) for suffix in suffixes), (vendor_id, host)


def test_the_crawlable_platforms_are_the_scrapers(ats):
    crawlable = ats.crawlable_platforms()

    assert {"greenhouse", "lever", "ashby", "smartrecruiters", "workday"} <= crawlable
    assert {"workable", "recruitee", "teamtailor", "bamboohr"} <= crawlable
    assert "jazzhr" not in crawlable
