"""A per-download decision, never an implicit or remembered acceptance."""
import tkinter as tk
from tkinter import ttk


class GapDialog(tk.Toplevel):
    def __init__(self, parent, title, gaps, available, on_decision):
        super().__init__(parent)
        self.title('目录需要核对 · 是否继续下载')
        self.geometry('850x510')
        self.minsize(700, 420)
        self.configure(bg='#f4f6fa')
        self.transient(parent)
        self.on_decision = on_decision
        self.decided = False
        numbering = sum(item.get('kind') == 'numbering' for item in gaps)
        suspected = len(gaps) - numbering
        tk.Label(self, text='目录有 %s 处需要核对' % len(gaps), bg='#f4f6fa', fg='#854f10',
                 font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w', padx=24, pady=(20, 8))
        tk.Label(self, text='《%s》 · 源站现有 %s 个目录条目' % (title, available),
                 bg='#f4f6fa', fg='#24374a', font=('Microsoft YaHei UI', 11)).pack(anchor='w', padx=24)
        tk.Label(self, text='疑似缺号 %s 处 · 编号异常 %s 处；不能据此计算实际缺章数。' % (suspected, numbering),
                 bg='#f4f6fa', fg='#657581', wraplength=790, justify='left').pack(anchor='w', padx=24, pady=(8, 12))
        frame = tk.Frame(self, bg='white')
        frame.pack(fill='both', expand=True, padx=24)
        self.details = tk.Text(frame, wrap='word', relief='flat', padx=14, pady=12,
                               font=('Microsoft YaHei UI', 11), fg='#24374a', height=10)
        scroll = ttk.Scrollbar(frame, command=self.details.yview)
        self.details.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.details.pack(side='left', fill='both', expand=True)
        for index, gap in enumerate(gaps, 1):
            if gap.get('kind') == 'numbering':
                label = '编号异常：' + gap.get('reason', '章号的编号方式可能发生变化，请核对前后条目。')
            else:
                missing = '第%s章' % gap['start'] if gap['start'] == gap['end'] else '第%s—%s章' % (gap['start'], gap['end'])
                label = '疑似缺号：' + missing
            self.details.insert('end', '%s. %s\n    前一条目：%s\n    后一条目：%s\n' % (index, label, gap['before'], gap['after']))
            before, after = gap.get('before_index'), gap.get('after_index')
            if before is not None and after is not None:
                self.details.insert('end', '    源站目录位置：第 %s 项 → 第 %s 项\n' % (before, after))
            if gap.get('volume') is not None:
                self.details.insert('end', '    编号段 %s\n' % gap['volume'])
            self.details.insert('end', '\n')
        self.details.configure(state='disabled')
        tk.Label(self, text='继续后仅下载源站现有条目，以上核对记录会保留。每次下载都会重新询问。',
                 bg='#f4f6fa', fg='#657581').pack(anchor='w', padx=24, pady=(12, 6))
        actions = tk.Frame(self, bg='#f4f6fa')
        actions.pack(fill='x', padx=24, pady=(8, 20))
        self.continue_button = ttk.Button(actions, text='继续下载现有章节', style='Accent.TButton', command=lambda: self.choose(True))
        self.continue_button.pack(side='right')
        self.cancel_button = ttk.Button(actions, text='取消下载', command=lambda: self.choose(False))
        self.cancel_button.pack(side='right', padx=(0, 12))
        self.protocol('WM_DELETE_WINDOW', lambda: self.choose(False))
        self.bind('<Escape>', lambda event: self.choose(False))
        self.bind('<Return>', lambda event: self.choose(False))
        self.cancel_button.focus_set()

    def choose(self, accepted):
        if not self.decided:
            self.decided = True
            self.on_decision(accepted)
            self.destroy()
