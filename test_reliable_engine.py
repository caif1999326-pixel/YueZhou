import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import reliable_engine as engine


class Fixture:
    def __init__(self, count=4, mode='', delay=0):
        self.count, self.mode, self.delay = count, mode, delay
        self.requests, self.catalogs = {}, 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.requests[self.path] = owner.requests.get(self.path, 0) + 1
                time.sleep(owner.delay)
                code = 200
                if self.path == '/book':
                    owner.catalogs += 1
                    indexes = list(range(1, owner.count + 1))
                    if owner.mode == 'gap': indexes.remove(2)
                    if owner.mode == 'changed' and owner.catalogs > 1: indexes.append(owner.count + 1)
                    body = '<title>测试小说</title><h1>测试小说</h1><ul id="catalog">' + ''.join('<li><a href="/c%d">第%d章 标题%d</a></li>' % (i, i, i) for i in indexes) + '</ul>'
                elif self.path.startswith('/c'):
                    i = int(self.path[2:].split('-')[0])
                    second = '-' in self.path
                    title = '第%d章 标题%d' % (i, i)
                    if second: title += ' (2/2)'
                    if owner.mode == 'wrong' and i == 2: title = '第99章 错章'
                    content = (('共同的重复内容。' if owner.mode == 'duplicate' else '这是章节%d的%s页正文。' % (i, '第二' if second else '第一')) * 24)
                    if owner.mode == 'short' and i == 2: content = '太短'
                    if owner.mode == 'missing' and i == 2: code = 404
                    if owner.mode == 'retry' and i == 2 and owner.requests[self.path] == 1: code = 500
                    if owner.mode == 'throttle' and i == 2 and owner.requests[self.path] == 1: code = 429
                    if owner.mode == 'page-count' and i == 2: title += ' (1/2)'
                    body = '<h1>%s</h1><div id="content"><p>%s</p></div>' % (title, content)
                    if owner.mode in ('pages', 'unruled') and i == 1 and not second:
                        body += '<a id="next" href="/c1-2">下一页</a>'
                    if owner.mode == 'loop' and i == 1:
                        body += '<a id="next" href="/c1">下一页</a>'
                else:
                    body, code = 'missing', 404
                value = ('<html><head><meta charset="utf-8"></head><body>' + body + '</body></html>').encode()
                self.send_response(code)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(value)))
                self.end_headers()
                self.wfile.write(value)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%s/book' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

    def args(self, root, **options):
        rule = dict(url=self.url, name='本地测试', book={}, toc={'item': '#catalog li a'},
                    chapter={'title': 'h1', 'content': '#content', 'nextPage': '#next'})
        if self.mode == 'unruled': rule['chapter'].pop('nextPage')
        root = Path(root)
        rules = root / 'rules.json'
        rules.write_text(json.dumps([rule]), encoding='utf-8')
        values = dict(url=self.url, rules=str(rules), output=str(root / 'downloads'), format='epub',
                      workers=4, interval=0, retries=0, timeout=5, min_chars=100, refresh=False)
        values.update(options)
        return argparse.Namespace(**values)


class EngineTests(unittest.TestCase):
    def run_good(self, mode='', fmt='epub'):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, Fixture(mode=mode) as fixture:
            d = engine.Downloader(fixture.args(tmp, format=fmt))
            result = d.run()
            self.assertTrue(result.exists())
            self.assertEqual(json.loads(d.report_path.read_text('utf-8'))['status'], 'passed')
            if fmt == 'epub':
                with zipfile.ZipFile(result) as z:
                    self.assertIsNone(z.testzip())
                    self.assertEqual(len([n for n in z.namelist() if n.startswith('OEBPS/chapters/')]), 4)
            return d.total_pages

    def test_epub_pages(self): self.assertEqual(self.run_good('pages'), 5)
    def test_txt(self): self.run_good(fmt='txt')
    def test_html(self): self.run_good(fmt='html')

    def blocked(self, mode):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, Fixture(mode=mode) as fixture:
            d = engine.Downloader(fixture.args(tmp))
            with self.assertRaises(ValueError): d.run()
            self.assertEqual(list(d.output.glob('*.epub')), [])

    def test_missing_chapter(self): self.blocked('missing')
    def test_short_body(self): self.blocked('short')
    def test_wrong_title(self): self.blocked('wrong')
    def test_duplicate_body(self): self.blocked('duplicate')
    def test_catalog_gap(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
            d = engine.Downloader(fixture.args(tmp))
            d.confirm_gaps = lambda title, gaps, total: True
            result = d.run()
            report = json.loads(d.report_path.read_text('utf-8'))
            self.assertEqual(report['status'], 'completed_with_warnings')
            self.assertIn('缺少章号 2', report['warnings'][0])
            self.assertTrue(report['gap_confirmation']['accepted'])
            self.assertEqual(report['missing_chapters'][0]['start'], 2)
            with zipfile.ZipFile(result) as z:
                self.assertIsNone(z.testzip())
                self.assertEqual(len([n for n in z.namelist() if n.startswith('OEBPS/chapters/')]), 3)
    def test_catalog_changed(self): self.blocked('changed')
    def test_unhandled_page(self): self.blocked('unruled')
    def test_pagination_loop(self): self.blocked('loop')
    def test_failed_chapter_repair(self): self.run_good('retry')
    def test_rate_limit_recovery(self): self.run_good('throttle')
    def test_missing_page_link(self): self.blocked('page-count')

    def test_exclusive_task_lock(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, Fixture() as f:
            d = engine.Downloader(f.args(tmp))
            with engine.task_lock(d.cache / 'download.lock'):
                with self.assertRaisesRegex(ValueError, '另一个下载任务'): d.run()

    def test_resume_and_corrupt_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, Fixture() as f:
            args = f.args(tmp)
            first = engine.Downloader(args)
            first.run()
            requests = dict(f.requests)
            resumed = engine.Downloader(args)
            resumed.run()
            self.assertEqual(resumed.reused, 4)
            self.assertEqual(f.requests['/c1'], requests['/c1'])
            cache = resumed.cache_path(resumed.chapters[1])
            data = json.loads(cache.read_text('utf-8'))
            data['body'] = '被修改'
            cache.write_text(json.dumps(data), encoding='utf-8')
            repaired = engine.Downloader(args)
            repaired.run()
            self.assertEqual(repaired.reused, 3)
            self.assertEqual(f.requests['/c2'], 2)

    def test_cancel_preserves_previous_output(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, Fixture() as f:
            args = f.args(tmp)
            d = engine.Downloader(args)
            result = d.run()
            previous = result.read_bytes()
            stopped = threading.Event()
            stopped.set()
            with self.assertRaises(InterruptedError): engine.Downloader(args, stopped).run()
            self.assertEqual(result.read_bytes(), previous)

    def test_concurrency_speed(self):
        timings = []
        for workers in (1, 6):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, Fixture(count=18, delay=.08) as f:
                started = time.monotonic()
                engine.Downloader(f.args(tmp, workers=workers)).run()
                timings.append(time.monotonic() - started)
        self.assertLess(timings[1], timings[0] * .7)
        Path(__file__).with_name('benchmark.json').write_text(json.dumps(dict(single=timings[0], parallel=timings[1], speedup=timings[0] / timings[1]), indent=2))


if __name__ == '__main__':
    engine.emit = lambda *args, **kwargs: None
    unittest.main(verbosity=2)
