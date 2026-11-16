"""Public feed adapters adapted from the original JobBot collectors."""
from dataclasses import dataclass
import json
import re
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .ats import plain
from .catalog import Vacancy


def read_feed(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "JobBot-Pub/0.1", "Accept": "application/json, application/xml"})
    with urlopen(request, timeout=15) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Feed exceeds the size limit")
    return data


@dataclass(frozen=True)
class PublicFeed:
    name: str
    label: str
    url: str

    def collect(self) -> list[Vacancy]:
        data = read_feed(self.url)
        vacancies = []
        if self.name == "feed:germantechjobs":
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError as error:
                raise ValueError("Invalid RSS feed") from error
            for item in root.findall("./channel/item"):
                heading = item.findtext("title", "")
                salary_match = re.search(r"\[([^]]+)\]\s*$", heading)
                salary = salary_match[1] if salary_match else ""
                heading = heading[:salary_match.start()].strip() if salary_match else heading
                title, _, company = heading.partition(" @ ")
                raw_text = item.findtext("description", "")
                location = re.search(r"(?:Location|Standort|City|Ort)\s*:\s*([^<\n]{1,100})", raw_text, re.I)
                vacancies.append(Vacancy(
                    plain(company) or "Company not specified", plain(title), item.findtext("link", ""),
                    plain(location[1]) if location else "", "unknown", plain(raw_text), self.name,
                    salary_quote=plain(salary),
                ))
            return vacancies
        payload = json.loads(data)
        if self.name == "feed:arbeitnow":
            items = payload["data"]
        elif self.name == "feed:remotive":
            items = payload["jobs"]
        else:
            items = payload
        for item in items:
            if not isinstance(item, dict) or "legal" in item:
                continue
            if self.name == "feed:remoteok":
                company, title = item.get("company", ""), item.get("position", "")
                location = item.get("location") or ""
                mode = "remote"
            else:
                company, title = item.get("company_name", ""), item.get("title", "")
                location = item.get("candidate_required_location") or item.get("location") or ""
                mode = "remote" if self.name == "feed:remotive" or item.get("remote") else "unknown"
            if not title or not item.get("url"):
                continue
            contract = item.get("job_type") or ", ".join(item.get("job_types") or [])
            salary = item.get("salary") if isinstance(item.get("salary"), str) else ""
            vacancies.append(Vacancy(
                plain(company) or "Company not specified", plain(title), item["url"], plain(location), mode,
                plain(item.get("description") or ""), self.name,
                salary_quote=plain(salary), contract_quote=plain(contract),
            ))
        return vacancies


PUBLIC_FEEDS = (
    PublicFeed("feed:arbeitnow", "Arbeitnow API", "https://www.arbeitnow.com/api/job-board-api"),
    PublicFeed("feed:germantechjobs", "GermanTechJobs RSS", "https://germantechjobs.de/rss"),
    PublicFeed("feed:remoteok", "RemoteOK API", "https://remoteok.com/api"),
    PublicFeed("feed:remotive", "Remotive API", "https://remotive.com/api/remote-jobs"),
)
