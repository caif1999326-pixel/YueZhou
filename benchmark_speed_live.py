"""Small real-site sample; never writes to the user's downloads or claims a full book."""
import argparse
import concurrent.futures
import json
import tempfile
import time
from pathlib import Path
from statistics import mean

import reliable_engine as engine
from download_speed import speed_options


def main():
    root = Path(__file__).parent
    events = []
    engine.emit = lambda kind, **data: events.append(dict(kind=kind, **data))
    runs = []
    expected = None
    with tempfile.TemporaryDirectory(prefix='speed-live-', dir=root) as tmp:
        for index, mode in enumerate(('标准', '加速', '加速', '标准')):
            args = argparse.Namespace(url='https://quanben5.com/n/wodehunduncheng/',
                rules=str(root / 'rules.json'), output=str(Path(tmp) / str(index)),
                format='epub', retries=1, timeout=15, min_chars=100, refresh=True,
                **speed_options(mode))
            d = engine.Downloader(args)
            if index == 0:
                entries, _ = d.catalog()
                title = d.title
            d.title = title
            d.chapter_urls = {c['url'] for c in entries}
            d.chapters = entries[:6]
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=d.args.workers) as pool:
                chapters = list(pool.map(d.chapter, d.chapters))
            seconds = time.monotonic() - started
            fingerprints = [engine.digest(data['body']) for data, reused in chapters]
            assert not any(reused for data, reused in chapters)
            assert expected is None or fingerprints == expected
            expected = fingerprints
            run = dict(mode=mode, seconds=round(seconds, 3), chapters=len(chapters),
                       pages=sum(len(data['pages']) for data, reused in chapters), reused=0)
            runs.append(run)
            print(json.dumps(run, ensure_ascii=False), flush=True)
            if any('限流' in e.get('message', '') for e in events):
                raise RuntimeError('Site requested backoff; stop speed experiment')
            if index < 3:
                time.sleep(2)
    standard = mean(r['seconds'] for r in runs if r['mode'] == '标准')
    fast = mean(r['seconds'] for r in runs if r['mode'] == '加速')
    report = dict(scope='24 chapter fetches total: same first 6 chapters in standard/fast/fast/standard order. No cache, includes chapter parsing/validation/cache write, excludes catalog/export. Not a full-book benchmark.',
                  source=args.url, title=title, runs=runs, standard_seconds=round(standard, 3),
                  fast_seconds=round(fast, 3), speedup=round(standard / fast, 2),
                  identical_chapter_bodies=True)
    (root / 'benchmark_speed_live.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
