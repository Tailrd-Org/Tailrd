"""Extraction precision on realistic résumé and posting lines (Task 2.2 fix rounds 1-4; DECISIONS D-25..D-28).

Each case runs `Resolver.extract` over the committed data/taxonomy and asserts the EXACT mentions,
as (canonical id, surface) in text order: no false positive and every expected skill. The first
blocks hold every probe of the two fix-round reviews (licences in progress, ordinary words that
are not the skill in English and French, seasons, initials and money, fields of study and the
domains named like them, French elisions, merged ESCO labels, acronym slashes); the rest are
lines from software, data, engineering, finance, marketing, healthcare, the trades, students'
co-op lines and French. Where a review quoted a fragment ("Executed equity trades..."), the line
keeps the fragment verbatim and completes it. Round 3 (D-28): a status written around a licence
("RN student", "CPA (in progress)") reads as the licence concept, the status being the résumé
parser's business; only formal designations ("CPA PEP", "articling student") are in progress.
Round 4 (D-26, D-27): pay lines are money ("85,000 CAD plus benefits"), CAD stays the skill before
a noun of its work ("1,200 CAD drawings"), and "control system" is control systems again.
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
    ("prose", "RN student completing a nursing practicum.",  # round 3: the licence concept (D-28)
     [("cred.registered_nurse", "RN"), ("skill.nursing", "nursing")]),  # nursing: a domain (D-27)
    ("prose", "Articling student at a Toronto law firm.", [("cred.articling_student", "Articling student")]),
    ("prose", "309A apprentice electrician.", [("cred.electrician_apprentice", "309A apprentice electrician")]),
    ("prose", "Teacher candidate in the B.Ed program.", [("cred.teacher_candidate", "Teacher candidate")]),
    ("prose", "P.Eng. candidate with four years of experience.",  # round 3: the licence concept
     [("cred.professional_engineer", "P.Eng")]),
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
    ("prose", "Strong design sense and a passion for architecture, history and philosophy.", []),  # D-27
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
    # ─── fix round 2 probes, read under round 3 (D-28): the licence concept, the status left to context ─
    ("prose", "Primary Care Paramedic student, Centennial College.",
     [("cred.paramedic_license", "Primary Care Paramedic")]),
    ("prose", "Advanced Care Paramedic student", [("cred.paramedic_license", "Advanced Care Paramedic")]),
    ("prose", "Licensed Practical Nurse student", [("cred.licensed_practical_nurse", "Licensed Practical Nurse")]),
    ("prose", "Enrolled in the RPN program at Fanshawe.", [("cred.licensed_practical_nurse", "RPN")]),
    ("prose", "Nursing student in the RN program at Humber College.",
     [("cred.nursing_student", "Nursing student"), ("cred.registered_nurse", "RN")]),
    ("prose", "Registered Nurse candidate awaiting the NCLEX.", [("cred.registered_nurse", "Registered Nurse")]),
    ("prose", "Class A CDL student", [("cred.commercial_drivers_license", "Class A CDL")]),
    ("prose", "309A Electrical Apprentice",
     [("cred.electrician_license", "309A"), ("cred.electrician_apprentice", "Electrical Apprentice")]),
    ("prose", "Apprentice Electrician (309A), Level 2",
     [("cred.electrician_apprentice", "Apprentice Electrician"), ("cred.electrician_license", "309A")]),
    ("prose", "Construction and Maintenance Electrician 309A apprentice",
     [("cred.electrician_license", "Construction and Maintenance Electrician 309A")]),
    ("prose", "Professional Engineer in training", [("cred.professional_engineer", "Professional Engineer")]),
    ("prose", "Enrolled in the CPA Canada PEP (Core 2).", [("cred.cpa_in_progress", "CPA Canada PEP")]),
    ("prose", "CPA Ontario PEP student", [("cred.cpa_in_progress", "CPA Ontario PEP")]),
    ("prose", "CPA (in progress)", [("cred.cpa", "CPA")]),
    # ─── fix round 3: status around a licence is context; the posting's "candidate" is the applicant ─
    ("prose", "R.N. (in progress)", [("cred.registered_nurse", "R.N")]),
    ("prose", "C.P.A. (in progress)", [("cred.cpa", "C.P.A")]),
    ("prose", "L.P.N. (in progress)", [("cred.licensed_practical_nurse", "L.P.N")]),
    ("prose", "P.Eng. (in progress)", [("cred.professional_engineer", "P.Eng")]),
    ("prose", "Registered Nurse (RN) candidate awaiting the NCLEX.",
     [("cred.registered_nurse", "Registered Nurse"), ("cred.registered_nurse", "RN")]),
    ("prose", "Primary Care Paramedic (PCP) student at Humber College.",
     [("cred.paramedic_license", "Primary Care Paramedic")]),
    ("prose", "Construction and Maintenance Electrician (309A) Apprentice", [("cred.electrician_license", "309A")]),
    ("prose", "Professional Engineer (P.Eng.) candidate",
     [("cred.professional_engineer", "Professional Engineer"), ("cred.professional_engineer", "P.Eng")]),
    ("prose", "CPA, in progress", [("cred.cpa", "CPA")]),
    ("prose", "RN (pending NCLEX)", [("cred.registered_nurse", "RN")]),
    ("prose", "CDL-A trainee", [("cred.commercial_drivers_license", "CDL")]),
    ("prose", "RN students", [("cred.registered_nurse", "RN")]),
    ("prose", "Ideal candidate: RN with 3+ years of acute care experience.",
     [("cred.registered_nurse", "RN"), ("esco.acute_care", "acute care")]),
    ("prose", "What we look for in a candidate: RN, BScN preferred.", [("cred.registered_nurse", "RN")]),
    ("prose", "Ideal Candidate - Registered Nurse (RN) with current CNO registration.",
     [("cred.registered_nurse", "Registered Nurse"), ("cred.registered_nurse", "RN")]),
    ("prose", "The ideal candidate: P.Eng. or EIT with five years of design experience.",
     [("cred.professional_engineer", "P.Eng"), ("cred.eit", "EIT")]),
    ("prose", "Preferred candidate: Ontario Certified Teacher with ESL Part 1.",
     [("cred.teaching_license", "Ontario Certified Teacher")]),
    ("prose", "Graduate of an accredited RN program with current registration.", [("cred.registered_nurse", "RN")]),
    ("prose", "Enrolled as a CPA PEP student in 2025.", [("cred.cpa_in_progress", "CPA PEP")]),
    ("prose", "Registered as an EIT with PEO.", [("cred.eit", "EIT")]),
    ("prose", "Passed CFA Level II in 2024.", [("cred.cfa_candidate", "CFA Level II")]),
    ("prose", "Passed the NCLEX-RN in 2024.", []),  # an exam, not the licence
    # a formal designation never spans ":" or a dash; the CFA charter's candidacy included
    ("prose", "Elementary Teacher - Candidate must hold OCT certification.", []),
    ("prose", "Designation: CFA - Level II or higher.", [("cred.cfa", "CFA")]),
    # ─── fix round 3: "design" only in multi-word names; bodies named like a domain (D-27) ───
    ("prose", "Design, build and maintain scalable backend services in Java.",
     [("skill.backend", "backend"), ("skill.java", "Java")]),
    ("prose", "You will help design our data pipelines.", [("skill.data_engineering", "data pipelines")]),
    ("prose", "Participated in design reviews.", []),
    ("prose", "Led the HVAC design for a hospital wing.", [("skill.hvac", "HVAC")]),
    ("prose", "Applied design thinking to the onboarding flow.", [("skill.design", "design thinking")]),
    ("prose", "Analyst at Statistics Canada.", []),
    ("prose", "Policy analyst, Department of Finance Canada.", []),
    # ─── fix round 3: no synonyms into fields, none through a generic word (D-26) ────────────
    ("prose", "Five years of experience in computer software development.",
     [("skill.software_development", "software development")]),
    ("prose", "Optimized industrial process design for a refinery.", [("esco.design_process", "process design")]),
    ("prose", "Maintained regulatory systems in pharma manufacturing.", []),
    ("prose", "Provided financial planning advice to clients.", []),
    ("prose", "Studied plasma mechanics in the lab.", [("esco.mechanics", "mechanics")]),
    ("prose", "Gestion du programme de bourses.", []),
    ("prose", "Ran battery analysis methods on cell samples.", []),  # no root cause analysis
    ("prose", "Led the marketing analysis for a product launch.", [("skill.marketing", "marketing")]),
    # ─── fix round 3: currency context (D-27) ────────────────────────────────────────────────
    ("prose", "Traded USD/CAD and EUR/USD currency pairs.", []),
    ("prose", "Produced 1,200 CAD drawings for the plant.", [("skill.cad", "CAD")]),
    ("prose", "Updated the 2024 CAD standards manual.", [("skill.cad", "CAD")]),
    ("list", "Microsoft Office 365, CAD, SolidWorks",  # a number before a list comma is no amount
     [("skill.ms_office", "Microsoft Office"), ("skill.cad", "CAD"), ("skill.solidworks", "SolidWorks")]),
    ("prose", "Experience with Fusion 360 CAD.", [("skill.fusion360", "Fusion 360"), ("skill.cad", "CAD")]),
    ("prose", "Managed a 2M CAD budget.", []),  # a magnitude, then a money word
    # ─── fix round 4: an amount before a currency code is money unless a noun of CAD work follows ─
    ("prose", "Pay range: 70,000 - 85,000 CAD depending on experience.", []),
    ("prose", "We offer 85,000 CAD plus benefits.", []),
    ("prose", "100,000 CAD base salary plus bonus.", []),
    ("prose", "Range: 90,000 - 110,000 CAD + equity", []),
    ("prose", "The expected range is 95,000 CAD to 120,000 CAD.", []),
    ("prose", "Pay range: 70,000 CAD - 85,000 CAD", []),
    ("prose", "Starting pay of 22.50 CAD with tips.", []),
    ("prose", "Base: 120,000 CAD | Bonus: 10%", []),
    ("prose", "OTE 140,000 CAD (50/50 split).", []),
    ("prose", "120K CAD + equity", []),
    ("prose", "Awarded a 2,500 CAD scholarship for academic excellence.", []),
    ("prose", "Raised 3,000 CAD for the food bank.", []),
    ("prose", "Won a 1,000 CAD prize at the science fair.", []),
    ("prose", "Paid 25 CAD/hr.", []),
    ("prose", "Rates quoted in CAD/h.", []),
    ("prose", "80,000 CAD/year", []),
    ("prose", "Earning 80,000 CAD annually.", []),
    ("prose", "Paid 25 CAD hourly.", []),
    ("prose", "The range is 120,000 - 150,000 CAD, plus bonus.", []),
    ("prose", "Budget of 1.2M CAD.", []),
    ("prose", "Managed 1,500 CAD licences for the design team.", [("skill.cad", "CAD")]),
    ("prose", "Experience with Fusion 360 CAD (cloud) and SolidWorks.",  # 360 belongs to the name before
     [("skill.fusion360", "Fusion 360"), ("skill.cad", "CAD"), ("skill.solidworks", "SolidWorks")]),
    # a rate phrase needs a number before the code
    ("prose", "Taught CAD annually to new hires.", [("skill.cad", "CAD")]),
    ("prose", "Learned CAD a year ago.", [("skill.cad", "CAD")]),
    ("prose", "Reviewed CAD hourly during the shift.", [("skill.cad", "CAD")]),
    # ─── fix round 4: control system, record information, teamwork (D-26) ─────────────────────
    ("prose", "Developed the control system for a robotic arm.", [("skill.control_systems", "control system")]),
    ("prose", "PLC programming, control system design, SCADA",
     [("skill.plc", "PLC programming"), ("skill.control_systems", "control system"), ("skill.scada", "SCADA")]),
    ("list", "PLC programming, control system design, SCADA",
     [("skill.plc", "PLC programming"), ("skill.control_systems", "control system"), ("skill.scada", "SCADA")]),
    ("prose", "Accurately record information in the inventory system.", []),
    ("prose", "Record information in the CRM after each call.", [("skill.crm", "CRM")]),
    ("prose", "Ability to work in groups and independently.", [("skill.teamwork", "work in groups")]),
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


def test_control_system_design_is_control_systems_not_system_design(resolver) -> None:
    # Fix round 4: "control system" is the curated domain (an alias, and the merged ESCO concept's
    # own label rephrased, D-26); "system design" (a software skill) never takes its second word.
    for text in ("Control system design and tuning (PID).", "PLC programming, control system design, SCADA"):
        found = [m.canonical_id for m in resolver.extract(text)]
        assert "skill.control_systems" in found and "skill.system_design" not in found, (text, found)
    assert resolver.resolve("control system").canonical_id == "skill.control_systems"


def test_design_disciplines_are_fields_in_education_lines(resolver) -> None:
    # Fix round 4 (D-27): interior and product design name the field of study too, so an education
    # line reads the field; general text reads the skill.
    for discipline in ("Interior Design", "Product Design", "Industrial Design"):
        line = f"Diploma in {discipline}, Humber College"
        assert [(m.canonical_id, m.surface) for m in resolver.extract(line, include_fields=True)] == [
            ("field.design", discipline)], line
        assert [(m.canonical_id, m.surface) for m in resolver.extract(line)] == [("skill.design", discipline)], line
        assert resolver.resolve(discipline, prefer_fields=True).canonical_id == "field.design"


def test_licences_in_progress_never_reach_the_licence(resolver, config) -> None:
    licences = set(config.gates.license.professional_credential_ids)
    for text in ("CPA candidate", "CPA PEP Core 1", "CPA Ontario PEP", "writing the CFE", "nursing student",
                 "student nurse", "EIT", "Engineer-in-Training", "articling student", "309A apprentice",
                 "teacher candidate", "PharmD student", "medical student", "commercial learner's permit",
                 "paramedic student", "CFA Level III", "C.P.A. candidate"):
        found = {m.canonical_id for m in resolver.extract(text, context="list")}
        assert found and not found & licences, (text, found)
