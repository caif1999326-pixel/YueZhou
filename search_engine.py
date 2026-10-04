import concurrent.futures
import json
import random
import re
import threading
from types import SimpleNamespace
from urllib.parse import quote, urljoin, urlsplit

import requests
from lxml import html
from reliable_engine import Downloader, Fetcher, select, text_of, catalog_warnings, catalog_gaps


class CatalogPreview(Downloader):
    """Read the same complete catalog as downloading, without creating cache files."""
    def __init__(self, rule, url, stop):
        self.args = SimpleNamespace(url=url)
        self.rule = rule
        self.title, self.author = '目录预览', ''
        self.fetch = Fetcher(workers=1, interval=.25, retries=0, timeout=10, stop=stop)


def catalog_summary(rule, url, stop=None, fetch=None):
    if rule.get('adapter') == 'fanqie':
        from fanqie_source import catalog
        chapters, _, _ = catalog(fetch or Fetcher(workers=1, interval=.7, retries=0, timeout=12, stop=stop), url)
        return dict(count=len(chapters), latest=chapters[-1]['title'])
    preview = CatalogPreview(rule, url, stop)
    if fetch is not None:
        preview.fetch = fetch
    chapters, _ = preview.catalog()
    result = dict(count=len(chapters), latest=chapters[-1]['title'])
    warnings = catalog_warnings(chapters)
    if warnings:
        result['warnings'] = warnings
        result['gaps'] = catalog_gaps(chapters)
    return result


def supported(rule):
    search = rule.get('search') or {}
    if rule.get('disabled') or search.get('disabled') or not search.get('url'):
        return False
    if 'quanben5.com' in rule['url']:
        return True
    return not any('@js:' in str(v) for part in ('search', 'toc', 'chapter') for v in rule.get(part, {}).values()) and not rule.get('toc', {}).get('list')


def search_source(rule, query):
    if rule.get('adapter') == 'fanqie':
        from fanqie_source import search
        return search(query)
    spec = rule['search']
    endpoint = spec['url']
    special = 'quanben5.com' in rule['url']
    params = None
    if special:
        alphabet = 'PXhw7UT1B0a9kQDKZsjIASmOezxYG4CHo5Jyfg2b8FLpEvRr3WtVnlqMidu6cN'
        value = ''.join(random.choice(alphabet) + (alphabet[(alphabet.index(c)+3)%62] if c in alphabet else c) + random.choice(alphabet) for c in quote(query, safe=";/?:@&=+$,#"))
        endpoint = urljoin(rule['url'], '/')
        params = dict(c='book', a='search.json', callback='search', keywords=query, b=value)
    else:
        endpoint = endpoint.replace('%s', quote(query))
    data = {}
    for key, value in re.findall(r'([\w]+)\s*:\s*([^,}]+)', spec.get('data', '')):
        data[key] = value.strip().strip('\"\'').replace('%s', query)
    with requests.Session() as session:
        session.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': rule['url']})
        method = spec.get('method', 'get').lower()
        if method == 'get':
            params = dict(data, **(params or {})) or None
            data = {}
        response = session.request(method, endpoint, params=params, data=data or None, timeout=(5, 12))
        response.raise_for_status()
        charset = re.search(br'charset\s*=\s*[\"\x27]?([\w-]+)', response.content[:8192], re.I)
        encoding = charset[1].decode() if charset else response.encoding
        if not encoding or encoding.lower() == 'iso-8859-1':
            encoding = response.apparent_encoding or 'utf-8'
        content = response.content.decode(encoding, errors='strict')
    if special:
        match = re.search(r'search\((.*)\)\s*;?\s*$', content, re.S)
        if not match:
            raise ValueError('搜索接口返回异常')
        content = json.loads(match[1])['content']
    doc = html.fromstring(content)
    title = ''.join(doc.xpath('//title/text()')).lower()
    if any(s in title for s in ('just a moment', 'access denied', '人机验证', '安全验证', '验证码')):
        raise ValueError('书源要求验证，暂不可用')
    if any(s in title for s in ('需要登录', '请先登录', '用户登录', '登录 -')):
        raise ValueError('搜索服务要求登录，当前无法匿名搜索')
    if any(s in content.lower() for s in ('has been recently registered with namecheap', 'domain is for sale')):
        raise ValueError('原域名已变为停放页')
    rows = select(doc, '.pic_txt_list' if special else spec['result'])
    result, seen = [], set()
    for row in rows:
        nodes = select(row, spec['bookName'])
        if not nodes:
            continue
        node = nodes[0]
        links = [node] if node.get('href') else node.xpath('.//a[@href]')
        if not links:
            continue
        title = node.text_content().strip()
        if spec.get('titlePrefixRegex'):
            title = re.sub(spec['titlePrefixRegex'], '', title).strip()
        url = urljoin(response.url, links[0].get('href'))
        if not title or url in seen or urlsplit(url).scheme not in ('http', 'https'):
            continue
        seen.add(url)
        author = re.sub(r'^\s*作者\s*[:：]\s*', '', text_of(row, spec.get('author')))
        result.append(dict(title=title, author=author, latest=text_of(row, spec.get('latestChapter')), source=rule['name'], url=url))
    return result


def failure_message(exc):
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code
        return {403:'网站拒绝访问或要求验证',404:'原搜索接口已失效',429:'请求过于频繁，请稍后再试',502:'网站服务暂不可用',503:'网站维护或限流'}.get(code, '网站返回 HTTP %s' % code)
    if isinstance(exc, requests.Timeout):
        return '连接超时，请稍后重试'
    if isinstance(exc, requests.exceptions.SSLError):
        return '安全连接失败，当前网络无法访问'
    if isinstance(exc, requests.ConnectionError):
        return '连接中断或域名无法访问'
    return str(exc)[:160]


def search_with_retry(rule, query, stop):
    for attempt in range(2):
        if stop.is_set():
            raise InterruptedError('搜索已取消')
        try:
            return search_source(rule, query)
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            response = getattr(exc, 'response', None)
            if attempt or (response is not None and response.status_code not in (429, 500, 502, 503, 504)):
                raise
            if stop.wait(1.5):
                raise InterruptedError('搜索已取消')


def search_all(rules, query, notify, stop=None, cache=None):
    stop = stop or threading.Event()
    sources = [r for r in rules if supported(r)]
    # Share request pacing across every preview for a source, not one timer per book.
    fetchers = {r['url']: Fetcher(workers=1, interval=1, retries=0, timeout=12, stop=stop) for r in sources}
    gates = {r['url']: threading.Lock() for r in sources}
    catalog_pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    def count(row, rule):
        if stop.is_set():
            return
        try:
            with gates[rule['url']]:
                if stop.is_set():
                    return
                info = catalog_summary(rule, row['url'], stop, fetchers[rule['url']])
            if not stop.is_set():
                notify('catalog_result', url=row['url'], **info)
        except Exception as exc:
            if not stop.is_set():
                notify('catalog_result', url=row['url'], count=None, message=str(exc))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        pending = {pool.submit(search_with_retry, r, query, stop): r for r in sources}
        for n, future in enumerate(concurrent.futures.as_completed(pending), 1):
            rule = pending[future]
            if stop.is_set():
                continue
            try:
                rows = future.result()
                if rows and cache:
                    cache.put(rule, query, rows)
                elif not rows and cache:
                    rows = cache.get(rule, query)
                    if rows:
                        notify('log', message=rule['name'] + '：本次未返回结果，显示最近成功搜索的历史结果（需重新核对目录）')
                notify('search_result', rows=rows, source=rule['name'], done=n, total=len(sources))
                for row in sorted(rows, key=lambda r: r['title'].strip() != query.strip()):
                    catalog_pool.submit(count, row, rule)
            except Exception as exc:
                notify('search_failure', source=rule['name'], message=failure_message(exc), detail=str(exc), done=n, total=len(sources))
                rows = cache.get(rule, query) if cache else []
                if rows and not stop.is_set():
                    notify('search_result', rows=rows, source=rule['name'], done=n, total=len(sources))
                    notify('log', message=rule['name'] + '：实时搜索失败，已保留历史结果；历史链接不代表当前可下载')
                    for row in rows:
                        catalog_pool.submit(count, row, rule)
    notify('search_done')
    catalog_pool.shutdown(wait=True, cancel_futures=stop.is_set())
    if not stop.is_set():
        notify('catalog_done')
