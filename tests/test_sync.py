import base64
import json
import io
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fetch_granola as fetch
import granola_auth as auth
import build_docs as build
import tag_meetings as tag


def token(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "auth.json"
        for target, value in [("AUTH_FILE", self.path), ("DESKTOP_FILE", Path(self.tmp.name) / "supabase.json")]:
            p = patch.object(auth, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_login_uses_pkce_and_does_not_request_calendar_access(self):
        url = auth.begin_login()
        params = auth.urllib.parse.parse_qs(auth.urllib.parse.urlparse(url).query)
        pending = json.loads(self.path.with_suffix(".pending.json").read_text())
        expected = base64.urlsafe_b64encode(auth.hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
        self.assertEqual(params["code_challenge"], [expected])
        self.assertEqual(params["request_calendar_permissions"], ["false"])

    def test_login_accepts_current_nested_tokens(self):
        auth.begin_login()
        tokens = {"access_token": token(time.time() + 3600), "refresh_token": "new"}
        with patch.object(auth, "auth_post", return_value={"tokens": tokens, "config": {}}):
            auth.complete_login("https://www.granola.ai/app-redirect?code=one-time-code")
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "new")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.with_suffix(".pending.json").exists())

    def test_mismatched_login_is_rejected_before_exchange(self):
        auth.begin_login()
        with patch.object(auth, "auth_post") as post:
            with self.assertRaises(auth.AuthError):
                auth.complete_login("granola://login-complete?code=test&signInClickId=wrong")
        post.assert_not_called()

    def test_expired_sync_token_is_refreshed_and_rotation_saved(self):
        auth.atomic_json(self.path, {"access_token": token(1), "refresh_token": "old"})
        renewed = {"access_token": token(time.time() + 3600), "refresh_token": "rotated"}
        with patch.object(auth, "auth_post", return_value=renewed) as post:
            self.assertEqual(auth.load_token(), renewed["access_token"])
        self.assertEqual(post.call_args.args[1], {"refresh_token": "old"})
        self.assertEqual(json.loads(self.path.read_text())["refresh_token"], "rotated")

    def test_valid_token_uses_jwt_expiry(self):
        tokens = {"access_token": token(time.time() + 3600), "obtained_at": 1, "expires_in": 0}
        auth.atomic_json(self.path, tokens)
        with patch.object(auth, "auth_post") as post:
            self.assertEqual(auth.load_token(), tokens["access_token"])
        post.assert_not_called()

    def test_refresh_failure_preserves_credentials(self):
        original = {"access_token": token(1), "refresh_token": "original"}
        auth.atomic_json(self.path, original)
        with patch.object(auth, "auth_post", side_effect=auth.AuthError("HTTP 401")):
            with self.assertRaises(auth.AuthError):
                auth.load_token()
        self.assertEqual(json.loads(self.path.read_text()), original)

    def test_encrypted_desktop_store_has_actionable_error(self):
        auth.DESKTOP_FILE.with_suffix(".json.enc").write_bytes(b"encrypted")
        with patch.object(auth, "auth_post") as post:
            with self.assertRaisesRegex(auth.AuthError, "granola_auth.py login"):
                auth.load_token()
        post.assert_not_called()

    def test_stale_desktop_refresh_token_is_never_used(self):
        auth.DESKTOP_FILE.write_text(json.dumps({"workos_tokens": json.dumps({
            "access_token": token(1), "refresh_token": "desktop-owned",
        })}))
        with patch.object(auth, "auth_post") as post:
            with self.assertRaises(auth.AuthError):
                auth.load_token()
        post.assert_not_called()


class FetchTests(unittest.TestCase):
    def test_unchanged_archive_makes_no_transcript_or_summary_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "transcripts").mkdir()
            (root / "panels").mkdir()
            docs = [{"id": "a", "created_at": "2025-01-01T10:00:00Z", "updated_at": "same"}]
            (root / "documents.json").write_text(json.dumps(docs))
            (root / "transcripts/a.json").write_text('[{"text":"Complete transcript"}]')
            (root / "panels/a.json").write_text('[{"original_content":"<p>Summary</p>"}]')
            with patch.object(fetch, "OUT", root), \
                 patch.object(fetch, "TRANSCRIPTS_DIR", root / "transcripts"), \
                 patch.object(fetch, "PANELS_DIR", root / "panels"), \
                 patch.object(fetch, "load_token", return_value="test"), \
                 patch.object(fetch, "fetch_documents", return_value=docs), \
                 patch.object(fetch, "fetch_transcript") as transcript, \
                 patch.object(fetch, "fetch_panels") as panels:
                with redirect_stdout(io.StringIO()):
                    fetch.main()
            transcript.assert_not_called()
            panels.assert_not_called()

    def test_recent_recording_is_rechecked_for_final_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.json"
            path.write_text('[{"text":"Still recording"}]')
            now = time.time()
            os.utime(path, (now - 3601, now - 3601))
            created = auth.time.strftime("%Y-%m-%dT%H:%M:%SZ", auth.time.gmtime(now - 7200))
            doc = {"created_at": created}
            self.assertIn("final content", fetch.cache_refresh_reason(path, doc, doc))
            os.utime(path, (now, now))
            self.assertTrue(fetch.cache_current(path, doc, doc))

    def test_populated_but_empty_summary_is_retried_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "panels"
            folder.mkdir()
            path = folder / "a.json"
            path.write_text('[{"original_content":"","content":{}}]')
            self.assertTrue(fetch.cache_current(path, {}, {}))
            os.utime(path, (1, 1))
            self.assertIn("daily retry", fetch.cache_refresh_reason(path, {}, {}))

    def test_current_response(self):
        docs = [{"id": "a"}]
        with patch.object(fetch, "post", return_value=(200, {"docs": docs})):
            self.assertEqual(fetch.fetch_documents("test"), docs)

    def test_unrecognized_success_is_not_an_empty_export(self):
        with patch.object(fetch, "post", return_value=(200, {"error": "changed schema"})):
            with self.assertRaises(SystemExit):
                fetch.fetch_documents("test")

    def test_authorization_error_does_not_fall_back(self):
        with patch.object(fetch, "post", return_value=(401, {})) as post:
            with self.assertRaises(SystemExit):
                fetch.fetch_documents("test")
        self.assertEqual(post.call_count, 1)

    def test_repeated_page_is_not_silently_duplicated(self):
        docs = [{"id": str(i)} for i in range(1000)]
        with patch.object(fetch, "post", return_value=(200, {"docs": docs})), patch.object(fetch.time, "sleep"):
            with self.assertRaises(SystemExit):
                fetch.fetch_documents("test")

    def test_changed_corrupt_and_old_empty_caches_are_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.json"
            current = {"updated_at": "new"}
            path.write_text('[{"text":"hello"}]')
            self.assertTrue(fetch.cache_current(path, current, current))
            self.assertFalse(fetch.cache_current(path, current, {"updated_at": "old"}))
            path.write_text('{')
            self.assertFalse(fetch.cache_current(path, current, current))
            path.write_text('[]')
            self.assertTrue(fetch.cache_current(path, current, current))
            os.utime(path, (1, 1))
            self.assertFalse(fetch.cache_current(path, current, current))

    def test_failed_fetch_preserves_document_index_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = [{"id": "a", "updated_at": "old"}]
            (root / "documents.json").write_text(json.dumps(original))
            with patch.object(fetch, "OUT", root), \
                 patch.object(fetch, "TRANSCRIPTS_DIR", root / "transcripts"), \
                 patch.object(fetch, "PANELS_DIR", root / "panels"), \
                 patch.object(fetch, "load_token", return_value="test"), \
                 patch.object(fetch, "fetch_documents", return_value=[{"id": "a", "updated_at": "new"}]), \
                 patch.object(fetch, "fetch_transcript", return_value=([{"text": "updated"}], None)), \
                 patch.object(fetch, "fetch_panels", return_value=(None, "HTTP 503")), \
                 patch.object(fetch.time, "sleep"):
                with self.assertRaises(SystemExit):
                    fetch.main()
            self.assertEqual(json.loads((root / "documents.json").read_text()), original)


class BuildTests(unittest.TestCase):
    def test_case_only_filename_collision_preserves_both_meetings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            built = root / "built"
            docs = [{"id": "first-id", "title": "Catch up", "created_at": "2025-12-04T10:00:00Z"},
                    {"id": "second-id", "title": "catch up", "created_at": "2025-12-04T11:00:00Z"}]
            (root / "documents.json").write_text(json.dumps(docs))
            with patch.object(build, "ROOT", root), patch.object(build, "OUT", built), redirect_stdout(io.StringIO()):
                build.main()
                manifest = json.loads((built / "manifest.json").read_text())
                self.assertEqual(len({m["filename"].casefold() for m in manifest}), 2)
                for m in manifest:
                    self.assertIn("Granola Meeting ID: " + m["id"], Path(m["path"]).read_text())
                for p in built.iterdir():
                    os.utime(p, (10, 10))
                build.main()
                self.assertTrue(all(p.stat().st_mtime == 10 for p in built.iterdir()))

    def test_second_build_and_tag_preserve_content_and_mtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            built = root / "built"
            (root / "transcripts").mkdir()
            (root / "panels").mkdir()
            doc = {"id": "a", "title": "Call about fundraising", "created_at": "2025-01-01T10:00:00Z",
                   "notes_plain": "Tags: a line in private notes"}
            (root / "documents.json").write_text(json.dumps([doc]))
            (root / "transcripts/a.json").write_text('[{"text":"Complete transcript"}]')
            (root / "panels/a.json").write_text('[{"original_content":"<p>Summary</p>"}]')
            with patch.object(build, "ROOT", root), patch.object(build, "OUT", built), \
                 patch.object(tag, "BUILT", built), redirect_stdout(io.StringIO()):
                build.main()
                tag.main()
                paths = list(built.iterdir())
                contents = {p: p.read_bytes() for p in paths}
                for p in paths:
                    os.utime(p, (10, 10))
                build.main()
                tag.main()
                self.assertEqual({p: p.read_bytes() for p in paths}, contents)
                self.assertTrue(all(p.stat().st_mtime == 10 for p in paths))
                dossier = next(built.glob("*.txt"))
                self.assertIn("Tags: a line in private notes", dossier.read_text())
                (root / "transcripts/a.json").write_text('[{"text":"Final, improved transcript"}]')
                build.main()
                tag.main()
                self.assertIn("Final, improved transcript", dossier.read_text())
                self.assertGreater(dossier.stat().st_mtime, 10)
                with patch.object(tag, "TITLE_RULES", [(r".*", "industry-talk", [])]):
                    build.main()
                    tag.main()
                self.assertIn("Tags: industry-talk", dossier.read_text())


class RefreshTests(unittest.TestCase):
    def test_env_file_controls_export_and_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "python"
            python.write_text('#!/bin/bash\nexit 0\n')
            python.chmod(0o700)
            rclone = root / "rclone"
            rclone.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/arguments"\n')
            rclone.chmod(0o700)
            (root / ".granola-to-drive.env").write_text(
                f'GRANOLA_PYTHON="{python}"\nRCLONE_BIN="{rclone}"\n'
                'GRANOLA_RCLONE_REMOTE="custom"\nGRANOLA_DRIVE_FOLDER="Meeting Notes"\n'
                f'GRANOLA_EXPORT_DIR="{root}/export"\n')
            result = subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "refresh.sh")],
                                    env={**os.environ, "HOME": tmp}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = (root / "arguments").read_text().splitlines()
            self.assertEqual(args[:3], ["copy", str(root / "export/built"), "custom:Meeting Notes/"])


if __name__ == "__main__":
    unittest.main()
