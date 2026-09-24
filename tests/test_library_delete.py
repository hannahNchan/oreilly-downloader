import json
import tempfile
import unittest
from pathlib import Path

import config
from plugins.library import LibraryPlugin


class FakeOutput:
    def __init__(self, path: Path):
        self.path = path

    def get_default_dir(self) -> Path:
        return self.path


class LibraryDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.output = self.base / "output"
        self.library_root = self.output / "library"
        self.output.mkdir()
        self.previous_library_dir = config.LIBRARY_DIR
        config.LIBRARY_DIR = self.library_root
        self.plugin = LibraryPlugin()
        self.plugin.kernel = {"output": FakeOutput(self.output)}
        self.plugin.ensure_root()

    def tearDown(self):
        config.LIBRARY_DIR = self.previous_library_dir
        self.temp.cleanup()

    def make_published(self, work_id="d1abcdef", title="Libro de prueba"):
        obj = self.library_root / "objects" / work_id[:2] / work_id
        obj.mkdir(parents=True)
        (obj / "book.epub").write_bytes(b"epub")
        (obj / "cover.jpg").write_bytes(b"cover")
        (obj / "metadata.json").write_text(json.dumps({
            "work_id": work_id,
            "book_id": "123",
            "content_type": "book",
            "title": title,
            "authors": [],
            "publishers": [],
        }), encoding="utf-8")
        self.plugin.rebuild_index()
        return next(item for item in self.plugin.scan() if item["folder"] == work_id)

    def test_published_delete_removes_object_cover_and_index_entry(self):
        item = self.make_published()
        stale_shard = self.library_root / "objects" / "ff"
        stale_shard.mkdir()
        cover = self.library_root / "covers" / f"{item['folder']}.jpg"
        self.assertTrue(cover.exists())

        self.plugin.delete_item(item)
        self.plugin.finish_deletions({"library"})

        self.assertFalse(Path(item["path"]).exists())
        self.assertFalse(Path(item["path"]).parent.exists())
        self.assertFalse(stale_shard.exists())
        self.assertFalse(cover.exists())
        index = json.loads(
            (self.library_root / "index" / "library.json").read_text(encoding="utf-8"))
        self.assertEqual(index["items"], [])

    def test_local_delete_removes_only_indexed_output_folder(self):
        local = self.output / "libro-local"
        local.mkdir()
        (local / ".book_id").write_text("456", encoding="utf-8")
        (local / "book.txt").write_text("contenido", encoding="utf-8")
        item = next(
            entry for entry in self.plugin.scan(refresh=True)
            if entry["folder"] == "libro-local")

        self.plugin.delete_item(item)
        self.plugin.finish_deletions({"local"})

        self.assertFalse(local.exists())
        cache = json.loads(
            (self.output / ".library-index.json").read_text(encoding="utf-8"))
        self.assertEqual(cache["items"], [])

    def test_published_delete_rejects_path_outside_objects(self):
        outside = self.base / "do-not-delete"
        outside.mkdir()
        item = {
            "folder": outside.name,
            "location": "library",
            "rel": str(outside),
            "title": "Ruta manipulada",
        }

        with self.assertRaises(ValueError):
            self.plugin.delete_item(item)

        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
