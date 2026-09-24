"""You.com provider mapping tests (no external network)."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import web_retrieval_mcp.server as server
from web_retrieval_mcp.fetch_cache import make_fetch_plan


class YouSearchTests(unittest.TestCase):
    def test_provider_is_strict(self):
        output = asyncio.run(server.web_search("query", provider="unknown"))
        self.assertIn("unsupported provider", output)

    def test_you_search_maps_results_and_discloses_approximations(self):
        captured: dict = {}

        def fake_search(query, count, params):
            captured.update(query=query, count=count, params=params)
            return {"results": [{
                "title": "You Result",
                "url": "https://example.com/you-result",
                "snippet": "you.com snippet",
            }]}

        with mock.patch.object(server, "_you_search_sync", fake_search):
            output = asyncio.run(server.web_search(
                "query",
                num_results=99,
                mode="deep",
                provider="you",
            ))
        self.assertEqual(captured["count"], 20)
        self.assertIn("[served by: you search]", output)
        self.assertIn("mode='deep' has no You.com equivalent", output)
        self.assertIn("https://example.com/you-result", output)

    def test_you_failure_uses_firecrawl_fallback_with_provenance(self):
        def broken(*args, **kwargs):
            raise server.RetrievalError("synthetic You.com failure")

        fallback = [{
            "title": "Fallback",
            "url": "https://example.com/fallback",
            "text": "fallback excerpt",
            "highlights": [],
        }]
        with (
            mock.patch.object(server, "_you_search_sync", broken),
            mock.patch.object(server, "_firecrawl_search_sync", return_value=fallback),
        ):
            output = asyncio.run(server.web_search("query", provider="you"))
        self.assertIn("served by: firecrawl search", output)
        self.assertIn("you unavailable: synthetic You.com failure", output)

    def test_recency_and_domain_options_map_gracefully(self):
        params, notices = server._you_search_options(
            mode="auto",
            recency_days=7,
            recency_hours=None,
            start_published_date=None,
            end_published_date=None,
            category="news",
            include_domains=["example.com"],
            exclude_domains=["spam.example"],
        )
        self.assertIn("freshness", params)
        self.assertIn("search_type", params)
        self.assertEqual(params["search_type"], "news")
        self.assertIn("include_domains", params)
        self.assertIn("exclude_domains", params)

    def test_invalid_dates_degrade_safely(self):
        params, notices = server._you_search_options(
            mode="auto",
            recency_days=None,
            recency_hours=None,
            start_published_date="not-a-date",
            end_published_date="also-not-a-date",
            category=None,
            include_domains=None,
            exclude_domains=None,
        )
        self.assertTrue(any("invalid start_published_date" in item for item in notices))
        self.assertTrue(any("invalid end_published_date" in item for item in notices))

    def test_domain_caps_are_disclosed(self):
        params, notices = server._you_search_options(
            mode="auto",
            recency_days=None,
            recency_hours=None,
            start_published_date=None,
            end_published_date=None,
            category=None,
            include_domains=[f"include-{index}.example" for index in range(25)],
            exclude_domains=[f"exclude-{index}.example" for index in range(25)],
        )
        self.assertEqual(len(params["include_domains"].split(",")), 20)
        self.assertEqual(len(params["exclude_domains"].split(",")), 20)
        self.assertIn("include_domains was capped at 20 entries", notices)
        self.assertIn("exclude_domains was capped at 20 entries", notices)

    def test_key_resolution(self):
        """YDC_API_KEY is resolved through _you_key."""
        with mock.patch.dict(os.environ, {"YDC_API_KEY": "test-you-key-12345"}, clear=True):
            key = server._you_key()
        self.assertEqual(key, "test-you-key-12345")


if __name__ == "__main__":
    unittest.main()