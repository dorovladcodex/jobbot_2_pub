"""Public Greenhouse and Lever feeds adapted from the personal JobBot collectors."""
import html
import json
import re
from urllib.request import Request, urlopen

from .catalog import Vacancy


def plain(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(value)).split())[:16000]


class ATSBoard:
    def __init__(self, kind: str, slug: str):
        if kind not in {"greenhouse", "lever", "lever-eu"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", slug):
            raise ValueError("Invalid ATS board. Use greenhouse:slug, lever:slug or lever-eu:slug.")
        self.kind = kind
        self.slug = slug
        self.name = f"{kind}:{slug}"

    def collect(self) -> list[Vacancy]:
        if self.kind == "greenhouse":
            endpoint = f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs?content=true"
        else:
            host = "api.eu.lever.co" if self.kind == "lever-eu" else "api.lever.co"
            endpoint = f"https://{host}/v0/postings/{self.slug}?mode=json"
        request = Request(endpoint, headers={"User-Agent": "JobBot-Pub/0.1"})
        max_bytes = 8 * 1024 * 1024
        with urlopen(request, timeout=10) as response:
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("ATS response exceeds the size limit")
        body = json.loads(payload)
        if self.kind == "greenhouse":
            return [
                Vacancy(self.slug, item["title"], item["absolute_url"],
                        (item.get("location") or {}).get("name") or "", "unknown",
                        plain(item.get("content") or ""), self.name)
                for item in body["jobs"]
            ]
        return [
            Vacancy(self.slug, item["text"], item["hostedUrl"],
                    (item.get("categories") or {}).get("location") or "",
                    str(item.get("workplaceType") or "unknown").lower(),
                    plain(" ".join([item.get("descriptionPlain") or "", item.get("additionalPlain") or "",
                                    " ".join(part.get("content") or "" for part in item.get("lists") or [])])),
                    self.name)
            for item in body
        ]
