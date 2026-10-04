"""Verified, resumable downloads for static SoNovel CSS/XPath rules.

Never publishes an artifact until catalog, chapter and archive checks pass.
Only the source's currently published catalog can be checked, not author originals.
"""
import argparse
import concurrent.futures as futures
import contextlib
import copy
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urldefrag
from xml.etree import ElementTree as ET

import requests
from lxml import html as LH

VERSION = 3
PRINT_LOCK = threading.Lock()


@contextlib.contextmanager
def task_lock(path):
    """Prevents two assistants from modifying the same book cache concurrently."""
    with open(path, 'a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('这本小说正在另一个下载任务中运行，请先暂停另一任务') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def emit(kind, **values):
    with PRINT_LOCK:
        print(json.dumps(dict(event=kind, **values), ensure_ascii=False), flush=True)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def atomic_json(path, obj):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def canonical(value):
    value = unicodedata.normalize('NFKC', value)
    value = re.sub(r'\(?第?\d+[/／]\d+页?\)?|[（(]\d+[）)]$|[（(][上下中][）)]$', '', value)
    return re.sub(r'[^\w\u4e00-\u9fff]', '', value).lower()


def number(value):
    m = re.match(r'^\s*第\s*([\d零〇一二两三四五六七八九十百千万]+)\s*[章节回]', unicodedata.normalize('NFKC', value))
    if not m:
        return None
    text = m[1]
    if text.isdigit():
        return int(text)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    total = section = digit = 0
    for c in text:
        if c in digits:
            digit = digits[c]
        elif c == '万':
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * {'十': 10, '百': 100, '千': 1000}[c]
            digit = 0
    return total + section + digit


def numbering_anomalies(entries):
    # A single misnumbered entry between otherwise consecutive neighbors is
    # ambiguous, not evidence that hundreds of chapters have disappeared.
    numbers = [number(c['title']) for c in entries]
    return {i for i in range(1, len(entries) - 1)
            if numbers[i-1] is not None and numbers[i] is not None and numbers[i+1] is not None
            and numbers[i+1] == numbers[i-1] + 2 and numbers[i] != numbers[i-1] + 1}


def _numbering_scopes(entries):
    scopes, current, previous = [], [], None
    for index, entry in enumerate(entries):
        n = number(entry['title'])
        if n == 1 and previous is not None and previous > 1 and current:
            scopes.append(current)
            current = []
        current.append((index, entry))
        if n is not None:
            previous = n
    if current:
        scopes.append(current)
    return scopes


def _mixed_numbering(entries, scopes):
    """Find evidence of book-wide labels inside restarted volume numbering.

    Catalog position is only a heuristic, never a replacement chapter number.
    Require several matching labels and a return to the smaller local sequence.
    Uncertain entries become comparison barriers, not removed/renumbered chapters.
    """
    candidates, sandwich = set(), False
    tolerance = max(10, len(entries) // 50)
    for scope in scopes[1:]:
        start = scope[0][0]
        if start < 20:
            continue
        selected = set()
        for index, entry in scope:
            n = number(entry['title'])
            if (n is not None and abs(n - (index + 1)) <= tolerance
                    and n - (index - start + 1) >= max(20, start // 2)):
                selected.add(index)
        # A plain missing block followed by a high sequence is insufficient.
        # There must also be smaller numbered entries after a high label.
        low = [(index, number(entry['title'])) for index, entry in scope
               if index not in selected and number(entry['title']) is not None]
        for index in selected:
            n = number(entries[index]['title'])
            if (any(j < index and value < n - 20 for j, value in low)
                    and any(j > index and value < n - 20 for j, value in low)):
                sandwich = True
        candidates.update(selected)
    return candidates if len(candidates) >= 2 and sandwich else set()


def catalog_numbering_issues(entries):
    scopes = _numbering_scopes(entries)
    mixed = _mixed_numbering(entries, scopes)
    issues = []
    for volume, scope in enumerate(scopes, 1):
        groups = []
        for index, entry in scope:
            if index in mixed:
                if groups and index == groups[-1][-1] + 1:
                    groups[-1].append(index)
                else:
                    groups.append([index])
        for group in groups:
            before, after = max(scope[0][0], group[0] - 1), group[-1]
            reason = '疑似混用卷内章号与全书章号；编号不在同一计数范围，无法据此计算缺章数。'
            if after < scope[-1][0]:
                reason += ' 后续条目：' + entries[after + 1]['title']
            issues.append(dict(kind='numbering', volume=volume,
                               before_index=before + 1, after_index=after + 1,
                               before=entries[before]['title'], after=entries[after]['title'],
                               reason=reason))
    return issues


def catalog_review(entries):
    return sorted(catalog_gaps(entries) + catalog_numbering_issues(entries),
                  key=lambda item: item['before_index'])


def catalog_gaps(entries):
    from bisect import bisect_left, bisect_right

    # A reset to chapter 1 starts a separate numbering scope. Other backwards
    # jumps can be misplaced links; their chapters must not be called missing.
    scopes = _numbering_scopes(entries)
    mixed = _mixed_numbering(entries, scopes)

    gaps = []
    for scope_number, scope in enumerate(scopes, 1):
        # Keep unknown labels in place: joining their neighbors would invent a
        # gap where those intervening entries may supply the missing numbers.
        volume = [entry if index not in mixed else dict(title='') for index, entry in scope]
        anomalies = numbering_anomalies(volume)
        numbers = [number(c['title']) for c in volume]
        occupied = {n: (scope[i][0], entry['title']) for i, (n, entry) in enumerate(zip(numbers, volume)) if n is not None}
        # Preserve the existing isolated-numbering-typo treatment even when a
        # different, large backwards jump spans that same number later on.
        for i in anomalies:
            occupied.setdefault(numbers[i - 1] + 1, (scope[i][0], volume[i]['title']))
        known = sorted(occupied)
        candidates = [(a + 1, b - 1) for i, (a, b) in enumerate(zip(numbers, numbers[1:]))
                      if i not in anomalies and i + 1 not in anomalies
                      and a is not None and b is not None and b > a + 1]
        merged = []
        for start, end in sorted(candidates):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        # Work on intervals and observed labels, never range(start, end): a
        # malformed chapter number can otherwise allocate millions of items.
        for start, end in merged:
            cursor = start
            indexes = known[bisect_left(known, start):bisect_right(known, end)]
            for present in indexes + [end + 1]:
                if cursor < present:
                    before = known[bisect_left(known, cursor) - 1]
                    after = known[bisect_right(known, present - 1)]
                    gaps.append(dict(start=cursor, end=present - 1, count=present - cursor,
                                     before=occupied[before][1], after=occupied[after][1],
                                     before_index=occupied[before][0] + 1, after_index=occupied[after][0] + 1,
                                     volume=scope_number))
                cursor = present + 1
    return gaps


def catalog_warnings(entries):
    warnings = []
    for gap in catalog_gaps(entries):
        missing = str(gap['start']) if gap['start'] == gap['end'] else '%s—%s' % (gap['start'], gap['end'])
        warnings.append('源站目录疑似缺章（缺少章号 %s）：%s → %s' % (missing, gap['before'], gap['after']))
    for issue in catalog_numbering_issues(entries):
        warnings.append('源站编号混用（目录第%s—%s项）：%s → %s；%s' %
                        (issue['before_index'], issue['after_index'], issue['before'], issue['after'], issue['reason']))
    mixed = _mixed_numbering(entries, _numbering_scopes(entries))
    entries = [entry if i not in mixed else dict(title='') for i, entry in enumerate(entries)]
    anomalies = {i for i in numbering_anomalies(entries)
                 if not (number(entries[i]['title']) == 1 and number(entries[i-1]['title']) > 1)}
    for i in sorted(anomalies):
        warnings.append('源站章号疑似编号错误：%s → %s → %s；仅凭编号无法确认缺章' % (entries[i-1]['title'], entries[i]['title'], entries[i+1]['title']))
    seen, repeated = set(), set()
    previous, previous_index = None, None
    for i, entry in enumerate(entries):
        n = number(entry['title'])
        if n is None:
            continue
        if n == 1 and previous is not None and previous > 1:
            seen, repeated = set(), set()
        elif previous is not None and n < previous and i not in anomalies and previous_index not in anomalies:
            warnings.append('源站目录顺序或章号异常：%s → %s；保留源站顺序，请核对' %
                            (entries[previous_index]['title'], entry['title']))
        if n in seen and n not in repeated:
            warnings.append('源站目录存在重复章号 %s（可能分段或误编号）；保留全部章节，请核对' % n)
            repeated.add(n)
        seen.add(n)
        previous, previous_index = n, i
    return warnings


def title_matches(catalog_title, page_title, allow_prefix=False):
    a, b = canonical(catalog_title), canonical(page_title)
    if a == b:
        return True
    # Only opt-in sources with confirmed truncated catalog labels may use this.
    return (allow_prefix and len(a) >= 16 and b.startswith(a)
            and number(catalog_title) is not None and number(catalog_title) == number(page_title))


def select(doc, selector):
    if not selector:
        return []
    if '@js:' in selector:
        raise ValueError('此书源需要 JavaScript 解析，暂不支持严格校验；请换用静态目录书源')
    return doc.xpath(selector) if selector.startswith('/') else doc.cssselect(selector)


def text_of(doc, selector):
    nodes = select(doc, selector)
    return nodes[0].text_content().strip() if nodes else ''


def safe_url(base, value):
    result = urldefrag(urljoin(base, value))[0]
    if urlsplit(result).scheme not in ('http', 'https'):
        raise ValueError('无效的章节或分页网址')
    return result


class PermanentFetchError(ValueError):
    """A request that cannot be repaired by repeating it in this download."""
    retryable = False

    def __init__(self, message, url='', status_code=None, reason_code='permanent'):
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.reason_code = reason_code


def retry_after_seconds(value, now=None):
    """Read delta seconds or an HTTP date without truncating a server's wait."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError, OSError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


class Fetcher:
    def __init__(self, workers=6, interval=.15, retries=4, timeout=25, stop=None):
        self.workers = max(1, int(workers))
        self.interval = interval
        self.retries = retries
        self.timeout = timeout
        self.stop = stop or threading.Event()
        self.lock = threading.Lock()
        self.local = threading.local()
        self.next_request = 0
        self.cooldown = 0
        self.slowdown = 1.0
        self.successes = 0
        self.active_limit = self.workers
        self.active = 0
        self.requests = 0
        self.retry_count = 0
        self.rate_limits = 0
        self.success_streak = 0
        self.access_refusals = {}

    def snapshot(self):
        with self.lock:
            return dict(requests=self.requests, retries=self.retry_count, active=self.active,
                        active_limit=self.active_limit, max_workers=self.workers,
                        interval_ms=round(self.interval * self.slowdown * 1000),
                        rate_limits=self.rate_limits,
                        cooldown_seconds=round(max(0, self.cooldown - time.monotonic()), 1))

    def wait(self, seconds):
        if self.stop.wait(max(0, seconds)):
            raise InterruptedError('下载已停止')

    def _remember_refusal(self, error):
        if error.status_code in (401, 403) or error.reason_code in ('challenge', 'certificate', 'long_cooldown'):
            address = urlsplit(error.url)
            with self.lock:
                self.access_refusals.setdefault((address.scheme, address.netloc), (
                    str(error), error.status_code, error.reason_code))

    def _refusal_locked(self, url):
        address = urlsplit(url)
        refusal = self.access_refusals.get((address.scheme, address.netloc))
        if refusal is not None:
            message, status, reason = refusal
            # A fresh exception avoids sharing traceback state across threads.
            raise PermanentFetchError('已停止继续请求此书源：' + message, url, status, reason)

    def _acquire(self, url):
        # A bounded request gate, separate from the chapter executor. Rate-limit
        # responses can reduce active requests even while many chapters are queued.
        while True:
            if self.stop.is_set():
                raise InterruptedError('下载已停止')
            with self.lock:
                self._refusal_locked(url)
                now = time.monotonic()
                delay = max(self.next_request, self.cooldown) - now
                if self.active < self.active_limit and delay <= 0:
                    if self.stop.is_set():
                        raise InterruptedError('下载已停止')
                    self.active += 1
                    self.next_request = now + self.interval * self.slowdown
                    return
            # Check cancellation while waiting for a slot as well as a cooldown.
            self.wait(min(.1, max(.01, delay)))

    def _rate_limited(self, response, attempt, request_url=None):
        requested_wait = retry_after_seconds(response.headers.get('Retry-After'))
        if requested_wait > 120:
            with self.lock:
                self.rate_limits += 1
                self.success_streak = 0
                self.active_limit = max(1, self.active_limit // 2)
                self.slowdown = min(12, self.slowdown * 1.7)
            raise PermanentFetchError('HTTP %s：书源要求等待超过 120 秒，本次停止请求；请稍后再试或更换书源' %
                                      response.status_code, request_url or response.url,
                                      response.status_code, 'long_cooldown')
        pause = min(120, max(3 * 2 ** attempt,
                            requested_wait)
                    + random.uniform(0, .5))
        with self.lock:
            self.rate_limits += 1
            self.success_streak = 0
            self.active_limit = max(1, self.active_limit // 2)
            self.slowdown = min(12, self.slowdown * 1.7)
            self.cooldown = max(self.cooldown, time.monotonic() + pause)
        emit('log', message='书源限流 HTTP %s，并发降至 %s，等待 %.1f 秒后重试' %
             (response.status_code, self.active_limit, pause))

    @staticmethod
    def _permanent_http(status):
        return 400 <= status < 500 and status not in (408, 425, 429)

    def get(self, url):
        if not hasattr(self.local, 'session'):
            session = requests.Session()
            session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36'
            self.local.session = session
        error = None
        for attempt in range(self.retries + 1):
            self._acquire(url)
            try:
                if self.stop.is_set():
                    raise InterruptedError('下载已停止')
                with self.lock:
                    self._refusal_locked(url)
                    self.requests += 1
                    self.retry_count += int(attempt > 0)
                response = self.local.session.get(url, timeout=(10, self.timeout))
                if self._permanent_http(response.status_code):
                    reason = ('网站拒绝访问或要求登录验证，请更换书源' if response.status_code in (401, 403)
                              else '页面不存在或请求接口已失效，请更换书源')
                    raise PermanentFetchError('HTTP %s：%s；%s' % (response.status_code, reason, url),
                                              url, response.status_code, 'http_permanent')
                if response.status_code in (429, 503):
                    self._rate_limited(response, attempt, url)
                response.raise_for_status()
                if not response.content:
                    raise ValueError('服务器返回空页面')
                # Prefer declared HTML encoding; requests defaults to Latin-1 for GBK pages.
                charset = re.search(br'charset\s*=\s*[\"\x27]?([\w-]+)', response.content[:8192], re.I)
                encoding = charset[1].decode('ascii') if charset else response.encoding
                if not encoding or encoding.lower() == 'iso-8859-1':
                    encoding = response.apparent_encoding or 'utf-8'
                content = response.content.decode(encoding, errors='strict')
                doc = LH.fromstring(content, base_url=response.url)
                page_title = ''.join(doc.xpath('//title/text()')).lower()
                if any(s in page_title for s in ('just a moment', 'access denied', 'attention required', '人机验证', '安全验证')):
                    raise PermanentFetchError('返回验证页面，未获得小说正文；请更换书源', url,
                                              reason_code='challenge')
                with self.lock:
                    self.successes += 1
                    self.success_streak += 1
                    if self.success_streak >= 20 and time.monotonic() >= self.cooldown:
                        self.success_streak = 0
                        self.active_limit = min(self.workers, self.active_limit + 1)
                        self.slowdown = max(1, self.slowdown * .85)
                return doc, response.url
            except PermanentFetchError as exc:
                self._remember_refusal(exc)
                raise
            except (requests.RequestException, ValueError, LookupError) as exc:
                error = exc
                with self.lock:
                    self.success_streak = 0
                status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
                certificate_failure = isinstance(exc, requests.exceptions.SSLError) and any(
                    marker in str(exc).lower() for marker in
                    ('certificate_verify_failed', 'certificate verify failed', 'self signed certificate'))
                if certificate_failure:
                    permanent = PermanentFetchError('书源证书校验失败，请更换书源；%s' % url, url,
                                                    reason_code='certificate')
                    self._remember_refusal(permanent)
                    raise permanent from exc
                if status is not None and self._permanent_http(status):
                    permanent = PermanentFetchError('HTTP %s：页面不可用，请更换书源；%s' % (status, url),
                                                    url, status, 'http_permanent')
                    self._remember_refusal(permanent)
                    raise permanent from exc
                if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                                    requests.exceptions.InvalidSchema)):
                    raise PermanentFetchError('书源网址无效；%s' % url, url, reason_code='invalid_url') from exc
            finally:
                with self.lock:
                    self.active -= 1
            if attempt < self.retries:
                self.wait(min(30, 1.5 * 2 ** attempt) + random.uniform(0, .3))
        raise ValueError('页面读取失败：%s；%s' % (url, error))


class Downloader:
    def __init__(self, args, stop=None):
        self.args = copy.copy(args)
        self.output = Path(args.output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.task_id = digest(args.url)[:16]
        self.cache = self.output / '.novel-cache' / self.task_id
        self.cache.mkdir(parents=True, exist_ok=True)
        self.report_path = self.cache / 'verification.json'
        self.stop = stop or threading.Event()
        self.rule = self.find_rule()
        limits = self.rule.get('crawl', {})
        self.args.workers = min(args.workers, max(1, int(limits.get('concurrency', args.workers))))
        self.fetch = Fetcher(self.args.workers,
                             max(args.interval, int(limits.get('minInterval', 0))) / 1000,
                             args.retries, args.timeout, self.stop)
        self.rule_hash = digest(json.dumps(self.rule, sort_keys=True, ensure_ascii=False))
        self.title = ''
        self.author = ''
        self.chapters = []
        self.chapter_urls = set()
        self.catalog_pages = []
        self.total_pages = 0
        self.completed = 0
        self.reused = 0
        self.started = time.monotonic()
        self.last_progress = 0
        self.confirm_gaps = None

    def find_rule(self):
        rules = json.loads(Path(self.args.rules).read_text(encoding='utf-8-sig'))
        host = urlsplit(self.args.url).hostname.removeprefix('www.')
        for rule in rules:
            if urlsplit(rule.get('url', '')).hostname and urlsplit(rule['url']).hostname.removeprefix('www.') == host:
                for section in ('toc', 'chapter'):
                    for value in rule.get(section, {}).values():
                        if isinstance(value, str) and '@js:' in value:
                            raise ValueError('该书源需要动态规则，无法确认完整性，请切换书源')
                return rule
        raise ValueError('所选规则中没有匹配该网址的书源，请切换书源规则')

    def template(self, value):
        if not value:
            return self.args.url
        if '%s' in value:
            pattern = self.rule.get('book', {}).get('url', '')
            match = re.search(pattern, self.args.url) if pattern else None
            if not match or not match.groups():
                raise ValueError('无法从详情页提取书籍编号')
            return value.replace('%s', match[1])
        return safe_url(self.args.url, value)

    def catalog(self):
        rule = self.rule['toc']
        todo = [self.template(rule.get('url'))]
        visited, entries, seen = set(), [], set()
        while todo:
            if rule.get('pageNumberRegex'):
                def page_number(target):
                    match = re.search(rule['pageNumberRegex'], target)
                    return int(match[1]) if match else 0
                todo.sort(key=page_number)
            url = todo.pop(0)
            if url in visited:
                continue
            if len(visited) >= 300:
                raise ValueError('目录分页超过安全上限，可能存在循环')
            visited.add(url)
            doc, actual = self.fetch.get(url)
            if not self.title:
                book = self.rule.get('book', {})
                def meta(name):
                    found = doc.xpath('//meta[@property="%s" or @name="%s"]/@content' % (name, name))
                    return found[0].strip() if found else ''
                self.title = (text_of(doc, book.get('bookName')) or meta('og:novel:book_name') or
                              text_of(doc, 'h1') or text_of(doc, 'title').split('_')[0].split('-')[0] or '小说')
                self.author = text_of(doc, book.get('author')) or meta('og:novel:author')
            if rule.get('list'):
                raise ValueError('目录包含额外列表转换，严格模式暂不支持该规则')
            items = select(doc, rule['item'])
            if not items:
                raise ValueError('目录页没有解析到章节：' + url)
            base = self.template(rule['baseUri']) if rule.get('baseUri') else actual
            for node in items:
                target = node.get('href')
                title = node.text_content().strip()
                if not target or not title or target.startswith(('#', 'javascript:')):
                    continue
                target = safe_url(base, target)
                if target not in seen:
                    seen.add(target)
                    entries.append(dict(title=title, url=target))
            for node in select(doc, rule.get('nextPage')):
                target = node.get('href') or node.get('value')
                if target and not target.startswith(('#', 'javascript:')):
                    page = safe_url(actual, target)
                    if page not in visited and page not in todo:
                        todo.append(page)
        if rule.get('isDesc'):
            entries.reverse()
        if not entries:
            raise ValueError('目录为空，不能生成小说')
        return entries, sorted(visited)

    def cache_path(self, chapter):
        return self.cache / (digest(chapter['url']) + '.json')

    def validate(self, chapter, data):
        if data.get('version') != VERSION or data.get('rule_hash') != self.rule_hash:
            raise ValueError('缓存版本或书源规则已变化')
        if data.get('url') != chapter['url'] or canonical(data.get('title', '')) != canonical(chapter['title']):
            raise ValueError('缓存目录不匹配')
        body = data.get('body', '')
        if data.get('sha256') != digest(body):
            raise ValueError('缓存内容校验值不匹配')
        if not data.get('pages') or data.get('pages', [None])[0] != chapter['url']:
            raise ValueError('缺少分页抓取记录')
        self.check_body(body)
        return data

    def check_body(self, body):
        count = len(re.sub(r'\s', '', body))
        if count < self.args.min_chars:
            raise ValueError('正文仅 %s 字，低于短章阈值 %s，需确认或更换书源' % (count, self.args.min_chars))
        if '\ufffd' in body:
            raise ValueError('正文含解码替换字符')
        if re.search(r'正文加载中|章节内容正在加载|本章内容缺失|本章节缺失|请开启JavaScript|访问过于频繁|验证您是真人|本章未完[，,]?\s*请点击下一页', body, re.I):
            raise ValueError('正文包含加载、缺章或未完成提示')

    def chapter(self, chapter, force=False):
        path = self.cache_path(chapter)
        if path.exists() and not force and not self.args.refresh:
            try:
                return self.validate(chapter, json.loads(path.read_text(encoding='utf-8'))), True
            except (ValueError, KeyError, TypeError):
                pass
        rule = self.rule['chapter']
        url, pages, texts, body_hashes = chapter['url'], [], [], set()
        expected_body_pages = None
        while url:
            if url in pages or len(pages) >= 80:
                raise ValueError('正文分页循环或超过 80 页')
            doc, actual = self.fetch.get(url)
            page_title = text_of(doc, rule.get('title'))
            if rule.get('titlePrefixRegex'):
                page_title = re.sub(rule['titlePrefixRegex'], '', page_title, count=1).strip()
            if rule.get('titleSuffixRegex'):
                page_title = re.sub(rule['titleSuffixRegex'], '', page_title, count=1).strip()
            if not page_title or not title_matches(chapter['title'], page_title, rule.get('catalogTitleTruncated', False)):
                raise ValueError('目录与正文标题不匹配：%s / %s' % (chapter['title'], page_title))
            nodes = select(doc, rule['content'])
            if len(nodes) != 1:
                raise ValueError('正文选择器匹配 %s 个区块，无法确认正文范围' % len(nodes))
            content = copy.deepcopy(nodes[0])
            for bad in select(content, rule.get('filterTag')) + content.xpath('.//script|.//style|.//h1'):
                if bad.getparent() is not None:
                    bad.drop_tree()
            for br in content.xpath('.//br'):
                br.tail = '\n' + (br.tail or '')
            for p in content.xpath('.//p|.//div'):
                p.tail = '\n' + (p.tail or '')
            body = content.text_content().strip()
            body_page_count = None
            if rule.get('bodyPageCountRegex'):
                body_page_count = re.search(rule['bodyPageCountRegex'], body)
                if not body_page_count or int(body_page_count[1]) != len(pages) + 1:
                    raise ValueError('正文分页编号缺失或不连续')
                total = int(body_page_count[2])
                if total < 1 or (expected_body_pages is not None and total != expected_body_pages):
                    raise ValueError('同一章的正文总页数发生变化')
                expected_body_pages = total
            if rule.get('filterTxt'):
                body = re.sub(rule['filterTxt'], '', body)
            body = '\n'.join(s.strip() for s in body.splitlines() if s.strip())
            # Some sources repeat the heading as plain text in the content container.
            if body.startswith(page_title):
                body = body[len(page_title):].lstrip()
            if not body:
                raise ValueError('正文分页为空')
            body_hash = digest(re.sub(r'\s', '', body))
            if body_hash in body_hashes:
                raise ValueError('同一章的多个分页正文重复')
            body_hashes.add(body_hash)
            pages.append(url)
            texts.append(body)
            next_nodes = select(doc, rule.get('nextPage'))
            next_urls = []
            for node in next_nodes:
                target = node.get('href') or node.get('value')
                label = node.text_content().strip()
                if target and not target.startswith(('#', 'javascript:')) and (rule.get('nextPageEvenIfChapterLabel') or not re.search('下一章|下章|目录|上一', label)):
                    candidate = safe_url(actual, target)
                    if candidate not in self.chapter_urls or candidate == chapter['url']:
                        next_urls.append(candidate)
            if len(set(next_urls)) > 1:
                raise ValueError('发现多个不同的下一页地址')
            url = next_urls[0] if next_urls else None
            if body_page_count and bool(url) != (int(body_page_count[1]) < int(body_page_count[2])):
                raise ValueError('正文分页数量与下一页链接不一致，已阻止残章')
            if not url:
                page_count = re.search(r'(\d+)\s*[/／]\s*(\d+)\s*页?', page_title)
                if page_count and int(page_count[1]) < int(page_count[2]):
                    raise ValueError('正文标题提示仍有后续分页，但没有可读取的下一页链接')
                # Don't silently truncate a source whose rule omitted pagination.
                for link in doc.xpath('//a[@href]'):
                    label = link.text_content().strip()
                    if re.fullmatch(r'(?:下[一1]?页|下一页[»>›]*)', label):
                        target = link.get('href', '')
                        if target and not target.startswith(('#', 'javascript:')):
                            candidate = safe_url(actual, target)
                            if candidate not in self.chapter_urls and candidate not in pages:
                                raise ValueError('发现未被书源规则覆盖的正文下一页，已阻止生成残章')
        data = dict(version=VERSION, rule_hash=self.rule_hash, title=chapter['title'], url=chapter['url'],
                    body='\n'.join(texts), pages=pages, fetched_at=time.time())
        data['sha256'] = digest(data['body'])
        self.validate(chapter, data)
        atomic_json(path, data)
        return data, False

    def progress(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress < .12 and self.completed != len(self.chapters):
            return
        self.last_progress = now
        emit('progress', current=self.completed, total=len(self.chapters), reused=self.reused,
             pages=self.total_pages, seconds=time.monotonic() - self.started,
             network=self.fetch.snapshot())

    def run(self):
        with task_lock(self.cache / 'download.lock'):
            atomic_json(self.report_path, dict(status='running', source=self.args.url))
            try:
                return self._run()
            except Exception as exc:
                report = json.loads(self.report_path.read_text(encoding='utf-8'))
                report.update(status='paused' if isinstance(exc, InterruptedError) else 'blocked',
                              source=self.args.url, reason=str(exc), network=self.fetch.snapshot())
                atomic_json(self.report_path, report)
                raise

    def _run(self):
        emit('log', message='当前书源：最多 %s 章并行，请求间隔至少 %s 毫秒；限流时自动延长间隔' %
             (self.args.workers, round(self.fetch.interval * 1000)))
        emit('stage', message='正在核对源站完整目录…')
        self.chapters, self.catalog_pages = self.catalog()
        warnings = catalog_warnings(self.chapters)
        gaps = catalog_gaps(self.chapters)
        numbering_issues = catalog_numbering_issues(self.chapters)
        review = sorted(gaps + numbering_issues, key=lambda item: item['before_index'])
        for warning in warnings:
            emit('log', message=warning)
        self.chapter_urls = {c['url'] for c in self.chapters}
        atomic_json(self.cache / 'manifest.json', dict(url=self.args.url, title=self.title,
                    chapters=self.chapters, catalog_pages=self.catalog_pages, rule_hash=self.rule_hash))
        emit('book', title=self.title, author=self.author, total=len(self.chapters))
        consent = None
        if review:
            atomic_json(self.report_path, dict(status='awaiting_confirmation', source=self.args.url,
                        title=self.title, chapters=len(self.chapters), missing_chapters=gaps,
                        numbering_issues=numbering_issues, warnings=warnings))
            if self.stop.is_set():
                raise InterruptedError('下载已停止')
            if self.confirm_gaps is not None:
                accepted = self.confirm_gaps(self.title, review, len(self.chapters))
            else:
                accepted = getattr(self.args, 'allow_missing', False)
                if not accepted:
                    raise ValueError('目录有疑似缺号或编号异常，尚未确认继续下载；核对清单已写入校验报告。命令行确认后可使用 --allow-missing')
            if accepted is not True or self.stop.is_set():
                raise InterruptedError('已取消：未下载需要核对的目录')
            consent = dict(accepted=True, at=time.time())
            atomic_json(self.report_path, dict(status='running', source=self.args.url, title=self.title,
                        missing_chapters=gaps, numbering_issues=numbering_issues,
                        warnings=warnings, gap_confirmation=consent))
            emit('log', message='已确认继续下载源站现有 %s 个目录条目；疑似缺号与编号异常清单保留在校验报告中' % len(self.chapters))
        emit('stage', message='正在下载及校验章节（自动补抓失败章节）…')
        results, failures, permanent_failures = {}, {}, set()
        pending = list(enumerate(self.chapters))
        for round_no in range(2):
            if not pending or self.stop.is_set():
                break
            workers = self.args.workers if round_no == 0 else max(1, self.args.workers // 2)
            if round_no:
                emit('stage', message='正在降低并发补抓 %s 个问题章节…' % len(pending))
            with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                # Bounded queue avoids creating thousands of futures for large novels.
                iterator = iter(pending)
                active = {}
                def fill():
                    while len(active) < workers * 2 and not self.stop.is_set():
                        item = next(iterator, None)
                        if item is None:
                            break
                        i, chapter = item
                        active[pool.submit(self.chapter, chapter, round_no > 0)] = i
                fill()
                while active:
                    done, _ = futures.wait(active, return_when=futures.FIRST_COMPLETED)
                    for future in done:
                        i = active.pop(future)
                        try:
                            data, reused = future.result()
                            results[i] = data
                            failures.pop(i, None)
                            self.completed += 1
                            self.reused += int(reused)
                            self.total_pages += len(data['pages'])
                            self.progress()
                        except Exception as exc:
                            failures[i] = str(exc)
                            if isinstance(exc, PermanentFetchError):
                                permanent_failures.add(i)
                            emit('log', message=self.chapters[i]['title'] + '：' + str(exc))
                    fill()
            pending = [(i, self.chapters[i]) for i in failures if i not in permanent_failures]
            self.progress(force=True)
        if self.stop.is_set():
            raise InterruptedError('下载已停止，已校验缓存已保留')
        # A duplicate body under different titles is not counted as complete.
        hashes = {}
        for i in sorted(results):
            value = digest(re.sub(r'\s', '', results[i]['body']))
            if value in hashes:
                failures[i] = '正文与 %s 重复，需换源确认' % self.chapters[hashes[value]]['title']
            else:
                hashes[value] = i
        if failures:
            self.completed = len(results) - len([i for i in failures if i in results])
            self.progress(force=True)
            atomic_json(self.report_path, dict(status='blocked', expected=len(self.chapters),
                        missing_chapters=gaps, numbering_issues=numbering_issues, warnings=warnings, gap_confirmation=consent,
                        validated=len(results) - len([i for i in failures if i in results]),
                        problems=[dict(index=i + 1, **self.chapters[i], reason=reason,
                                       retryable=i not in permanent_failures) for i, reason in sorted(failures.items())]))
            raise ValueError('%s 章未通过检查；未生成成品。问题清单：%s' % (len(failures), self.report_path))
        emit('stage', message='章节已抓齐，正在复核目录是否变化…')
        latest, _ = self.catalog()
        if latest != self.chapters:
            raise ValueError('下载期间源站目录发生变化，请再次下载以补齐；缓存已保留')
        emit('stage', message='目录与正文检查通过，正在生成并验证文件…')
        ordered = [results[i] for i in range(len(self.chapters))]
        result = self.export(ordered)
        report = dict(status='completed_with_warnings' if warnings else 'passed', warnings=warnings,
                      missing_chapters=gaps, numbering_issues=numbering_issues, gap_confirmation=consent,
                      scope='仅下载源站现有目录；目录疑似缺章，不能视为完整本' if gaps else '当前目录已下载，存在章号异常，详见 warnings' if warnings else '当前源站目录；无法证明源站自身未删改正文或作品已完结',
                      source=self.args.url, title=self.title, chapters=len(ordered), pages=self.total_pages,
                      reused=self.reused, output=str(result), seconds=round(time.monotonic() - self.started, 2),
                      network=self.fetch.snapshot())
        atomic_json(self.report_path, report)
        emit('complete', **report, report=str(self.report_path))
        return result

    def export(self, chapters):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', self.title).strip('. ')[:90] or '小说'
        target = self.output / (name + '-' + self.task_id[:8] + '.' + self.args.format)
        temp = target.with_suffix(target.suffix + '.partial')
        if self.args.format == 'epub':
            with zipfile.ZipFile(temp, 'w') as z:
                z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
                def write(path, content):
                    z.writestr(path, content, compress_type=zipfile.ZIP_DEFLATED)
                write('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                manifest, spine, nav, ncx = [], [], [], []
                for i, chapter in enumerate(chapters, 1):
                    href = 'chapters/%05d.xhtml' % i
                    manifest.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>' % (i, href))
                    spine.append('<itemref idref="c%d"/>' % i)
                    nav.append('<li><a href="%s">%s</a></li>' % (href, html.escape(chapter['title'])))
                    ncx.append('<navPoint id="n%d" playOrder="%d"><navLabel><text>%s</text></navLabel><content src="%s"/></navPoint>' % (i, i, html.escape(chapter['title']), href))
                    write('OEBPS/' + href, self.xhtml(chapter))
                write('OEBPS/nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head><body><nav epub:type="toc"><ol>' + ''.join(nav) + '</ol></nav></body></html>')
                write('OEBPS/toc.ncx', '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="%s"/></head><docTitle><text>%s</text></docTitle><navMap>%s</navMap></ncx>' % (self.task_id, html.escape(self.title), ''.join(ncx)))
                write('OEBPS/content.opf', '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">%s</dc:identifier><dc:title>%s</dc:title><dc:creator>%s</dc:creator><dc:language>zh-CN</dc:language><meta property="dcterms:modified">%s</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>%s</manifest><spine toc="ncx">%s</spine></package>' % (self.task_id, html.escape(self.title), html.escape(self.author), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), ''.join(manifest), ''.join(spine)))
            with zipfile.ZipFile(temp) as z:
                if z.testzip() is not None:
                    raise ValueError('EPUB 压缩包校验失败')
                ns = {'o': 'http://www.idpf.org/2007/opf'}
                opf = ET.fromstring(z.read('OEBPS/content.opf'))
                items = {item.attrib['id']: item.attrib['href'] for item in opf.findall('o:manifest/o:item', ns)}
                refs = opf.findall('o:spine/o:itemref', ns)
                if len(refs) != len(chapters):
                    raise ValueError('EPUB 阅读顺序条目数不匹配')
                for ref, chapter in zip(refs, chapters):
                    node = ET.fromstring(z.read('OEBPS/' + items[ref.attrib['idref']]))
                    actual = '\n'.join(p.text or '' for p in node.findall('.//{http://www.w3.org/1999/xhtml}p'))
                    if actual != chapter['body']:
                        raise ValueError('EPUB 正文回读不一致')
                ET.fromstring(z.read('OEBPS/nav.xhtml'))
                ET.fromstring(z.read('OEBPS/toc.ncx'))
        elif self.args.format == 'txt':
            content = self.title + '\n' + self.author + '\n\n' + '\n\n'.join(c['title'] + '\n' + c['body'] for c in chapters)
            temp.write_text(content, encoding='utf-8')
            if temp.read_text(encoding='utf-8') != content:
                raise ValueError('TXT 回读校验失败')
        elif self.args.format == 'html':
            content = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + html.escape(self.title) + '</title><body>' + ''.join('<section><h2>' + html.escape(c['title']) + '</h2>' + ''.join('<p>' + html.escape(p) + '</p>' for p in c['body'].split('\n')) + '</section>' for c in chapters) + '</body></html>'
            temp.write_text(content, encoding='utf-8')
            doc = LH.fromstring(temp.read_text(encoding='utf-8'))
            sections = doc.xpath('//section')
            if len(sections) != len(chapters) or any('\n'.join(p.text_content() for p in s.xpath('./p')) != c['body'] for s, c in zip(sections, chapters)):
                raise ValueError('HTML 回读校验失败')
        else:
            raise ValueError('严格下载支持 EPUB、TXT、HTML')
        if self.stop.is_set():
            raise InterruptedError('已停止生成；保留章节缓存')
        os.replace(temp, target)
        return target

    @staticmethod
    def xhtml(chapter):
        return '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh"><head><title>%s</title></head><body><h1>%s</h1>%s</body></html>' % (html.escape(chapter['title']), html.escape(chapter['title']), ''.join('<p>%s</p>' % html.escape(p) for p in chapter['body'].split('\n')))


def create_downloader(args, stop=None):
    if urlsplit(args.url).hostname == 'fanqienovel.com':
        from fanqie_source import FanqieDownloader
        return FanqieDownloader(args, stop)
    return Downloader(args, stop)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--rules', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--format', choices=['epub', 'txt', 'html'], default='epub')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--interval', type=int, default=150)
    parser.add_argument('--retries', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=25)
    parser.add_argument('--min-chars', type=int, default=100)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--allow-missing', action='store_true', help='已查看缺章报告，并明确同意仅下载现有目录')
    args = parser.parse_args()
    if not 1 <= args.workers <= 16 or not 0 <= args.retries <= 8 or not 0 <= args.interval <= 10000 or not 5 <= args.timeout <= 120 or not 1 <= args.min_chars <= 5000:
        parser.error('下载参数超出允许范围')
    stop = threading.Event()
    def read_stop():
        for line in sys.stdin:
            if line.strip() == 'stop':
                stop.set()
                return
    threading.Thread(target=read_stop, daemon=True).start()
    downloader = None
    try:
        downloader = create_downloader(args, stop)
        downloader.run()
    except InterruptedError as exc:
        emit('cancelled', message=str(exc))
        return 2
    except Exception as exc:
        emit('error', message=str(exc), report=str(downloader.report_path) if downloader else '')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
