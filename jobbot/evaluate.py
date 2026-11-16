"""Synthetic ranking smoke evaluation with an explicit, versioned relevance judgment."""
import argparse
import json
from math import log2
from time import perf_counter

from .catalog import DEMO_CV, DEMO_SKILLS, DemoSource, collect_catalog
from .codex import CodexMatcher
from .matching import KeywordMatcher
from .pipeline import rank, shortlist_candidates

# Relevance labels are specific to this profile and intended role.
RELEVANCE = {"Applied AI Engineer": 3, "AI Platform Engineer": 1, "ML Infrastructure Engineer": 0}


def dcg(labels: list[int]) -> float:
    return sum((2 ** label - 1) / log2(index + 2) for index, label in enumerate(labels))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", action="store_true", help="Also evaluate using a local authenticated Codex CLI")
    args = parser.parse_args()
    catalog, _ = collect_catalog((DemoSource(),))
    candidates, _ = shortlist_candidates(catalog, "Germany", "remote")
    matchers = [KeywordMatcher(), CodexMatcher()] if args.codex else [KeywordMatcher()]
    for matcher in matchers:
        started = perf_counter()
        jobs = rank(candidates, DEMO_CV, DEMO_SKILLS, matcher)
        labels = [RELEVANCE[job["title"]] for job in jobs[:3]]
        print(json.dumps({
            "matcher": matcher.name,
            "ndcg_at_3": round(dcg(labels) / dcg(sorted(RELEVANCE.values(), reverse=True)[:3]), 3),
            "seconds": round(perf_counter() - started, 3),
            "scores": [{"title": job["title"], "score": job["score"], "scorer": job["scorer"]} for job in jobs[:3]],
        }))


if __name__ == "__main__":
    main()
