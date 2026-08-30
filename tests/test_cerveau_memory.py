#!/usr/bin/env python3
"""
Unit tests for app/services/cerveau_memory.py — chunking, key stability, and
the HTTP contract with Cerveau's tenant-aware `POST /api/memory` (httpx mocked
out; no daemon, no Postgres, no embedding calls).

What these deliberately pin down, because each one is a silent-failure mode:
  - the tenant headers are ALWAYS sent, and a host `agent` is always named —
    the endpoint is fail-closed on both, but a missing header would be a 400
    per chunk rather than anything visible in the dashboard;
  - keys are deterministic per filename, which is what makes a re-upload an
    upsert instead of a duplicate;
  - a failure on the first base falls over to the second rather than losing
    the chunk.

Run: `python3 -m unittest tests.test_cerveau_memory` from the avry-backend root.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.services.cerveau_memory as m  # noqa: E402


def _response(status=200, text="ok"):
    res = MagicMock()
    res.status_code = status
    res.text = text
    return res


class Chunking(unittest.TestCase):
    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(m.chunk_text("   \n\n  "), [])

    def test_short_document_is_one_chunk(self):
        self.assertEqual(m.chunk_text("Refunds are processed in 5 days."),
                         ["Refunds are processed in 5 days."])

    def test_paragraphs_pack_up_to_the_size_limit(self):
        para = "x" * 700
        chunks = m.chunk_text(f"{para}\n\n{para}\n\n{para}", size=1500, overlap=100)
        # Two 700-char paragraphs fit in one 1500 chunk (700 + 2 + 700); the third starts a new one.
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= 1500 for c in chunks))

    def test_oversized_paragraph_is_hard_split_with_overlap(self):
        chunks = m.chunk_text("y" * 3200, size=1000, overlap=100)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        # Overlap means the pieces cover more than the original length.
        self.assertGreater(sum(len(c) for c in chunks), 3200)

    def test_chunk_count_is_capped(self):
        chunks = m.chunk_text("\n\n".join(["z" * 1400] * (m.MAX_CHUNKS + 50)))
        self.assertEqual(len(chunks), m.MAX_CHUNKS)


class Slugs(unittest.TestCase):
    def test_same_filename_gives_same_slug(self):
        self.assertEqual(m.slug_for("Price List 2026.pdf"), m.slug_for("Price List 2026.pdf"))

    def test_slug_is_key_safe_and_bounded(self):
        slug = m.slug_for("Harga/Produk — Q3 (final).xlsx" + "a" * 200)
        self.assertRegex(slug, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(slug), 48)

    def test_unnameable_file_still_gets_a_slug(self):
        self.assertEqual(m.slug_for("???"), "document")


class Ingest(unittest.TestCase):
    def _run(self, post, text="hello world", filename="faq.pdf"):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post = post
        with patch.object(m.httpx, "Client", return_value=client):
            return m.ingest_document("user_abc", "customer_service", filename, text)

    def test_stores_one_memory_per_chunk_with_tenant_headers(self):
        post = MagicMock(return_value=_response())
        stored, truncated = self._run(post, text="\n\n".join(["p" * 1400] * 3))

        self.assertEqual(stored, 3)
        self.assertFalse(truncated)
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertTrue(call.args[0].endswith("/api/memory"))
            headers = call.kwargs["headers"]
            self.assertEqual(headers["X-Tenant-Id"], "user_abc")
            self.assertEqual(headers["X-Agent-Type"], "customer_service")
            body = call.kwargs["json"]
            # A tenant-scoped write with no host agent is a 400 by design.
            self.assertEqual(body["agent"], m.CERVEAU_HOST_AGENT)
            # `core` is the durable tier: the Postgres lifecycle age-prunes
            # only `conversation` and `daily`.
            self.assertEqual(body["category"], "core")
            self.assertIn("[Document: faq.pdf — part", body["content"])

    def test_keys_are_deterministic_so_a_re_upload_upserts(self):
        post_a = MagicMock(return_value=_response())
        self._run(post_a, text="\n\n".join(["p" * 1400] * 2))
        post_b = MagicMock(return_value=_response())
        self._run(post_b, text="\n\n".join(["different content " * 60] * 2))

        keys_a = [c.kwargs["json"]["key"] for c in post_a.call_args_list]
        keys_b = [c.kwargs["json"]["key"] for c in post_b.call_args_list]
        self.assertEqual(keys_a, ["doc:faq-pdf:001", "doc:faq-pdf:002"])
        self.assertEqual(keys_a, keys_b)

    def test_oversized_document_is_reported_as_truncated(self):
        post = MagicMock(return_value=_response())
        stored, truncated = self._run(post, text="w" * (m.MAX_INGEST_CHARS + 10))
        self.assertTrue(truncated)
        self.assertGreater(stored, 0)

    def test_first_base_failure_falls_over_to_the_second(self):
        post = MagicMock(side_effect=[_response(503, "boom"), _response(200)])
        stored, _ = self._run(post)
        self.assertEqual(stored, 1)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[0].args[0].startswith(m.CERVEAU_BASES[0]))
        self.assertTrue(post.call_args_list[1].args[0].startswith(m.CERVEAU_BASES[1]))

    def test_every_base_failing_raises_rather_than_reporting_success(self):
        post = MagicMock(return_value=_response(400, "tenant headers must be sent together"))
        with self.assertRaises(m.IngestError):
            self._run(post)

    def test_text_with_no_content_raises(self):
        with self.assertRaises(m.IngestError):
            self._run(MagicMock(return_value=_response()), text="   ")


if __name__ == "__main__":
    unittest.main()
