import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import check_sources as checker


class PublicFeedTests(unittest.TestCase):
    def test_generated_files_stay_in_repository(self):
        self.assertEqual(checker.ROOT, Path(checker.__file__).resolve().parent)
        self.assertEqual(checker.OUTPUT.parent, checker.ROOT)
        self.assertEqual(checker.HISTORY.parent, checker.ROOT)

    def test_failed_run_keeps_previous_public_leads(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "feed.json"
            history = Path(folder) / "history.json"
            lead = {"title": "既有公开目录", "url": "https://www.nhsa.gov.cn/example", "published": "2026-10-01", "directory": "医保"}
            output.write_text(json.dumps({
                "checkedOn": "2026-10-01T10:00:00+08:00", "knownUrls": [lead["url"]],
                "establishedSources": ["医保"], "relevantItems": [lead],
                "announcementItems": [], "versionCandidates": [], "sources": [],
            }))
            with patch.object(checker, "OUTPUT", output), patch.object(checker, "HISTORY", history), \
                 patch.object(checker, "fetch", side_effect=URLError("offline")), \
                 patch.object(checker, "fetch_nhc_announcements", side_effect=URLError("offline")):
                self.assertEqual(checker.main(), 1)
            feed = json.loads(output.read_text())
            self.assertFalse(feed["runSuccess"])
            self.assertIsNone(feed["newCount"])
            self.assertEqual(feed["relevantItems"], [lead])
            self.assertEqual(json.loads(history.read_text())["entries"][0]["result"], "failed")


if __name__ == "__main__":
    unittest.main()
