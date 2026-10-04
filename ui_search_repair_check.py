import tempfile
from pathlib import Path
from app import App
app=App(); app.withdraw()
row=dict(title='我的混沌城',author='凌虚月影',source='全本小说（备用）',url='https://www.quanben.io/n/wodehunduncheng/',latest='',cached_at=123)
app.handle('search_result',dict(rows=[row],source=row['source'],done=1,total=6))
iid=next(iter(app.rows))
assert app.tree.set(iid,'source')=='历史 · 全本小说（备用）'
app.handle('catalog_result',dict(url=row['url'],count=1240,latest='第1240章 未来值得期待（全书完）'))
assert app.tree.set(iid,'count')=='1,240 章'
fresh=dict(row,source='笔趣集',url='https://www.biquji.com/62/62965/')
fresh.pop('cached_at')
app.handle('search_result',dict(rows=[fresh],source=fresh['source'],done=2,total=6))
app.handle('search_done',{})
assert app.rows[app.tree.get_children()[0]]['source']=='笔趣集'
app.tree.selection_set(iid)
assert app.rows[iid]['url']==row['url']
app.destroy()
print('UI cached source label, catalog count and selection verified')
