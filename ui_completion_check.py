import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from app import App
from test_reliable_engine import Fixture

with tempfile.TemporaryDirectory() as tmp, Fixture(mode='pages') as fixture, patch('app.BASE', Path(tmp)):
    app = App(); app.withdraw()
    args = fixture.args(tmp)
    app.rules_path = Path(args.rules)
    app.output.set(args.output)
    app.start_download(dict(title='测试小说', url=fixture.url))
    deadline = time.time() + 30
    while app.busy and time.time() < deadline:
        app.update(); time.sleep(.03)
    assert app.artifact and Path(app.artifact).is_file(), app.status.get()
    assert app.progress['value'] == 100
    path = Path(app.artifact)
    dialog = app.completion_dialog
    assert dialog is not None and dialog.winfo_exists()
    assert dialog.path_entry.get() == str(path.resolve())
    assert 'KB' in app.stats.get()
    with patch('app.os.startfile') as open_path:
        dialog.file_button.invoke()
        assert open_path.call_args.args == (str(path),)
        dialog.folder_button.invoke()
        assert open_path.call_args.args == (str(path.parent),)
        # Changing the destination for the next task must not redirect the
        # completed-file action into a different, empty directory.
        unused = Path(tmp) / '下一本的空目录'
        app.output.set(str(unused))
        app.open_folder()
        assert open_path.call_args.args == (str(path.parent),)
        assert not unused.exists()
    settings = json.loads(app.settings_file.read_text('utf-8'))
    assert settings['last_completed']['path'] == str(path)
    app.destroy()
    restored = App(); restored.withdraw()
    assert restored.artifact == str(path)
    assert restored.open_file_btn.winfo_manager() == 'pack'
    with patch('app.os.startfile') as open_path:
        restored.open_folder()
        assert open_path.call_args.args == (str(path.parent),)
    restored.destroy()
    # Completion events cannot announce a missing or zero-byte final artifact.
    app = App(); app.withdraw(); app.artifact = None; app.progress['value'] = 99
    details = dict(output=str(Path(tmp)/'missing.epub'), chapters=4, seconds=1)
    app.handle('complete', details)
    assert app.completion_dialog is None and app.progress['value'] < 100
    zero = Path(tmp)/'empty.epub'; zero.touch(); details['output'] = str(zero)
    app.handle('complete', details)
    assert app.completion_dialog is None and '大小为零' in app.status.get()
    app.destroy()
print('Verified completion popup, exact file/folder actions, restart restore, and missing/zero-byte rejection')
