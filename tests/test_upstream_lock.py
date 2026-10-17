"""Exercise lock failures on disposable repositories, never on the real baseline."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("lock_upstream", Path(__file__).resolve().parents[1] / "tools/lock_upstream.py")
lock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lock)


class UpstreamLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "upstream-openpilot"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "remote", "add", "origin", "https://example.invalid/official.git")
        self.payload = b"model asset bytes for offline verification\n"
        self.pointer = ("version https://git-lfs.github.com/spec/v1\n"
                        f"oid sha256:{hashlib.sha256(self.payload).hexdigest()}\n"
                        f"size {len(self.payload)}\n").encode()
        (self.repo / "model.onnx").write_bytes(self.pointer)
        (self.repo / "source.py").write_text("answer = 42\n")
        self.commit(self.repo)
        self.root_patch = patch.object(lock, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.temp.cleanup)

    @staticmethod
    def git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self, repo):
        self.git(repo, "add", "--all")
        self.git(repo, "-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")

    def test_downloaded_asset_and_sources_pass(self):
        (self.repo / "model.onnx").write_bytes(self.payload)
        manifest, _ = lock.collect_repo(self.repo)
        self.assertEqual(lock.verify_sources(self.repo, manifest, False), (1, 0))

    def test_source_modification_is_rejected(self):
        manifest, _ = lock.collect_repo(self.repo)
        (self.repo / "source.py").write_text("answer = 43\n")
        with self.assertRaisesRegex(ValueError, "Tracked source differs"):
            lock.verify_sources(self.repo, manifest, True)

    def test_strict_rejects_pointer_even_if_lock_created_without_asset(self):
        manifest, _ = lock.collect_repo(self.repo)
        self.assertEqual(manifest["lfs_objects"][0]["status"], "upstream_pointer_only")
        with self.assertRaisesRegex(ValueError, "LFS content missing or changed"):
            lock.verify_sources(self.repo, manifest, False)

    def test_source_only_mode_reports_intact_pointer(self):
        manifest, _ = lock.collect_repo(self.repo)
        self.assertEqual(lock.verify_sources(self.repo, manifest, True), (0, 1))

    def test_modified_pointer_is_rejected_in_source_only_mode(self):
        manifest, _ = lock.collect_repo(self.repo)
        (self.repo / "model.onnx").write_bytes(self.pointer.replace(b"size", b"sixe"))
        with self.assertRaisesRegex(ValueError, "Tracked source differs"):
            lock.verify_sources(self.repo, manifest, True)

    def test_missing_asset_is_rejected(self):
        (self.repo / "model.onnx").unlink()
        with self.assertRaisesRegex(ValueError, "Missing or damaged LFS"):
            lock.collect_repo(self.repo)

    def test_corrupt_asset_of_correct_size_is_rejected(self):
        (self.repo / "model.onnx").write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(ValueError, "Missing or damaged LFS"):
            lock.collect_repo(self.repo)

    def test_changed_executable_bit_is_rejected(self):
        manifest, _ = lock.collect_repo(self.repo)
        (self.repo / "source.py").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "executable mode changed"):
            lock.verify_sources(self.repo, manifest, True)

    def test_gitlink_mismatch_is_rejected(self):
        child = self.repo / "child"
        child.mkdir()
        self.git(child, "init", "-q")
        (child / "code.py").write_text("version = 1\n")
        self.commit(child)
        self.commit(self.repo)
        self.assertEqual(len(list(lock.repos_recursive(self.repo))), 2)
        (child / "code.py").write_text("version = 2\n")
        self.commit(child)
        with self.assertRaisesRegex(ValueError, "Gitlink mismatch"):
            list(lock.repos_recursive(self.repo))


if __name__ == "__main__":
    unittest.main()
