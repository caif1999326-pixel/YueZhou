import json
import tempfile
import time
from pathlib import Path
from app import App
from test_reliable_engine import Fixture
from search_engine import catalog_summary

with tempfile.TemporaryDirectory() as tmp, Fixture(mode='gap') as fixture:
    app = App()
    app.withdraw()
    args = fixture.args(tmp)
    app.rules_path = Path(args.rules)
    app.output.set(args.output)
    row = dict(title='测试小说', author='', source='本地测试', latest='', url=fixture.url)
    app.handle('search_result', dict(rows=[row], source='本地测试', done=1, total=1))
    app.handle('search_done', {})
    rule = json.loads(Path(args.rules).read_text('utf-8'))[0]
    app.handle('catalog_result', dict(url=fixture.url, **catalog_summary(rule, fixture.url)))
    assert app.tree.set(next(iter(app.rows)), 'count') == '3 章 · 有跳号'
    app.download()
    deadline = time.time() + 15
    while app.gap_dialog is None and time.time() < deadline:
        app.update()
        time.sleep(.03)
    assert app.gap_dialog is not None, app.status.get()
    assert '第2章' in app.gap_dialog.details.get('1.0', 'end')
    assert fixture.requests == {'/book': 2}, fixture.requests
    app.gap_dialog.cancel_button.invoke()
    while app.busy and time.time() < deadline:
        app.update()
        time.sleep(.03)
    assert not app.busy and app.artifact is None
    assert fixture.requests == {'/book': 2}, fixture.requests
    app.download()
    deadline = time.time() + 15
    while app.gap_dialog is None and time.time() < deadline:
        app.update()
        time.sleep(.03)
    assert app.gap_dialog is not None
    app.gap_dialog.continue_button.invoke()
    deadline = time.time() + 30
    while app.busy and time.time() < deadline:
        app.update()
        time.sleep(.03)
    assert app.artifact and Path(app.artifact).exists(), app.status.get()
    assert app.progress['value'] == 100
    assert '仅下载现有章节' in app.status.get()
    assert json.loads(Path(app.report).read_text('utf-8'))['status'] == 'completed_with_warnings'
    app.destroy()
    print('UI gap list, cancel-before-body, explicit continue and export verified')
