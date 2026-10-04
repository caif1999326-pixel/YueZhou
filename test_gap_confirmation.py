import json
import tempfile
import threading
import unittest
from pathlib import Path
import reliable_engine as engine
from test_reliable_engine import Fixture


class GapConfirmationTests(unittest.TestCase):
    def test_gap_ranges_and_volume_resets(self):
        entries = [dict(title=t) for t in ['第一百二十七章 甲', '第129章 乙', '第133章 丙', '第一章 新卷', '第二章 完']]
        gaps = engine.catalog_gaps(entries)
        self.assertEqual([(g['start'], g['end'], g['count']) for g in gaps], [(128,128,1),(130,132,3)])
        self.assertEqual(gaps[0]['before'], entries[0]['title'])

    def test_missing_confirmation_blocks_before_any_body_request(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
            d = engine.Downloader(fixture.args(tmp))
            with self.assertRaisesRegex(ValueError, '尚未确认'): d.run()
            self.assertEqual(fixture.requests, {'/book': 1})
            self.assertEqual(list(Path(tmp).rglob('*.epub')), [])
            report = json.loads(d.report_path.read_text('utf-8'))
            self.assertEqual(report['missing_chapters'][0]['start'], 2)

    def test_single_misnumbered_entry_does_not_invent_hundreds_of_gaps(self):
        entries = [dict(title=t) for t in ['第536章 甲','第357章 乙','第538章 丙','第540章 丁']]
        gaps = engine.catalog_gaps(entries)
        self.assertEqual([(g['start'],g['end']) for g in gaps], [(539,539)])
        self.assertTrue(any('编号错误' in message for message in engine.catalog_warnings(entries)))

    def test_rejected_confirmation_preserves_list_and_makes_no_book(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
            d = engine.Downloader(fixture.args(tmp))
            d.confirm_gaps = lambda *args: False
            with self.assertRaisesRegex(InterruptedError, '已取消'): d.run()
            self.assertEqual(fixture.requests, {'/book': 1})
            self.assertEqual(json.loads(d.report_path.read_text('utf-8'))['status'], 'paused')

    def test_confirmation_is_requested_again_for_cached_book(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
            first = engine.Downloader(fixture.args(tmp))
            first.confirm_gaps = lambda *args: True
            result = first.run()
            original = result.read_bytes()
            calls = []
            second = engine.Downloader(fixture.args(tmp))
            def decline(title, gaps, total):
                calls.append((gaps, total))
                return False
            second.confirm_gaps = decline
            with self.assertRaises(InterruptedError): second.run()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], 3)
            self.assertEqual(result.read_bytes(), original)

    def test_pause_during_confirmation_never_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
            stop = threading.Event()
            d = engine.Downloader(fixture.args(tmp), stop)
            def confirm(*args):
                stop.set()
                return True
            d.confirm_gaps = confirm
            with self.assertRaises(InterruptedError): d.run()
            self.assertEqual(fixture.requests, {'/book': 1})


if __name__ == '__main__': unittest.main()
