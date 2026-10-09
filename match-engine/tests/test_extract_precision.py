"""Extraction precision on realistic résumé and posting lines (Task 2.2 fix rounds 1-2; DECISIONS D-25..D-28).

Each case runs `Resolver.extract` over the committed data/taxonomy and asserts the EXACT mentions,
as (canonical id, surface) in text order: no false positive and every expected skill. The first
blocks hold every probe of the two fix-round reviews (licences in progress, ordinary words that
are not the skill in English and French, seasons, initials and money, fields of study and the
domains named like them, French elisions, merged ESCO labels, acronym slashes); the rest are
lines from software, data, engineering, finance, marketing, healthcare, the trades, students'
co-op lines and French. Where a review quoted a fragment ("Executed equity trades..."), the line
keeps the fragment verbatim and completes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matcher.graph.loader import load_resolver

TAXONOMY = Path(__file__).resolve().parents[1] / "data" / "taxonomy"

CASES = [
    # ─── the review's probes ───────────────────────────────────────────────────────────────
    ("prose", "CPA candidate (CPA PEP Core 2), currently writing the CFE in 2027.",
     [("cred.cpa_in_progress", "CPA candidate"), ("cred.cpa_in_progress", "CPA PEP Core 2"),
      ("cred.cpa_in_progress", "writing the CFE")]),
    ("prose", "Passed CFA Level I; CFA candidate for Level II.",
     [("cred.cfa_candidate", "CFA Level I"), ("cred.cfa_candidate", "CFA candidate")]),
    ("prose", "RN student completing a nursing practicum.",
     [("cred.nursing_student", "RN student"), ("skill.nursing", "nursing")]),  # nursing: a domain (D-27)
    ("prose", "Articling student at a Toronto law firm.", [("cred.articling_student", "Articling student")]),
    ("prose", "309A apprentice electrician.", [("cred.electrician_apprentice", "309A apprentice electrician")]),
    ("prose", "Teacher candidate in the B.Ed program.", [("cred.teacher_candidate", "Teacher candidate")]),
    ("prose", "P.Eng. candidate with four years of experience.", [("cred.eit", "P.Eng. candidate")]),
    ("prose", "Dean's List, Fall 2023 and Spring 2024.", []),
    ("prose", "Software Developer Intern, Spring 2024 (Shopify)", []),
    ("prose", "Analyst at American Express, Summer 2024", []),
    ("prose", "Sous Chef: managed a team of six.", []),
    ("prose", "John R. Smith | Toronto, ON", []),
    ("prose", "Dr. Mary C. Jones", []),
    ("prose", "Took Vitamin C daily.", []),
    ("prose", "Worked in Building C on campus.", []),
    ("prose", "Played for the Swift Current Broncos.", []),
    ("prose", "Administered 10 mL of normal saline to post-operative patients.", []),
    ("prose", "Organized networking events for the alumni association.", []),
    ("prose", "Loaded and unloaded shipping containers at the port.", []),
    ("prose", "Skilled in the trades.", []),
    ("prose", "Ran a regression on the housing data.", []),
    ("prose", "Holds a black belt in karate.", []),
    ("prose", "Black Belt in Taekwondo", []),
    ("list", "Skills: Excel (pivot tables, VLOOKUP)",
     [("skill.excel", "Excel"), ("skill.pivot_tables", "pivot tables"), ("skill.vlookup", "VLOOKUP")]),
    ("prose", "Experience in software design and web design.",
     [("skill.software_design", "software design"), ("skill.web_design", "web design")]),
    ("prose", "Learned to make business decisions quickly.", []),
    ("prose", "Bon esprit d'équipe et morale élevée.", [("skill.teamwork", "esprit d'équipe")]),
    ("prose", "Saisie des commandes et service à la clientèle.",
     [("skill.customer_service", "service à la clientèle")]),
    ("prose", "Maîtrise d'Excel et d'Access; connaissance d'AWS et d'Azure.",
     [("skill.excel", "Excel"), ("skill.ms_access", "Access"), ("skill.aws", "AWS"), ("skill.azure", "Azure")]),
    ("prose", "Projets en l'apprentissage automatique et projets d'infonuagique.",
     [("skill.machine_learning", "apprentissage automatique"), ("skill.cloud_computing", "infonuagique")]),
    ("prose", "Strong design sense and a passion for architecture, history and philosophy.",
     [("skill.design", "design")]),  # design: a domain (D-27); the rest are fields of study only
    ("prose", "Volunteered in education and commerce; studied physics and law.", []),
    ("prose", "Built backend services in Go.", [("skill.backend", "backend"), ("skill.go", "Go")]),
    ("prose", "Analysed data in R and Python.", [("skill.r", "R"), ("skill.python", "Python")]),
    ("prose", "Wrote firmware in C.", [("esco.firmware", "firmware"), ("skill.c", "C")]),
    ("prose", "Advanced Excel", [("skill.excel", "Advanced Excel")]),
    ("prose", "Spring Boot microservices", [("skill.spring", "Spring Boot"), ("skill.microservices", "microservices")]),
    ("prose", "Express.js API", [("skill.express", "Express.js"), ("skill.apis", "API")]),
    ("prose", "Used Kibana, Logstash and CircleCI; wrote OCaml and Hibernate code in PyCharm.",
     [("skill.kibana", "Kibana"), ("skill.logstash", "Logstash"), ("skill.circleci", "CircleCI"),
      ("skill.ocaml", "OCaml"), ("skill.hibernate", "Hibernate"), ("skill.pycharm", "PyCharm")]),
    ("prose", "Ran agile ceremonies as Scrum Master.", [("skill.agile", "agile"), ("skill.scrum_master", "Scrum Master")]),
    ("prose", "Posted daily on instagram.", [("skill.instagram", "instagram")]),
    # ─── fix round 2: licences through long forms (D-28) ───────────────────────────────────
    ("prose", "Primary Care Paramedic student, Centennial College.",
     [("cred.paramedic_student", "Primary Care Paramedic student")]),
    ("prose", "Advanced Care Paramedic student", [("cred.paramedic_student", "Advanced Care Paramedic student")]),
    ("prose", "Licensed Practical Nurse student", [("cred.nursing_student", "Licensed Practical Nurse student")]),
    ("prose", "Enrolled in the RPN program at Fanshawe.", [("cred.nursing_student", "RPN program")]),
    ("prose", "Nursing student in the RN program at Humber College.",
     [("cred.nursing_student", "Nursing student"), ("cred.nursing_student", "RN program")]),
    ("prose", "Registered Nurse candidate awaiting the NCLEX.",
     [("cred.nursing_student", "Registered Nurse candidate")]),
    ("prose", "Class A CDL student", [("cred.cdl_trainee", "Class A CDL student")]),
    ("prose", "309A Electrical Apprentice", [("cred.electrician_apprentice", "Electrical Apprentice")]),
    ("prose", "Apprentice Electrician (309A), Level 2", [("cred.electrician_apprentice", "Apprentice Electrician")]),
    ("prose", "Construction and Maintenance Electrician 309A apprentice",
     [("cred.electrician_apprentice", "Construction and Maintenance Electrician 309A apprentice")]),
    ("prose", "Professional Engineer in training", [("cred.eit", "Professional Engineer in training")]),
    ("prose", "Enrolled in the CPA Canada PEP (Core 2).", [("cred.cpa_in_progress", "CPA Canada PEP")]),
    ("prose", "CPA Ontario PEP student", [("cred.cpa_in_progress", "CPA Ontario PEP student")]),
    ("prose", "CPA (in progress)", [("cred.cpa_in_progress", "CPA (in progress)")]),
    # ─── fix round 2: French one-word aliases, and the round-1 fragments verbatim (D-27) ──────
    ("prose", "Nous sommes à la recherche d'un stagiaire en développement logiciel pour notre équipe.",
     [("skill.software_development", "développement logiciel")]),
    ("prose", "Rabais employé de 30 % sur les achats.", []),
    ("prose", "Horaire selon les prévisions d'achalandage.", []),
    ("prose", "Utilisation du système de point de vente et manipulation d'argent.",
     [("skill.pos", "système de point de vente"), ("skill.cash_handling", "manipulation d'argent")]),
    ("prose", "Executed equity trades for retail clients.", []),
    ("prose", "Le poste exige un effort physique important et le port de charges.", []),
    ("prose", "Vous aurez droit à quatre semaines de vacances par année.", []),
    ("prose", "Vérification des antécédents judiciaires requise.", []),
    ("prose", "Provided patient care on a 30-bed medical-surgical unit", [("skill.patient_care", "patient care")]),
    # ─── fix round 2: acronyms in slash tokens (D-25) ──────────────────────────────────────
    ("prose", "Experience with AI/ML frameworks such as PyTorch.",
     [("skill.ai", "AI"), ("skill.machine_learning", "ML"), ("skill.pytorch", "PyTorch")]),
    ("prose", "We are hiring an AI/ML Engineer Intern.", [("skill.ai", "AI"), ("skill.machine_learning", "ML")]),
    ("list", "AI/ML, NLP, Computer Vision",
     [("skill.ai", "AI"), ("skill.machine_learning", "ML"), ("skill.nlp", "NLP"),
      ("skill.computer_vision", "Computer Vision")]),
    ("prose", "Built ML/AI pipelines on AWS.",
     [("skill.machine_learning", "ML"), ("skill.ai", "AI"), ("skill.aws", "AWS")]),
    ("prose", "Worked on BI/reporting dashboards.", [("skill.business_intelligence", "BI")]),
    ("prose", "Strong grasp of OS/networking fundamentals.", [("skill.operating_systems", "OS")]),
    ("prose", "Granted R/W access to shares.", []),
    # ─── fix round 2: domains that are also fields of study (D-27) ─────────────────────────
    ("prose", "Strong knowledge of accounting principles and statistics.",
     [("skill.accounting", "accounting principles"), ("skill.statistics", "statistics")]),
    ("prose", "Experience in marketing, finance or data science is an asset.",
     [("skill.marketing", "marketing"), ("skill.finance", "finance"), ("skill.data_science", "data science")]),
    ("prose", "Developed marketing strategies for three product launches.",
     [("skill.marketing", "marketing strategies")]),
    ("prose", "Background in economics and nursing.", [("skill.economics", "economics"), ("skill.nursing", "nursing")]),
    # ─── fix round 2: synonyms a merged concept brings when they share a word (D-26) ───────
    ("prose", "Provided customer support by phone and email.", [("skill.customer_service", "customer support")]),
    ("prose", "Applied supervised learning and unsupervised learning to churn data.",
     [("skill.machine_learning", "supervised learning"), ("skill.machine_learning", "unsupervised learning")]),
    ("prose", "Known for problem resolution under pressure.", [("skill.problem_solving", "problem resolution")]),
    ("prose", "Able to manage projects end to end.", [("skill.project_management", "manage projects")]),
    # ─── fix round 2: minors (D-27, D-28) ──────────────────────────────────────────────────
    ("prose", "Built the portal in ASP.NET. Core features included search.", [("skill.dotnet", "ASP.NET")]),
    ("prose", "Licensed P.Eng. Candidate pool screening is ongoing.", [("cred.professional_engineer", "P.Eng")]),
    ("list", "Technical Skills: Python, SQL", [("skill.python", "Python"), ("skill.sql", "SQL")]),
    ("prose", "Managed Cross-Functional Teams across three sites.", []),
    ("prose", "Collaborated over Microsoft Teams daily.", [("skill.ms_teams", "Microsoft Teams")]),
    ("prose", "Worked as a line Chef at a bistro.", []),
    ("prose", "Automated server configuration with Chef Infra.", [("skill.chef", "Chef Infra")]),
    ("prose", "Drove for Swift Transportation for two years.", []),
    ("prose", "Placed through Express Employment Professionals.", []),
    ("prose", "Graduated in the Spring of 2024.", []),
    ("prose", "Salary: 80,000 CAD per year", []),
    ("prose", "$25/hr CAD", []),
    ("prose", "Proficient in CAD (SolidWorks)", [("skill.cad", "CAD"), ("skill.solidworks", "SolidWorks")]),
    # ─── software ──────────────────────────────────────────────────────────────────────────
    ("prose", "Built REST APIs in Java with Spring Boot and PostgreSQL, deployed on AWS with Docker and Kubernetes.",
     [("skill.rest", "REST APIs"), ("skill.java", "Java"), ("skill.spring", "Spring Boot"),
      ("skill.postgresql", "PostgreSQL"), ("skill.aws", "AWS"), ("skill.docker", "Docker"),
      ("skill.kubernetes", "Kubernetes")]),
    ("prose", "Developed a React and TypeScript front end; wrote unit tests with Jest.",
     [("skill.react", "React"), ("skill.typescript", "TypeScript"), ("skill.frontend", "front end"),
      ("skill.unit_testing", "unit tests"), ("skill.jest", "Jest")]),
    ("prose", "Migrated CI/CD pipelines from Jenkins to GitHub Actions.",
     [("skill.ci_cd", "CI/CD pipelines"), ("skill.jenkins", "Jenkins"), ("skill.github_actions", "GitHub Actions")]),
    ("prose", "Led the Go-Live of the new SAP module and the R&D budget review.", [("skill.sap", "SAP")]),
    ("prose", "Reduced page load time by 40% using Redis caching.", [("skill.redis", "Redis")]),
    # ─── data ──────────────────────────────────────────────────────────────────────────────
    ("prose", "Cleaned and analysed survey data with pandas and SQL, then built Tableau dashboards.",
     [("skill.pandas", "pandas"), ("skill.sql", "SQL"), ("skill.tableau", "Tableau")]),
    ("prose", "Trained machine learning models in scikit-learn and PyTorch.",
     [("skill.machine_learning", "machine learning models"), ("skill.scikit_learn", "scikit-learn"),
      ("skill.pytorch", "PyTorch")]),
    ("list", "Python, R, SQL, Power BI, data cleaning, regression analysis",
     [("skill.python", "Python"), ("skill.r", "R"), ("skill.sql", "SQL"), ("skill.power_bi", "Power BI"),
      ("skill.data_cleaning", "data cleaning"), ("skill.regression", "regression analysis")]),
    ("prose", "Performed regression testing before each release.", [("skill.regression_testing", "regression testing")]),
    # ─── engineering ───────────────────────────────────────────────────────────────────────
    ("prose", "Designed brackets in SolidWorks and ran FEA in ANSYS.",
     [("skill.solidworks", "SolidWorks"), ("skill.fea", "FEA"), ("skill.ansys", "ANSYS")]),
    ("prose", "Programmed PLCs and HMIs for a packaging line.", [("skill.plc", "PLCs"), ("skill.hmi", "HMIs")]),
    ("prose", "Built a line-following robot with an Arduino and C programming.",
     [("skill.arduino", "Arduino"), ("skill.c", "C programming")]),
    # ─── finance and accounting ────────────────────────────────────────────────────────────
    ("prose", "Prepared bank reconciliations and month-end journal entries in QuickBooks.",
     [("skill.reconciliation", "bank reconciliations"), ("skill.quickbooks", "QuickBooks")]),
    ("prose", "Built a DCF valuation model in Excel.",
     [("skill.valuation", "DCF"), ("skill.valuation", "valuation"), ("skill.excel", "Excel")]),
    ("prose", "Supported the external audit and prepared financial statements under IFRS.",
     [("skill.auditing", "external audit"), ("skill.financial_reporting", "financial statements"),
      ("skill.ifrs", "IFRS")]),
    # ─── marketing and sales ───────────────────────────────────────────────────────────────
    ("prose", "Grew Instagram engagement by 30% and ran Google Ads campaigns.",
     [("skill.instagram", "Instagram"), ("skill.google_ads", "Google Ads")]),
    ("prose", "Managed the HubSpot CRM and wrote email marketing copy.",
     [("skill.hubspot", "HubSpot CRM"), ("skill.email_marketing", "email marketing")]),
    ("prose", "Exceeded sales targets in a B2B sales role.", [("skill.sales", "sales"), ("skill.sales", "B2B sales")]),
    # ─── nursing and healthcare ────────────────────────────────────────────────────────────
    ("prose", "Monitored vital signs and administered medication on a 30-bed surgical unit.",
     [("skill.vital_signs", "vital signs"), ("skill.medication_administration", "administered medication")]),
    ("prose", "Documented patient care in Epic and followed infection control protocols.",
     [("skill.patient_care", "patient care"), ("skill.epic", "Epic"), ("skill.infection_control", "infection control")]),
    ("prose", "Registered Nurse with BLS and ACLS certification.",
     [("cred.registered_nurse", "Registered Nurse"), ("cred.bls", "BLS"), ("cred.acls", "ACLS certification")]),
    # ─── trades ────────────────────────────────────────────────────────────────────────────
    ("prose", "Performed MIG welding and read blueprints on site.",
     [("skill.welding", "MIG welding"), ("skill.blueprint_reading", "read blueprints")]),
    ("prose", "Operated a forklift and a pallet jack in a busy warehouse.",
     [("skill.forklift", "forklift"), ("skill.pallet_jack", "pallet jack")]),
    ("prose", "Red Seal electrician with WHMIS and Working at Heights training.",
     [("cred.red_seal", "Red Seal"), ("cred.whmis", "WHMIS"), ("cred.working_at_heights", "Working at Heights training")]),
    # ─── students' co-op and volunteer lines ───────────────────────────────────────────────
    ("prose", "Software Engineering Intern (Winter 2025 co-op), Toronto, ON", []),
    ("prose", "Teaching Assistant, Fall 2024: held office hours for 200 students.", []),
    ("prose", "Summer 2023: camp counsellor; First Aid and CPR certified.",
     [("cred.first_aid", "First Aid"), ("cred.cpr", "CPR certified")]),
    ("prose", "Volunteer, Spring 2022: organised a charity run.", []),
    # ─── French ────────────────────────────────────────────────────────────────────────────
    ("prose", "Développement web avec React et Node.js; gestion de projet agile.",
     [("skill.web_development", "Développement web"), ("skill.react", "React"), ("skill.nodejs", "Node.js"),
      ("skill.agile", "gestion de projet agile")]),  # an imported synonym of agile project management
    ("prose", "J'ai travaillé en équipe et c'est là que j'ai appris l'analyse de données.",
     [("skill.data_analysis", "analyse de données")]),
    ("prose", "Service à la clientèle, gestion des stocks et caisse.",
     [("skill.customer_service", "Service à la clientèle"), ("skill.inventory_management", "gestion des stocks")]),
]


@pytest.fixture(scope="module")
def resolver(config):
    return load_resolver(TAXONOMY, config)


@pytest.mark.parametrize("context, text, expected", CASES, ids=[f"{n}:{case[1][:40]}" for n, case in
                                                                enumerate(CASES, 1)])
def test_extract_finds_exactly_these_mentions(resolver, context: str, text: str, expected: list) -> None:
    mentions = resolver.extract(text, context=context)
    assert [(m.canonical_id, m.surface) for m in mentions] == expected
    assert all(text[m.start:m.end] == m.surface for m in mentions)


def test_there_are_about_sixty_realistic_lines() -> None:
    assert len(CASES) >= 120
    assert len({(context, text) for context, text, _ in CASES}) == len(CASES)


def test_fields_of_study_come_back_when_asked_for(resolver) -> None:
    line = "B.Sc. Computer Science, University of Waterloo; minor in Physics"
    assert resolver.extract(line) == ()
    assert [(m.canonical_id, m.surface) for m in resolver.extract(line, include_fields=True)] == [
        ("field.science", "B.Sc"), ("field.computer_science", "Computer Science"), ("field.physics", "Physics")]


def test_licences_in_progress_never_reach_the_licence(resolver, config) -> None:
    licences = set(config.gates.license.professional_credential_ids)
    for text in ("CPA candidate", "CPA PEP Core 1", "writing the CFE", "RN student", "nursing student",
                 "P.Eng candidate", "articling student", "309A apprentice", "teacher candidate", "PharmD student",
                 "medical student", "CDL trainee", "paramedic student", "CFA Level III", "C.P.A. candidate"):
        found = {m.canonical_id for m in resolver.extract(text, context="list")}
        assert found and not found & licences, (text, found)
