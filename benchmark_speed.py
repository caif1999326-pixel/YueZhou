"""Compare v1.5 settings to v1.6, including validated EPUB export; no body cache."""
import json
import tempfile
import time
import zipfile
from pathlib import Path
from statistics import mean

import reliable_engine as engine
from download_speed import speed_options
from test_reliable_engine import Fixture


def main():
    engine.emit = lambda *args, **kwargs: None
    runs = []
    expected = None
    with tempfile.TemporaryDirectory() as tmp, Fixture(count=24, delay=.08) as fixture:
        for index, name in enumerate(('标准', '加速', '加速', '标准')):
            root = Path(tmp) / str(index)
            root.mkdir()
            started = time.monotonic()
            d = engine.Downloader(fixture.args(root, **speed_options(name)))
            result = d.run()
            seconds = time.monotonic() - started
            with zipfile.ZipFile(result) as z:
                assert z.testzip() is None
                bodies = {n: z.read(n) for n in z.namelist() if n.startswith('OEBPS/chapters/')}
                assert len(bodies) == 24 and all(bodies.values())
                assert expected is None or bodies == expected
                expected = bodies
            assert d.reused == 0
            runs.append(dict(mode=name, seconds=round(seconds, 3), chapters=24, reused=0))
            print(json.dumps(runs[-1], ensure_ascii=False), flush=True)
    standard = mean(r['seconds'] for r in runs if r['mode'] == '标准')
    fast = mean(r['seconds'] for r in runs if r['mode'] == '加速')
    report = dict(scope='Local HTTP fixture: 24 chapters, 80ms response delay, fresh download and validated EPUB export. Real website speed varies.',
                  runs=runs, standard_seconds=round(standard, 3), fast_seconds=round(fast, 3),
                  speedup=round(standard / fast, 2), identical_chapter_bodies=True)
    Path(__file__).with_name('benchmark_speed.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
