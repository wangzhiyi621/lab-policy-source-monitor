#!/usr/bin/env python3
"""Check public official directories and publish review-only metadata."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "feed.json"
HISTORY = ROOT / "history.json"
SOURCES = [
    ("国家医保局 · 立项指南", "https://www.nhsa.gov.cn/col/col201/index.html"),
    ("国家医保局 · 政策解读", "https://www.nhsa.gov.cn/col/col202/index.html"),
    ("国家医保局 · 医保动态", "https://www.nhsa.gov.cn/col/col14/index.html"),
    ("国家卫健委 · 卫生健康标准", "https://www.nhc.gov.cn/wjw/wsbzxx/wsbz.shtml"),
]
SAMR_NAME = "全国标准信息公共服务平台 · WS/T近期检索"
SAMR_SEARCH = "https://std.samr.gov.cn/search/stdPage?q=WS%2FT&tid="
NHC_ANNOUNCEMENTS_NAME = "卫生健康标准网 · 发布通告"
NHC_ANNOUNCEMENTS = "https://wsbz.nhc.gov.cn/wsbzw/BzcxAction.do?dispatch=bzgg&flag=7"
KEYWORDS = ("检验", "生化", "免疫", "参考区间", "互认", "立项指南", "医疗服务价格")
RECORD = re.compile(r"<record>\s*<!\[CDATA\[(.*?)\]\]>\s*</record>", re.S | re.I)
LINK = re.compile(r'<a\s+href="([^"]+)"\s+title="([^"]+)"[^>]*>.*?</a>\s*<span>(\d{4}-\d{2}-\d{2})</span>', re.S | re.I)
SAMR_LINK = re.compile(r'<a\s+href="#"\s+tid="BV_HB"\s+pid="([0-9A-F]+)"[^>]*>\s*<span class="en-code">(.*?)</span>\s*&nbsp;&nbsp;\s*(.*?)</a>', re.S | re.I)
SAMR_DATE = re.compile(r'<time class="post-date"[^>]*>(\d{4}-\d{2}-\d{2})</time>')
NHC_ANNOUNCEMENT = re.compile(r'<a\s+href="([^"]+)"[^>]*class="bluelink"[^>]*>(.*?)</a>\s*</td>\s*<td[^>]*>(\d{4}-\d{2}-\d{2})</td>', re.S | re.I)
NHC_STANDARD_ROW = re.compile(r'<tr\b[^>]*class="xx"[^>]*>(.*?)</tr>', re.S | re.I)
NHC_STANDARD_LINK = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*title="([^"]+)"', re.S | re.I)
def parse_records(page: str, directory: str, source_name: str) -> list[dict[str, str]]:
    records = []
    for block in RECORD.findall(page):
        match = LINK.search(block)
        if not match:
            continue
        href, title, published = match.groups()
        url = urljoin(directory, unescape(href))
        if urlparse(url).hostname not in ("www.nhsa.gov.cn", "nhsa.gov.cn"):
            continue
        records.append({"title": unescape(title).strip(), "url": url, "published": published, "directory": source_name, "code": "", "effective": ""})
    return records


def parse_nhc_standards(page: str, directory: str, source_name: str) -> list[dict[str, str]]:
    """Read only rows in the NHC's public standard table, not site navigation."""
    records = []
    for row in NHC_STANDARD_ROW.findall(page):
        cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row, flags=re.S | re.I)
        link = NHC_STANDARD_LINK.search(cells[2]) if len(cells) >= 5 else None
        if not link:
            continue
        code = unescape(re.sub(r'<[^>]+>', '', cells[1])).strip()
        published = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', cells[3])
        effective = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', cells[4])
        url = urljoin(directory, unescape(link.group(1)))
        if not code or not published or urlparse(url).hostname not in ('www.nhc.gov.cn', 'nhc.gov.cn'):
            continue
        date = f'{int(published.group(1)):04d}-{int(published.group(2)):02d}-{int(published.group(3)):02d}'
        effective_date = f'{int(effective.group(1)):04d}-{int(effective.group(2)):02d}-{int(effective.group(3)):02d}' if effective else ''
        records.append({'title': unescape(link.group(2)).strip(), 'url': url, 'published': date,
                        'directory': source_name, 'code': code, 'effective': effective_date})
    return records


def parse_samr(page: str) -> list[dict[str, str]]:
    records = []
    for block in page.split('<div class="panel panel-default post">')[1:]:
        match = SAMR_LINK.search(block)
        dates = SAMR_DATE.findall(block)
        if not match or not dates:
            continue
        pid, raw_code, title = match.groups()
        code = unescape(re.sub(r"<[^>]+>", "", raw_code)).strip()
        records.append({
            "title": unescape(re.sub(r"<[^>]+>", "", title)).strip(),
            "url": f"https://std.samr.gov.cn/hb/search/stdHBDetailed?id={pid}",
            "published": dates[0], "effective": dates[1] if len(dates) > 1 else "",
            "directory": SAMR_NAME, "code": code,
        })
    return records


def parse_nhc_announcements(page: str) -> list[dict[str, str]]:
    """Parse the official announcement list; titles alone do not verify replacements."""
    records = []
    for href, raw_title, published in NHC_ANNOUNCEMENT.findall(page):
        url = urljoin(NHC_ANNOUNCEMENTS, unescape(href))
        if urlparse(url).hostname != "wsbz.nhc.gov.cn" or "/article/" not in urlparse(url).path:
            continue
        records.append({
            "title": unescape(re.sub(r"<[^>]+>", "", raw_title)).strip(),
            "url": url, "published": published, "directory": NHC_ANNOUNCEMENTS_NAME,
            "code": "", "effective": "",
        })
    return records


def read_url(request: Request, encoding: str) -> str:
    """Retry transient connection failures; access-control responses stay failed."""
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                return response.read().decode(encoding, errors="replace")
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(1 + attempt)
    raise RuntimeError("unreachable retry state")


def fetch(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LabPolicySourceCheck/1.0)", "Accept": "text/html"})
    return read_url(request, "utf-8")


def fetch_nhc_announcements() -> str:
    request = Request(NHC_ANNOUNCEMENTS, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    return read_url(request, "gbk")


def write_results(snapshot: dict) -> None:
    """Retain a bounded audit of manual checks, including failed checks."""
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {"startedOn": snapshot["checkedOn"], "entries": []}
    states = snapshot["sources"]
    entry = {
        "checkedOn": snapshot["checkedOn"],
        "trigger": snapshot["trigger"],
        "result": "failed" if not snapshot["runSuccess"] else "complete" if all(item["state"] == "success" for item in states) else "partial",
        "newLinkCount": snapshot["newCount"],
        "versionCandidateCount": len(snapshot["versionCandidates"]),
        "sources": [{"name": item["name"], "state": item["state"], "count": item["count"]} for item in states],
    }
    history["entries"] = ([entry] + [item for item in history["entries"] if item["checkedOn"] != entry["checkedOn"]])[:30]
    OUTPUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else None
    known = set(previous.get("knownUrls", [])) if previous else set()
    established_sources = set(previous.get("establishedSources", [])) if previous else set()
    if previous and "establishedSources" not in previous:
        established_sources = {item["name"] for item in previous.get("sources", []) if item["state"] == "success"}
    checked_on = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    trigger = "scheduled" if os.environ.get("POLICY_CHECK_TRIGGER") == "schedule" else "manual"
    samr_urls = [SAMR_SEARCH if number == 1 else f"{SAMR_SEARCH}&pageNo={number}" for number in range(1, 4)]
    # Official sites can time out independently. Read them concurrently so one
    # unavailable directory does not hold the whole weekly check for minutes.
    with ThreadPoolExecutor(max_workers=8) as pool:
        source_pages = {url: pool.submit(fetch, url) for _, url in SOURCES}
        announcement_page = pool.submit(fetch_nhc_announcements)
        samr_pages = {url: pool.submit(fetch, url) for url in samr_urls}
        return compile_snapshot(previous, known, established_sources, checked_on, trigger, source_pages, announcement_page, samr_pages, samr_urls)


def compile_snapshot(previous, known, established_sources, checked_on, trigger, source_pages, announcement_page, samr_pages, samr_urls) -> int:
    all_records: dict[str, dict[str, str]] = {}
    statuses = []
    for name, url in SOURCES:
        try:
            page = source_pages[url].result()
            records = parse_records(page, url, name) if "nhsa.gov.cn" in url else parse_nhc_standards(page, url, name)
            if not records:
                raise ValueError("页面可访问，但目录条目无法解析")
            statuses.append({"name": name, "url": url, "state": "success", "count": len(records), "detail": "仅检查当前目录响应"})
            for record in records:
                all_records.setdefault(record["url"], record)
        except (HTTPError, URLError, ValueError, TimeoutError) as exc:
            code = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
            statuses.append({"name": name, "url": url, "state": "failed", "count": 0, "detail": f"读取失败：{code}；不能据此判断无更新"})
    nhc_announcements = []
    try:
        nhc_announcements = parse_nhc_announcements(announcement_page.result())
        if not nhc_announcements:
            raise ValueError("公告列表可访问，但条目无法解析")
        statuses.append({"name": NHC_ANNOUNCEMENTS_NAME, "url": NHC_ANNOUNCEMENTS, "state": "success", "count": len(nhc_announcements), "detail": "仅检查最新公告列表第一页；替代关系仍需打开正文核对"})
        for record in nhc_announcements:
            all_records.setdefault(record["url"], record)
    except (HTTPError, URLError, ValueError, TimeoutError) as exc:
        code = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else type(exc).__name__
        statuses.append({"name": NHC_ANNOUNCEMENTS_NAME, "url": NHC_ANNOUNCEMENTS, "state": "failed", "count": 0, "detail": f"读取失败：{code}；不能据此判断无更新"})
    samr_records = []
    samr_pages_read = 0
    samr_errors = []
    for page_number, page_url in enumerate(samr_urls, start=1):
        try:
            parsed = parse_samr(samr_pages[page_url].result())
            if not parsed:
                raise ValueError("标准条目无法解析")
            samr_records.extend(parsed)
            samr_pages_read += 1
        except (HTTPError, URLError, ValueError, TimeoutError) as exc:
            samr_errors.append(f"第{page_number}页：{f'HTTP {exc.code}' if isinstance(exc, HTTPError) else type(exc).__name__}")
    samr_state = "success" if samr_pages_read == 3 else "partial" if samr_pages_read else "failed"
    statuses.append({"name": SAMR_NAME, "url": SAMR_SEARCH, "state": samr_state, "count": len(samr_records), "detail": f"检索页 {samr_pages_read}/3；仅作标准元数据线索" + (f"；{'; '.join(samr_errors)}" if samr_errors else "")})
    for record in samr_records:
        all_records.setdefault(record["url"], record)
    run_success = any(item["state"] in ("success", "partial") for item in statuses)
    if not run_success:
        snapshot = {
            "checkedOn": checked_on, "mode": "github-scheduled", "trigger": trigger, "baseline": previous is None,
            "runSuccess": False,
            "newSourceBaselines": [],
            "establishedSources": sorted(established_sources),
            "scope": "本次没有成功读取任何官方目录，无法判断是否有更新；保留上次线索仅供回看",
            "sources": statuses,
            "relevantItems": [
                {**item, "observedOn": item.get("observedOn", previous.get("relevantItemsCheckedOn", previous.get("checkedOn", "")))}
                for item in previous.get("relevantItems", [])
            ] if previous else [],
            "relevantItemsCheckedOn": previous.get("relevantItemsCheckedOn", previous.get("checkedOn", "")) if previous else "",
            "announcementItems": previous.get("announcementItems", []) if previous else [],
            "announcementItemsCheckedOn": previous.get("announcementItemsCheckedOn", previous.get("checkedOn", "")) if previous else "",
            "versionCandidates": previous.get("versionCandidates", []) if previous else [],
            "versionCandidatesCompleteOn": previous.get("versionCandidatesCompleteOn", previous.get("checkedOn", "")) if previous else "",
            "newItems": [], "newCount": None, "knownUrls": sorted(known),
        }
        write_results(snapshot)
        print("No official directory was parsed; failure snapshot written.", file=sys.stderr)
        return 1
    current_relevant = [item for item in all_records.values() if any(word in item["title"] for word in KEYWORDS)]
    readable_sources = {item["name"] for item in statuses if item["state"] in ("success", "partial")}
    # Keep old leads only for directories that could not be read this week.
    # The feed marks their last successful observation instead of presenting
    # them as current results. New-link counts still use this run only.
    retained = [
        {**item, "observedOn": item.get("observedOn", previous.get("relevantItemsCheckedOn", previous.get("checkedOn", "")))}
        for item in previous.get("relevantItems", [])
        if item.get("directory") not in readable_sources
    ] if previous else []
    relevant = sorted(
        {item["url"]: item for item in [*retained, *[{**item, "observedOn": checked_on} for item in current_relevant]]}.values(),
        key=lambda item: (item["published"], item["url"]), reverse=True,
    )
    # The public repository has no hospital or local standards inventory.
    # Matching a newer year to a locally held PDF belongs in the private site.
    version_candidates = []
    version_candidates_complete_on = ""
    new_source_baselines = [item["name"] for item in statuses if item["state"] == "success" and item["name"] not in established_sources]
    new = list({item["url"]: item for item in [*current_relevant, *nhc_announcements] if previous and item["directory"] in established_sources and item["url"] not in known}.values())
    established_sources.update(new_source_baselines)
    announcement_success = any(item["name"] == NHC_ANNOUNCEMENTS_NAME and item["state"] == "success" for item in statuses)
    snapshot = {
        "checkedOn": checked_on,
        "mode": "github-scheduled",
        "trigger": trigger,
        "baseline": previous is None,
        "runSuccess": True,
        "newSourceBaselines": new_source_baselines,
        "establishedSources": sorted(established_sources),
        "scope": "仅已列目录响应中的标题、日期和链接；目录可能延迟或缺项，关键词匹配不是政策适用性判断",
        "sources": statuses,
        "relevantItems": relevant[:16],
        "relevantItemsCheckedOn": checked_on,
        "announcementItems": nhc_announcements if announcement_success else previous.get("announcementItems", []) if previous else [],
        "announcementItemsCheckedOn": checked_on if announcement_success else previous.get("announcementItemsCheckedOn", previous.get("checkedOn", "")) if previous else "",
        "versionCandidates": version_candidates,
        "versionCandidatesCompleteOn": version_candidates_complete_on,
        "newItems": new[:16],
        "newCount": len(new),
        "knownUrls": sorted(known | set(all_records)),
    }
    write_results(snapshot)
    print(f"Checked {len(statuses)} directories; parsed {len(all_records)} entries; relevant {len(relevant)}; new {len(new)}; baseline={previous is None}")
    for item in statuses:
        print(f"{item['name']}: {item['state']} ({item['count']}) {item['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
