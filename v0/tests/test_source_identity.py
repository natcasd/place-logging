from __future__ import annotations

import unittest

from source_identity import canonical_source_url


class SourceIdentityTests(unittest.TestCase):
    def test_normalizes_instagram_post_variants(self) -> None:
        expected = "https://www.instagram.com/reel/AbC_123-/"
        variants = [
            "https://instagram.com/reel/AbC_123-",
            "https://www.instagram.com/reel/AbC_123-/?igsh=tracking",
            "https://m.instagram.com/reel/AbC_123-/?utm_source=share",
        ]
        self.assertEqual({canonical_source_url(url) for url in variants}, {expected})

    def test_keeps_instagram_post_kinds_distinct(self) -> None:
        self.assertNotEqual(
            canonical_source_url("https://instagram.com/p/same/"),
            canonical_source_url("https://instagram.com/reel/same/"),
        )

    def test_normalizes_youtube_video_variants(self) -> None:
        expected = "https://www.youtube.com/watch?v=AbC_123-"
        variants = [
            "https://youtu.be/AbC_123-?si=tracking",
            "https://www.youtube.com/watch?v=AbC_123-&feature=share",
            "https://m.youtube.com/shorts/AbC_123-?si=tracking",
            "https://youtube.com/embed/AbC_123-",
        ]
        self.assertEqual({canonical_source_url(url) for url in variants}, {expected})

    def test_normalizes_tiktok_video_variants_by_numeric_id(self) -> None:
        expected = "https://www.tiktok.com/@_/video/7668090902816017671"
        variants = [
            "https://www.tiktok.com/@creator/video/7668090902816017671?_r=1",
            "https://m.tiktok.com/@old_handle/video/7668090902816017671/",
            "https://www.tiktokv.com/share/video/7668090902816017671/?tracking=1",
        ]
        self.assertEqual({canonical_source_url(url) for url in variants}, {expected})

    def test_tiktok_photo_and_video_routes_share_one_post_identity(self) -> None:
        expected = "https://www.tiktok.com/@_/video/7685866670224706847"
        variants = [
            "https://www.tiktok.com/@craftedbyfabio/photo/7685866670224706847?_r=1",
            "https://www.tiktok.com/@craftedbyfabio/video/7685866670224706847",
            "https://m.tiktok.com/@oldhandle/photo/7685866670224706847/",
        ]

        self.assertEqual({canonical_source_url(url) for url in variants}, {expected})

    def test_normalizes_opaque_tiktok_share_link_tracking(self) -> None:
        self.assertEqual(
            canonical_source_url("https://vt.tiktok.com/ZSVv88Y6S/?share_app_id=123#x"),
            "https://vt.tiktok.com/ZSVv88Y6S/",
        )

    def test_only_removes_fragment_from_unsupported_urls(self) -> None:
        self.assertEqual(
            canonical_source_url("https://example.com/a?x=1#section"),
            "https://example.com/a?x=1",
        )


if __name__ == "__main__":
    unittest.main()
