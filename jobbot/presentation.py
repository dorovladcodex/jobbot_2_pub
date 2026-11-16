"""Compact vacancy facts derived from saved posting text and matching evidence."""
import re

from .pipeline import CEFR, CONTRACT_PATTERNS, ENGLISH_SIGNAL, GERMAN_LEVEL


NUMBER = r"\d[\d.,]*(?:\s?[kK])?"
MONEY = (
    r"(?:(?:€|EUR|\$|USD|£|GBP)\s*" + NUMBER
    + r"(?:\s*[-–—]\s*(?:€|EUR|\$|USD|£|GBP)?\s*" + NUMBER + r")?"
    + r"|" + NUMBER + r"(?:\s*[-–—]\s*" + NUMBER + r")?\s*(?:EUR|USD|GBP|€|£))"
)
SALARY = re.compile(
    r"(?:salary|base pay|pay range|compensation|gehalt|vergütung)[^.!?\n]{0,60}?" + MONEY
    + r"(?:\s*(?:gross|brutto))?(?:\s*(?:per year|annually|/year|/yr|p\.a\.|pro Jahr))?"
    + r"|" + MONEY + r"\s*(?:gross\s*|brutto\s*)?(?:per year|annually|/year|/yr|p\.a\.|pro Jahr)",
    re.I,
)
GERMAN_REQUIRED = re.compile(
    r"\b(?:(?:fluent|fließend\w*|verhandlungssicher\w*)\s+(?:German|Deutsch)"
    r"|(?:German|Deutsch)\s+(?:is\s+)?(?:required|mandatory|essential|erforderlich|fluent))\b", re.I,
)
OPTIONAL = re.compile(r"\b(?:optional|preferred|nice to have|a plus|von Vorteil|wünschenswert)\b", re.I)
CONTRACT_LABELS = {"permanent": "Permanent", "fixed_term": "Fixed-term", "freelance": "Freelance / B2B"}


def vacancy_presentation(job: dict, preferences: dict, skills: list[str]) -> dict:
    text = job.get("description", "")
    salary_text = job.get("salary_quote") or text
    salary_match = SALARY.search(salary_text)
    salary = salary_match[0].strip() if salary_match else ""
    # A separately requested salary quote can omit the surrounding salary heading.
    if not salary and job.get("salary_quote") and re.search(MONEY, salary_text, re.I):
        salary = salary_text.strip()
    if salary:
        salary = salary[re.search(MONEY, salary, re.I).start():]

    language_text = " ".join(filter(None, (text, job.get("language_quote", ""))))
    english = bool(ENGLISH_SIGNAL.search(language_text))
    required_levels = set()
    optional_german = False
    for match in GERMAN_LEVEL.finditer(language_text):
        context = language_text[max(0, match.start() - 25):match.end() + 35]
        if OPTIONAL.search(context):
            optional_german = True
        else:
            required_levels.add((match[1] or match[2]).upper())
    generic_german = bool(GERMAN_REQUIRED.search(language_text)) and not optional_german
    level = max(required_levels, key=CEFR.index) if required_levels else None
    languages = ["English"] if english else []
    if level:
        languages.append("German " + level)
    elif generic_german:
        languages.append("German required")
    elif optional_german:
        languages.append("German optional")
    language = " · ".join(languages)
    risk = None
    reason = "Language requirements are not specified in the available posting text."
    maximum = preferences.get("max_german_level")
    if level:
        risk = "high" if maximum and CEFR.index(level) > CEFR.index(maximum) else "medium"
        reason = "German " + level + (" exceeds your selected " + maximum + " limit." if risk == "high"
                                      else " is stated. Confirm that it suits your language level.")
    elif generic_german:
        risk = "high"
        reason = "German is required. Confirm the expected level with the employer."
    elif english:
        risk = "low"
        reason = "English is explicitly stated. No mandatory German requirement was found in the available text."

    contract_text = text + " " + (job.get("contract_quote") or "")
    contracts = [label for kind, label in CONTRACT_LABELS.items()
                 if re.search(CONTRACT_PATTERNS[kind], contract_text, re.I)]
    evidence = job.get("evidence", [])
    matched = {item["skill"].casefold() for item in evidence}
    not_evidenced = [skill for skill in skills if skill.casefold() not in matched]
    analysis = str(len(evidence)) + " selected skills have supporting quotes in both your CV and the posting."
    if not_evidenced:
        analysis += " Other selected skills need verification against the full vacancy."
    return {**job, "fit_score": job.get("fit_score"), "salary": salary or None,
            "language": language or None, "language_risk": risk, "language_risk_reason": reason,
            "contract_type": " / ".join(contracts) or None, "analysis": job.get("analysis") or analysis,
            "not_evidenced_skills": not_evidenced}
