# 配布用の単一exe (dist/kc19-watch.exe) をビルドする。
#   ./build_exe.ps1
# 前提: uv が入っていること。Playwright(自動予約)はexe版では非対応のため同梱しない。
uv sync
uv run pyinstaller --noconfirm --clean --onefile --name kc19-watch `
  --add-data "kemocon/static;kemocon/static" `
  --exclude-module playwright `
  main.py
Write-Host "`n=> dist\kc19-watch.exe"
