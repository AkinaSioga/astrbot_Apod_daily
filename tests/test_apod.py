import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


# Load the plugin logic without requiring a running AstrBot installation.
source = Path(__file__).resolve().parents[1] / "main.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
tree.body = [node for node in tree.body if not (
    isinstance(node, ast.ImportFrom) and node.module.startswith("astrbot")
)]
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == "ApodDailyPlugin":
        node.decorator_list = []
        for method in node.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method.decorator_list = [d for d in method.decorator_list
                                         if isinstance(d, ast.Name) and d.id == "staticmethod"]
scope = dict(Star=object, Context=object, AstrMessageEvent=object, logger=Mock())
exec(compile(tree, str(source), "exec"), scope)
Plugin = scope["ApodDailyPlugin"]


def record(date="2026-10-08", **extra):
    return dict(date=date, title="Saturn &amp; moons", media_type="image",
                hdurl="https://example.com/saturn.png?v=2", explanation="<strong>Explanation:</strong> A <a href='x'>moon</a>.<p>Next &amp; last.</p>", **extra)


class ApodTests(unittest.TestCase):
    def test_latest_and_html(self):
        result = Plugin._normalize_apod([record("2026-10-07"), record()])
        self.assertEqual(result["date"], "2026-10-08")
        self.assertEqual(result["title"], "Saturn & moons")
        self.assertEqual(result["explanation"], "A moon. Next & last.")

    def test_invalid_records(self):
        for data in ([], {}, [None], record("2026-02-30")):
            with self.subTest(data=data), self.assertRaises((ValueError, RuntimeError)):
                Plugin._normalize_apod(data)

    def test_no_page_or_logo_as_image(self):
        for url in ("", "https://example.com/nasa-logo@2x.png"):
            data = record()
            data.update(hdurl=url, url="https://example.com/article")
            with self.assertRaises(RuntimeError):
                Plugin._normalize_apod(data)

    def test_non_image_does_not_download_or_translate(self):
        for kind in ("video", "iframe"):
            data = record()
            data.update(media_type=kind, hdurl="")
            plugin = self.plugin()
            plugin._fetch_apod = lambda: Plugin._normalize_apod(data)
            plugin.download_image_if_needed = Mock(side_effect=AssertionError)
            plugin.translate_with_llm = Mock(side_effect=AssertionError)
            result = asyncio.run(plugin.get_apod_payload())
            self.assertEqual(result["local_image_path"], "")
            self.assertEqual(result["media_url"], "")

    def plugin(self):
        plugin = Plugin.__new__(Plugin)
        plugin.platform_id = "test"
        plugin.explanation_word_limit = 250
        plugin.load_cache = lambda: {}
        plugin.save_cache = Mock()
        return plugin

    def test_migration_preserves_push_history_and_cleans_before_llm(self):
        plugin = self.plugin()
        data = Plugin._normalize_apod(record())
        plugin._fetch_apod = lambda: data
        plugin.load_cache = lambda: dict(date=data["date"], media_url=data["hdurl"],
                                        pushed_groups={"123": data["date"]})
        seen = []
        async def translate(event, title, explanation):
            seen.append(explanation)
            return "土星", "说明"
        plugin.translate_with_llm = translate
        plugin.download_image_if_needed = Mock(return_value="new.png")
        result = asyncio.run(plugin.get_apod_payload())
        self.assertEqual(seen, ["A moon. Next & last."])
        self.assertTrue(plugin.has_pushed_today(result, "123"))
        self.assertTrue(plugin.is_cache_hit(data, result))
        plugin.load_cache = lambda: result
        asyncio.run(plugin.get_apod_payload())
        self.assertEqual(len(seen), 1)

    def test_image_url_change_and_reject_html(self):
        plugin = self.plugin()
        with tempfile.TemporaryDirectory() as folder:
            plugin.images_dir = Path(folder)
            (plugin.images_dir / "2026-10-08.png").write_bytes(b"old logo")
            response = Mock(headers={"content-type": "image/png"}, content=b"new image")
            with patch.object(scope["httpx"], "get", return_value=response) as get:
                first = plugin.download_image_if_needed("2026-10-08", "https://example.com/a.png")
                self.assertEqual(Path(first).read_bytes(), b"new image")
                self.assertEqual(plugin.download_image_if_needed("2026-10-08", "https://example.com/a.png"), first)
                self.assertEqual(get.call_count, 1)
                second = plugin.download_image_if_needed("2026-10-08", "https://example.com/b.png")
                self.assertNotEqual(first, second)
                response.headers = {"content-type": "text/html"}
                self.assertEqual(plugin.download_image_if_needed("2026-10-08", "https://example.com/c.png"), "")


if __name__ == "__main__":
    unittest.main()
