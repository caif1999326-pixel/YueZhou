import argparse
import concurrent.futures
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from urllib.parse import urlsplit
from tkinter import ttk, filedialog, messagebox, simpledialog

import reliable_engine as engine
from search_engine import search_all, supported
from search_cache import SearchCache
from gap_dialog import GapDialog
from completion_dialog import CompletionDialog, file_size_text
from download_speed import SPEEDS, DEFAULT_SPEED, speed_options
from source_quality import QualityStore, probe_source, matching_rows, rank_key, quality_label, same_source

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
RESOURCE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('阅舟 · 小说下载')
        self.geometry('1120x790')
        self.minsize(1040, 740)
        self.configure(bg='#f4f6fa')
        self.events = queue.Queue()
        self.rules_path = BASE / '书源.json' if (BASE / '书源.json').exists() else RESOURCE / 'rules.json'
        self.rules = json.loads(self.rules_path.read_text('utf-8-sig'))
        self.state_dir = BASE / '阅舟数据'
        self.state_dir.mkdir(exist_ok=True)
        self.search_cache = SearchCache(self.state_dir / '搜索缓存')
        self.quality_store = QualityStore(self.state_dir / '书源测速.json')
        self.settings_file = self.state_dir / 'settings.json'
        try:
            self.settings = json.loads(self.settings_file.read_text('utf-8'))
        except (OSError, ValueError):
            self.settings = {}
        self.query = tk.StringVar()
        self.output = tk.StringVar(value=self.settings.get('output', str(BASE / '我的小说')))
        self.format = tk.StringVar(value=self.settings.get('format', 'epub'))
        saved_speed = self.settings.get('speed', DEFAULT_SPEED)
        self.speed = tk.StringVar(value=saved_speed if saved_speed in SPEEDS else DEFAULT_SPEED)
        self.status = tk.StringVar(value='输入书名，开始找一本想读的小说')
        self.search_status = tk.StringVar(value='已启用 %s 个书源；可在“书源状态”查看实测记录和停用原因' % sum(supported(r) for r in self.rules))
        self.stats = tk.StringVar(value='已校验 0 / 0 章     ·     等待任务')
        self.book = tk.StringVar(value='还没有下载任务')
        self.rows, self.history = {}, []
        self.busy = self.searching = False
        self.probing = False
        self.probe_stop = threading.Event()
        self.probe_id = 0
        self.probe_focus = set()
        self.search_id = 0
        self.catalog_stop = threading.Event()
        self.stop = threading.Event()
        self.report = self.artifact = None
        self.download_url = None
        self.gap_dialog = None
        self.completion_dialog = None
        saved = self.settings.get('last_completed') or {}
        if saved.get('path') and Path(saved['path']).is_file():
            self.artifact = str(Path(saved['path']).resolve())
            self.report = saved.get('report')
        self.build()
        if self.artifact:
            self.book.set(Path(self.artifact).name)
            self.status.set('上次下载文件已找到，可直接打开或定位所在文件夹')
            self.show_file_actions()
        engine.emit = self.emit
        self.after(100, self.poll)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def emit(self, kind, **data):
        self.events.put((kind, data))

    def build(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TButton', padding=(15, 9), background='#e8edf3', borderwidth=0)
        style.configure('Accent.TButton', background='#256c62', foreground='white', font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Accent.TButton', background=[('active', '#1c574f'), ('disabled', '#98b1ad')])
        style.configure('Treeview', rowheight=43, background='white', fieldbackground='white', foreground='#24374a', borderwidth=0)
        style.configure('Treeview.Heading', background='#edf2f5', foreground='#6b7885', padding=(8, 12))
        style.map('Treeview', background=[('selected', '#d9ede8')], foreground=[('selected', '#154c43')])
        style.configure('TProgressbar', background='#256c62', troughcolor='#e7eeec', borderwidth=0)
        sidebar = tk.Frame(self, bg='#163e38', width=180)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text='阅 舟', bg='#163e38', fg='white', font=('Microsoft YaHei UI', 27, 'bold')).pack(anchor='w', padx=25, pady=(30, 0))
        tk.Label(sidebar, text='把好故事带走', bg='#163e38', fg='#a9c6bf', font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=27, pady=(8, 35))
        for label, action in [('搜索小说', lambda: self.entry.focus_set()), ('我的下载', self.open_folder), ('校验报告', self.open_report), ('书源状态', self.show_sources)]:
            tk.Button(sidebar, text=label, command=action, bg='#204e46', fg='white', relief='flat', bd=0, activebackground='#2a6358', activeforeground='white', font=('Microsoft YaHei UI', 11), pady=13).pack(fill='x', padx=16, pady=5)
        tk.Label(sidebar, text='阅舟 1.8\n\n书源测速\n断点续传\n校验后导出', justify='left', bg='#163e38', fg='#a9c6bf', font=('Microsoft YaHei UI', 10), anchor='w').pack(side='bottom', fill='x', padx=26, pady=30)
        main = tk.Frame(self, bg='#f4f6fa', padx=28, pady=24)
        main.pack(side='left', fill='both', expand=True)
        tk.Label(main, text='下一本，读什么？', bg='#f4f6fa', fg='#172f3a', font=('Microsoft YaHei UI', 24, 'bold')).pack(anchor='w')
        tk.Label(main, text='搜索书名，核对作者，选好书源就能下载。', bg='#f4f6fa', fg='#73828d', font=('Microsoft YaHei UI', 10)).pack(anchor='w', pady=(7, 20))
        bar = tk.Frame(main, bg='#f4f6fa')
        bar.pack(fill='x')
        self.entry = ttk.Entry(bar, textvariable=self.query, font=('Microsoft YaHei UI', 13))
        self.entry.pack(side='left', fill='x', expand=True, ipady=9, padx=(0, 10))
        self.entry.bind('<Return>', lambda e: self.search())
        self.search_btn = ttk.Button(bar, text='搜索全书源', style='Accent.TButton', command=self.search)
        self.search_btn.pack(side='right')
        tk.Label(main, textvariable=self.search_status, bg='#f4f6fa', fg='#73828d', anchor='w', wraplength=770, justify='left').pack(fill='x', pady=(10, 10))
        table = tk.Frame(main, bg='white', height=175)
        table.pack(fill='both', expand=True)
        table.pack_propagate(False)
        self.tree = ttk.Treeview(table, columns=('title', 'author', 'source', 'count', 'quality', 'latest'), show='headings', selectmode='browse', height=6)
        for key, label, width in [('title', '书名', 135), ('author', '作者', 90), ('source', '书源', 125), ('count', '目录总章数', 140), ('quality', '书源抽检 / 速度', 175), ('latest', '最新章节', 175)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=70)
        self.tree.column('count', anchor='center', stretch=False)
        self.tree.tag_configure('sample_passed', foreground='#236c52')
        self.tree.tag_configure('sample_failed', foreground='#98523c')
        self.tree.heading('quality', command=self.sort_results)
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        horizontal.pack(side='bottom', fill='x')
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', lambda e: self.download())
        self.tree.bind('<<TreeviewSelect>>', self.show_quality_summary)
        options = tk.Frame(main, bg='#f4f6fa')
        options.pack(fill='x', pady=14)
        ttk.Combobox(options, textvariable=self.format, values=('epub', 'txt'), state='readonly', width=7).pack(side='left', ipady=5)
        self.speed_box = ttk.Combobox(options, textvariable=self.speed, values=tuple(SPEEDS), state='readonly', width=5)
        self.speed_box.pack(side='left', padx=(8, 0), ipady=5)
        ttk.Button(options, text='保存位置', command=self.choose_folder).pack(side='left', padx=8)
        ttk.Button(options, text='粘贴书籍网址', command=self.by_url).pack(side='left')
        self.probe_btn = ttk.Button(options, text='测速排序', command=self.probe_selected)
        self.probe_btn.pack(side='left', padx=(8, 0))
        self.download_btn = ttk.Button(options, text='一键下载选中小说', style='Accent.TButton', command=self.download)
        self.download_btn.pack(side='right')
        card = tk.Frame(main, bg='white', padx=18, pady=15)
        card.pack(fill='x')
        top = tk.Frame(card, bg='white')
        top.pack(fill='x')
        tk.Label(top, textvariable=self.book, bg='white', fg='#183d35', font=('Microsoft YaHei UI', 12, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        self.pause_btn = ttk.Button(top, text='暂停', command=self.pause, state='disabled')
        self.pause_btn.pack(side='right')
        self.open_file_btn = ttk.Button(top, text='打开文件', command=self.open_artifact)
        self.locate_file_btn = ttk.Button(top, text='打开所在文件夹', command=self.open_folder)
        self.progress = ttk.Progressbar(card, maximum=100)
        self.progress.pack(fill='x', pady=(14, 10))
        tk.Label(card, textvariable=self.stats, bg='white', fg='#6e7e89', anchor='w').pack(fill='x')
        tk.Label(card, textvariable=self.status, bg='white', fg='#256c62', anchor='w', wraplength=740, justify='left').pack(fill='x', pady=(8, 0))
        self.log = tk.Text(main, height=3, bg='#f4f6fa', fg='#74818c', relief='flat', font=('Microsoft YaHei UI', 9), state='disabled')
        self.log.pack(fill='x', pady=(10, 0))
        tk.Label(main, text='发现目录跳号会列出缺章清单，等你确认后下载；正文抓取失败、错章或重复仍会阻止导出。', bg='#f4f6fa', fg='#83918c', anchor='w', font=('Microsoft YaHei UI', 9)).pack(fill='x', pady=(7, 0))
        self.entry.focus_set()

    def write_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', time.strftime('%H:%M:%S  ') + text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')
        self.history.append(text)

    def search(self):
        query = self.query.get().strip().strip('《》').strip()
        if not query or self.searching or self.busy or self.probing:
            return
        self.query.set(query)
        self.searching = True
        self.search_btn.configure(state='disabled')
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.probe_focus.clear()
        self.catalog_stop.set()
        self.catalog_stop = threading.Event()
        self.search_id += 1
        search_id = self.search_id
        def notify(kind, **data):
            self.emit(kind, search_id=search_id, **data)
        self.search_status.set('正在连接书源，搜索结果会陆续出现…')
        threading.Thread(target=search_all, args=(self.rules, query, notify, self.catalog_stop, self.search_cache), daemon=True).start()

    def rule_for_row(self, row):
        return next((r for r in self.rules if same_source(r.get('url', ''), row.get('url', ''))), None)

    def sort_results(self, select_first=False):
        selection = self.tree.selection()
        ordered = sorted(self.rows, key=lambda iid: (
            bool(self.probe_focus) and self.rows[iid]['url'] not in self.probe_focus,
            rank_key(self.rows[iid], self.query.get())))
        for position, iid in enumerate(ordered):
            self.tree.move(iid, '', position)
        if select_first and not selection and ordered:
            self.tree.selection_set(ordered[0])

    def set_quality_display(self, iid, result):
        label = quality_label(result)
        # Keep history visibly distinct even when the narrow table cell truncates.
        if result and result.get('historical'):
            current = dict(result, historical=False)
            label = '历史 · ' + quality_label(current)
        self.tree.set(iid, 'quality', label)
        tag = {'passed': 'sample_passed', 'failed': 'sample_failed'}.get((result or {}).get('status'))
        self.tree.item(iid, tags=(tag,) if tag else ())

    def show_quality_summary(self, event=None):
        selected = self.tree.selection()
        if not selected or self.busy or self.probing:
            return
        row = self.rows.get(selected[0], {})
        result = row.get('quality')
        if result:
            checked = time.strftime('%H:%M', time.localtime(result.get('checked_at', time.time())))
            self.search_status.set('%s · %s · %s 检查 · %s' % (
                row.get('source', ''), quality_label(result), checked,
                '抽检通过不代表整本完整，下载时仍逐章校验。' if result.get('status') == 'passed' else result.get('message', '可重新测速。')[:100]))

    def probe_selected(self):
        if self.busy:
            return
        if self.probing:
            self.probe_stop.set()
            self.probe_btn.configure(text='正在停止…', state='disabled')
            self.search_status.set('正在停止测速，等待已发出的请求返回…')
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo('选择小说', '先选中一本小说，再对同书名、同作者的渠道测速。', parent=self)
            return
        candidates = matching_rows(self.rows[selected[0]], list(self.rows.values()))
        jobs = [(dict(row), self.rule_for_row(row)) for row in candidates]
        jobs = [(row, rule) for row, rule in jobs if rule is not None]
        if not jobs:
            self.search_status.set('所选网址没有匹配的书源规则，无法测速。')
            return
        self.catalog_stop.set()
        self.search_id += 1
        self.searching = False
        self.probing = True
        self.probe_id += 1
        probe_id = self.probe_id
        self.probe_stop = stop = threading.Event()
        self.probe_focus = {row['url'] for row, rule in jobs}
        self.search_btn.configure(state='disabled')
        self.download_btn.configure(state='disabled')
        self.probe_btn.configure(text='停止测速')
        for iid, row in self.rows.items():
            if self.tree.set(iid, 'count') == '查询中…':
                self.tree.set(iid, 'count', '待查询')
            if row['url'] in self.probe_focus:
                row['quality'] = dict(status='checking')
                self.set_quality_display(iid, row['quality'])
        self.search_status.set('正在检测 %s 个同书渠道 · 读取完整目录，抽检首、中、末章；最多同时检测 2 个书源。' % len(jobs))

        def run():
            # Different sources may run in parallel, but aliases of one host queue.
            gates = {}
            for row, rule in jobs:
                gates.setdefault((urlsplit(rule['url']).hostname or '').removeprefix('www.'), threading.Lock())
            def check(job):
                row, rule = job
                try:
                    with gates[(urlsplit(rule['url']).hostname or '').removeprefix('www.')]:
                        result = probe_source(rule, row, stop, self.state_dir / '测速临时')
                except Exception as exc:
                    result = dict(status='cancelled' if stop.is_set() else 'failed', count=None,
                                  message=str(exc)[:200], checked_at=time.time(), sample_count=0)
                if not stop.is_set() and result.get('status') not in ('cancelled', 'checking'):
                    try:
                        self.quality_store.put(rule, row['url'], result)
                    except OSError as exc:
                        self.emit('log', message='测速记录未能保存：' + str(exc))
                self.emit('quality_result', probe_id=probe_id, url=row['url'], result=result)
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(check, jobs))
            finally:
                self.emit('quality_done', probe_id=probe_id, cancelled=stop.is_set())
        threading.Thread(target=run, daemon=True).start()

    def download(self):
        selected = self.tree.selection()
        if self.busy or self.probing:
            return
        if not selected:
            messagebox.showinfo('选择小说', '请先搜索并选中一本小说，核对作者后再下载。')
            return
        self.start_download(self.rows[selected[0]])

    def by_url(self):
        if self.busy or self.probing:
            return
        url = simpledialog.askstring('书籍网址', '粘贴小说详情页或目录页网址：', parent=self)
        if url and url.startswith(('https://', 'http://')):
            self.start_download(dict(url=url.strip(), title='正在识别小说…'))

    def start_download(self, row):
        if self.busy or self.probing:
            return
        self.busy = True
        self.download_url = row['url']
        # Stop queued search/preview requests before prioritising book bodies.
        # In-flight responses may finish, but their old events cannot change the UI.
        self.catalog_stop.set()
        self.search_id += 1
        self.searching = False
        self.search_btn.configure(state='disabled')
        self.speed_box.configure(state='disabled')
        self.probe_btn.configure(state='disabled')
        for iid, result in self.rows.items():
            if result.get('count') is None and self.tree.set(iid, 'count') == '查询中…':
                self.tree.set(iid, 'count', '下载时暂停查询')
        self.search_status.set('下载中 · 已停止后台搜索和目录查询，将网络优先用于当前小说；已有结果保留。')
        self.stop = threading.Event()
        self.report = self.artifact = None
        self.open_file_btn.pack_forget()
        self.locate_file_btn.pack_forget()
        self.pause_btn.pack(side='right')
        self.book.set(row['title'])
        self.status.set('读取目录，建立章节清单…')
        self.stats.set('准备中 · 完成章节核对后显示进度')
        self.progress['value'] = 0
        self.download_btn.configure(state='disabled')
        self.pause_btn.configure(state='normal')
        folder = Path(self.output.get()).expanduser()
        if not folder.is_absolute():
            folder = BASE / folder
        args = argparse.Namespace(url=row['url'], rules=str(self.rules_path), output=str(folder.resolve()), format=self.format.get(), **speed_options(self.speed.get()), retries=3, timeout=20, min_chars=100, refresh=False)
        self.output.set(args.output)
        self.settings.update(output=args.output, format=args.format, speed=self.speed.get())
        self.save_settings()
        self.write_log(self.speed.get() + '模式 · 遵守书源限速，遇到限流自动降速；缓存及章节校验保留')
        def run():
            downloader = None
            try:
                downloader = engine.create_downloader(args, self.stop)
                downloader.confirm_gaps = self.confirm_download_gaps
                downloader.run()
            except InterruptedError as exc:
                self.emit('cancelled', message=str(exc), report=str(downloader.report_path) if downloader else '')
            except Exception as exc:
                self.emit('error', message=str(exc), report=str(downloader.report_path) if downloader and downloader.report_path.exists() else '')
        threading.Thread(target=run, daemon=True).start()

    def confirm_download_gaps(self, title, gaps, available):
        request = dict(title=title, gaps=gaps, available=available,
                       ready=threading.Event(), accepted=False, stop=self.stop)
        self.emit('confirm_gaps', request=request)
        while not request['ready'].wait(.1):
            if request['stop'].is_set():
                return False
        return request['accepted'] and not request['stop'].is_set()

    def pause(self):
        self.stop.set()
        if self.gap_dialog is not None and self.gap_dialog.winfo_exists():
            self.gap_dialog.choose(False)
        self.pause_btn.configure(state='disabled')
        self.status.set('正在暂停，等待当前网络请求结束；缓存会保留…')

    def poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                self.handle(kind, data)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def handle(self, kind, d):
        if d.get('search_id', self.search_id) != self.search_id:
            return
        if kind.startswith('quality_'):
            if d.get('probe_id') != self.probe_id or not self.probing:
                return
            if kind == 'quality_result':
                result = d['result']
                for iid, row in self.rows.items():
                    if row['url'] == d['url']:
                        row['quality'] = result
                        self.set_quality_display(iid, result)
                        if result.get('count') is not None:
                            row['count'] = result['count']
                            label = '{:,} 章'.format(result['count'])
                            if result.get('status') == 'gaps':
                                label += ' · 待核对' if result.get('numbering_issues') else ' · 有跳号'
                            self.tree.set(iid, 'count', label)
                        if result.get('latest'):
                            row['latest'] = result['latest']
                            self.tree.set(iid, 'latest', result['latest'])
                        self.write_log(row['source'] + ' · ' + quality_label(result) + ' · ' + result.get('message', ''))
                complete = sum(r.get('quality', {}).get('status') != 'checking' for r in self.rows.values() if r['url'] in self.probe_focus)
                self.search_status.set('已检测 %s / %s 个渠道 · 样本通过不代表整本完整，下载仍会逐章校验。' % (complete, len(self.probe_focus)))
            elif kind == 'quality_done':
                self.probing = False
                self.probe_btn.configure(text='测速排序', state='normal')
                self.search_btn.configure(state='normal')
                self.download_btn.configure(state='normal')
                for iid, row in self.rows.items():
                    if row.get('quality', {}).get('status') == 'checking':
                        row['quality'] = dict(status='cancelled')
                        self.set_quality_display(iid, row['quality'])
                self.sort_results()
                self.search_status.set(('测速已停止' if d.get('cancelled') else '测速完成') + ' · 同书渠道已排序，原选中项保留；抽检通过不代表整本完整。')
            return
        if kind == 'confirm_gaps':
            request = d['request']
            if request['stop'].is_set() or not self.busy:
                request['ready'].set()
                return
            self.status.set('目录需要核对 · 等待你确认，尚未下载正文')
            def decide(accepted):
                request['accepted'] = accepted
                request['ready'].set()
                self.gap_dialog = None
            self.gap_dialog = GapDialog(self, request['title'], request['gaps'], request['available'], decide)
            return
        if kind == 'catalog_result':
            for iid, row in self.rows.items():
                if row['url'] == d['url']:
                    row['count'] = d['count']
                    self.tree.set(iid, 'count', '{:,} 章'.format(d['count']) if d['count'] is not None else '读取失败')
                    if d.get('warnings'):
                        self.tree.set(iid, 'count', '{:,} 章 · {}'.format(d['count'], '有跳号' if d.get('gaps') else '编号异常'))
                        for warning in d['warnings']:
                            self.write_log(row['title'] + ' · ' + warning)
                    if d.get('latest'):
                        row['latest'] = d['latest']
                        self.tree.set(iid, 'latest', d['latest'])
                    if d.get('message'):
                        self.write_log(row['title'] + ' · ' + row['source'] + ' · 目录读取失败：' + d['message'][:160])
                        row['quality'] = dict(status='failed', phase='catalog', checked_at=time.time(), message=d['message'])
                        self.set_quality_display(iid, row['quality'])
            return
        if kind == 'catalog_done':
            counted = sum(row.get('count') is not None for row in self.rows.values())
            if self.rows:
                self.search_status.set('共 %s 条结果 · %s 条目录已读取；总章数含番外等目录条目，不代表作品已完结。' % (len(self.rows), counted))
            return
        if kind.startswith('search_'):
            if kind == 'search_result':
                for row in d['rows']:
                    rule = self.rule_for_row(row)
                    if rule is not None:
                        previous = self.quality_store.get(rule, row['url'])
                        if previous:
                            row = dict(row, quality=previous)
                    label = ('历史 · ' if row.get('cached_at') else '') + row['source']
                    iid = self.tree.insert('', 'end', values=(row['title'], row['author'], label, '查询中…', quality_label(row.get('quality')), row['latest'] or '未提供'))
                    self.rows[iid] = row
                    self.set_quality_display(iid, row.get('quality'))
                self.write_log(d['source'] + '：返回 %s 条结果' % len(d['rows']))
            elif kind == 'search_failure':
                self.write_log(d['source'] + '：暂不可用 · ' + d['message'][:160])
            if kind == 'search_done':
                self.searching = False
                self.search_btn.configure(state='normal')
                self.sort_results(select_first=True)
                self.search_status.set('搜索完成 · %s 条结果；正在后台补充目录总章数，可直接选择下载。' % len(self.rows) if self.rows else '没有可用结果。可重试、换书名，或粘贴书籍网址；查看“书源状态”了解原因。')
            else:
                self.search_status.set('已查询 %s / %s 个书源 · 找到 %s 条结果' % (d['done'], d['total'], len(self.rows)))
        elif kind == 'progress':
            current, total = d['current'], d['total']
            self.progress['value'] = min(99, current / max(1, total) * 100)
            self.stats.set('已校验 %s / %s 章   ·   复用缓存 %s 章   ·   %s%%' % (current, total, d.get('reused', 0), int(self.progress['value'])))
            network = d.get('network')
            if network:
                self.stats.set(self.stats.get() + '   ·   当前并发上限 %s' % network['active_limit'])
        elif kind == 'book':
            self.book.set(d['title'])
            for iid, row in self.rows.items():
                if row['url'] == self.download_url:
                    row['count'] = d['total']
                    self.tree.set(iid, 'count', '{:,} 章'.format(d['total']))
        elif kind == 'stage':
            self.status.set(d['message'])
        elif kind == 'log':
            self.write_log(d['message'])
        elif kind in ('complete', 'error', 'cancelled'):
            self.busy = False
            self.download_btn.configure(state='normal')
            self.search_btn.configure(state='normal')
            self.speed_box.configure(state='readonly')
            self.probe_btn.configure(state='normal')
            self.search_status.set('已有 %s 条搜索结果保留 · 可继续选择下载，或重新搜索补充目录总章数。' % len(self.rows))
            self.pause_btn.configure(state='disabled')
            self.report = d.get('report')
            if kind == 'complete':
                artifact = Path(d['output']).resolve()
                if not artifact.is_file() or artifact.stat().st_size == 0:
                    self.status.set('未完成 · 成品文件不存在或大小为零，请检查保存位置：' + str(artifact))
                    self.write_log(self.status.get())
                    return
                self.artifact = str(artifact)
                self.progress['value'] = 100
                self.status.set('下载完成 · 源站目录有跳号，仅下载现有章节；详情见校验报告' if d.get('missing_chapters') else '下载完成 · 目录有编号异常，文件回读校验通过；详见报告' if d.get('warnings') else '下载完成 · 当前书源目录已全部下载，文件回读校验通过')
                self.stats.set('%s / %s 章   ·   %.1f 秒   ·   %s · %s' % (d['chapters'], d['chapters'], d['seconds'], artifact.suffix[1:].upper(), file_size_text(artifact.stat().st_size)))
                self.write_log('文件已保存：' + self.artifact)
                self.settings['last_completed'] = dict(path=self.artifact, report=self.report)
                self.save_settings()
                self.show_file_actions()
                if self.completion_dialog is not None and self.completion_dialog.winfo_exists():
                    self.completion_dialog.destroy()
                self.completion_dialog = CompletionDialog(self, artifact, d, self.open_artifact, self.open_artifact_folder)
            else:
                self.status.set(('已暂停 · ' if kind == 'cancelled' else '未完成 · ') + d['message'][:240])
                self.write_log(d['message'])

    def choose_folder(self):
        path = filedialog.askdirectory(initialdir=self.output.get() if Path(self.output.get()).exists() else str(BASE))
        if path:
            self.output.set(path)

    def open_folder(self):
        completed = self.artifact or (self.settings.get('last_completed') or {}).get('path')
        if completed:
            self.open_artifact_folder(Path(completed))
            return
        path = Path(self.output.get()).resolve()
        self.open_local_path(path)

    def show_file_actions(self):
        self.pause_btn.pack_forget()
        self.open_file_btn.pack(side='right', padx=(8, 0))
        self.locate_file_btn.pack(side='right')

    def save_settings(self):
        try:
            engine.atomic_json(self.settings_file, self.settings)
        except OSError as exc:
            self.write_log('设置未能保存：' + str(exc))

    def open_local_path(self, path):
        path = Path(path)
        if not path.exists():
            messagebox.showinfo('未找到文件或目录', '原保存位置已不存在，文件可能被移动：\n' + str(path), parent=self)
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror('无法打开', '请检查文件关联或手动打开所在文件夹。\n%s\n%s' % (path, exc), parent=self)

    def open_artifact(self, path=None):
        path = path or self.artifact
        if path:
            self.open_local_path(path)

    def open_artifact_folder(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            messagebox.showinfo('下载文件已不在原位置', '文件可能被移动或删除，原保存位置是：\n' + str(path), parent=self)
            return
        self.open_local_path(path.parent)

    def open_report(self):
        if self.report and Path(self.report).exists():
            report = json.loads(Path(self.report).read_text('utf-8'))
            self.show_text('章节校验报告', json.dumps(report, ensure_ascii=False, indent=2))
        else:
            messagebox.showinfo('校验报告', '下载完成或发现问题章节后，这里会显示校验报告。')

    def show_text(self, title, content):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry('800x520')
        text = tk.Text(win, wrap='word', font=('Microsoft YaHei UI', 10), padx=15, pady=15)
        text.pack(fill='both', expand=True)
        text.insert('1.0', content)
        text.configure(state='disabled')

    def show_sources(self):
        lines = ['默认仅查询启用的书源；停用书源不会反复请求。', '书源可用性会随网络和网站状态变化，下方列出本次修复与检测原因。', '将兼容 SoNovel 的规则文件命名为“书源.json”放在软件旁，重启即可加载。', '']
        lines += [r['name'] + (' · 已启用 · ' + r.get('verifiedAt', '未实测') if supported(r) else ' · 已停用 · ' + r.get('disabledReason', '搜索规则停用或暂不支持')) for r in self.rules]
        self.show_text('书源与实测记录', '\n'.join(lines + ['\n最近记录'] + self.history[-80:]))

    def close(self):
        self.catalog_stop.set()
        if self.probing:
            self.probe_stop.set()
            self.after(200, self.finish_close)
            return
        if self.busy:
            self.pause()
            self.after(200, self.finish_close)
        else:
            self.destroy()

    def finish_close(self):
        if self.busy or self.probing:
            self.after(200, self.finish_close)
        else:
            self.destroy()


if __name__ == '__main__':
    if os.name == 'nt':
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    App().mainloop()



