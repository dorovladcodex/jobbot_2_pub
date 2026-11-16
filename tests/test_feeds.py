import json
import unittest
from unittest.mock import patch

from jobbot.feeds import PUBLIC_FEEDS
from jobbot.web_search import search_queries


class FeedTests(unittest.TestCase):
    def test_original_bot_feeds_keep_full_requirements_and_source_facts(self):
        payloads = {
            "feed:arbeitnow": {"data": [{
                "company_name": "Example", "title": "Data Engineer", "location": "Dortmund",
                "remote": True, "url": "https://example.com/jobs/1",
                "description": "<p>Python. Excellent communication skills in English.</p>",
                "job_types": ["permanent"],
            }]},
            "feed:remoteok": [{"legal": "Feed terms"}, {
                "company": "Example", "position": "Data Engineer", "location": "Germany",
                "url": "https://example.com/jobs/2", "description": "Python. German optional.",
            }],
            "feed:remotive": {"jobs": [{
                "company_name": "Example", "title": "Data Engineer", "candidate_required_location": "Europe",
                "url": "https://example.com/jobs/3", "description": "Python and SQL.", "salary": "EUR 90,000",
                "job_type": "full_time",
            }]},
        }
        for feed in PUBLIC_FEEDS:
            if feed.name == "feed:germantechjobs":
                data = b'<rss><channel><item><title>Data Engineer @ Example [EUR 90,000]</title><link>https://example.com/jobs/4</link><description>Location: Dortmund\nPython and SQL.</description></item></channel></rss>'
            else:
                data = json.dumps(payloads[feed.name]).encode()
            with self.subTest(feed=feed.name), patch("jobbot.feeds.read_feed", return_value=data):
                result = feed.collect()
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].company, "Example")
                self.assertIn("Python", result[0].description)
                self.assertEqual(result[0].source, feed.name)
        with patch("jobbot.feeds.read_feed", return_value=b"invalid XML"):
            with self.assertRaises(ValueError):
                PUBLIC_FEEDS[1].collect()

    def test_search_plan_covers_local_and_remote_without_sending_cv(self):
        queries = search_queries({
            "base_location": "Germany", "work_modes": ["remote", "hybrid"], "local_areas": ["NRW", "Dortmund"],
            "target_roles": ["Data Engineer", "Applied AI Engineer"],
        }, ("linkedin.com",))
        self.assertEqual(len(queries), 4)
        self.assertTrue(any('"Germany" remote' in query for query in queries))
        self.assertTrue(any('"NRW"' in query for query in queries))
        self.assertTrue(any('"Applied AI Engineer" "Dortmund"' in query for query in queries))
