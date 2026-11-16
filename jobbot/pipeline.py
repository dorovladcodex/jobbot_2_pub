"""Pure matching stages shared by the workflow and the offline evaluation."""
from dataclasses import asdict
import re
import math
import subprocess
import unicodedata

from .matching import Evidence, KeywordMatcher, Matcher, quote_for, validate_evidence


CEFR = ("A1", "A2", "B1", "B2", "C1", "C2")
GERMAN_LEVEL = re.compile(
    r"\b(?:German|Deutsch)\s*(?:language\s*)?(?:level\s*)?[:(]?\s*(A[12]|B[12]|C[12])\b"
    r"|\b(A[12]|B[12]|C[12])(?:[- ]level)?\s+(?:German|Deutsch)\b",
    re.IGNORECASE,
)
ANNUAL_EUR = re.compile(
    r"(?:€|EUR\s*)\s*(\d{2,3}(?:[.,]\d{3}|\s?[kK])|\d{5,6})"
    r"(?:\s*[-–]\s*(?:€|EUR\s*)?\s*(\d{2,3}(?:[.,]\d{3}|\s?[kK])|\d{5,6}))?"
    r"\s*(?:gross\s*)?(?:per year\b|annually\b|/year\b|/yr\b|p\.a\.|pro Jahr\b)",
    re.IGNORECASE,
)
CONTRACT_PATTERNS = {
    "permanent": r"\b(?:permanent (?:role|position|contract|employment|employee)|full[- ]?time permanent"
                 r"|open-ended contract|feste Anstellung|unbefristet\w*)\b",
    "fixed_term": r"\b(?:fixed[- ]term (?:role|position|contract|employment)|befristet\w*)\b",
    "freelance": r"\b(?:freelance (?:role|position|contract)|freelancer|B2B contract)\b",
}
ENGLISH_SIGNAL = re.compile(
    r"\b(?:working language\s*(?:is|:)\s*English|English\s*(?:is\s*)?(?:required|essential)|"
    r"fluent(?:\s+in)?\s+English|fluency\s+in\s+(?:the\s+)?English(?:\s+language)?|English\s+(?:A[12]|B[12]|C[12])"
    r"|(?:A[12]|B[12]|C[12])\s+English|English[- ]speaking team|Arbeitssprache\s*(?:ist|:)\s*Englisch"
    r"|(?:excellent|strong|good|very good)\s+(?:(?:written|verbal|spoken|oral|communication|presentation|and|in|skills)\s+){0,10}English"
    r"|English\s+(?:communication|presentation)\s+skills"
    r"|(?:gute|fließende|verhandlungssichere)\s+Englischkenntnisse)\b",
    re.I,
)
ENGLISH_UNKNOWN = "English working language unverified"
ROLE_FAMILIES = (
    re.compile(
        r"\b(?:applied|forward[- ]deployed|generative) AI engineer\b"
        r"|\b(?:AI data|AI platform|LLM|MLOps|artificial intelligence) engineer\b"
        r"|^(?:(?:senior|sr\.?|staff|lead|principal) )?AI engineer\b", re.I,
    ),
    re.compile(r"\b(?:data engineer|data engineering|data platform engineer)\b"
               r"|\bplatform engineer\b.{0,12}\bdata\b", re.I),
)
REMOTE_TITLE_AREA = re.compile(r"\bremote (?:eligible )?(?:in|within|from) ([^()]+)(?:\)|$)", re.I)
TITLE_WORK_MODE = re.compile(r"\b(hybrid|on[- ]site) (?:in|from)\b|\((remote|hybrid|on[- ]site)\)", re.I)
AREA_ALIASES = {
    "de": "germany", "deutschland": "germany",
    "the uk": "uk", "united kingdom": "uk", "gb": "uk",
    "dusseldorf": "duesseldorf", "cologne": "koln", "koeln": "koln",
    "north rhine-westphalia": "nrw", "nordrhein-westfalen": "nrw",
    "united states": "usa", "us": "usa", "the us": "usa",
}
REMOTE_COUNTRIES = {
    "germany", "uk", "usa", "canada", "india", "bulgaria", "costa rica",
    "netherlands", "france", "poland", "spain", "ireland", "portugal", "austria", "switzerland",
}
REMOTE_REGIONS = {"eu", "europe", "european union", "emea", "eea", "european economic area"}
NRW_AREAS = (
    "nrw", "aachen", "bielefeld", "bochum", "bonn", "dortmund", "duisburg", "duesseldorf",
    "essen", "gelsenkirchen", "hagen", "koln", "krefeld", "leverkusen", "monchengladbach",
    "mulheim", "munster", "neuss", "oberhausen", "paderborn", "recklinghausen", "siegen",
    "witten", "wuppertal",
    "frechen", "ratingen", "hilden", "herne", "bottrop", "castrop-rauxel", "holzwickede",
    "kamp-lintfort", "moers",
)


def normalized_area(value: str) -> str:
    plain = "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                    if not unicodedata.combining(char))
    return re.sub(r"\b\w+(?:[ -]\w+)*\b",
                  lambda match: AREA_ALIASES.get(match[0], match[0]), plain.strip())


def matches_local_area(location: str, areas: list[str]) -> bool:
    places = [normalized_area(part) for part in re.split(r"[,/;|]", location)]
    for area in areas:
        normalized = normalized_area(area)
        names = NRW_AREAS if normalized == "nrw" else (normalized,)
        if any(quote_for(name, place) for name in names for place in places):
            return True
    return False


def matches_role(role: str, title: str) -> bool:
    # Seniority is checked separately so a Senior target can match a role without that prefix.
    role = re.sub(r"^(?:senior|sr\.?|staff|principal|lead)\s+", "", role.strip(), flags=re.I)
    return bool(quote_for(role, title)) or any(
        family.search(role) and family.search(title) for family in ROLE_FAMILIES
    )


def preference_checks(vacancy: dict, preferences: dict, reasons: list[str], unknown: list[str]) -> None:
    title = vacancy["title"]
    description = " ".join(filter(None, (
        vacancy["description"], vacancy.get("salary_quote"), vacancy.get("language_quote"),
        vacancy.get("contract_quote"),
    )))
    roles = preferences.get("target_roles", [])
    if roles and not any(matches_role(role, title) for role in roles):
        reasons.append("title does not match target roles")

    if preferences.get("seniority") == "senior_plus":
        if re.search(r"\b(?:junior|intern|internship|werkstudent|working student|trainee|mid[- ]level)\b", title, re.I):
            reasons.append("below senior level")
        elif not re.search(r"\b(?:senior|sr|staff|principal|lead|head|director)\b", title, re.I):
            unknown.append("seniority unverified")

    if preferences.get("language") == "english" and not ENGLISH_SIGNAL.search(description):
        unknown.append(ENGLISH_UNKNOWN)

    max_german = preferences.get("max_german_level")
    if max_german:
        levels = []
        for match in GERMAN_LEVEL.finditer(description):
            context = description[max(0, match.start() - 25):match.end() + 30]
            if not re.search(r"\b(?:optional|preferred|nice to have|a plus|von Vorteil)\b", context, re.I):
                levels.append(CEFR.index((match.group(1) or match.group(2)).upper()))
        if levels and min(levels) > CEFR.index(max_german):
            reasons.append("German requirement exceeds your level")
        elif not levels and re.search(r"\b(?:German|Deutsch)\s+(?:is\s+)?(?:required|mandatory|essential)\b", description, re.I):
            unknown.append("minimum German level unverified")

    contract = preferences.get("contract_type")
    if contract and contract != "any":
        known = {kind for kind, pattern in CONTRACT_PATTERNS.items() if re.search(pattern, description, re.I)}
        if not known:
            unknown.append("contract type unverified")
        elif contract not in known:
            reasons.append("contract type differs")

    minimum = preferences.get("min_salary_eur")
    if minimum:
        amounts = [
            int(amount.lower().replace(" ", "").replace("k", "000").replace(",", "").replace(".", ""))
            for match in ANNUAL_EUR.finditer(description)
            for amount in match.groups() if amount
        ]
        if not amounts:
            unknown.append("annual EUR salary unverified")
        elif max(amounts) < minimum:
            reasons.append("advertised salary below target")


def shortlist_candidates(
    vacancies: list[dict], location: str, mode: str, preferences: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    preferences = preferences or {}
    modes = preferences.get("work_modes") or ([mode] if mode != "any" else [])
    local_areas = preferences.get("local_areas", [])
    included, excluded = [], []
    for vacancy in vacancies:
        unknown = []
        reasons = []
        title_area = REMOTE_TITLE_AREA.search(vacancy["title"])
        work_mode = vacancy["mode"]
        if work_mode == "unknown":
            title_mode = TITLE_WORK_MODE.search(vacancy["title"])
            if title_area:
                work_mode = "remote"
            elif title_mode:
                work_mode = (title_mode[1] or title_mode[2]).lower().replace("-", "").replace(" ", "")
        if modes and work_mode not in modes:
            if work_mode in {"remote", "hybrid", "onsite"}:
                reasons.append("work mode differs")
            else:
                unknown.append("work mode unverified")
        if work_mode in {"remote", "unknown"}:
            if location:
                restriction = re.search(
                    r"\bremote\s+(?:within|only in)\s+([a-z][a-z .-]{1,40}?)(?=[.!?;,]|\s+only\b|$)",
                    vacancy["description"], re.I,
                )
                title_areas = {normalized_area(area) for area in re.split(
                    r"\s*[,/&]\s*|\s+(?:and|or)\s+", title_area[1], flags=re.I,
                )} if title_area else set()
                restricted_area = normalized_area(restriction[1]) if restriction else ""
                listed_area = normalized_area(vacancy["location"])
                base_area = normalized_area(location)
                listed_countries = {
                    country for country in REMOTE_COUNTRIES
                    if quote_for(country, listed_area)
                    or any(quote_for(alias, listed_area) for alias, canonical in AREA_ALIASES.items()
                           if canonical == country)
                }
                title_excludes_base = title_areas and title_areas <= REMOTE_COUNTRIES and base_area not in title_areas
                description_excludes_base = (
                    not title_areas and restricted_area and restricted_area == listed_area
                    and restricted_area not in REMOTE_REGIONS and restricted_area != base_area
                )
                listed_excludes_base = (
                    work_mode == "remote" and listed_countries and base_area not in listed_countries
                    and not title_areas and not quote_for(base_area, restricted_area)
                )
                if title_excludes_base or description_excludes_base or listed_excludes_base:
                    reasons.append("remote location restriction differs")
                elif (base_area not in title_areas and base_area not in listed_countries
                      and base_area not in {listed_area, restricted_area}
                      and listed_area not in {"worldwide", "global", "anywhere"}):
                    unknown.append("location eligibility unverified")
        elif local_areas or location:
            if not vacancy["location"]:
                unknown.append("local work location unverified")
            elif not matches_local_area(vacancy["location"], local_areas or [location]):
                reasons.append("location differs")
        preference_checks(vacancy, preferences, reasons, unknown)
        if reasons:
            excluded.append({"id": vacancy["id"], "title": vacancy["title"], "reasons": reasons})
        else:
            included.append({**vacancy, "mode": work_mode, "unknown": unknown})
    return included, excluded


def rank(
    vacancies: list[dict], cv: str, skills: list[str], matcher: Matcher, budget: int = 6,
    preferences: dict | None = None,
) -> list[dict]:
    baseline = KeywordMatcher()
    candidates = [
        (vacancy, baseline.match(cv, skills, vacancy["description"]))
        for vacancy in vacancies
    ]
    candidates.sort(key=lambda item: (-len(item[1]), item[0]["id"]))
    assessments = {}
    assessment_method = getattr(matcher, "assess_many", None)
    assessment_failure = False
    if assessment_method and candidates:
        selected = [vacancy for vacancy, _ in candidates[:40]]
        allowed = {vacancy["id"] for vacancy in selected}
        try:
            raw_assessments = assessment_method(cv, skills, selected, preferences or {})
            for item in raw_assessments:
                if not isinstance(item, dict):
                    continue
                fit = item.get("fit_score")
                if (item.get("id") in allowed and isinstance(fit, (int, float))
                        and not isinstance(fit, bool) and 0 <= fit <= 10 and math.isfinite(fit)
                        and isinstance(item.get("analysis"), str) and item["analysis"].strip()
                        and isinstance(item.get("recommendation"), str) and item["recommendation"].strip()
                        and isinstance(item.get("evidence"), list)):
                    assessments[item["id"]] = item
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            assessment_failure = True
    scored = []
    for index, (vacancy, keyword_evidence) in enumerate(candidates):
        evidence = keyword_evidence
        scorer = baseline.name
        assessment = assessments.get(vacancy["id"])
        if assessment:
            try:
                raw = [Evidence(**item) for item in assessment["evidence"]]
                evidence = validate_evidence(raw, cv, skills, vacancy["description"])
                if (raw and not evidence) or (assessment["fit_score"] >= 7 and not evidence):
                    assessment = None
                    scorer = "keyword fallback (invalid evidence)"
                    evidence = keyword_evidence
                else:
                    scorer = matcher.name
            except (TypeError, KeyError, AttributeError):
                assessment = None
                scorer = "keyword fallback (invalid assessment)"
        elif assessment_failure:
            scorer = "keyword fallback (model unavailable)"
        elif matcher.name != baseline.name and not assessment_method and index < budget:
            try:
                raw = matcher.match(cv, skills, vacancy["description"])
                evidence = validate_evidence(raw, cv, skills, vacancy["description"])
                scorer = matcher.name
                if raw and not evidence:
                    evidence = keyword_evidence
                    scorer = "keyword fallback (invalid evidence)"
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                scorer = "keyword fallback (model unavailable)"
        score = round(100 * len(evidence) / len(skills)) if skills else 0
        scored.append({**vacancy, "score": score, "scorer": scorer,
                       "fit_score": round(assessment["fit_score"], 1) if assessment else None,
                       "analysis": assessment["analysis"][:1600] if assessment else None,
                       "recommendation": assessment["recommendation"][:1000] if assessment else None,
                       "evidence": [asdict(item) for item in evidence]})
    return sorted(scored, key=lambda item: (
        item["fit_score"] is None, -(item["fit_score"] or 0), -item["score"],
        ENGLISH_UNKNOWN in item.get("unknown", []), item["id"],
    ))
