import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deerflow.config.paths import Paths
from deerflow.thread_client import DeerFlowThreadClient
from deerflow.uploads.companions import register_companion
from deerflow.uploads.manager import UnsafeUploadPathError


class ThreadClientFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.paths = Paths(temporary)
        self.enterContext(patch("deerflow.thread_client.get_paths", return_value=self.paths))
        self.enterContext(patch("deerflow.thread_client.load_client_config"))
        self.client = DeerFlowThreadClient()
        self.uploads = self.paths.sandbox_uploads_dir("thread_001", user_id="user_001")
        self.uploads.mkdir(parents=True)

    def test_upload_download_overwrite_and_delete(self):
        for content in (b"", b"\x00\xff\x80\r\n"):
            with self.subTest(content=content):
                self.client.upload_file("user_001", "thread_001", "资料.txt", content)
                self.assertEqual(self.client.thread_files("user_001", "thread_001"), ["资料.txt"])
                self.assertEqual(self.client.download_file("user_001", "thread_001", "资料.txt"), content)
        self.client.delete_file("user_001", "thread_001", "资料.txt")
        self.assertEqual(self.client.thread_files("user_001", "thread_001"), [])
        with self.assertRaises(FileNotFoundError):
            self.client.download_file("user_001", "thread_001", "资料.txt")

    def test_all_file_operations_reject_paths(self):
        names = ["uploads/test.txt", "data/test.txt", "/tmp/test.txt", "../test.txt", "./test.txt", "data\\test.txt", "C:\\test.txt", "test.txt/", ".", "..", "", None, Path("test.txt")]
        (self.uploads / "test.txt").write_bytes(b"original")
        for name in names:
            for operation in (self.client.upload_file, self.client.download_file, self.client.delete_file):
                arguments = (b"changed",) if operation == self.client.upload_file else ()
                with self.subTest(name=name, operation=operation.__name__), self.assertRaises(ValueError):
                    operation("user_001", "thread_001", name, *arguments)
        self.assertEqual((self.uploads / "test.txt").read_bytes(), b"original")

    def test_listing_excludes_nested_files_and_converted_companions(self):
        (self.uploads / "data").mkdir()
        (self.uploads / "data/test.txt").write_bytes(b"nested")
        for name in ("test.txt", "report.pdf", "report.md", "notes.md", ".upload-test.part"):
            (self.uploads / name).write_bytes(b"root")
        register_companion(self.uploads / "report.pdf", self.uploads / "report.md")
        self.assertEqual(self.client.thread_files("user_001", "thread_001"), ["notes.md", "report.pdf", "test.txt"])
        self.assertEqual(self.client.download_file("user_001", "thread_001", "test.txt"), b"root")
        self.client.delete_file("user_001", "thread_001", "test.txt")
        with self.assertRaises(FileNotFoundError):
            self.client.download_file("user_001", "thread_001", "test.txt")
        self.assertEqual((self.uploads / "data/test.txt").read_bytes(), b"nested")
        self.client.delete_file("user_001", "thread_001", "report.pdf")
        self.assertFalse((self.uploads / "report.md").exists())
        self.assertEqual(self.client.thread_files("user_001", "thread_001"), ["notes.md"])

    def test_download_is_scoped_to_user_and_thread(self):
        (self.uploads / "test.txt").write_bytes(b"private")
        for user_id, thread_id in (("user_002", "thread_001"), ("user_001", "thread_002")):
            uploads = self.paths.sandbox_uploads_dir(thread_id, user_id=user_id)
            uploads.mkdir(parents=True)
            with self.subTest(user_id=user_id, thread_id=thread_id), self.assertRaises(FileNotFoundError):
                self.client.download_file(user_id, thread_id, "test.txt")

    def test_download_and_listing_handle_missing_thread_or_uploads(self):
        with self.assertRaises(FileNotFoundError):
            self.client.download_file("user_001", "missing", "test.txt")
        self.assertEqual(self.client.thread_files("user_001", "missing"), [])
        self.uploads.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.client.download_file("user_001", "thread_001", "test.txt")
        self.assertEqual(self.client.thread_files("user_001", "thread_001"), [])

    def test_download_rejects_directory_and_symlink(self):
        outside = self.paths.base_dir / "outside.txt"
        outside.write_bytes(b"private")
        (self.uploads / "directory").mkdir()
        (self.uploads / "link.txt").symlink_to(outside)
        for name in ("directory", "link.txt"):
            with self.subTest(name=name), self.assertRaises(UnsafeUploadPathError):
                self.client.download_file("user_001", "thread_001", name)
        self.assertEqual(self.client.thread_files("user_001", "thread_001"), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "当前平台不支持 FIFO")
    def test_download_rejects_fifo_without_blocking(self):
        os.mkfifo(self.uploads / "pipe")
        with self.assertRaises(UnsafeUploadPathError):
            self.client.download_file("user_001", "thread_001", "pipe")

    def test_download_rejects_file_replaced_during_open(self):
        target = self.uploads / "test.txt"
        target.write_bytes(b"original")
        replacement = self.uploads / "replacement.txt"
        replacement.write_bytes(b"replacement")
        original_open = os.open

        def replace_and_open(path, flags):
            replacement.replace(target)
            return original_open(path, flags)

        with patch("deerflow.thread_client.os.open", side_effect=replace_and_open):
            with self.assertRaises(UnsafeUploadPathError):
                self.client.download_file("user_001", "thread_001", "test.txt")


if __name__ == "__main__":
    unittest.main()
