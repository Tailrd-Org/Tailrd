"""Builds tests/fixtures/raw_mini/: a tiny stand-in for data/raw/ (Task 2.2).

Same layout, file names and columns as the real downloads (see scripts/fetch_sources.py), with
a handful of rows chosen to exercise every taxonomy build rule without network access. The
committed files are this module's output; `tests/test_taxonomy_build.py` fails when they drift.
Regenerate from match-engine/ with:

    ./.venv/Scripts/python.exe tests/fixtures/build_raw_mini.py
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

RAW_MINI = Path(__file__).resolve().parent / "raw_mini"

_SKILL = "http://data.europa.eu/esco/skill/"
_SCHEME = "http://data.europa.eu/esco/concept-scheme/skills"

# ─── ESCO ──────────────────────────────────────────────────────────────────────────────────

# (uuid, skillType, reuseLevel, preferred en, alt en, hidden en, preferred fr, alt fr)
CONCEPTS = (
    ("00000000-0000-4000-8000-000000000001", "knowledge", "sector-specific", "Python (computer programming)",
     ["Python programming", "Py"], ["Python 2.7"], "Python (programmation informatique)", ["langage Python"]),
    ("00000000-0000-4000-8000-000000000002", "knowledge", "sector-specific", "JavaScript",
     ["JS", "ECMAScript programming"], [], "JavaScript", []),
    ("00000000-0000-4000-8000-000000000003", "knowledge", "sector-specific", "Ruby (computer programming)",
     ["Ruby", "Ruby language", "Matz"], [], "Ruby (programmation informatique)", []),
    ("00000000-0000-4000-8000-000000000004", "knowledge", "sector-specific", "Scratch (computer programming)",
     ["Scratch programming"], [], "Scratch (programmation informatique)", []),
    ("00000000-0000-4000-8000-000000000005", "knowledge", "sector-specific", "ML (computer programming)",
     [], [], "ML (programmation informatique)", []),
    ("00000000-0000-4000-8000-000000000006", "skill/competence", "transversal", "work in teams",
     ["teamwork", "team working", "collaborate with colleagues"], ["team player"], "travailler en équipe",
     ["travail d'équipe", "collaboration"]),
    ("a7c3f2e1-0000-4000-8000-0000000000a7", "skill/competence", "occupation-specific", "operate pumping equipment",
     ["run pumps"], [], "faire fonctionner des équipements de pompage", []),
    ("b8d4e5f6-0000-4000-8000-0000000000b8", "skill/competence", "occupation-specific", "operate pumping equipment",
     ["run oil pumps"], [], "utiliser des équipements de pompage", []),
    ("00000000-0000-4000-8000-000000000009", "knowledge", "cross-sector", "machine learning",
     ["ML", "statistical learning"], [], "apprentissage automatique", ["apprentissage machine"]),
    ("00000000-0000-4000-8000-000000000010", "skill/competence", "transversal", "speak French",
     ["communicate orally in French"], [], "parler français", []),
    ("00000000-0000-4000-8000-000000000011", "knowledge", "cross-sector", "mathematics",
     ["maths knowledge"], [], "mathématiques", []),
    ("00000000-0000-4000-8000-000000000012", "skill/competence", "cross-sector", "use spreadsheets software",
     ["work with spreadsheets"], ["Microsoft Office Excel", "Gnumeric"], "utiliser un tableur", []),
    ("00000000-0000-4000-8000-000000000013", "knowledge", "cross-sector", "computer programming",
     ["coding", "software programming", "web design"], [], "programmation informatique", []),
    ("00000000-0000-4000-8000-000000000014", "skill/competence", "cross-sector", "think critically",
     ["critical thinking", "synergy"], [], "faire preuve d'esprit critique", ["pensée critique"]),
    # A one-word French preferred label that is an ordinary French word, unlike the English one.
    ("00000000-0000-4000-8000-000000000015", "knowledge", "cross-sector", "morality",
     ["moral standards"], [], "morale", ["principes moraux"]),
    # A programming language ESCO files as knowledge under "computer programming".
    ("00000000-0000-4000-8000-000000000016", "knowledge", "sector-specific", "Common Lisp",
     ["CL programming"], [], "Common Lisp", []),
)

# (code, uuid or isced uri suffix, preferred en, preferred fr)
GROUPS = (
    ("K", _SKILL + "00000000-0000-4000-8000-0000000000f1", "knowledge", "savoirs"),
    ("S", _SKILL + "00000000-0000-4000-8000-0000000000f2", "skills", "aptitudes"),
    ("T", _SKILL + "00000000-0000-4000-8000-0000000000f3", "transversal skills and competences",
     "aptitudes et compétences transversales"),
    ("L", _SKILL + "00000000-0000-4000-8000-0000000000f4", "language skills and knowledge",
     "aptitudes et connaissances linguistiques"),
    ("06", "http://data.europa.eu/esco/isced-f/06", "information and communication technologies (icts)",
     "technologies de l'information et de la communication (TIC)"),
    ("0613", "http://data.europa.eu/esco/isced-f/0613", "software and applications development and analysis",
     "conception et analyse de logiciels et d'applications"),
    ("0541", "http://data.europa.eu/esco/isced-f/0541", "mathematics", "mathématiques"),
    ("T4", _SKILL + "00000000-0000-4000-8000-0000000000f5", "social and communication skills and competences",
     "aptitudes sociales et de communication"),
    ("T4.3", _SKILL + "00000000-0000-4000-8000-0000000000f6", "collaborating in teams and networks",
     "collaborer au sein d'équipes et de réseaux"),
    ("S5", _SKILL + "00000000-0000-4000-8000-0000000000f7", "working with computers", "travailler avec des ordinateurs"),
    ("S8", _SKILL + "00000000-0000-4000-8000-0000000000f8", "working with machinery and specialised equipment",
     "travailler avec des machines et des équipements spécialisés"),
    ("L1", _SKILL + "00000000-0000-4000-8000-0000000000f9", "languages", "langues"),
)

_GROUP_URI = {code: uri for code, uri, _, _ in GROUPS}


def _c(n: str) -> str:
    return _SKILL + "00000000-0000-4000-8000-" + n


# (concept uri, broader uri)
BROADER = (
    (_c("000000000001"), _c("000000000013")),  # Python -> computer programming (concept -> concept)
    (_c("000000000001"), _GROUP_URI["0613"]),
    (_c("000000000002"), _GROUP_URI["0613"]),
    (_c("000000000003"), _GROUP_URI["0613"]),
    (_c("000000000004"), _GROUP_URI["0613"]),
    (_c("000000000005"), _GROUP_URI["0613"]),
    (_c("000000000013"), _GROUP_URI["0613"]),
    (_c("000000000016"), _c("000000000013")),  # Common Lisp -> computer programming
    (_c("000000000009"), _GROUP_URI["06"]),
    (_c("000000000011"), _GROUP_URI["0541"]),
    (_c("000000000006"), _GROUP_URI["T4.3"]),
    (_c("000000000014"), _GROUP_URI["T"]),
    (_SKILL + "a7c3f2e1-0000-4000-8000-0000000000a7", _GROUP_URI["S8"]),
    (_SKILL + "b8d4e5f6-0000-4000-8000-0000000000b8", _GROUP_URI["S8"]),
    (_c("000000000010"), _GROUP_URI["L1"]),
    (_c("000000000012"), _GROUP_URI["S5"]),
    (_GROUP_URI["0613"], _GROUP_URI["06"]),
    (_GROUP_URI["06"], _GROUP_URI["K"]),
    (_GROUP_URI["0541"], _GROUP_URI["K"]),
    (_GROUP_URI["T4.3"], _GROUP_URI["T4"]),
    (_GROUP_URI["T4"], _GROUP_URI["T"]),
    (_GROUP_URI["S5"], _GROUP_URI["S"]),
    (_GROUP_URI["S8"], _GROUP_URI["S"]),
    (_GROUP_URI["L1"], _GROUP_URI["L"]),
)

TRANSVERSAL = ("000000000006", "000000000014")
LANGUAGE = ("000000000010",)

# ─── O*NET ─────────────────────────────────────────────────────────────────────────────────

TECHNOLOGY = (  # soc, example, commodity code, commodity title, hot, in demand
    ("15-1252.00", "Python", "43232405", "Object or component oriented development software", "Y", "Y"),
    ("15-1252.00", "Microsoft Excel", "43232110", "Spreadsheet software", "Y", "N"),
    ("15-1252.00", "Atlassian Confluence", "43232201", "Content workflow software", "Y", "N"),
    ("15-1252.00", "Amazon Web Services AWS software", "43233513", "Cloud-based data access and sharing software",
     "Y", "Y"),
    ("15-1252.00", "Structured query language SQL", "43232306", "Data base user interface and query software",
     "Y", "Y"),
    ("15-1252.00", "Google Analytics", "43232306", "Data base user interface and query software", "N", "N"),
    ("15-1252.00", "J", "43232405", "Object or component oriented development software", "N", "N"),
    ("15-1252.00", "Advanced Weather System AWS", "43232605", "Analytical or scientific software", "N", "N"),
    ("53-7051.00", "Forklift software", "43231508", "Inventory management software", "N", "N"),
    ("43-3021.00", "Billing software", "43231601", "Accounting software", "N", "N"),  # generic: no "Billing"
    ("27-1024.00", "Microsoft Paint", "43232102", "Graphics or photo imaging software", "N", "N"),
    ("15-2041.00", "Analyze", "43232605", "Analytical or scientific software", "N", "N"),  # a common word
    ("17-2141.00", "Autodesk AutoCAD", "43232604", "Computer aided design CAD software", "Y", "N"),
    ("17-2141.00", "Computer aided design CAD software", "43232604", "Computer aided design CAD software", "N", "N"),
    ("17-2141.00", "Microsoft Excel", "43232110", "Spreadsheet software", "Y", "N"),
    ("15-1299.00", "Microsoft Excel", "43232110", "Spreadsheet software", "Y", "N"),
    ("15-1299.00", "Python", "43232405", "Object or component oriented development software", "N", "N"),
    ("15-1299.00", "Atlassian Confluence", "43232201", "Content workflow software", "N", "Y"),
    ("29-2043.00", "Epic Systems", "43232610", "Medical software", "Y", "N"),
    # 17-2199.00 ("All Other") has no rows of its own; two detailed siblings share AutoCAD.
    ("17-2199.01", "Autodesk AutoCAD", "43232604", "Computer aided design CAD software", "Y", "N"),
    ("17-2199.02", "Autodesk AutoCAD", "43232604", "Computer aided design CAD software", "Y", "N"),
    ("17-2199.02", "Python", "43232405", "Object or component oriented development software", "Y", "N"),
)

TOOLS = (  # soc, example, commodity code, commodity title
    ("53-7051.00", "Forklifts", "24101622", "Forklifts"),
    ("15-1252.00", "Laptop computers", "43211503", "Notebook computers"),
    ("29-2043.00", "Stethoscopes", "42181604", "Stethoscopes"),
    ("29-2043.00", "Laptop computers", "43211503", "Notebook computers"),
    ("47-2031.00", "Levels", "41111604", "Levels"),  # a common word
)

# Ordinary prose that makes "analyze", "billing", "paint" and "levels" common words (>= 2 lower-case uses).
TASKS = (
    ("15-1252.00", "Review and analyze billing records, then paint the signs and check levels."),
    ("43-3021.00", "Collect and analyze vendor billing; paint walls; record levels of stock."),
)

OCCUPATIONS = (
    ("11-1011.00", "Chief Executives"),
    ("11-1021.00", "General and Operations Managers"),
    ("11-9199.00", "Managers, All Other"),
    ("15-1252.00", "Software Developers"),
    ("15-1299.00", "Computer Occupations, All Other"),
    ("17-2141.00", "Mechanical Engineers"),
    ("29-2043.00", "Paramedics"),
    ("43-1011.00", "First-Line Supervisors of Office and Administrative Support Workers"),
    ("53-7051.00", "Industrial Truck and Tractor Operators"),
    ("55-3016.00", "Infantry"),
)

ALTERNATE_TITLES = (  # soc, title, short title
    ("15-1252.00", "Software Engineer", "n/a"),
    ("15-1252.00", "Application Developer (App Developer)", "App Developer"),
    ("11-1021.00", "Office Manager", "n/a"),
    ("43-1011.00", "Office Manager", "n/a"),
    ("11-1021.00", "Program Manager", "n/a"),
    ("11-9199.00", "Program Manager", "n/a"),
    ("17-2141.00", "Mechanical Design Engineer", "n/a"),
    ("15-1299.00", "Software Developers", "n/a"),  # another SOC's primary title: the primary wins
    ("15-1252.00", "Intern", "n/a"),  # generic: names no occupation
)

REPORTED_TITLES = (  # soc, title
    ("15-1252.00", "Software Developer"),
    ("11-1011.00", "CEO (Chief Executive Officer)"),
    ("17-2141.00", "Co-op"),  # generic
)

JOB_ZONES = (
    ("11-1011.00", "5"), ("11-1021.00", "4"), ("11-9199.00", "4"), ("15-1252.00", "4"), ("15-1299.00", "4"),
    ("17-2141.00", "4"), ("29-2043.00", "3"), ("43-1011.00", "3"), ("53-7051.00", "2"),
)

KNOWLEDGE = (  # soc, element id, element name, importance (IM)
    ("15-1299.00", "2.C.4.a", "Mathematics", "3.90"),
    ("15-1299.00", "2.C.3.a", "Computers and Electronics", "4.50"),
    ("55-3016.00", "2.C.4.a", "Mathematics", "2.00"),
    ("29-2043.00", "2.C.4.a", "Mathematics", "3.10"),
)

MILITARY_COLUMNS = (
    "SVC", "MPC", "MOC", "MOC_TITLE", "OLD_DODOCC", "OLD_DODMED", "DODOCC", "DODOCC_TITLE", "STATUS", "SDATE",
    "EDATE", "CHANGE_TYPE", "XDATE", "SOC1", "SOC2", "SOC3", "SOC4", "SOC1_TITLE", "SOC2_TITLE", "SOC3_TITLE",
    "SOC4_TITLE", "MOTD1", "MOTD1_TITLE", "MOTD2", "MOTD2_TITLE", "ONET1", "ONET2", "ONET3", "ONET4",
    "ONET1_TITLE", "ONET2_TITLE", "ONET3_TITLE", "ONET4_TITLE", "record_changed",
)

MILITARY = (  # svc, mpc, moc, title, status, onet codes
    ("A", "E", "25B", "Information Technology Specialist", "A", ("15-1299.00",)),
    ("A", "E", "11X", "Infantry Recruit", "O", ("55-3016.00",)),
    ("M", "E", "0311", "Rifleman", "A", ("55-3016.00",)),
    ("M", "O", "0952", "Formal School Officer Instructor", "A", ("11-1021.00",)),
    ("N", "O", "0952", "Nurse Anesthetist", "A", ("29-2043.00",)),
    ("F", "E", "3D052", "Cyber Transport Systems Journeyman", "A", ("15-1299.00",)),
    ("H", "E", "3D052", "Cyber Transport Systems Journeyman", "A", ("15-1299.00",)),
    ("F", "E", "3D072", "Cyber Transport Systems Craftsman", "A", ("15-1252.00",)),
    ("A", "E", "68W", "Combat Medic Specialist", "A", ("29-2043.00",)),
    ("G", "-", "2210", "Information Technology Management", "A", ("15-1299.00",)),
    ("A", "E", "09L", "Interpreter/Translator", "A", ()),
    ("A", "E", "12T", "Technical Engineer", "A", ("17-2199.00",)),
)

NAICS = (
    ("11", "Agriculture, Forestry, Fishing and Hunting"),
    ("111", "Crop Production"),
    ("31-33", "Manufacturing"),
    ("336", "Transportation Equipment Manufacturing"),
    ("51", "Information"),
    ("513", "Publishing Industries"),
    ("5132", "Software Publishers"),
    ("513210", "Software Publishers"),
)


# ─── writing ───────────────────────────────────────────────────────────────────────────────


def _csv(rows: list[list[str]], delimiter: str = ",") -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL if delimiter == "," else csv.QUOTE_NONE)
    writer.writerows(rows)
    return buffer.getvalue()


def _skills(lang: str) -> str:
    rows = [["conceptType", "conceptUri", "skillType", "reuseLevel", "preferredLabel", "altLabels", "hiddenLabels",
             "status", "modifiedDate", "scopeNote", "definition", "inScheme", "description"]]
    for uuid, skill_type, reuse, pref_en, alt_en, hidden_en, pref_fr, alt_fr in CONCEPTS:
        pref, alt, hidden = (pref_en, alt_en, hidden_en) if lang == "en" else (pref_fr, alt_fr, [])
        rows.append(["KnowledgeSkillCompetence", _SKILL + uuid, skill_type, reuse, pref, "\n".join(alt),
                     "\n".join(hidden), "released", "2024-01-01T00:00:00Z", "", "", _SCHEME, f"About {pref_en}."])
    return _csv(rows)


def _groups(lang: str) -> str:
    rows = [["conceptType", "conceptUri", "preferredLabel", "altLabels", "hiddenLabels", "status", "modifiedDate",
             "scopeNote", "inScheme", "description", "code"]]
    for code, uri, pref_en, pref_fr in GROUPS:
        rows.append(["SkillGroup", uri, pref_en if lang == "en" else pref_fr, "", "", "released", "", "", _SCHEME,
                     "", code])
    return _csv(rows)


def _labels() -> dict[str, str]:
    labels = {_SKILL + uuid: pref for uuid, _, _, pref, *_ in CONCEPTS}
    labels.update({uri: pref for _, uri, pref, _ in GROUPS})
    return labels


def _broader() -> str:
    labels = _labels()
    group_uris = {uri for _, uri, _, _ in GROUPS}
    rows = [["conceptType", "conceptUri", "conceptLabel", "broaderType", "broaderUri", "broaderLabel"]]
    for concept, broader in BROADER:
        kind = "SkillGroup" if concept in group_uris else "KnowledgeSkillCompetence"
        broader_kind = "SkillGroup" if broader in group_uris else "KnowledgeSkillCompetence"
        rows.append([kind, concept, labels[concept], broader_kind, broader, labels[broader]])
    return _csv(rows)


def _collection(suffixes: tuple[str, ...]) -> str:
    by_uuid = {uuid: (skill_type, reuse, pref) for uuid, skill_type, reuse, pref, *_ in CONCEPTS}
    rows = [["conceptType", "conceptUri", "skillType", "reuseLevel", "preferredLabel", "status", "altLabels",
             "description", "broaderConceptUri", "broaderConceptPT"]]
    for suffix in suffixes:
        uuid = "00000000-0000-4000-8000-" + suffix
        skill_type, reuse, pref = by_uuid[uuid]
        rows.append(["KnowledgeSkillCompetence", _SKILL + uuid, skill_type, reuse, pref, "released", "", "", "", ""])
    return _csv(rows)


def _military() -> str:
    rows = [list(MILITARY_COLUMNS)]
    for svc, mpc, moc, title, status, onet in MILITARY:
        record = dict.fromkeys(MILITARY_COLUMNS, "")
        record.update(SVC=svc, MPC=mpc, MOC=moc, MOC_TITLE=title, STATUS=status, SDATE="202001", EDATE="999912")
        for index, code in enumerate(onet, 1):
            record[f"ONET{index}"] = code
            record[f"SOC{index}"] = code[:7]
        rows.append([record[column] for column in MILITARY_COLUMNS])
    return _csv(rows)


def files() -> dict[str, str]:
    """Relative path -> text content of every raw_mini file."""
    out = {
        "esco/v1.2.1/en/skills_en.csv": _skills("en"),
        "esco/v1.2.1/fr/skills_fr.csv": _skills("fr"),
        "esco/v1.2.1/en/skillGroups_en.csv": _groups("en"),
        "esco/v1.2.1/fr/skillGroups_fr.csv": _groups("fr"),
        "esco/v1.2.1/en/broaderRelationsSkillPillar_en.csv": _broader(),
        "esco/v1.2.1/en/transversalSkillsCollection_en.csv": _collection(TRANSVERSAL),
        "esco/v1.2.1/en/languageSkillsCollection_en.csv": _collection(LANGUAGE),
        "esco/v1.2.1/en/occupations_en.csv": _csv([
            ["conceptType", "conceptUri", "iscoGroup", "preferredLabel", "altLabels", "hiddenLabels", "status",
             "modifiedDate", "regulatedProfessionNote", "scopeNote", "definition", "inScheme", "description", "code",
             "naceCode"],
            ["Occupation", "http://data.europa.eu/esco/occupation/00000000-0000-4000-8000-0000000000c1", "2512",
             "software developer", "", "", "released", "", "", "", "", "", "Software developers write code.",
             "2512.1", ""],
        ]),
    }
    onet = "onet/db_30_0_text/"
    out[onet + "Technology Skills.txt"] = _csv(
        [["O*NET-SOC Code", "Example", "Commodity Code", "Commodity Title", "Hot Technology", "In Demand"],
         *map(list, TECHNOLOGY)], "\t")
    out[onet + "Tools Used.txt"] = _csv(
        [["O*NET-SOC Code", "Example", "Commodity Code", "Commodity Title"], *map(list, TOOLS)], "\t")
    out[onet + "Task Statements.txt"] = _csv(
        [["O*NET-SOC Code", "Task ID", "Task", "Task Type", "Incumbents Responding", "Date", "Domain Source"],
         *([soc, str(index), task, "Core", "20", "08/2023", "Incumbent"] for index, (soc, task) in enumerate(TASKS))],
        "\t")
    out[onet + "Occupation Data.txt"] = _csv(
        [["O*NET-SOC Code", "Title", "Description"], *([soc, title, f"{title}."] for soc, title in OCCUPATIONS)], "\t")
    out[onet + "Alternate Titles.txt"] = _csv(
        [["O*NET-SOC Code", "Alternate Title", "Short Title", "Source(s)"],
         *([soc, title, short, "08"] for soc, title, short in ALTERNATE_TITLES)], "\t")
    out[onet + "Sample of Reported Titles.txt"] = _csv(
        [["O*NET-SOC Code", "Reported Job Title", "Shown in My Next Move"],
         *([soc, title, "Y"] for soc, title in REPORTED_TITLES)], "\t")
    out[onet + "Job Zones.txt"] = _csv(
        [["O*NET-SOC Code", "Job Zone", "Date", "Domain Source"],
         *([soc, zone, "08/2023", "Analyst"] for soc, zone in JOB_ZONES)], "\t")
    out[onet + "Knowledge.txt"] = _csv(
        [["O*NET-SOC Code", "Element ID", "Element Name", "Scale ID", "Data Value", "N", "Standard Error",
          "Lower CI Bound", "Upper CI Bound", "Recommend Suppress", "Not Relevant", "Date", "Domain Source"],
         *([soc, element, name, "IM", value, "20", "0.1", "1", "5", "N", "n/a", "08/2023", "Incumbent"]
           for soc, element, name, value in KNOWLEDGE)], "\t")
    out["onet/military_crosswalk/military_crosswalk/milx0724.csv"] = _military()
    out["naics/naics_2022_codes.tsv"] = _csv([["code", "title"], *map(list, NAICS)], "\t")

    def record(title: str, file: str, extracted: str | None = None, converted: str | None = None) -> dict:
        entry = {"title": title, "url": f"https://example.invalid/{file}", "file": file, "bytes": 1,
                 "sha256": "0" * 64, "fetched": "2026-01-01", "licence": "test fixture"}
        if extracted:
            entry["extracted_to"] = extracted
        if converted:
            entry["converted_to"] = converted
        return entry

    out["fetch_record.json"] = json.dumps({"format": 1, "downloads": {
        "esco_en": record("ESCO v1.2.1 classification, English, CSV", "esco/en.zip", "esco/v1.2.1/en"),
        "esco_fr": record("ESCO v1.2.1 classification, French, CSV", "esco/fr.zip", "esco/v1.2.1/fr"),
        "onet_db": record("O*NET 30.0 database, text files", "onet/db_30_0_text.zip", "onet"),
        "onet_military": record("O*NET military crosswalk", "onet/military_crosswalk.zip",
                                "onet/military_crosswalk"),
        "naics_2022": record("NAICS 2022 codes", "naics/codes.xlsx", converted="naics/naics_2022_codes.tsv"),
    }}, indent=1, sort_keys=True) + "\n"
    out[".gitattributes"] = "# Byte-exact inputs for the deterministic-build test.\n* -text\n"
    return out


def build(target: Path = RAW_MINI) -> None:
    for relative, text in files().items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    build()
