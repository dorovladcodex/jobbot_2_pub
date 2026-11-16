"""Scoring contracts and a deterministic comparison baseline."""
from dataclasses import dataclass
import re
from typing import Protocol

SKILL_ALIASES = {"Apache Spark": ("Spark",), "Oracle Data Integrator": ("ODI",)}


def quote_for(skill: str, document: str) -> str:
    pattern = re.compile(r"(?<![\w])" + re.escape(skill) + r"(?![\w])", re.IGNORECASE)
    for fragment in re.split(r"(?<=[.!?])\s+|[\r\n]+", document):
        match = pattern.search(fragment)
        if match:
            return fragment[max(0, match.start() - 80):match.end() + 100].strip()
    return ""


def compact(text: str) -> str:
    return " ".join(text.split())


def skill_quote(skill: str, document: str) -> str:
    for term in (skill, *SKILL_ALIASES.get(skill, ())):
        quote = quote_for(term, document)
        if quote:
            return quote
    return ""


@dataclass(frozen=True)
class Evidence:
    skill: str
    cv_quote: str
    vacancy_quote: str


class Matcher(Protocol):
    name: str

    def match(self, cv: str, skills: list[str], description: str) -> list[Evidence]: ...


class KeywordMatcher:
    name = "keyword baseline"

    def match(self, cv: str, skills: list[str], description: str) -> list[Evidence]:
        evidence = []
        for skill in skills:
            cv_quote = skill_quote(skill, cv)
            vacancy_quote = skill_quote(skill, description)
            if cv_quote and vacancy_quote:
                evidence.append(Evidence(skill, cv_quote, vacancy_quote))
        return evidence


def validate_evidence(items: list[Evidence], cv: str, skills: list[str], description: str) -> list[Evidence]:
    approved = {skill.casefold(): skill for skill in skills}
    results = {}
    for item in items:
        skill = approved.get(item.skill.casefold())
        if skill and compact(item.cv_quote) and compact(item.vacancy_quote):
            if compact(item.cv_quote) in compact(cv) and compact(item.vacancy_quote) in compact(description):
                results.setdefault(skill, Evidence(skill, item.cv_quote, item.vacancy_quote))
    return list(results.values())
