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
            self.assertEqual(feed["relevantItems"], [{**lead, "observedOn": "2026-10-01T10:00:00+08:00"}])
            self.assertEqual(json.loads(history.read_text())["entries"][0]["result"], "failed")


    def test_partial_run_retains_failed_source_lead_without_counting_it_new(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "feed.json"
            history = Path(folder) / "history.json"
            old = {"title": "卫生健康检验旧线索", "url": "https://www.nhc.gov.cn/example", "published": "2026-10-01", "directory": checker.SOURCES[-1][0]}
            output.write_text(json.dumps({
                "checkedOn": "2026-10-01T10:00:00+08:00", "knownUrls": [old["url"]],
                "establishedSources": [checker.SOURCES[0][0], checker.SOURCES[-1][0]],
                "relevantItems": [old], "announcementItems": [], "versionCandidates": [], "sources": [],
            }))
            page = '<record><![CDATA[<a href="/example-new" title="检验新文件">检验新文件</a><span>2026-10-02</span>]]></record>'
            def fetch_one(url):
                if url == checker.SOURCES[0][1]:
                    return page
                raise URLError("offline")
            with patch.object(checker, "OUTPUT", output), patch.object(checker, "HISTORY", history), \
                 patch.object(checker, "fetch", side_effect=fetch_one), \
                 patch.object(checker, "fetch_nhc_announcements", side_effect=URLError("offline")):
                self.assertEqual(checker.main(), 0)
            feed = json.loads(output.read_text())
            self.assertEqual(feed["newCount"], 1)
            self.assertEqual([item["url"] for item in feed["newItems"]], ["https://www.nhsa.gov.cn/example-new"])
            self.assertEqual(next(item for item in feed["relevantItems"] if item["url"] == old["url"])["observedOn"], "2026-10-01T10:00:00+08:00")

if __name__ == "__main__":
    unittest.main()
