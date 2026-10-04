"""Real Tk + local HTTP checks for source sampling and download handoff.

All configuration and books live in a temporary BASE. No desktop input,
clipboard access, browser launch, external network, or user settings are used.
"""
import json
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import App
from test_reliable_engine import Fixture


def until(app, condition, label, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.update()
        time.sleep(.025)
    app.update()
    assert condition(), '%s: %s / %s' % (label, app.search_status.get(), app.status.get())


def add_rows(app, rows):
    app.handle('search_result', dict(rows=[dict(row) for row in rows], source='本地测试', done=1, total=1))
    app.handle('search_done', {})
    return {row['url']: iid for iid, row in app.rows.items()}


def assert_buttons_ready(app):
    assert app.probe_btn.instate(['!disabled'])
    assert app.probe_btn['text'] == '测速排序'
    assert app.search_btn.instate(['!disabled'])
    assert app.download_btn.instate(['!disabled'])


def destroy(app):
    # Each App owns a recurring poll. Remove its scheduled callback before the
    # next Tk instance is created so no stale Tcl callback runs on restart.
    for callback in app.tk.splitlist(app.tk.call('after', 'info')):
        app.after_cancel(callback)
    app.destroy()


def main():
    with (tempfile.TemporaryDirectory() as temporary,
          Fixture(mode='pages') as good,
          Fixture(mode='gap') as gaps,
          Fixture(count=3, mode='wrong') as bad,
          Fixture(count=3, delay=.45) as slow,
          patch('app.BASE', Path(temporary))):
        base = Path(temporary)
        rules = []
        for index, fixture in enumerate((good, gaps, bad, slow)):
            scratch = base / ('fixture-%s' % index)
            scratch.mkdir()
            args = fixture.args(scratch)
            rule = json.loads(Path(args.rules).read_text('utf-8'))[0]
            rule['name'] = '本地测试%s' % index
            rules.append(rule)
        rules_file = base / '书源.json'
        rules_file.write_text(json.dumps(rules, ensure_ascii=False), encoding='utf-8')
        rows = [dict(title='测试小说', author='测试作者', source=rule['name'], url=fixture.url,
                     latest='第4章 标题4' if fixture.count == 4 else '第3章 标题3')
                for fixture, rule in zip((good, gaps, bad), rules)]
        # These three rows must never join the selected work's sample group.
        rows.extend([
            dict(title='测试小说', author='另一位作者', source='同名异作', url=good.url + '?other-author', latest='其他作品末章'),
            dict(title='测试小说', author='', source='作者未知', url=good.url + '?unknown-author', latest='未提供'),
            dict(title='另一本小说', author='测试作者', source='另一书名', url=good.url + '?other-title', latest='其他书末章'),
            dict(title='测试小说', author='取消测试作者', source='慢速取消测试', url=slow.url, latest='第3章 标题3'),
        ])
        app = App()
        app.withdraw()
        try:
            app.output.set(str(base / 'downloads'))
            app.query.set('测试小说')
            ids = add_rows(app, rows)
            app.tree.selection_set(ids[gaps.url])
            selection = app.tree.selection()
            old_search = app.search_id
            old_catalog_stop = app.catalog_stop
            app.probe_btn.invoke()
            assert app.probing and old_catalog_stop.is_set()
            assert app.probe_focus == {good.url, gaps.url, bad.url}, app.probe_focus
            assert app.search_btn.instate(['disabled']) and app.download_btn.instate(['disabled'])
            assert app.probe_btn['text'] == '停止测速'
            assert app.artifact is None and not app.busy
            current_probe = app.probe_id
            app.handle('quality_result', dict(probe_id=current_probe - 1, url=good.url,
                                             result=dict(status='passed', count=999, latest='过期末章')))
            app.handle('quality_done', dict(probe_id=current_probe - 1))
            app.handle('catalog_result', dict(search_id=old_search, url=good.url, count=999, latest='过期末章'))
            app.handle('search_done', dict(search_id=old_search))
            assert app.probing and app.search_btn.instate(['disabled'])
            assert app.tree.set(ids[good.url], 'count') != '999 章'
            until(app, lambda: not app.probing, 'sample completion')
            assert_buttons_ready(app)
            assert app.tree.selection() == selection, (app.tree.selection(), selection)
            assert app.rows[ids[good.url]]['quality']['status'] == 'passed'
            assert app.rows[ids[good.url]]['quality']['sample_count'] == 3
            assert app.rows[ids[good.url]]['quality']['sample_pages'] == 4
            assert app.rows[ids[gaps.url]]['quality']['status'] == 'gaps'
            assert app.rows[ids[bad.url]]['quality']['status'] == 'failed'
            assert app.tree.set(ids[good.url], 'count') == '4 章'
            assert app.tree.set(ids[gaps.url], 'count') == '3 章 · 有跳号'
            assert app.tree.set(ids[bad.url], 'count') == '3 章'
            assert app.tree.set(ids[good.url], 'latest') == '第4章 标题4'
            assert app.tree.set(ids[gaps.url], 'latest') == '第4章 标题4'
            assert app.tree.set(ids[bad.url], 'latest') == '第3章 标题3'
            assert '抽检失败' in app.tree.set(ids[bad.url], 'quality')
            ordered = app.tree.get_children()
            assert ordered.index(ids[good.url]) < ordered.index(ids[gaps.url]) < ordered.index(ids[bad.url])
            assert gaps.requests == {'/book': 1}, gaps.requests
            assert slow.requests == {}, slow.requests
            assert not any('?' in path for path in good.requests), good.requests
            assert not (base / 'downloads').exists(), 'Sampling wrote a user download'
            assert not list((app.state_dir / '测速临时').iterdir()), 'Probe temporary files leaked'
            for row in rows[3:6]:
                assert not app.rows[ids[row['url']]].get('quality')

            # Cancellation during an actual HTTP request must wait honestly and
            # then restore controls without promoting old success to new success.
            app.tree.selection_set(ids[slow.url])
            app.probe_btn.invoke()
            until(app, lambda: bool(slow.requests), 'request start before cancellation')
            assert app.probe_focus == {slow.url}
            app.probe_btn.invoke()
            assert app.probe_stop.is_set()
            assert app.probe_btn.instate(['disabled'])
            until(app, lambda: not app.probing, 'sample cancellation')
            assert_buttons_ready(app)
            assert app.rows[ids[slow.url]]['quality']['status'] == 'cancelled'
            assert app.tree.set(ids[slow.url], 'quality') == '已取消抽检'
            assert slow.requests == {'/book': 1}, slow.requests
            assert app.quality_store.get(rules[-1], slow.url) is None
            assert app.tree.selection() == (ids[slow.url],)
            # Late messages after completion cannot replace a cancelled result.
            app.handle('quality_result', dict(probe_id=app.probe_id, url=slow.url,
                                             result=dict(status='passed', count=777)))
            assert app.rows[ids[slow.url]]['quality']['status'] == 'cancelled'
        finally:
            app.probe_stop.set()
            destroy(app)

        # Stored results retain their historical label after a real Tk restart.
        restored = App()
        restored.withdraw()
        try:
            restored.output.set(str(base / 'downloads'))
            restored.query.set('测试小说')
            ids = add_rows(restored, rows[:3])
            for row in rows[:3]:
                quality = restored.rows[ids[row['url']]]['quality']
                assert quality['historical'] is True
                assert restored.tree.set(ids[row['url']], 'quality').startswith('历史 · ')
            assert '抽检失败' in restored.tree.set(ids[bad.url], 'quality')
            restored.tree.selection_set(ids[good.url])
            before = dict(good.requests)
            restored.download_btn.invoke()
            assert restored.busy and restored.probe_btn.instate(['disabled'])
            until(restored, lambda: not restored.busy, 'download after probe')
            assert_buttons_ready(restored)
            assert restored.artifact and Path(restored.artifact).is_file(), restored.status.get()
            assert restored.progress['value'] == 100
            report = json.loads(Path(restored.report).read_text('utf-8'))
            assert report['status'] == 'passed' and report['chapters'] == 4 and report['pages'] == 5, report
            assert report['reused'] == 0, 'Sampling must not seed a trusted download cache'
            assert good.requests['/c2'] > before.get('/c2', 0), 'Unsampled chapter was skipped'
            assert good.requests['/c1'] > before.get('/c1', 0), 'Download did not recheck sampled chapter'
            assert restored.completion_dialog.path_entry.get() == restored.artifact
            assert restored.tree.set(ids[good.url], 'count') == '4 章'
            with zipfile.ZipFile(restored.artifact) as archive:
                assert archive.testzip() is None
                assert len([n for n in archive.namelist() if n.startswith('OEBPS/chapters/')]) == 4
        finally:
            restored.stop.set()
            destroy(restored)
    print('Verified source grouping, live async samples, gap-before-body, failed samples, count/latest, selection retention, cancellation, stale events, history labels, and full download + completion dialog after sampling')


if __name__ == '__main__':
    main()
