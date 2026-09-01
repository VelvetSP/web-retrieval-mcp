"""Tavily provider mapping and tier-routing tests (no external network)."""

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


class TavilySearchTests(unittest.TestCase):
    def test_provider_is_strict(self):
        output = asyncio.run(server.web_search("query", provider="unknown"))
        self.assertIn("unsupported provider", output)

    def test_tavily_search_maps_results_and_discloses_approximations(self):
        captured: dict = {}

        def fake_search(query, count, options):
            captured.update(query=query, count=count, options=options)
            return {"results": [{
                "title": "Result",
                "url": "https://example.com/result",
                "text": "provider excerpt",
            }]}

        with mock.patch.object(server, "_tavily_search_sync", fake_search):
            output = asyncio.run(server.web_search(
                "query",
                num_results=99,
                mode="fast",
                category="publication",
                summary=True,
                sort_by_date=True,
                provider="tavily",
            ))
        self.assertEqual(captured["count"], 20)
        self.assertEqual(captured["options"]["search_depth"], "fast")
        self.assertIn("[served by: tavily search]", output)
        self.assertIn("no exact Tavily equivalent", output)
        self.assertIn("summary has no per-result Tavily equivalent", output)
        self.assertIn("sort_by_date is not supported by Tavily", output)
        self.assertIn("https://example.com/result", output)

    def test_tavily_failure_uses_firecrawl_fallback_with_provenance(self):
        def broken(*args, **kwargs):
            raise server.RetrievalError("synthetic Tavily failure")

        fallback = [{
            "title": "Fallback",
            "url": "https://example.com/fallback",
            "text": "fallback excerpt",
            "highlights": [],
        }]
        with (
            mock.patch.object(server, "_tavily_search_sync", broken),
            mock.patch.object(server, "_firecrawl_search_sync", return_value=fallback),
        ):
            output = asyncio.run(server.web_search("query", provider="tavily"))
        self.assertIn("served by: firecrawl search", output)
        self.assertIn("tavily unavailable: synthetic Tavily failure", output)

    def test_invalid_and_absurd_dates_degrade_safely_and_visibly(self):
        options, notices = server._tavily_search_options(
            mode="auto",
            recency_days=7,
            recency_hours=10**10,
            start_published_date="not-a-date",
            end_published_date="also-not-a-date",
            category=None,
            include_domains=None,
            exclude_domains=None,
        )
        self.assertIn("start_date", options)
        self.assertTrue(any("invalid start_published_date" in item for item in notices))
        self.assertTrue(any("invalid end_published_date" in item for item in notices))
        self.assertTrue(any("100-year bound" in item for item in notices))

    def test_domain_caps_are_disclosed(self):
        options, notices = server._tavily_search_options(
            mode="fast",
            recency_days=None,
            recency_hours=None,
            start_published_date=None,
            end_published_date=None,
            category=None,
            include_domains=[f"include-{index}.example" for index in range(25)],
            exclude_domains=[f"exclude-{index}.example" for index in range(25)],
        )
        self.assertEqual(len(options["include_domains"]), 20)
        self.assertEqual(len(options["exclude_domains"]), 20)
        self.assertIn("include_domains was capped at 20 entries", notices)
        self.assertIn("exclude_domains was capped at 20 entries", notices)


class TavilyFetchTests(unittest.TestCase):
    def setUp(self):
        self.old_cache = server._completed_fetch_cache
        server._completed_fetch_cache = server.CompletedFetchCache(enabled=False)

    def tearDown(self):
        server._completed_fetch_cache = self.old_cache

    @staticmethod
    async def _public_url(_url):
        return None

    def test_tavily_follows_local_browser_and_precedes_firecrawl(self):
        calls: list[str] = []

        def exa(*args):
            calls.append("exa")
            return "thin", "crawled"

        async def browser(*args):
            calls.append("camoufox")
            raise server.RetrievalError("browser failed")

        def tavily(*args):
            calls.append("tavily")
            return "T" * 300

        def firecrawl(*args):
            calls.append("firecrawl")
            return "F" * 300

        with (
            mock.patch.object(server, "_validate_public_url_async", self._public_url),
            mock.patch.object(server, "_exa_contents_sync", exa),
            mock.patch.object(server, "_camoufox_render", browser),
            mock.patch.object(server, "_tavily_extract_sync", tavily),
            mock.patch.object(server, "_firecrawl_sync", firecrawl),
        ):
            output = asyncio.run(server.web_fetch(
                "https://example.com", max_chars=5000, tavily=True
            ))
        self.assertEqual(calls, ["exa", "camoufox", "tavily"])
        self.assertIn("[served by: tavily]", output)

    def test_semantic_and_freshness_contracts_skip_tavily(self):
        calls: list[str] = []

        def exa(*args):
            calls.append("exa")
            return "", "crawled"

        def tavily(*args):
            calls.append("tavily")
            return "T" * 300

        def firecrawl(*args):
            calls.append("firecrawl")
            return "summary body " * 20

        with (
            mock.patch.object(server, "_validate_public_url_async", self._public_url),
            mock.patch.object(server, "_exa_contents_sync", exa),
            mock.patch.object(server, "_tavily_extract_sync", tavily),
            mock.patch.object(server, "_firecrawl_sync", firecrawl),
        ):
            semantic = asyncio.run(server.web_fetch(
                "https://example.com/a", mode="concise", tavily=True
            ))
            fresh = asyncio.run(server.web_fetch(
                "https://example.com/b", render="never", max_age_hours=0, tavily=True
            ))
        self.assertNotIn("tavily", calls)
        self.assertIn("semantic extraction is unsupported", semantic)
        self.assertIn("max_age_hours is unsupported", fresh)

    def test_environment_flag_is_strict_and_zero_is_false(self):
        with mock.patch.dict(os.environ, {"WEB_FETCH_TAVILY_TIER": "0"}):
            self.assertFalse(server._tavily_fetch_enabled(None))
        with mock.patch.dict(os.environ, {"WEB_FETCH_TAVILY_TIER": "maybe"}):
            output = asyncio.run(server.web_fetch("https://example.com"))
        self.assertIn("WEB_FETCH_TAVILY_TIER must be", output)

    def test_cache_identity_includes_tavily_routing(self):
        common = dict(
            cache_url="https://example.com",
            render="auto",
            mode="full",
            question=None,
            effective_max_chars=20_000,
            max_chars_omitted=True,
            explicit_large=False,
            max_age_hours=None,
        )
        self.assertNotEqual(
            make_fetch_plan(**common, tavily_enabled=False).key,
            make_fetch_plan(**common, tavily_enabled=True).key,
        )


class PublicOutputRedactionTests(unittest.TestCase):
    def test_display_url_redacts_userinfo_query_and_fragment_credentials(self):
        raw = (
            "https://alice:password@example.com/file?api_key=exa-secret&x=visible"
            "#/view?access_token=oauth-secret"
        )
        shown = server._display_url(raw)
        self.assertEqual(
            shown,
            "https://***@example.com/file?api_key=***&x=visible#/view?access_token=***",
        )
        for secret in ("alice", "password", "exa-secret", "oauth-secret"):
            self.assertNotIn(secret, shown)

    def test_search_result_and_grounding_urls_are_redacted(self):
        secret = "signed-value"
        raw = f"https://user:pass@example.com/page?X-Amz-Signature={secret}"
        output = server._render_search(
            "query",
            [{"url": raw, "title": "Result", "text": "body"}],
            output={
                "content": "answer",
                "grounding": [{"citations": [{"url": raw, "title": "Result"}]}],
            },
        )
        self.assertNotIn(secret, output)
        self.assertNotIn("user", output)
        self.assertNotIn("pass", output)
        self.assertIn("X-Amz-Signature=***", output)

    def test_fetch_header_redacts_signed_url(self):
        secret = "signed-value"
        raw = f"https://user:pass@example.com/page?token={secret}"

        async def public_url(_url):
            return None

        def exa(*_args):
            return "public body " * 30, "crawled"

        old_cache = server._completed_fetch_cache
        server._completed_fetch_cache = server.CompletedFetchCache(enabled=False)
        try:
            with (
                mock.patch.object(server, "_validate_public_url_async", public_url),
                mock.patch.object(server, "_exa_contents_sync", exa),
            ):
                output = asyncio.run(server.web_fetch(raw))
        finally:
            server._completed_fetch_cache = old_cache

        self.assertNotIn(secret, output)
        self.assertNotIn("user", output)
        self.assertNotIn("pass", output)
        self.assertIn("https://***@example.com/page?token=***", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
