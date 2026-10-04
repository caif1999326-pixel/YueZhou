$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m PyInstaller --noconfirm --onefile --windowed --name YueZhou --add-data 'fanqie_font.json;.' --add-data 'rules.json;.' --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox --hidden-import tkinter.simpledialog --hidden-import lxml.cssselect --hidden-import cssselect app.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
