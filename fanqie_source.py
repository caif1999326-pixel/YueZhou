"""Public Fanqie pages; fail closed on unavailable text or unknown font encodings."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from lxml import html
import reliable_engine as engine

SEARCH_URL = 'https://novel.snssdk.com/api/novel/channel/homepage/search/search/v1/'
FONT = json.loads(Path(__file__).with_name('fanqie_font.json').read_text(encoding='utf-8'))
TRANSLATION = {int(k): v for k, v in FONT['mapping'].items()}


def state(doc):
    for script in doc.xpath('//script/text()'):
        match = re.search(r'window\.__INITIAL_STATE__\s*=\s*', script)
        if match:
            return json.JSONDecoder().raw_decode(script[match.end():])[0]
    raise ValueError('番茄网页未提供可读取的数据，请稍后重试或换源')


def search(query):
    response = requests.get(SEARCH_URL, params=dict(device_platform='android',
        parent_enterfrom='novel_channel_search.tab.', offset=0, aid=1967, q=query),
        headers={'User-Agent': 'Mozilla/5.0'}, timeout=(5, 15))
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or not isinstance(payload.get('data', {}).get('ret_data'), list):
        raise ValueError('番茄搜索接口返回异常')
    rows, seen = [], set()
    for item in payload['data']['ret_data']:
        bid = str(item.get('book_id', ''))
        title = html.fromstring('<div>' + item.get('title', '') + '</div>').text_content().strip()
        if bid.isdigit() and bid not in seen and title:
            seen.add(bid)
            rows.append(dict(title=title, author=item.get('author', ''), source='番茄小说',
                             latest='', url='https://fanqienovel.com/page/' + bid))
    return rows


def catalog(fetch, url):
    match = re.fullmatch(r'/page/(\d+)/?', urlsplit(url).path)
    if not match:
        raise ValueError('请使用番茄书籍详情页网址：https://fanqienovel.com/page/书籍编号')
    doc, actual = fetch.get(url)
    page = state(doc).get('page', {})
    if str(page.get('bookId')) != match[1] or not page.get('bookName'):
        raise ValueError('此书未提供可读取的番茄网页目录，可能仅支持 App 阅读')
    chapters, ids = [], set()
    for volume in page.get('chapterListWithVolume', []):
        for entry in volume:
            cid = str(entry.get('itemId', ''))
            if not cid.isdigit() or cid in ids or not entry.get('title'):
                raise ValueError('番茄目录含重复或无效章节，已停止')
            ids.add(cid)
            chapters.append(dict(title=entry['title'], url='https://fanqienovel.com/reader/' + cid,
                item_id=cid, book_id=match[1], order=int(entry['realChapterOrder'])))
    chapters.sort(key=lambda c: c['order'])
    if not chapters or len(chapters) != int(page.get('chapterTotal', 0)):
        raise ValueError('番茄目录数量与官方总章数不一致，已阻止缺章下载')
    if [c['order'] for c in chapters] != list(range(1, len(chapters) + 1)):
        raise ValueError('番茄目录章节序号不连续，已停止')
    return chapters, [actual], page


def decode_content(content, css):
    if re.search('[\ue000-\uf8ff]', content):
        if FONT['font_file'] not in css:
            raise ValueError('番茄字体编码已变化，需更新适配，未导出乱码正文')
        content = content.translate(TRANSLATION)
    if re.search('[\ue000-\uf8ff]', content):
        raise ValueError('番茄正文含未知字体字符，已停止')
    doc = html.fromstring(re.sub(r'<\?xml.*?\?>', '', content, flags=re.S))
    for bad in doc.xpath('//script|//style|//head'):
        if bad.getparent() is not None:
            bad.drop_tree()
    for node in doc.xpath('//p|//div|//br'):
        node.tail = '\n' + (node.tail or '')
    return '\n'.join(line.strip() for line in doc.text_content().splitlines() if line.strip())


class FanqieDownloader(engine.Downloader):
    def __init__(self, args, stop=None):
        super().__init__(args, stop)
        self.rule_hash = engine.digest(self.rule_hash + json.dumps(FONT, sort_keys=True))
        # Published front matter may be short; validate against official word count below.
        self.args.min_chars = 1

    def catalog(self):
        chapters, pages, page = catalog(self.fetch, self.args.url)
        self.title, self.author = page['bookName'], page.get('author', '')
        return chapters, pages

    def validate(self, chapter, data):
        super().validate(chapter, data)
        expected = int(data.get('expected_chars', 0))
        actual = len(re.sub(r'\s', '', data['body']))
        if expected <= 0 or abs(actual - expected) > max(3, expected * .02):
            raise ValueError('正文长度与番茄公布字数不符：%s / %s，需核查' % (actual, expected))
        if re.search('[\ue000-\uf8ff]', data['body']):
            raise ValueError('正文含未还原的字体字符')
        return data

    def chapter(self, chapter, force=False):
        path = self.cache_path(chapter)
        if path.exists() and not force and not self.args.refresh:
            try:
                return self.validate(chapter, json.loads(path.read_text(encoding='utf-8'))), True
            except (ValueError, KeyError, TypeError):
                pass
        doc, _ = self.fetch.get(chapter['url'])
        payload = state(doc)
        data = payload.get('reader', {}).get('chapterData', {})
        if any(data.get(key) not in (None, False, 0, '0', '') for key in
               ('needPay', 'isChapterLock', 'isPaidPublication', 'isPaidStory')):
            raise ValueError('此章需要付费或未开放网页阅读，无法生成完整下载')
        if (str(data.get('itemId')) != chapter['item_id'] or str(data.get('bookId')) != chapter['book_id']
            or data.get('title') != chapter['title'] or int(data.get('realChapterOrder', 0)) != chapter['order']):
            raise ValueError('番茄正文与目录的书籍、章节或标题不匹配')
        if not data.get('content'):
            raise ValueError('番茄未返回此章完整正文，请在官方 App 阅读或更换书源')
        body = decode_content(data['content'], payload.get('common', {}).get('css', ''))
        result = dict(version=engine.VERSION, rule_hash=self.rule_hash, title=chapter['title'],
            url=chapter['url'], body=body, pages=[chapter['url']], fetched_at=time.time(),
            expected_chars=int(data.get('chapterWordNumber', 0)), sha256=engine.digest(body))
        self.validate(chapter, result)
        engine.atomic_json(path, result)
        return result, False
