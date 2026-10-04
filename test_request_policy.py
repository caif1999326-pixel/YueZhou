import concurrent.futures
import json
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

import requests

import reliable_engine as engine
from test_reliable_engine import Fixture


def response(status=200, body='<html><title>小说</title><p>正文</p></html>', headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = 'https://example.test/chapter'
    result._content = body.encode('utf-8')
    result.encoding = 'utf-8'
    result.headers.update(headers or {})
    return result


class RequestPolicyTests(unittest.TestCase):
    def test_stopped_fetch_never_starts_a_request(self):
        stop = threading.Event()
        stop.set()
        fetch = engine.Fetcher(interval=0, stop=stop)
        with patch.object(requests.Session, 'get') as get:
            with self.assertRaises(InterruptedError):
                fetch.get('https://example.test/chapter')
        get.assert_not_called()
        self.assertEqual(fetch.snapshot()['requests'], 0)
        self.assertEqual(fetch.snapshot()['active'], 0)

    def test_permanent_http_and_challenge_are_single_attempt(self):
        cases = [(response(status), 'http_permanent') for status in (400, 401, 403, 404, 405, 410)]
        cases.append((response(body='<html><title>Just a moment...</title></html>'), 'challenge'))
        for result, reason in cases:
            with self.subTest(status=result.status_code, reason=reason):
                fetch = engine.Fetcher(interval=0, retries=4)
                with patch.object(requests.Session, 'get', return_value=result) as get:
                    with self.assertRaises(engine.PermanentFetchError) as caught:
                        fetch.get(result.url)
                self.assertEqual(get.call_count, 1)
                self.assertEqual(caught.exception.reason_code, reason)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(fetch.snapshot()['retries'], 0)
                self.assertEqual(fetch.snapshot()['active'], 0)

    def test_certificate_failure_does_not_retry_but_ssl_eof_does(self):
        fetch = engine.Fetcher(interval=0, retries=2)
        with patch.object(requests.Session, 'get', side_effect=requests.exceptions.SSLError(
                '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')) as get:
            with self.assertRaises(engine.PermanentFetchError) as caught:
                fetch.get('https://example.test/chapter')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(caught.exception.reason_code, 'certificate')
        fetch = engine.Fetcher(interval=0, retries=1)
        with patch.object(requests.Session, 'get', side_effect=[
                requests.exceptions.SSLError('[SSL: UNEXPECTED_EOF_WHILE_READING] EOF'), response()]) as get, \
                patch.object(fetch, 'wait'):
            fetch.get('https://example.test/chapter')
        self.assertEqual(get.call_count, 2)

    def test_missing_chapter_is_not_retried_in_the_second_round(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(mode='missing') as fixture:
            downloader = engine.Downloader(fixture.args(tmp, retries=3))
            with self.assertRaisesRegex(ValueError, '未生成成品'):
                downloader.run()
            self.assertEqual(fixture.requests['/c2'], 1)
            report = json.loads(downloader.report_path.read_text('utf-8'))
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(report['problems'][0]['retryable'], False)
            self.assertIn('HTTP 404', report['problems'][0]['reason'])
            self.assertFalse(list(downloader.output.glob('*.epub')))

    def test_challenge_chapter_is_not_retried_in_the_second_round(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture() as fixture:
            downloader = engine.Downloader(fixture.args(tmp, retries=3))
            original = requests.Session.get
            calls = []

            def challenge(session, url, **kwargs):
                if url.endswith('/c2'):
                    calls.append(url)
                    return response(body='<html><title>安全验证</title></html>')
                return original(session, url, **kwargs)

            with patch.object(requests.Session, 'get', challenge):
                with self.assertRaisesRegex(ValueError, '未生成成品'):
                    downloader.run()
            self.assertEqual(len(calls), 1)
            report = json.loads(downloader.report_path.read_text('utf-8'))
            self.assertFalse(report['problems'][0]['retryable'])
            self.assertIn('验证页面', report['problems'][0]['reason'])

    def test_source_refusal_stops_queued_requests_without_marking_paused(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture(count=40) as fixture:
            downloader = engine.Downloader(fixture.args(tmp, retries=3, workers=4))
            original = requests.Session.get
            calls = []

            def forbidden(session, url, **kwargs):
                if not url.endswith('/book'):
                    calls.append(url)
                    time.sleep(.02)
                    return response(403)
                return original(session, url, **kwargs)

            with patch.object(requests.Session, 'get', forbidden):
                with self.assertRaisesRegex(ValueError, '未生成成品'):
                    downloader.run()
            self.assertGreaterEqual(len(calls), 1)
            self.assertLessEqual(len(calls), 4)
            self.assertFalse(downloader.stop.is_set())
            report = json.loads(downloader.report_path.read_text('utf-8'))
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(len(report['problems']), 40)
            self.assertTrue(all(not problem['retryable'] for problem in report['problems']))
            self.assertFalse(list(downloader.output.glob('*.epub')))

    def test_refusal_is_scoped_to_the_fetcher_and_origin(self):
        fetch = engine.Fetcher(interval=0, retries=0)
        with patch.object(requests.Session, 'get', side_effect=[response(403), response(), response()]) as get:
            with self.assertRaises(engine.PermanentFetchError):
                fetch.get('https://example.test/denied')
            with self.assertRaises(engine.PermanentFetchError):
                fetch.get('https://example.test/other')
            fetch.get('https://another.test/chapter')
            engine.Fetcher(interval=0).get('https://example.test/chapter')
        self.assertEqual(get.call_count, 3)

    def test_fetcher_concurrency_and_pacing_are_clamped_to_source_limits(self):
        with tempfile.TemporaryDirectory() as tmp, Fixture() as fixture:
            args = fixture.args(tmp, workers=6, interval=100)
            rules = json.loads(Path(args.rules).read_text('utf-8'))
            rules[0]['crawl'] = dict(concurrency=2, minInterval=700)
            Path(args.rules).write_text(json.dumps(rules), encoding='utf-8')
            downloader = engine.Downloader(args)
            snapshot = downloader.fetch.snapshot()
            self.assertEqual(downloader.args.workers, 2)
            self.assertEqual(snapshot['max_workers'], 2)
            self.assertEqual(snapshot['active_limit'], 2)
            self.assertEqual(snapshot['interval_ms'], 700)

    def test_transient_errors_have_bounded_retries(self):
        for failure in (requests.exceptions.Timeout('timeout'), requests.exceptions.ConnectionError('reset'),
                        response(408), response(500), response(502), response(504)):
            with self.subTest(failure=str(failure)):
                fetch = engine.Fetcher(interval=0, retries=2)
                effects = [failure, failure, response()]
                with patch.object(requests.Session, 'get', side_effect=effects) as get, patch.object(fetch, 'wait'):
                    fetch.get('https://example.test/chapter')
                self.assertEqual(get.call_count, 3)
                self.assertEqual(fetch.snapshot()['retries'], 2)
        fetch = engine.Fetcher(interval=0, retries=2)
        with patch.object(requests.Session, 'get', return_value=response(500)) as get, patch.object(fetch, 'wait'):
            with self.assertRaisesRegex(ValueError, '页面读取失败'):
                fetch.get('https://example.test/chapter')
        self.assertEqual(get.call_count, 3)

    def test_retry_after_accepts_dates_seconds_and_invalid_values(self):
        now = 1700000000
        self.assertEqual(engine.retry_after_seconds('17', now), 17)
        self.assertEqual(engine.retry_after_seconds(formatdate(now + 45, usegmt=True), now), 45)
        self.assertEqual(engine.retry_after_seconds(formatdate(now - 5, usegmt=True), now), 0)
        self.assertEqual(engine.retry_after_seconds('9999', now), 9999)
        self.assertEqual(engine.retry_after_seconds('nonsense', now), 0)
        self.assertEqual(engine.retry_after_seconds(None, now), 0)
        self.assertEqual(engine.retry_after_seconds('NaN', now), 0)
        self.assertEqual(engine.retry_after_seconds('inf', now), 0)
        fetch = engine.Fetcher(workers=6, interval=.2)
        with patch.object(engine.time, 'time', return_value=now), \
                patch.object(engine.time, 'monotonic', return_value=100), \
                patch.object(engine.random, 'uniform', return_value=0):
            fetch._rate_limited(response(429, headers={'Retry-After': formatdate(now + 45, usegmt=True)}), 0)
        self.assertEqual(fetch.cooldown, 145)
        self.assertEqual(fetch.active_limit, 3)
        self.assertGreaterEqual(fetch.snapshot()['interval_ms'], 200)

    def test_long_retry_after_blocks_this_task_without_early_retry(self):
        for status in (429, 503):
            fetch = engine.Fetcher(interval=0, retries=4)
            with patch.object(requests.Session, 'get', return_value=response(
                    status, headers={'Retry-After': '300'})) as get:
                with self.assertRaises(engine.PermanentFetchError) as caught:
                    fetch.get('https://example.test/chapter')
                self.assertEqual(caught.exception.reason_code, 'long_cooldown')
                with self.assertRaises(engine.PermanentFetchError):
                    fetch.get('https://example.test/another-chapter')
            self.assertEqual(get.call_count, 1)
            self.assertEqual(fetch.snapshot()['rate_limits'], 1)

    def test_cancel_during_cooldown_starts_no_more_requests(self):
        stop, first_request = threading.Event(), threading.Event()
        fetch = engine.Fetcher(workers=1, interval=0, retries=3, stop=stop)

        def limited(*args, **kwargs):
            first_request.set()
            return response(429, headers={'Retry-After': '60'})

        with patch.object(requests.Session, 'get', side_effect=limited) as get:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fetch.get, 'https://example.test/chapter')
                self.assertTrue(first_request.wait(1))
                started = time.monotonic()
                stop.set()
                with self.assertRaises(InterruptedError):
                    future.result(timeout=1)
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(get.call_count, 1)
        self.assertEqual(fetch.snapshot()['active'], 0)

    def test_stop_cancels_waiting_request_slots(self):
        stop, entered, release = threading.Event(), threading.Event(), threading.Event()
        fetch = engine.Fetcher(workers=1, interval=0, stop=stop)

        def held_request(*args, **kwargs):
            entered.set()
            release.wait(2)
            return response()

        with patch.object(requests.Session, 'get', side_effect=held_request) as get:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                in_flight = pool.submit(fetch.get, 'https://example.test/one')
                self.assertTrue(entered.wait(1))
                waiting = pool.submit(fetch.get, 'https://example.test/two')
                stop.set()
                with self.assertRaises(InterruptedError):
                    waiting.result(timeout=1)
                release.set()
                in_flight.result(timeout=1)
            self.assertEqual(get.call_count, 1)

    def test_rate_limit_reduces_real_concurrency_then_recovers_within_cap(self):
        fetch = engine.Fetcher(workers=4, interval=.01, retries=0)
        with patch.object(requests.Session, 'get', return_value=response(429)):
            with self.assertRaises(ValueError):
                fetch.get('https://example.test/limited')
        self.assertEqual(fetch.active_limit, 2)
        self.assertEqual(fetch.snapshot()['rate_limits'], 1)
        # Elapse just the cooldown; keep actual pacing and request-slot behavior.
        with fetch.lock:
            fetch.cooldown = 0
        lock = threading.Lock()
        active = peak = 0
        starts = []

        def measured(*args, **kwargs):
            nonlocal active, peak
            with lock:
                starts.append(time.monotonic())
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.06)
                return response()
            finally:
                with lock:
                    active -= 1

        with patch.object(requests.Session, 'get', side_effect=measured):
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(fetch.get, ['https://example.test/chapter'] * 12))
        self.assertEqual(peak, 2)
        self.assertTrue(all(b - a >= .009 for a, b in zip(starts, starts[1:])), starts)
        with patch.object(requests.Session, 'get', return_value=response()):
            for _ in range(48):
                fetch.get('https://example.test/chapter')
        snapshot = fetch.snapshot()
        self.assertEqual(snapshot['active_limit'], 4)
        self.assertEqual(snapshot['max_workers'], 4)
        self.assertGreaterEqual(snapshot['interval_ms'], 10)
        self.assertEqual(snapshot['active'], 0)


if __name__ == '__main__':
    engine.emit = lambda *args, **kwargs: None
    unittest.main(verbosity=2)
