"""Builds tests/fixtures/parse_taxonomy/: tiny titles, NAICS and military-crosswalk artifacts (Task 6.3).

The parse tests read these instead of the real data/taxonomy extras, so their expectations never
move when the taxonomy is rebuilt. The files are this module's output, written through
`write_artifacts` (an empty skill graph plus three extra documents) so no hash is typed by hand;
`tests/test_taxonomy_extras.py::test_committed_parse_fixture_is_current` fails when they drift.
The documents have the shapes of the real ones (`scripts/build_taxonomy.py`). Regenerate from
match-engine/ with:

    ./.venv/Scripts/python.exe tests/fixtures/build_parse_taxonomy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":  # run as a script: make `matcher` importable without installing it
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from matcher.graph.loader import write_artifacts  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "parse_taxonomy"
SOURCES = {"fixture": "hand-built parse extras (tests/fixtures/build_parse_taxonomy.py)"}

_COMPUTING = "Computer and Mathematical Occupations"
_EDUCATION = "Educational Instruction and Library Occupations"

TITLES = {
    "format": 1,
    "titles": {
        "accountant": {"family": "Business and Financial Operations Occupations", "job_zone": 4,
                       "soc": "13-2011.00"},
        "data analyst": {"family": _COMPUTING, "job_zone": 4, "soc": "15-2051.01"},
        "front end developer": {"family": _COMPUTING, "job_zone": 3, "soc": "15-1254.00"},
        "machine learning engineer": {"family": _COMPUTING, "job_zone": 5, "soc": "15-1221.00"},
        "registered nurse": {"family": "Healthcare Practitioners and Technical Occupations", "job_zone": 4,
                             "soc": "29-1141.00"},
        "rifleman": {"family": "Military Specific Occupations", "job_zone": None, "soc": "55-3016.00"},
        "software developer": {"family": _COMPUTING, "job_zone": 4, "soc": "15-1252.00"},
        "software engineer": {"family": _COMPUTING, "job_zone": 4, "soc": "15-1252.00"},
        "teaching assistant": {"family": _EDUCATION, "job_zone": 3, "soc": "25-9042.00"},
    },
}

NAICS = {
    "format": 1,
    "codes": {
        "44": "Retail Trade",
        "45": "Retail Trade",
        "51": "Information",
        "513": "Publishing Industries",
        "5132": "Software Publishers",
        "52": "Finance and Insurance",
        "5221": "Depository Credit Intermediation",
        "92": "Public Administration",
    },
}

_NO_DATA = "O*NET publishes no skill data for the occupation(s)"
MOS = {
    "format": 1,
    "common_knowledge_left_out": ["skill.customer_service"],
    "codes": {
        "0311": {"branch": "marine_corps", "branches": ["marine_corps"], "note": f"55-3016.00: {_NO_DATA}",
                 "skill_ids": [], "soc": ["55-3016.00"], "title": "Rifleman"},
        "1371": {"branch": "marine_corps", "branches": ["marine_corps"],
                 "skill_ids": ["skill.autocad", "skill.blueprint_reading"], "soc": ["47-2061.00"],
                 "title": "Combat Engineer"},
        "25B": {"branch": "army", "branches": ["army"], "note": f"15-1299.00: {_NO_DATA}", "skill_ids": [],
                "soc": ["15-1299.00"], "title": "Information Technology Specialist"},
        "3D0X2": {"branch": "air_force", "branches": ["air_force"],
                  "skill_ids": ["skill.linux", "skill.network_administration"], "soc": ["15-1244.00"],
                  "title": "Cyber Systems Operations"},
        "68W": {"branch": "army", "branches": ["army"],
                "skill_ids": ["skill.emergency_medicine", "skill.patient_care"], "soc": ["29-2043.00"],
                "title": "Combat Medic Specialist"},
        "8404": {"branch": "navy", "branches": ["navy"], "skill_ids": ["skill.first_aid"], "soc": ["29-2043.00"],
                 "title": "Field Medical Service Technician"},
        "HM": {"branch": "navy", "branches": ["navy"], "skill_ids": ["skill.patient_care"],
               "soc": ["31-1131.00"], "title": "Hospital Corpsman"},
        "HR": {"branch": "navy", "branches": ["navy"], "skill_ids": ["skill.first_aid"], "soc": ["31-1131.00"],
               "title": "Hospitalman Recruit"},
        "IT": {"alternatives": [{"branch": "coast_guard", "skill_ids": ["skill.cisco_ios"], "soc": ["15-1244.00"],
                                 "title": "Information Systems Technician"}],
               "branch": "navy", "branches": ["coast_guard", "navy"],
               "skill_ids": ["skill.active_directory", "skill.windows_server"], "soc": ["15-1232.00"],
               "title": "Information Systems Technician"},
    },
}


def build(target: Path = FIXTURE_DIR) -> None:
    """Write the fixture: an empty graph (nodes.json, idf.json, pmi_edges.json) and the three extras."""
    write_artifacts(
        target,
        (),
        {},
        {},
        n_postings=0,
        sources=SOURCES,
        compress=False,
        extra={"titles.json": TITLES, "naics.json": NAICS, "mos_crosswalk.json": MOS},
    )


if __name__ == "__main__":
    build()
