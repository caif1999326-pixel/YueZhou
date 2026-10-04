"""Regression catalogs contain chapter titles only, never downloaded bodies."""
import argparse
import copy
import html
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

import reliable_engine as engine
from source_quality import probe_source


def entries(numbers):
    return [dict(title='第%s章 标题%s' % (number, index), url='https://example.test/c%s' % index)
            for index, number in enumerate(numbers, 1)]


def mixed_entries():
    return entries(list(range(1, 31)) + [1, 2, 33, 4, 5, 36, 7, 8, 39, 10])


class NumberingFixture:
    """Real local HTTP catalog + unique bodies; request list proves consent order."""
    def __init__(self, chapters=None):
        self.chapters = copy.deepcopy(chapters or mixed_entries())
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                if self.path == '/book':
                    body = '<h1>分卷测试小说</h1><ul id="catalog">' + ''.join(
                        '<li><a href="/c%s">%s</a></li>' % (i, html.escape(chapter['title']))
                        for i, chapter in enumerate(owner.chapters, 1)) + '</ul>'
                elif self.path.startswith('/c') and self.path[2:].isdigit():
                    index = int(self.path[2:])
                    chapter = owner.chapters[index - 1]
                    body = '<h1>%s</h1><div id="content"><p>%s</p></div>' % (
                        html.escape(chapter['title']), '目录第%s条的独立测试正文。' % index * 30)
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
        self.rule = dict(name='分卷测试源', url=self.url, book={'bookName': 'h1'},
                         toc={'item': '#catalog a'}, chapter={'title': 'h1', 'content': '#content'})
        self.row = dict(title='分卷测试小说', author='', source='分卷测试源', url=self.url)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

    def args(self, root, **options):
        root = Path(root)
        rules = root / 'rules.json'
        rules.write_text(json.dumps([self.rule], ensure_ascii=False), encoding='utf-8')
        values = dict(url=self.url, rules=str(rules), output=str(root / 'downloads'),
                      format='epub', workers=4, interval=0, retries=0, timeout=5,
                      min_chars=100, refresh=False)
        values.update(options)
        return argparse.Namespace(**values)


class VolumeNumberingTests(unittest.TestCase):
    def test_real_catalogs_keep_numbering_problems_separate_from_missing_counts(self):
        fixture = json.loads(Path(__file__).with_name('testdata').joinpath(
            'volume_numbering_catalogs.json').read_text('utf-8'))
        self.assertEqual([item['count'] for item in fixture], [851, 867, 846])
        for item in fixture:
            with self.subTest(count=item['count']):
                chapters = [dict(title=title, url='https://example.test/c%s' % index)
                            for index, title in enumerate(item['titles'], 1)]
                before = copy.deepcopy(chapters)
                gaps = engine.catalog_gaps(chapters)
                issues = engine.catalog_numbering_issues(chapters)
                review = engine.catalog_review(chapters)
                self.assertEqual(len(chapters), item['count'])
                self.assertEqual(chapters, before, 'Catalog analysis must not delete, reorder or rename entries')
                self.assertTrue(issues)
                self.assertLess(sum(gap['count'] for gap in gaps), 20)
                expected_missing = {851: {41, 36, 34, 45, 132, 135},
                                    867: {41, 34, 45, 132, 135},
                                    846: {41, 22, 24, 36, 34, 132}}[item['count']]
                self.assertEqual({gap['start'] for gap in gaps}, expected_missing)
                self.assertTrue(all(gap['start'] == gap['end'] for gap in gaps))
                self.assertEqual([row for row in review if row.get('kind') == 'numbering'], issues)
                self.assertEqual([row for row in review if row.get('kind') != 'numbering'], gaps)
                for issue in issues:
                    self.assertEqual(issue['kind'], 'numbering')
                    self.assertNotIn('count', issue)
                    self.assertGreater(issue['volume'], 1)
                for row in review:
                    self.assertEqual(row['before'], chapters[row['before_index'] - 1]['title'])
                    self.assertEqual(row['after'], chapters[row['after_index'] - 1]['title'])
                if item['count'] == 851:
                    for start, end in ((178, 752), (754, 765), (768, 771), (78, 849)):
                        self.assertNotIn((start, end), [(gap['start'], gap['end']) for gap in gaps])
                    self.assertFalse(any(gap['start'] <= 173 <= gap['end'] for gap in gaps))
                    # These labels remain in the original list and are surfaced
                    # as ambiguous numbering instead of silently discarded.
                    for title in ('第753章', '第767章', '第772章', '第850章'):
                        self.assertTrue(any(title in row['after'] for row in issues), title)

    def test_mixed_volume_labels_are_reviewable_without_invented_missing_count(self):
        chapters = mixed_entries()
        original = copy.deepcopy(chapters)
        self.assertEqual(engine.catalog_gaps(chapters), [])
        issues = engine.catalog_numbering_issues(chapters)
        self.assertEqual(len(issues), 3)
        self.assertTrue(all(issue['kind'] == 'numbering' and 'count' not in issue for issue in issues))
        self.assertTrue(all(issue['volume'] == 2 for issue in issues))
        self.assertEqual(engine.catalog_review(chapters), issues)
        self.assertEqual(chapters, original)

    def test_real_missing_128_is_preserved_and_volume_indexes_are_one_based(self):
        chapters = entries(list(range(1, 31)) + [1, 2, 3, 127, 129, 130])
        gaps = engine.catalog_gaps(chapters)
        exact = next(gap for gap in gaps if gap['start'] == 128)
        self.assertEqual((exact['end'], exact['count'], exact['volume']), (128, 1, 2))
        self.assertEqual((exact['before_index'], exact['after_index']), (34, 35))
        self.assertEqual(engine.catalog_numbering_issues(chapters), [])

    def test_chinese_and_arabic_labels_alone_do_not_imply_mixed_numbering(self):
        chapters = [dict(title=title) for title in
                    ('第一章 开始', '第2章 继续', '第三章 继续', '第４章 继续', '第五章 结束')]
        self.assertEqual(engine.catalog_review(chapters), [])
        self.assertEqual(engine.catalog_gaps(chapters), [])
        self.assertEqual(engine.catalog_numbering_issues(chapters), [])

    def test_large_single_volume_gap_is_still_a_compact_missing_range(self):
        chapters = entries([1, 2, 10 ** 9, 10 ** 9 + 1])
        self.assertEqual(engine.catalog_numbering_issues(chapters), [])
        gaps = engine.catalog_gaps(chapters)
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]['start'], gaps[0]['end'], gaps[0]['count']),
                         (3, 10 ** 9 - 1, 10 ** 9 - 3))

    def test_reset_then_high_sequence_without_return_to_low_numbers_is_not_suppressed(self):
        chapters = entries(list(range(1, 31)) + [1, 2, 33, 34, 35, 36])
        self.assertEqual(engine.catalog_numbering_issues(chapters), [])
        self.assertEqual([(gap['start'], gap['end']) for gap in engine.catalog_gaps(chapters)], [(3, 32)])

    def test_reset_and_large_labels_far_from_catalog_positions_remain_unresolved_gaps(self):
        chapters = entries(list(range(1, 31)) + [1, 2, 933, 934, 5, 6, 936, 937, 9, 10])
        self.assertEqual(engine.catalog_numbering_issues(chapters), [])
        self.assertTrue(any(gap['count'] > 100 for gap in engine.catalog_gaps(chapters)))

    def test_numbering_only_default_blocks_before_body(self):
        with tempfile.TemporaryDirectory() as tmp, NumberingFixture() as fixture:
            downloader = engine.Downloader(fixture.args(tmp))
            with self.assertRaisesRegex(ValueError, '尚未确认'):
                downloader.run()
            self.assertEqual(fixture.requests, ['/book'])
            report = json.loads(downloader.report_path.read_text('utf-8'))
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(report['missing_chapters'], [])
            self.assertTrue(report['numbering_issues'])
            self.assertFalse(list(Path(tmp).rglob('*.epub')))

    def test_numbering_only_cancel_blocks_before_body(self):
        with tempfile.TemporaryDirectory() as tmp, NumberingFixture() as fixture:
            downloader = engine.Downloader(fixture.args(tmp))
            shown = []

            def decline(title, issues, total):
                self.assertEqual(fixture.requests, ['/book'])
                shown.extend(issues)
                self.assertEqual(total, 40)
                return False

            downloader.confirm_gaps = decline
            with self.assertRaises(InterruptedError):
                downloader.run()
            self.assertTrue(shown)
            self.assertTrue(all(issue.get('kind') == 'numbering' for issue in shown))
            self.assertEqual(fixture.requests, ['/book'])
            report = json.loads(downloader.report_path.read_text('utf-8'))
            self.assertEqual(report['status'], 'paused')
            self.assertEqual(report['missing_chapters'], [])
            self.assertTrue(report['numbering_issues'])

    def test_numbering_only_confirmation_preserves_every_entry_in_export(self):
        with tempfile.TemporaryDirectory() as tmp, NumberingFixture() as fixture:
            downloader = engine.Downloader(fixture.args(tmp))
            choices = []

            def accept(title, issues, total):
                self.assertEqual(fixture.requests, ['/book'])
                choices.append((issues, total))
                return True

            downloader.confirm_gaps = accept
            result = downloader.run()
            self.assertEqual(len(choices), 1)
            self.assertEqual(choices[0][1], 40)
            with zipfile.ZipFile(result) as archive:
                self.assertIsNone(archive.testzip())
                names = [name for name in archive.namelist() if name.startswith('OEBPS/chapters/')]
                self.assertEqual(len(names), 40)
                headings = [ET.fromstring(archive.read(name)).find('.//{http://www.w3.org/1999/xhtml}h1').text
                            for name in names]
                self.assertEqual(headings, [chapter['title'] for chapter in fixture.chapters])
            report = json.loads(downloader.report_path.read_text('utf-8'))
            self.assertEqual(report['status'], 'completed_with_warnings')
            self.assertEqual(report['chapters'], 40)
            self.assertEqual(report['missing_chapters'], [])
            self.assertEqual(len(report['numbering_issues']), 3)
            self.assertTrue(report['gap_confirmation']['accepted'])
            self.assertEqual([chapter['title'] for chapter in downloader.chapters],
                             [chapter['title'] for chapter in fixture.chapters])

    def test_source_probe_numbering_only_never_samples_body_without_consent(self):
        with tempfile.TemporaryDirectory() as tmp, NumberingFixture() as fixture:
            result = probe_source(fixture.rule, fixture.row, scratch_dir=tmp)
            self.assertEqual(result['status'], 'gaps', result)
            self.assertEqual(result['sample_count'], 0)
            self.assertEqual(result['missing'], [])
            self.assertEqual(result['missing_count'], 0)
            self.assertTrue(result['numbering_issues'])
            self.assertEqual(fixture.requests, ['/book'])
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    engine.emit = lambda *args, **kwargs: None
    unittest.main(verbosity=2)
