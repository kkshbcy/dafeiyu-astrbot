import unittest

from plugin.astrbot_plugin_dsh_companion.image_sizes import qq_seedream_size, seedream_pixels


class QQImageSizeTest(unittest.TestCase):
    def test_unspecified_and_unapproved_hd_stay_at_one_k(self):
        for message in (
            "发张自拍", "画一张高清自拍", "不用2K，给我拍一张", "不要 2048×2048",
            "2K 和 1.5K 什么区别", "用 2K 或者 1.5K 都行", "再来一张，像刚才那样",
        ):
            with self.subTest(message=message):
                self.assertEqual(qq_seedream_size(message), "1K")

    def test_exact_quality_in_the_current_qq_message(self):
        for text, expected in (
            ("来一张2K的自拍", "2K"),
            ("/生图 小鲸鱼 分辨率１．５ｋ", "1.5K"),
            ("1920 × 1080 的横图", "1920x1080"),
            ("请用 2048*2048", "2048x2048"),
            ("画个 1024x1024 自画像", "1024x1024"),
        ):
            with self.subTest(text=text):
                self.assertEqual(qq_seedream_size(text), expected)

    def test_unsupported_explicit_size_fails_locally(self):
        for text in ("自拍 3K", "画一张 512x512", "画成 3500x3500", "请用 100x10000"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                qq_seedream_size(text)

    def test_pixel_parser_checks_documented_area_and_aspect_ratio(self):
        self.assertEqual(seedream_pixels("1280x720"), "1280x720")
        self.assertIsNone(seedream_pixels("1279x720"))
        self.assertIsNone(seedream_pixels("2151x2150"))
        self.assertIsNone(seedream_pixels("10000x100"))
        self.assertIsNone(seedream_pixels("200000000x0"))
