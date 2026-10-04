import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from source_quality import (MAX_RECORDS, TTL_SECONDS, QualityStore, matching_rows,
                            probe_source, quality_label, rank_key, same_source)


class SourceFixture:
    def __init__(self, mode='', stop=None, count=5):
        self.requests, self.times = [], []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                owner.times.append(time.monotonic())
                if self.path in ('/book', '/list2'):
                    indexes = list(range(1, min(3, count) + 1)) if self.path == '/book' else list(range(4, count + 1))
                    if mode == 'gap' and 2 in indexes:
                        indexes.remove(2)
                    body = '<h1>测试小说</h1><span class="author">作者：测试作者</span><ul id="catalog">'
                    body += ''.join('<li><a href="/c%d">第%d章 标题%d</a></li>' % (n, n, n) for n in indexes)
                    body += '</ul>'
                    if self.path == '/book' and count > 3:
                        body += '<a class="toc-next" href="/list2">下一页</a>'
                    if mode == 'cancel' and self.path == '/list2':
                        stop.set()
                elif self.path.startswith('/c'):
                    n = int(self.path[2:].split('-')[0])
                    second = '-' in self.path
                    title = '第%d章 标题%d' % (n, n)
                    if second:
                        title += ' (2/2)'
                    if mode == 'wrong' and n == 3:
                        title = '第9章 错误标题'
                    content = (('重复的章节内容。' if mode == 'duplicate' else '章节%d第%s页的测试正文。' % (n, 2 if second else 1)) * 24)
                    if mode == 'empty' and n == 3:
                        content = ''
                    body = '<h1>%s</h1><div id="content"><p>%s</p></div>' % (title, content)
                    if mode == 'pages' and n == 1 and not second:
                        body += '<a id="next" href="/c1-2">下一页</a>'
                    if mode == 'cancel-body':
                        stop.set()
                else:
                    self.send_error(404)
                    return
                data = ('<html><meta charset="utf-8"><body>' + body + '</body></html>').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%s/book' % self.server.server_port
        self.rule = dict(url=self.url, name='测试书源', book={'bookName': 'h1', 'author': '.author'},
                         toc={'item': '#catalog li a', 'nextPage': '.toc-next'},
                         chapter={'title': 'h1', 'content': '#content', 'nextPage': '#next'})
        self.row = dict(url=self.url, title='测试小说', author='测试作者', source='测试书源')

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


class SourceProbeTests(unittest.TestCase):
    def test_single_chapter_sample_respects_stricter_source_pacing(self):
        with SourceFixture(count=1) as fixture:
            fixture.rule['crawl'] = {'concurrency': 8, 'minInterval': 1000}
            result = probe_source(fixture.rule, fixture.row)
            self.assertEqual(result['status'], 'passed', result)
            self.assertEqual(result['sample_count'], 1)
            self.assertEqual(result['sample_indices'], [1])
            self.assertEqual(fixture.requests, ['/book', '/c1'])
            self.assertGreaterEqual(fixture.times[1] - fixture.times[0], .95)

    def test_reads_complete_catalog_and_three_full_samples_at_bounded_pace(self):
        with tempfile.TemporaryDirectory() as tmp, SourceFixture('pages') as fixture:
            result = probe_source(fixture.rule, fixture.row, scratch_dir=tmp)
            self.assertEqual(result['status'], 'passed', result)
            self.assertEqual(result['count'], 5)
            self.assertEqual(result['latest'], '第5章 标题5')
            self.assertEqual(result['sample_indices'], [1, 3, 5])
            self.assertEqual(result['sample_pages'], 4)
            self.assertEqual(fixture.requests, ['/book', '/list2', '/c1', '/c1-2', '/c3', '/c5'])
            self.assertTrue(all(right - left >= .65 for left, right in zip(fixture.times, fixture.times[1:])))
            self.assertGreater(result['seconds_per_chapter'], .7)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertNotIn('正文。', json.dumps(result, ensure_ascii=False))
            self.assertIn('不代表整本', result['message'])

    def test_gap_stops_before_any_body_request(self):
        with SourceFixture('gap') as fixture:
            result = probe_source(fixture.rule, fixture.row)
            self.assertEqual(result['status'], 'gaps', result)
            self.assertEqual(result['count'], 4)
            self.assertEqual(result['missing_count'], 1)
            self.assertEqual(result['missing'][0]['start'], 2)
            self.assertEqual(result['sample_count'], 0)
            self.assertEqual(fixture.requests, ['/book', '/list2'])

    def test_invalid_or_duplicate_samples_fail(self):
        for mode, message in [('wrong', '标题不匹配'), ('empty', '为空'), ('duplicate', '正文重复')]:
            with self.subTest(mode=mode), SourceFixture(mode) as fixture:
                result = probe_source(fixture.rule, fixture.row)
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(result['phase'], 'sample')
                self.assertIn(message, result['message'])
                self.assertEqual(result['sample_count'], 1)
                self.assertIsNone(result['seconds_per_chapter'])
                self.assertNotIn('/c5', fixture.requests)

    def test_stop_before_and_during_catalog_never_samples(self):
        stop = threading.Event()
        stop.set()
        with SourceFixture(stop=stop) as fixture:
            self.assertEqual(probe_source(fixture.rule, fixture.row, stop)['status'], 'cancelled')
            self.assertEqual(fixture.requests, [])
        stop.clear()
        with SourceFixture('cancel', stop=stop) as fixture:
            self.assertEqual(probe_source(fixture.rule, fixture.row, stop)['status'], 'cancelled')
            self.assertEqual(fixture.requests, ['/book', '/list2'])

    def test_stop_during_body_response_cannot_mark_passed(self):
        stop = threading.Event()
        with SourceFixture('cancel-body', stop=stop, count=1) as fixture:
            result = probe_source(fixture.rule, fixture.row, stop)
            self.assertEqual(result['status'], 'cancelled', result)
            self.assertEqual(result['sample_count'], 0)
            self.assertIsNone(result['seconds_per_chapter'])

    def test_identity_mismatch_fails_before_body(self):
        with SourceFixture() as fixture:
            row = dict(fixture.row, author='另一位作者')
            result = probe_source(fixture.rule, row)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('作者', result['message'])
            self.assertEqual(fixture.requests, ['/book', '/list2'])

    def test_bad_host_and_dynamic_rules_do_not_send_requests(self):
        with SourceFixture() as fixture:
            result = probe_source(dict(fixture.rule, url='https://example.org/'), fixture.row)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('不匹配', result['message'])
            rule = copy.deepcopy(fixture.rule)
            rule['toc']['item'] = '@js:evil()'
            result = probe_source(rule, fixture.row)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('动态', result['message'])
            self.assertEqual(fixture.requests, [])


class QualityStoreTests(unittest.TestCase):
    def record(self, now=None, **values):
        result = dict(status='passed', url='https://example.org/book', count=10,
                      checked_at=time.time() if now is None else now, seconds_per_chapter=.9,
                      sample_count=3, sample_indices=[1, 6, 10], message='抽检通过')
        result.update(values)
        return result

    def test_cache_is_historical_rule_scoped_and_expiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QualityStore(Path(tmp) / 'quality.json')
            rule, url, now = {'url': 'https://example.org/'}, 'https://example.org/book', time.time()
            result = self.record(now, body='不得保存正文')
            self.assertTrue(store.put(rule, url, result))
            found = store.get(rule, url)
            self.assertTrue(found['historical'])
            self.assertEqual(found['sample_count'], 3)
            self.assertNotIn('body', found)
            self.assertNotIn('不得保存正文', store.path.read_text('utf-8'))
            self.assertIsNone(store.get(dict(rule, chapter={'content': '.changed'}), url))
            with patch('source_quality.time.time', return_value=now + TTL_SECONDS + 1):
                self.assertIsNone(store.get(rule, url))
            found['sample_indices'].append(999)
            self.assertNotIn(999, store.get(rule, url)['sample_indices'])

    def test_corrupt_records_cancelled_and_invalid_pass_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = QualityStore(Path(tmp) / 'quality.json')
            rule, url = {'url': 'https://example.org/'}, 'https://example.org/book'
            store.path.write_text('not-json', encoding='utf-8')
            self.assertIsNone(store.get(rule, url))
            for changes in ({'status': 'cancelled'}, {'seconds_per_chapter': float('nan')},
                            {'sample_count': 0}, {'checked_at': time.time() + 30}):
                self.assertFalse(store.put(rule, url, self.record(**changes)))
            self.assertTrue(store.put(rule, url, self.record()))

    def test_bounded_records_and_multiple_store_instances_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'quality.json'
            a, b = QualityStore(path), QualityStore(path)
            rule, now = {'url': 'https://example.org/'}, time.time()
            for i in range(MAX_RECORDS + 2):
                url = 'https://example.org/book%s' % i
                (a if i % 2 else b).put(rule, url, self.record(now - MAX_RECORDS + i - 2, url=url))
            self.assertEqual(len(json.loads(path.read_text('utf-8'))['records']), MAX_RECORDS)
            self.assertIsNone(a.get(rule, 'https://example.org/book0'))
            self.assertIsNotNone(b.get(rule, 'https://example.org/book101'))


class RankingTests(unittest.TestCase):
    def test_source_matching_normalizes_www_and_default_ports_only(self):
        self.assertTrue(same_source('https://www.example.org/', 'https://example.org:443/book'))
        self.assertTrue(same_source('http://example.org/', 'http://www.example.org:80/book'))
        for url in ('https://another.example.org/book', 'https://example.org.evil.org/book',
                    'https://example.org:8443/book', 'https://name@example.org/book',
                    'javascript:evil()', 'https://example.org:invalid/book'):
            self.assertFalse(same_source('https://example.org/', url), url)

    def row(self, **values):
        row = dict(title='测试小说', author='测试作者', source='书源', url='https://example.org/book')
        row.update(values)
        return row

    def test_same_work_only_and_selected_always_kept(self):
        selected = self.row()
        other_source = self.row(url='https://other.org/book', title='《测试小说》')
        other_author = self.row(url='https://other.org/another', author='另一个作者')
        unknown = self.row(url='https://unknown.org/book', author='未知')
        self.assertEqual(matching_rows(selected, [unknown, other_author, other_source]), [selected, other_source])
        self.assertEqual(matching_rows(unknown, [selected, other_source]), [unknown])

    def test_rank_prefers_matching_title_fresh_pass_then_speed(self):
        make = lambda seconds, **extra: dict(status='passed', seconds_per_chapter=seconds, **extra)
        fast = self.row(url='fast', quality=make(.5))
        slow = self.row(url='slow', quality=make(1.2))
        historic = self.row(url='historic', quality=make(.1, historical=True))
        unknown = self.row(url='unknown')
        gap = self.row(url='gap', quality={'status': 'gaps'})
        failed = self.row(url='failed', quality={'status': 'failed'})
        unrelated = self.row(url='unrelated', title='测试小说续集', quality=make(.01))
        rows = [failed, unrelated, gap, historic, slow, unknown, fast]
        self.assertEqual(sorted(rows, key=lambda row: rank_key(row, '测试小说')),
                         [fast, slow, historic, unknown, gap, failed, unrelated])

    def test_search_history_is_honest_without_hiding_fresh_probe(self):
        live = self.row(url='live')
        historical = self.row(url='historical', cached_at=time.time())
        proven = self.row(url='proven', cached_at=time.time(), quality={'status': 'passed', 'seconds_per_chapter': .8})
        self.assertEqual(sorted([historical, live, proven], key=lambda row: rank_key(row, '测试小说')),
                         [proven, live, historical])

    def test_equally_fresh_clean_catalog_ranks_before_faster_numbering_anomaly(self):
        clean = self.row(url='clean', quality={'status': 'passed', 'seconds_per_chapter': 1.2})
        anomaly = self.row(url='anomaly', quality={'status': 'passed', 'seconds_per_chapter': .5,
                                                  'warnings': ['目录章号异常']})
        different_author = self.row(url='other-work', author='另一位作者',
                                    quality={'status': 'passed', 'seconds_per_chapter': .1})
        self.assertEqual(sorted([anomaly, clean], key=lambda row: rank_key(row, '测试小说')), [clean, anomaly])
        self.assertEqual(matching_rows(clean, [anomaly, different_author]), [clean, anomaly])

    def test_labels_do_not_claim_complete_book(self):
        self.assertIn('抽检通过', quality_label({'status': 'passed', 'seconds_per_chapter': .85}))
        self.assertEqual(quality_label({'status': 'gaps'}), '疑似缺章')
        self.assertEqual(quality_label({'status': 'failed', 'phase': 'catalog'}), '目录失败')
        self.assertEqual(quality_label({'status': 'checking'}), '抽检中…')
        self.assertEqual(quality_label(None), '未抽检')
        label = quality_label({'status': 'passed', 'historical': True, 'checked_at': time.time() - 125})
        self.assertIn('历史', label)
        self.assertIn('2分前', label)
        self.assertNotIn('完整', label)


if __name__ == '__main__':
    unittest.main(verbosity=2)
