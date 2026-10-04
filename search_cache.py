"""Bounded search history; cached rows are never presented as a fresh result."""
import hashlib
import json
import threading
import time
from pathlib import Path


class SearchCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()

    def path(self, rule, query):
        key = json.dumps([rule, query], sort_keys=True, ensure_ascii=False)
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def get(self, rule, query):
        with self.lock:
            try:
                data = json.loads(self.path(rule, query).read_text('utf-8'))
                if time.time() - data['saved_at'] > 7 * 86400:
                    return []
                return [dict(row, cached_at=data['saved_at']) for row in data['rows']]
            except (OSError, ValueError, KeyError, TypeError):
                return []

    def put(self, rule, query, rows):
        if not rows:
            return
        with self.lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.path(rule, query)
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(dict(saved_at=time.time(), rows=rows), ensure_ascii=False), 'utf-8')
                tmp.replace(path)
                paths = sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in paths[200:]:
                    old.unlink()
            except OSError:
                pass  # Cache failure must not discard a successful live response.
