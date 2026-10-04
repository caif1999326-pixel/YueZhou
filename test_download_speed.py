import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import reliable_engine as engine
from download_speed import speed_options
from test_reliable_engine import Fixture


class SpeedTests(unittest.TestCase):
    def test_fast_still_respects_source_pacing_and_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(count=4, delay=.30) as f:
            args = f.args(tmp, **speed_options('加速'))
            rules = json.loads(Path(args.rules).read_text('utf-8'))
            rules[0]['crawl'] = dict(concurrency=1, minInterval=350)
            Path(args.rules).write_text(json.dumps(rules), encoding='utf-8')
            lock, starts = threading.Lock(), []
            active = peak = 0
            original = requests.Session.get

            def observed(session, *args, **kwargs):
                nonlocal active, peak
                with lock:
                    starts.append(time.monotonic())
                    active += 1
                    peak = max(peak, active)
                try:
                    return original(session, *args, **kwargs)
                finally:
                    with lock:
                        active -= 1

            with patch.object(requests.Session, 'get', observed):
                d = engine.Downloader(args)
                d.run()
            self.assertEqual(peak, 1)
            self.assertTrue(all(b - a >= .33 for a, b in zip(starts, starts[1:])), starts)
            self.assertEqual(d.completed, 4)

    def test_switching_speed_reuses_only_valid_cache(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture() as f:
            fast = engine.Downloader(f.args(tmp, **speed_options('加速')))
            fast.run()
            before = dict(f.requests)
            standard = engine.Downloader(f.args(tmp, **speed_options('标准')))
            standard.run()
            self.assertEqual(standard.reused, 4)
            self.assertEqual({k: n for k, n in f.requests.items() if k != '/book'},
                             {k: n for k, n in before.items() if k != '/book'})
            path = standard.cache_path(standard.chapters[1])
            broken = json.loads(path.read_text('utf-8'))
            broken['body'] = '损坏的缓存不应复用'
            path.write_text(json.dumps(broken), encoding='utf-8')
            repaired = engine.Downloader(f.args(tmp, **speed_options('加速')))
            repaired.run()
            self.assertEqual(repaired.reused, 3)
            self.assertEqual(f.requests['/c2'], 2)

    def test_fast_backs_off_and_finishes_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='throttle') as f:
            d = engine.Downloader(f.args(tmp, **speed_options('加速')))
            d.run()
            self.assertGreater(d.fetch.slowdown, 1)
            self.assertEqual(d.completed, 4)
            self.assertEqual(json.loads(d.report_path.read_text('utf-8'))['status'], 'passed')


if __name__ == '__main__':
    engine.emit = lambda *args, **kwargs: None
    unittest.main(verbosity=2)
