"""Bounded real source check, saving only metadata and no exported novel."""
import concurrent.futures
import json
import threading
from pathlib import Path

import reliable_engine as engine
from source_quality import probe_source, quality_label, rank_key, same_source


def main():
    root = Path(__file__).parent
    rules = json.loads((root / 'rules.json').read_text('utf-8-sig'))
    urls = ['https://quanben5.com/n/wodehunduncheng/',
            'https://www.quanben.io/n/wodehunduncheng/',
            'https://www.biquge365.net/book/11081/']
    stop = threading.Event()
    rows = []
    for url in urls:
        rule = next(rule for rule in rules if same_source(rule['url'], url))
        rows.append((rule, dict(title='我的混沌城', author='凌虚月影', source=rule['name'], url=url)))
    engine.emit = lambda kind, **data: None
    def check(job):
        rule, row = job
        result = probe_source(rule, row, stop, root / 'quality-live-work')
        print(json.dumps(dict(source=row['source'], **result), ensure_ascii=False), flush=True)
        return dict(row, quality=result)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        measured = list(pool.map(check, rows))
    measured.sort(key=lambda row: rank_key(row, '我的混沌城'))
    report = dict(scope='Real per-book source probes. Full catalog plus at most 3 validated body samples per source; no body if catalog gaps. Not a full-book integrity claim.',
                  rows=measured)
    (root / 'quality-live-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for row in measured:
        print(row['source'], quality_label(row['quality']), flush=True)


if __name__ == '__main__':
    main()
