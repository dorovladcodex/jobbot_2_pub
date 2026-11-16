"""Vacancy sources and canonical records."""
from dataclasses import dataclass
from collections.abc import Iterable
from hashlib import sha256
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True)
class Vacancy:
    company: str
    title: str
    url: str
    location: str
    mode: str
    description: str
    source: str
    salary_quote: str = ""
    language_quote: str = ""
    contract_quote: str = ""
    content_status: str = "source_text"


class Source(Protocol):
    name: str

    def collect(self) -> list[Vacancy]: ...


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None:
        raise ValueError("Invalid vacancy link")
    tracking = {"ref", "source", "gh_src", "fbclid", "gclid"}
    query = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                   if key.lower() not in tracking and not key.lower().startswith("utm_"))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


def collect_catalog(sources: tuple[Source, ...]) -> tuple[list[dict], list[str]]:
    vacancies = []
    unavailable = []
    for source in sources:
        try:
            vacancies.extend(source.collect())
        except (OSError, ValueError, KeyError, TypeError):
            unavailable.append(source.name)
    return merge_catalog(vacancies), unavailable


def merge_catalog(vacancies: Iterable[Vacancy]) -> list[dict]:
    catalog = {}
    for vacancy in vacancies:
        try:
            link = canonical_url(vacancy.url)
        except ValueError:
            continue
        if link not in catalog:
            catalog[link] = {
                "id": sha256(link.encode()).hexdigest()[:20],
                "company": vacancy.company, "title": vacancy.title, "url": link,
                "location": vacancy.location, "mode": vacancy.mode,
                "description": vacancy.description, "sources": [],
                "salary_quote": vacancy.salary_quote, "language_quote": vacancy.language_quote,
                "contract_quote": vacancy.contract_quote,
                "content_status": vacancy.content_status,
            }
        for field in ("salary_quote", "language_quote", "contract_quote"):
            if not catalog[link][field]:
                catalog[link][field] = getattr(vacancy, field)
        if vacancy.source not in catalog[link]["sources"]:
            catalog[link]["sources"].append(vacancy.source)
    return list(catalog.values())


DEMO_CV = """Alex Morgan
Built Python APIs with FastAPI and deployed services on Docker.
Created PostgreSQL pipelines and added LLM evaluation with Python.
Designed LangGraph workflows with human review and model monitoring."""

DEMO_SKILLS = ["Python", "FastAPI", "PostgreSQL", "LangGraph", "Docker", "LLM evaluation"]
DEMO_ROLES = ["Applied AI Engineer", "AI Platform Engineer", "Data Platform Engineer"]


class DemoSource:
    name = "synthetic catalog"

    def collect(self) -> list[Vacancy]:
        return [
            Vacancy("Harbor Systems", "Applied AI Engineer", "https://example.com/jobs/ai",
                    "Germany", "remote", "Build Python services with FastAPI. Design LangGraph workflows and own LLM evaluation. Working language: English. Permanent contract. €90,000–€110,000 per year.", self.name),
            Vacancy("Harbor Systems", "Applied AI Engineer", "https://example.com/jobs/ai?utm_medium=email",
                    "Germany", "remote", "Build Python services with FastAPI. Design LangGraph workflows and own LLM evaluation.", "synthetic mirror"),
            Vacancy("Delta Works", "Data Platform Engineer", "https://example.com/jobs/data",
                    "Berlin, Germany", "hybrid", "Build Python pipelines backed by PostgreSQL and ship with Docker.", self.name),
            Vacancy("Orbit Studio", "AI Platform Engineer", "https://example.com/jobs/platform",
                    "Germany", "remote", "Build Python services and deploy with Docker. Kubernetes experience required. German C1 required. Fixed-term contract.", self.name),
            Vacancy("Lakeside Labs", "ML Infrastructure Engineer", "https://example.com/jobs/ml",
                    "Canada", "remote", "Operate Kubernetes clusters and maintain Terraform modules.", self.name),
        ]
