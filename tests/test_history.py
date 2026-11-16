from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from jobbot.catalog import Vacancy
from jobbot.history import saved_runs, vacancy_history
from jobbot.matching import KeywordMatcher
from jobbot.workflow import build_workflow


class HistoryTests(unittest.TestCase):
    def test_seen_jobs_move_to_history_and_survive_restart_and_empty_run(self):
        class ChangingSource:
            name = "test"
            names = ["first"]

            def collect(self):
                return [Vacancy("Example", name, "https://example.com/jobs/" + name + "?utm_source=test",
                                "Germany", "remote", "Python.", self.name) for name in self.names]

        source = ChangingSource()
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "history.sqlite")
            with SqliteSaver.from_conn_string(path) as saver:
                graph = build_workflow((source,), KeywordMatcher(), saver)
                first_config = {"configurable": {"thread_id": "first"}}
                first = graph.invoke({
                    "cv": "Python.", "skills": ["Python"], "mode": "remote", "location": "Germany",
                    "created_at": "2026-11-15T12:00:00+00:00",
                }, first_config)
                first_job = first["shortlist"][0]
                self.assertEqual(vacancy_history(saved_runs(saver)), [])
                graph.invoke(Command(resume=[first_job["id"]]), first_config)

            with SqliteSaver.from_conn_string(path) as saver:
                graph = build_workflow((source,), KeywordMatcher(), saver)
                source.names = ["first", "second"]
                seen_ids = [job["id"] for job in vacancy_history(saved_runs(saver), include_latest=True)]
                second = graph.invoke({
                    "cv": "Python.", "skills": ["Python"], "mode": "remote", "location": "Germany",
                    "seen_ids": seen_ids, "created_at": "2026-11-16T12:00:00+00:00",
                }, {"configurable": {"thread_id": "second"}})
                self.assertEqual([job["title"] for job in second["shortlist"]], ["second"])
                self.assertEqual(second["historical_count"], 1)
                self.assertEqual(second["excluded"], [])
                history = vacancy_history(saved_runs(saver))
                self.assertEqual([job["title"] for job in history], ["first"])
                self.assertTrue(history[0]["kept"])
                self.assertEqual(history[0]["first_seen_at"], "2026-11-15T12:00:00+00:00")
                seen_ids = [job["id"] for job in vacancy_history(saved_runs(saver), include_latest=True)]
                third = graph.invoke({
                    "cv": "Python.", "skills": ["Python"], "mode": "remote", "location": "Germany",
                    "seen_ids": seen_ids, "created_at": "2026-11-17T12:00:00+00:00",
                }, {"configurable": {"thread_id": "third"}})
                self.assertEqual(third["shortlist"], [])
                self.assertEqual(third["historical_count"], 2)
                self.assertEqual([job["title"] for job in vacancy_history(saved_runs(saver))], ["second", "first"])

    def test_history_orders_by_berlin_date_then_score_and_preserves_first_snapshot(self):
        runs = [
            {"id": "old", "created_at": "2026-11-14T12:00:00+00:00", "state": {
                "shortlist": [{"id": "old", "score": 100}], "reviewed_ids": [],
            }},
            {"id": "early", "created_at": "2026-11-15T23:30:00+00:00", "state": {
                "shortlist": [{"id": "high", "score": 90}], "reviewed_ids": [],
            }},
            {"id": "late", "created_at": "2026-11-16T10:00:00+00:00", "state": {
                "shortlist": [{"id": "low", "score": 30}, {"id": "high", "score": 10}], "reviewed_ids": [],
            }},
            {"id": "latest", "created_at": "2026-11-17T12:00:00+00:00", "state": {"shortlist": []}},
        ]
        history = vacancy_history(runs)
        self.assertEqual([job["id"] for job in history], ["high", "low", "old"])
        self.assertEqual(history[0]["score"], 90)
        self.assertEqual(history[0]["run_id"], "early")

    def test_legacy_checkpoints_without_date_are_included(self):
        class Source:
            name = "legacy"

            def collect(self):
                return [Vacancy("Example", "Engineer", "https://example.com/jobs/legacy",
                                "Germany", "remote", "Python.", self.name)]

        with SqliteSaver.from_conn_string(":memory:") as saver:
            graph = build_workflow((Source(),), KeywordMatcher(), saver)
            graph.invoke({"cv": "Python.", "skills": ["Python"], "mode": "remote", "location": "Germany"},
                         {"configurable": {"thread_id": "legacy"}})
            runs = saved_runs(saver)
            self.assertTrue(runs[0]["created_at"])
            self.assertEqual(vacancy_history(runs, include_latest=True)[0]["title"], "Engineer")
