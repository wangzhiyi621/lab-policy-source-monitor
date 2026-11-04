import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import check_sources as checker


class PublicFeedTests(unittest.TestCase):
    def test_nhc_standard_table_extracts_official_link_and_dates(self):
        page = '''<tr bgcolor="#ffffff" class="xx"><td>·</td><td>WS/T 404.9—2026</td>
        <td><a href="/wjw/s9492/202606/example.shtml" title="临床常用生化检验项目参考区间">查看</a></td>
        <td>2026年5月25日</td><td><p>2026年11月1日</p></td></tr>'''
        records = checker.parse_nhc_standards(page, checker.SOURCES[-1][1], checker.SOURCES[-1][0])
        self.assertEqual(records, [{'title': '临床常用生化检验项目参考区间',
                                    'url': 'https://www.nhc.gov.cn/wjw/s9492/202606/example.shtml',
                                    'published': '2026-05-25', 'effective': '2026-11-01',
                                    'directory': checker.SOURCES[-1][0], 'code': 'WS/T 404.9—2026'}])

    def test_transient_connection_error_is_retried(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b"ok"
        with patch.object(checker, "urlopen", side_effect=[URLError("temporary"), Response()]) as opened, \
             patch.object(checker.time, "sleep") as pause:
            self.assertEqual(checker.fetch("https://www.nhsa.gov.cn/example"), "ok")
        self.assertEqual(opened.call_count, 2)
        pause.assert_called_once_with(1)

    def test_access_control_error_is_not_retried(self):
        error = HTTPError("https://www.nhc.gov.cn/example", 412, "blocked", {}, None)
        with patch.object(checker, "urlopen", side_effect=error) as opened, \
             patch.object(checker.time, "sleep") as pause:
            with self.assertRaises(HTTPError):
                checker.fetch("https://www.nhc.gov.cn/example")
        self.assertEqual(opened.call_count, 1)
        pause.assert_not_called()

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
            self.assertEqual(feed["trigger"], "manual")
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
            self.assertEqual(feed["trigger"], "manual")
            self.assertEqual([item["url"] for item in feed["newItems"]], ["https://www.nhsa.gov.cn/example-new"])
            self.assertEqual(next(item for item in feed["relevantItems"] if item["url"] == old["url"])["observedOn"], "2026-10-01T10:00:00+08:00")

if __name__ == "__main__":
    unittest.main()
