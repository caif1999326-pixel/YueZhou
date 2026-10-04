"""Safe local Tk regression for mixed/duplicate chapter-number confirmation.

Uses a temporary app BASE and loopback HTTP server. No global desktop input,
clipboard access, outside network requests, or writes to downloaded novels.
"""
import argparse
import copy
import ctypes
import json
import re
import tempfile
import time
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from app import App
from gap_dialog import GapDialog
import reliable_engine as engine
from test_reliable_engine import Fixture


class NumberingFixture(Fixture):
    """Real mixed numbering: a second volume temporarily uses whole-book labels."""
    def __init__(self):
        numbers = list(range(1, 26)) + [1, 2, 28, 29, 5, 6]
        super().__init__(count=len(numbers))
        owner = self
        self.titles = ['第%s章 条目%s' % (number, index)
                       for index, number in enumerate(numbers, 1)]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests[self.path] = owner.requests.get(self.path, 0) + 1
                code = 200
                if self.path == '/book':
                    owner.catalogs += 1
                    body = '<title>测试小说</title><h1>测试小说</h1><ul id="catalog">'
                    body += ''.join('<li><a href="/c%s">%s</a></li>' % (index, title)
                                    for index, title in enumerate(owner.titles, 1)) + '</ul>'
                elif re.fullmatch(r'/c\d+', self.path) and 1 <= int(self.path[2:]) <= owner.count:
                    index = int(self.path[2:])
                    content = ('这是目录中第%s个条目的独立完整正文。' % index) * 24
                    body = '<h1>%s</h1><div id="content"><p>%s</p></div>' % (owner.titles[index - 1], content)
                else:
                    body, code = 'missing', 404
                value = ('<html><head><meta charset="utf-8"></head><body>' + body + '</body></html>').encode()
                self.send_response(code)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(value)))
                self.end_headers()
                self.wfile.write(value)

        self.server.RequestHandlerClass = Handler


def until(app, condition, label, timeout=25):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.update()
        time.sleep(.025)
    app.update()
    assert condition(), '%s: %s' % (label, app.status.get())


def labels(widget):
    text = []
    if widget.winfo_class() in ('Label', 'TLabel'):
        text.append(str(widget.cget('text')))
    for child in widget.winfo_children():
        text.extend(labels(child))
    return text


def assert_wording(dialog, numbering_only=False):
    label_text = '\n'.join(labels(dialog))
    detail_text = dialog.details.get('1.0', 'end')
    assert re.search(r'目录有 \d+ 处需要核对', label_text), label_text
    assert '不能据此计算实际缺章数' in label_text
    assert '1370' not in label_text + detail_text
    assert not re.search(r'(?:疑似缺少|共缺少|缺少总计)\s*\d+\s*章', label_text + detail_text)
    if numbering_only:
        assert '疑似缺号 0 处' in label_text
        assert '编号异常' in detail_text and '疑似缺号：' not in detail_text
        assert '源站目录位置：' in detail_text
    return label_text, detail_text


def destroy(app):
    for callback in app.tk.splitlist(app.tk.call('after', 'info')):
        app.after_cancel(callback)
    app.destroy()


def invoke_binding(widget, sequence):
    # Directly call this widget's registered handler. No focus change or key
    # injection into the user's active game/window is involved.
    script = widget.bind(sequence)
    assert script, sequence
    command = script.split('[', 1)[1].split(' ', 1)[0]
    widget.tk.call(command, '')


def run_checks():
    assert hasattr(engine, 'catalog_review'), 'catalog_review integration is not ready'
    with tempfile.TemporaryDirectory() as temporary, NumberingFixture() as fixture, patch('app.BASE', Path(temporary)):
        base = Path(temporary)
        args = fixture.args(base)
        entries = [dict(title=title, url=fixture.url + '/chapter%s' % index)
                   for index, title in enumerate(fixture.titles)]
        assert engine.catalog_gaps(entries) == []
        assert engine.catalog_review(entries) and all(issue.get('kind') == 'numbering' for issue in engine.catalog_review(entries))
        app = App()
        app.withdraw()
        try:
            app.rules_path = Path(args.rules)
            app.rules = json.loads(app.rules_path.read_text('utf-8'))
            app.output.set(args.output)
            row = dict(title='测试小说', author='测试作者', source='本地测试', url=fixture.url, latest=fixture.titles[-1])
            app.handle('search_result', dict(rows=[row], source='本地测试', done=1, total=1))
            app.handle('search_done', {})

            app.download()
            until(app, lambda: app.gap_dialog is not None, 'numbering-only confirmation')
            assert_wording(app.gap_dialog, numbering_only=True)
            assert fixture.requests == {'/book': 1}, fixture.requests
            app.gap_dialog.cancel_button.invoke()
            until(app, lambda: not app.busy, 'cancel before body')
            assert fixture.requests == {'/book': 1}, fixture.requests
            assert app.artifact is None and not list(Path(args.output).glob('*.epub'))

            app.download()
            until(app, lambda: app.gap_dialog is not None, 'confirmation is asked again')
            assert_wording(app.gap_dialog, numbering_only=True)
            assert fixture.requests == {'/book': 2}, fixture.requests
            app.gap_dialog.continue_button.invoke()
            until(app, lambda: not app.busy, 'continue exports all existing entries')
            assert app.artifact and app.progress['value'] == 100, app.status.get()
            report = json.loads(Path(app.report).read_text('utf-8'))
            assert report['chapters'] == fixture.count and report['status'] == 'completed_with_warnings', report
            assert report['gap_confirmation']['accepted'] is True
            assert report.get('missing_chapters', []) == [], report.get('missing_chapters')
            assert all(fixture.requests['/c%s' % index] == 1 for index in range(1, fixture.count + 1))
            assert app.completion_dialog.path_entry.get() == app.artifact
            assert app.download_btn.instate(['!disabled']) and app.probe_btn.instate(['!disabled'])
            with zipfile.ZipFile(app.artifact) as archive:
                assert archive.testzip() is None
                assert len([name for name in archive.namelist() if name.startswith('OEBPS/chapters/')]) == fixture.count

            # Preserve ordinary missing-number detail and all cancel defaults.
            ordinary = [dict(start=2, end=2, count=1, before='第1章 前', after='第3章 后',
                             before_index=1, after_index=2, volume=1)]
            for action in ('close', '<Escape>', '<Return>'):
                decisions = []
                dialog = GapDialog(app, '普通缺号测试', ordinary, 3, decisions.append)
                dialog.withdraw()
                label_text, detail_text = assert_wording(dialog)
                assert '第2章' in detail_text and '疑似缺号 1 处' in label_text
                assert '源站目录位置：第 1 项 → 第 2 项' in detail_text
                assert '编号段 1' in detail_text
                if action == 'close':
                    dialog.tk.call(dialog.protocol('WM_DELETE_WINDOW'))
                else:
                    invoke_binding(dialog, action)
                assert decisions == [False], (action, decisions)
        finally:
            app.stop.set()
            destroy(app)
    print('Verified numbering-only confirmation before body, cancel, explicit continue with all chapters, no fabricated missing total, ordinary missing label, 1-based positions, close/Esc/Return default cancel, and validated EPUB completion')


def capture_manifest(manifest_path, output_path):
    """Render read-only catalog findings; capture only this dialog's own HWND."""
    import win32gui as gui
    import win32ui
    from PIL import Image
    data = json.loads(Path(manifest_path).read_text('utf-8'))
    chapters = data['chapters']
    original = copy.deepcopy(chapters)
    review = engine.catalog_review(chapters)
    assert chapters == original, 'Review mutated the source manifest'
    issues = review if isinstance(review, list) else review['issues']
    with tempfile.TemporaryDirectory() as temporary, patch('app.BASE', Path(temporary)):
        app = App()
        app.withdraw()
        try:
            dialog = GapDialog(app, data['title'], issues, len(chapters), lambda accepted: None)
            assert_wording(dialog)
            dialog.withdraw()
            dialog.transient('')
            dialog.update_idletasks()
            hwnd = gui.GetParent(dialog.winfo_id())
            # SW_SHOWNOACTIVATE paints our target window without foregrounding it.
            gui.ShowWindow(hwnd, 4)
            dialog.update()
            left, top, right, bottom = gui.GetWindowRect(hwnd)
            source = gui.GetWindowDC(hwnd)
            dc = win32ui.CreateDCFromHandle(source)
            memory = dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(dc, right - left, bottom - top)
            memory.SelectObject(bitmap)
            try:
                assert ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 2)
                image = Image.frombuffer('RGB', (right - left, bottom - top), bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(output_path)
            finally:
                gui.DeleteObject(bitmap.GetHandle())
                memory.DeleteDC()
                dc.DeleteDC()
                gui.ReleaseDC(hwnd, source)
        finally:
            destroy(app)
    print('Captured %s read-only entries in own dialog: %s' % (len(chapters), output_path))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest')
    parser.add_argument('--screenshot')
    options = parser.parse_args()
    run_checks()
    if options.manifest and options.screenshot:
        capture_manifest(options.manifest, options.screenshot)
