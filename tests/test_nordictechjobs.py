"""nordictechjobs robots compliance: ?page= / ?search= are never requested.

robots.txt disallows /*?page= and /*?search=, so the source must filter
such URLs out at discovery time. These tests are network-free.
"""
import unittest

from job_ingest.sources.nordictechjobs import (BANNED_QUERY_RE, JOB_URL_RE,
                                               _has_banned_query)


def _discoverable(entries):
    """Mirror of the discovery filter in fetch_nordictechjobs_jobs."""
    return [u for u, _ in entries
            if JOB_URL_RE.match(u) and not _has_banned_query(u)]


class TestBannedQueries(unittest.TestCase):
    def test_banned_query_detected(self):
        self.assertTrue(_has_banned_query(
            "https://nordictechjobs.com/jobs/abc123?page=2"))
        self.assertTrue(_has_banned_query(
            "https://nordictechjobs.com/jobs?search=python"))
        self.assertTrue(_has_banned_query(
            "https://nordictechjobs.com/some/path?x=1&page=3"))
        self.assertTrue(_has_banned_query(
            "https://nordictechjobs.com/jobs/abc123?SEARCH=go"))

    def test_clean_job_url_allowed(self):
        self.assertFalse(_has_banned_query(
            "https://nordictechjobs.com/jobs/29fFKSthRL"))
        self.assertFalse(_has_banned_query(
            "https://nordictechjobs.com/jobs/MV2J6wiM6a"))

    def test_job_url_pattern(self):
        self.assertIsNotNone(
            JOB_URL_RE.match("https://nordictechjobs.com/jobs/29fFKSthRL"))
        # Landing pages are NOT individual job URLs.
        self.assertIsNone(JOB_URL_RE.match(
            "https://nordictechjobs.com/jobs/english-speaking/sweden"))
        self.assertIsNone(JOB_URL_RE.match(
            "https://nordictechjobs.com/jobs/remote/nordics"))
        self.assertIsNone(JOB_URL_RE.match("https://nordictechjobs.com/jobs/"))

    def test_discovery_never_yields_banned_or_landing_urls(self):
        entries = [
            ("https://nordictechjobs.com/jobs/29fFKSthRL", "2026-10-16"),
            ("https://nordictechjobs.com/jobs/english-speaking/sweden", None),
            ("https://nordictechjobs.com/jobs/abc123?page=2", None),
            ("https://nordictechjobs.com/jobs?search=python", None),
            ("https://nordictechjobs.com/jobs/xyz?x=1&search=go", None),
        ]
        got = _discoverable(entries)
        self.assertEqual(got, ["https://nordictechjobs.com/jobs/29fFKSthRL"])
        for u in got:
            self.assertNotIn("page=", u)
            self.assertNotIn("search=", u)


if __name__ == "__main__":
    unittest.main()
