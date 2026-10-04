"""Small, per-book source samples. A passing sample is not a complete-book check."""
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import reliable_engine as engine


TTL_SECONDS = 30 * 60
MAX_RECORDS = 100
_STORE_LOCKS = {}
_STORE_LOCKS_GUARD = threading.Lock()
_STATUSES = {'passed', 'gaps', 'failed', 'cancelled'}
_RESULT_FIELDS = {
    'status', 'url', 'title', 'author', 'count', 'latest', 'catalog_seconds',
    'sample_seconds', 'seconds_per_chapter', 'sample_count', 'sample_pages',
    'sample_indices', 'checked_at', 'message', 'missing', 'missing_count',
    'warnings', 'elapsed_seconds', 'phase', 'numbering_issues',
}


def normalized(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _author(value):
    value = re.sub(r'^\s*作者\s*[:：]?\s*', '', str(value or '')).strip()
    result = normalized(value)
    return '' if result in {'', '佚名', '未知', '暂无', '无', '未知作者', '作者未知'} else result


def matching_rows(selected, rows):
    """Never treat a different or missing known author as the selected work."""
    result, seen = [selected], {selected.get('url')}
    title, author = normalized(selected.get('title')), _author(selected.get('author'))
    if not title or not author:
        return result
    for row in rows:
        if (row.get('url') not in seen and normalized(row.get('title')) == title
                and _author(row.get('author')) == author):
            seen.add(row.get('url'))
            result.append(row)
            if len(result) >= 6:
                break
    return result


def _same_source(rule_url, book_url):
    try:
        rule, book = urlsplit(rule_url), urlsplit(book_url)
        if any(p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
               for p in (rule, book)):
            return False
        # Existing rules use www and bare-host links interchangeably.
        host = lambda p: p.hostname.casefold().removeprefix('www.')
        port = lambda p: p.port or (443 if p.scheme == 'https' else 80)
        return host(rule) == host(book) and port(rule) == port(book)
    except (AttributeError, TypeError, ValueError):
        return False


def same_source(rule_url, book_url):
    """Match the rule's host and port without accepting unrelated subdomains."""
    return _same_source(rule_url, book_url)


def _has_dynamic_rule(value):
    if isinstance(value, dict):
        return any(_has_dynamic_rule(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_dynamic_rule(item) for item in value)
    return isinstance(value, str) and '@js:' in value


def _same_title(expected, actual, source):
    a, b = normalized(expected), normalized(actual)
    if not a or not b or b in {'小说', '目录预览'}:
        return True
    if a == b:
        return True
    if b.startswith(a):
        suffix = b[len(a):]
        for text in ('最新章节列表', '最新章节', '全文阅读', '免费阅读', '无弹窗', '无广告',
                     '章节目录', normalized(source)):
            if text:
                suffix = suffix.replace(text, '')
        return not suffix
    return False


def probe_source(rule, row, stop=None, scratch_dir=None):
    """Read the full catalog and at most three distinct chapters, with real checks.

    Probe requests are sequential, at least 700 ms apart, and respect stricter
    source rules. Only throwaway files are written; user download caches and
    completed books are never changed. A gap blocks body sampling before consent.
    """
    stop = stop or threading.Event()
    started = time.monotonic()
    result = dict(status='failed', url=row.get('url', ''), title=row.get('title', ''),
                  author=row.get('author', ''), count=None, latest='', catalog_seconds=None,
                  sample_seconds=0.0, seconds_per_chapter=None, sample_count=0,
                  sample_pages=0, sample_indices=[], checked_at=time.time(), message='',
                  missing=[], missing_count=0, numbering_issues=[], warnings=[], phase='catalog')
    downloader = None
    catalog_started = sample_started = None
    try:
        if stop.is_set():
            raise InterruptedError('抽检已取消')
        if not _same_source(rule.get('url', ''), result['url']):
            raise ValueError('书籍网址与所选书源不匹配')
        if _has_dynamic_rule({key: rule.get(key) for key in ('book', 'toc', 'chapter')}):
            raise ValueError('该书源需要动态解析，暂不支持正文抽检')
        if rule.get('adapter') not in (None, '', 'fanqie'):
            raise ValueError('该书源适配器暂不支持正文抽检')
        if scratch_dir is not None:
            Path(scratch_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='source-probe-', dir=scratch_dir) as temporary:
            root = Path(temporary)
            rules_path = root / 'rules.json'
            rules_path.write_text(json.dumps([rule], ensure_ascii=False), encoding='utf-8')
            args = SimpleNamespace(url=result['url'], rules=str(rules_path), output=str(root / 'sample'),
                                   format='epub', workers=1, interval=700, retries=0, timeout=8,
                                   min_chars=100, refresh=True)
            cls = engine.Downloader
            if rule.get('adapter') == 'fanqie':
                from fanqie_source import FanqieDownloader
                cls = FanqieDownloader
            downloader = cls(args, stop)
            catalog_started = time.monotonic()
            chapters, _ = downloader.catalog()
            result['catalog_seconds'] = round(time.monotonic() - catalog_started, 3)
            if stop.is_set():
                raise InterruptedError('抽检已取消')
            result.update(count=len(chapters), latest=chapters[-1]['title'],
                          missing=engine.catalog_gaps(chapters), warnings=engine.catalog_warnings(chapters),
                          numbering_issues=engine.catalog_numbering_issues(chapters))
            result['missing_count'] = sum(gap['count'] for gap in result['missing'])
            if not _same_title(row.get('title'), downloader.title, rule.get('name', '')):
                raise ValueError('目录书名与搜索结果不一致，请核对书籍')
            expected_author, actual_author = _author(row.get('author')), _author(downloader.author)
            if expected_author and actual_author and expected_author != actual_author:
                raise ValueError('目录作者与搜索结果不一致，请核对书籍')
            if result['missing'] or result['numbering_issues']:
                result.update(status='gaps', message='目录需核对：疑似缺号 %s 处，编号异常 %s 处；不能据此计算实际缺章数。未读取正文，下载前仍需确认' %
                              (len(result['missing']), len(result['numbering_issues'])))
            else:
                result['phase'] = 'sample'
                downloader.chapters = chapters
                downloader.chapter_urls = {chapter['url'] for chapter in chapters}
                indices = sorted({0, len(chapters) // 2, len(chapters) - 1})
                bodies = set()
                sample_started = time.monotonic()
                for index in indices:
                    if stop.is_set():
                        raise InterruptedError('抽检已取消')
                    data, _ = downloader.chapter(chapters[index], force=True)
                    if stop.is_set():
                        raise InterruptedError('抽检已取消')
                    body_hash = engine.digest(re.sub(r'\s', '', data['body']))
                    if body_hash in bodies:
                        raise ValueError('抽检章节正文重复，请更换书源或核对内容')
                    bodies.add(body_hash)
                    result['sample_count'] += 1
                    result['sample_pages'] += len(data['pages'])
                    result['sample_indices'].append(index + 1)
                result.update(status='passed', message='目录读取成功，%s 章正文抽检通过；不代表整本已校验' % len(indices))
    except InterruptedError:
        result.update(status='cancelled', message='抽检已取消')
    except Exception as exc:
        result.update(status='cancelled' if stop.is_set() else 'failed',
                      message='抽检已取消' if stop.is_set() else str(exc).replace('\n', ' ')[:240])
    finally:
        now = time.monotonic()
        if stop.is_set():
            result.update(status='cancelled', message='抽检已取消')
        if result['catalog_seconds'] is None and catalog_started is not None:
            result['catalog_seconds'] = round(now - catalog_started, 3)
        if sample_started is not None:
            result['sample_seconds'] = round(now - sample_started, 3)
        if result['status'] == 'passed' and result['sample_count']:
            result['seconds_per_chapter'] = round(result['sample_seconds'] / result['sample_count'], 3)
        result.update(checked_at=time.time(), elapsed_seconds=round(now - started, 3))
        if downloader is not None:
            session = getattr(downloader.fetch.local, 'session', None)
            if session is not None:
                session.close()
    return result


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _record(result, now):
    if not isinstance(result, dict) or result.get('status') not in _STATUSES - {'cancelled'}:
        return None
    checked = result.get('checked_at')
    if not _finite(checked) or not 0 <= now - checked < TTL_SECONDS:
        return None
    if result.get('count') is not None and (not isinstance(result['count'], int) or result['count'] < 1):
        return None
    if result['status'] == 'passed':
        speed, samples = result.get('seconds_per_chapter'), result.get('sample_count')
        if not _finite(speed) or speed <= 0 or not isinstance(samples, int) or not 1 <= samples <= 3:
            return None
    # Only probe metadata belongs in this file, never chapter text or arbitrary data.
    value = {key: copy.deepcopy(result[key]) for key in _RESULT_FIELDS if key in result}
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return value


class QualityStore:
    def __init__(self, path):
        self.path = Path(path)
        with _STORE_LOCKS_GUARD:
            self.lock = _STORE_LOCKS.setdefault(str(self.path.resolve()).casefold(), threading.Lock())

    @staticmethod
    def _key(rule, url):
        # Discard stale assessments from the old gap algorithm, not body caches.
        value = json.dumps([2, rule, url], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def _read(self, now):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('records'), dict):
                return {}
            result = {}
            for key, entry in value['records'].items():
                valid = _record(entry, now)
                if valid is not None:
                    result[key] = valid
            return result
        except (OSError, ValueError, TypeError):
            return {}

    def get(self, rule, url):
        now = time.time()
        with self.lock:
            result = self._read(now).get(self._key(rule, url))
        if result is None:
            return None
        result.update(historical=True, age_seconds=max(0, int(now - result['checked_at'])))
        return result

    def put(self, rule, url, result):
        now = time.time()
        valid = _record(result, now)
        if valid is None or result.get('url', url) != url:
            return False
        with self.lock:
            records = self._read(now)
            records[self._key(rule, url)] = valid
            records = dict(sorted(records.items(), key=lambda item: item[1]['checked_at'], reverse=True)[:MAX_RECORDS])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + '.tmp')
            temporary.write_text(json.dumps(dict(version=1, records=records), ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temporary, self.path)
        return True


def quality_label(result):
    if not isinstance(result, dict):
        return '未抽检'
    status = result.get('status')
    if status == 'passed':
        speed = result.get('seconds_per_chapter')
        label = '抽检通过' + (' · %.1f秒/章' % speed if _finite(speed) and speed > 0 else '')
        if result.get('warnings'):
            label += ' · 章号异常'
    else:
        label = {'gaps': '目录待核对' if result.get('numbering_issues') else '疑似缺章', 'failed': '目录失败' if result.get('phase') == 'catalog' else '抽检失败',
                 'checking': '抽检中…', 'cancelled': '已取消抽检'}.get(status, '未抽检')
    if result.get('historical'):
        checked = result.get('checked_at')
        age = max(0, int(time.time() - checked)) if _finite(checked) else None
        label = '历史 · ' + (('刚才' if age < 60 else '%s分前' % (age // 60)) + ' · ' if age is not None else '') + label
    return label


def rank_key(row, query):
    title, author = normalized(row.get('title')), _author(row.get('author'))
    quality = row.get('quality') if isinstance(row.get('quality'), dict) else {}
    status = quality.get('status')
    rank = {'passed': 0, 'gaps': 2, 'failed': 3}.get(status, 1)
    historical = bool(quality.get('historical'))
    speed = quality.get('seconds_per_chapter')
    speed = speed if status == 'passed' and _finite(speed) and speed > 0 else float('inf')
    # A freshly proven historical search link is stronger than an unchecked live link.
    search_history = bool(row.get('cached_at')) and not (status == 'passed' and not historical)
    return (title != normalized(query), title, author or '\uffff', rank, historical,
            bool(quality.get('warnings')) if status == 'passed' else False,
            search_history, speed, row.get('source', ''), row.get('url', ''))
