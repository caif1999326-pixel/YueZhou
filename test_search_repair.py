import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import requests
from search_cache import SearchCache
from search_engine import search_all, search_with_retry
from lxml import html
from test_reliable_engine import Fixture
from reliable_engine import Downloader

RULE = dict(name='测试', url='https://example.org/', search=dict(url='https://example.org/search'))
ROWS = [dict(title='我的混沌城', author='凌虚月影', url='https://example.org/book/1', source='测试', latest='第3章')]


class SearchRepairTests(unittest.TestCase):
    def test_temporary_failure_is_retried(self):
        stop = threading.Event()
        with patch.object(stop, 'wait', return_value=False), patch('search_engine.search_source', side_effect=[requests.ConnectionError('reset'), ROWS]) as request:
            self.assertEqual(search_with_retry(RULE, '我的混沌城', stop), ROWS)
            self.assertEqual(request.call_count, 2)

    def test_forbidden_is_not_retried(self):
        response = requests.Response(); response.status_code = 403
        with patch('search_engine.search_source', side_effect=requests.HTTPError(response=response)) as request:
            with self.assertRaises(requests.HTTPError):
                search_with_retry(RULE, 'x', threading.Event())
            self.assertEqual(request.call_count, 1)

    def test_cache_survives_restart_without_cross_query_or_stale_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SearchCache(tmp); cache.put(RULE, '我的混沌城', ROWS)
            self.assertEqual(SearchCache(tmp).get(RULE, '我的混沌城')[0]['title'], '我的混沌城')
            self.assertEqual(cache.get(RULE, 'other'), [])
            self.assertEqual(cache.get(dict(RULE, name='other'), '我的混沌城'), [])
            with patch('search_cache.time.time', return_value=time.time()+8*86400):
                self.assertEqual(cache.get(RULE, '我的混沌城'), [])

    def test_failed_or_empty_search_keeps_labelled_history(self):
        for outcome in ([], requests.ConnectionError('reset')):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                cache = SearchCache(tmp); cache.put(RULE, '我的混沌城', ROWS)
                events = []
                with patch('search_engine.search_with_retry', side_effect=[outcome]), patch('search_engine.catalog_summary', return_value=dict(count=3, latest='第3章')):
                    search_all([RULE], '我的混沌城', lambda kind, **data: events.append((kind, data)), cache=cache)
                row = next(data['rows'][0] for kind, data in events if kind == 'search_result')
                self.assertIn('cached_at', row)
                self.assertEqual(events[-1][0], 'catalog_done')
                self.assertIn('cached_at', cache.get(RULE, '我的混沌城')[0])

    def test_success_replaces_history_without_cached_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = SearchCache(tmp); cache.put(RULE, 'q', ROWS)
            fresh = [dict(ROWS[0], latest='第4章')]
            events = []
            with patch('search_engine.search_with_retry', return_value=fresh), patch('search_engine.catalog_summary', return_value=dict(count=4, latest='第4章')):
                search_all([RULE], 'q', lambda kind, **data: events.append((kind, data)), cache=cache)
            row = next(data['rows'][0] for kind, data in events if kind == 'search_result')
            self.assertNotIn('cached_at', row)
            self.assertEqual(cache.get(RULE, 'q')[0]['latest'], '第4章')

    def test_cancel_does_not_request(self):
        stop = threading.Event(); stop.set()
        with patch('search_engine.search_source') as request:
            with self.assertRaises(InterruptedError): search_with_retry(RULE, 'q', stop)
            request.assert_not_called()

    def test_mislabelled_next_chapter_follows_all_body_pages(self):
        self.body_pages([3, 3, 3], succeeds=True)

    def test_catalog_page_links_are_read_in_numeric_order(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture() as fixture:
            downloader = Downloader(fixture.args(tmp))
            downloader.rule['toc'] = dict(item='.chapters a', nextPage='.pager a', pageNumberRegex=r'/page/(\d+)')
            root = fixture.url.rsplit('/', 1)[0]
            pages = {
                fixture.url: '<h1>测试</h1><div class="chapters"><a href="/c1">第1章 A</a></div><div class="pager"><a href="/page/3">3</a><a href="/page/2">2</a></div>',
                root+'/page/2': '<div class="chapters"><a href="/c2">第2章 B</a></div>',
                root+'/page/3': '<div class="chapters"><a href="/c3">第3章 C</a></div>',
            }
            with patch.object(downloader.fetch, 'get', side_effect=lambda url: (html.fromstring(pages[url]), url)):
                chapters, _ = downloader.catalog()
            self.assertEqual([c['title'] for c in chapters], ['第1章 A', '第2章 B', '第3章 C'])

    def test_changed_body_page_count_blocks_truncation(self):
        self.body_pages([3, 2], succeeds=False)

    def body_pages(self, totals, succeeds):
        with tempfile.TemporaryDirectory() as tmp, Fixture() as fixture:
            downloader = Downloader(fixture.args(tmp))
            chapter = dict(title='第1章 标题1', url=fixture.url.replace('/book', '/c1'))
            next_chapter = fixture.url.replace('/book', '/c2')
            downloader.chapter_urls = {chapter['url'], next_chapter}
            downloader.rule['chapter'] = dict(title='h1', content='article', nextPage='#next',
                nextPageEvenIfChapterLabel=True, bodyPageCountRegex=r'第\((\d+)/(\d+)\)页', filterTxt=r'第\(\d+/\d+\)页')
            responses = []
            for i, total in enumerate(totals, 1):
                url = chapter['url'] if i == 1 else chapter['url'] + '-' + str(i)
                link = chapter['url'] + '-' + str(i + 1) if i < total else next_chapter
                doc = html.fromstring('<h1>第1章 标题1</h1><article>第(%s/%s)页%s</article><a id="next" href="%s">下一章</a>' % (i,total, ('独立正文%s' % i)*50,link))
                responses.append((doc,url))
            with patch.object(downloader.fetch, 'get', side_effect=responses):
                if succeeds:
                    result, _ = downloader.chapter(chapter)
                    self.assertEqual(len(result['pages']), 3)
                    self.assertIn('独立正文3', result['body'])
                else:
                    with self.assertRaisesRegex(ValueError, '总页数发生变化'):
                        downloader.chapter(chapter)


if __name__ == '__main__': unittest.main()
