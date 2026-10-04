import tkinter as tk
from tkinter import ttk


def file_size_text(size):
    return '%.2f MB' % (size / 1024 / 1024) if size >= 1024 * 1024 else '%.1f KB' % (size / 1024)


class CompletionDialog(tk.Toplevel):
    def __init__(self, parent, path, details, open_file, open_folder):
        super().__init__(parent)
        self.title('下载完成 · 阅舟')
        self.geometry('780x330')
        self.minsize(640, 330)
        self.configure(bg='#f4f6fa')
        self.transient(parent)
        tk.Label(self, text='下载完成', bg='#f4f6fa', fg='#256c62',
                 font=('Microsoft YaHei UI', 21, 'bold')).pack(anchor='w', padx=24, pady=(22, 8))
        tk.Label(self, text=path.name, bg='#f4f6fa', fg='#24374a',
                 font=('Microsoft YaHei UI', 12), wraplength=700, justify='left').pack(anchor='w', padx=24)
        summary = '%s 个目录条目 · %s · %s' % (details['chapters'], file_size_text(path.stat().st_size), path.suffix[1:].upper())
        tk.Label(self, text=summary, bg='#f4f6fa', fg='#657581').pack(anchor='w', padx=24, pady=(8, 4))
        note = '已按你的确认下载现有章节；缺章清单保留在校验报告中。' if details.get('missing_chapters') else '目录有编号异常，详情见校验报告。' if details.get('warnings') else '当前目录已下载，文件回读校验通过。'
        tk.Label(self, text=note, bg='#f4f6fa', fg='#657581', wraplength=700, justify='left').pack(anchor='w', padx=24)
        tk.Label(self, text='文件保存在：', bg='#f4f6fa', fg='#24374a').pack(anchor='w', padx=24, pady=(14, 5))
        self.saved_path = tk.StringVar(value=str(path))
        self.path_entry = ttk.Entry(self, textvariable=self.saved_path, state='readonly')
        self.path_entry.pack(fill='x', padx=24, ipady=5)
        actions = tk.Frame(self, bg='#f4f6fa')
        actions.pack(side='bottom', fill='x', padx=24, pady=20)
        self.copy_button = ttk.Button(actions, text='复制路径', command=self.copy_path)
        self.copy_button.pack(side='left')
        self.file_button = ttk.Button(actions, text='打开文件', command=lambda: open_file(path))
        self.file_button.pack(side='right', padx=(12, 0))
        self.folder_button = ttk.Button(actions, text='打开所在文件夹', style='Accent.TButton', command=lambda: open_folder(path))
        self.folder_button.pack(side='right')
        self.bind('<Escape>', lambda event: self.destroy())

    def copy_path(self):
        self.clipboard_clear()
        self.clipboard_append(self.saved_path.get())
        self.copy_button.configure(text='已复制')
