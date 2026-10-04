import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from app import App
from test_reliable_engine import Fixture


with tempfile.TemporaryDirectory() as tmp, Fixture(mode='pages') as fixture, patch('app.BASE', Path(tmp)):
    app = App()
    app.withdraw()
    assert app.speed.get() == '加速'
    args = fixture.args(tmp)
    app.rules_path = Path(args.rules)
    app.output.set(args.output)
    row = dict(title='测试小说', author='作者', source='本地测试', url=fixture.url, latest='')
    app.handle('search_result', dict(rows=[row], source=row['source'], done=1, total=1))
    iid = next(iter(app.rows))
    old_id, old_stop = app.search_id, app.catalog_stop
    app.searching = True
    app.start_download(row)
    assert old_stop.is_set()
    assert app.search_btn.instate(['disabled']) and app.speed_box.instate(['disabled'])
    assert app.tree.set(iid, 'count') == '下载时暂停查询'
    app.handle('search_done', dict(search_id=old_id))
    app.handle('catalog_result', dict(search_id=old_id, url=row['url'], count=999))
    assert app.search_btn.instate(['disabled'])
    assert app.tree.set(iid, 'count') != '999 章'
    with patch('app.search_all') as search:
        app.query.set('下载时不开始新搜索')
        app.search()
        search.assert_not_called()
    deadline = time.monotonic() + 30
    while app.busy and time.monotonic() < deadline:
        app.update()
        time.sleep(.03)
    assert app.artifact and app.progress['value'] == 100, app.status.get()
    assert app.search_btn.instate(['!disabled']) and app.speed_box.instate(['readonly'])
    assert len(app.rows) == 1
    report = json.loads(Path(app.report).read_text('utf-8'))
    assert report['pages'] == 5 and report['chapters'] == 4 and report['status'] == 'passed'
    assert app.completion_dialog.path_entry.get() == app.artifact
    app.speed.set('标准')
    app.start_download(row)
    while app.busy and time.monotonic() < deadline:
        app.update()
        time.sleep(.03)
    assert not app.busy
    report = json.loads(Path(app.report).read_text('utf-8'))
    assert report['reused'] == 4
    app.destroy()
    restored = App()
    restored.withdraw()
    assert restored.speed.get() == '标准'
    restored.destroy()
print('Verified speed persistence, no competing background searches, stale events ignored, pagination, completion popup, cross-mode cache reuse')
