# 配布用の zip (dist/kc19-watch.zip) をビルドする。
#   ./build_exe.ps1
# 前提: uv が入っていること。
# 予約アシスト(ブラウザ自動操作)は利用者PCの Chrome/Edge を使うため、
# Playwright はドライバのみ同梱し、ブラウザ本体は同梱しない。
uv sync
uv run pyinstaller --noconfirm --clean --onefile --name kc19-watch `
  --add-data "kemocon/static;kemocon/static" `
  --collect-all playwright `
  main.py
if ($LASTEXITCODE -ne 0 -or -not (Test-Path dist/kc19-watch.exe)) { Write-Error "ビルド失敗(古いexeが残っていてもzipは作りません)"; exit 1 }

# 同梱する説明書(非エンジニア向け)。zip内のファイル名は文字化け防止のためASCIIにする
$readme = @"
■ 空室監視Bot の使い方

1. この zip を好きな場所に展開(右クリック→すべて展開)します

2. 別途もらった設定ファイル(settings.share.yaml)を
   kc19-watch.exe と同じフォルダに置きます
   ※置き忘れても、あとで画面の「設定」タブから読み込めます

3. kc19-watch.exe をダブルクリックします
   少し待つとブラウザで画面が開きます
   ※Windows の青い警告が出たら「詳細情報」→「実行」を押してください

4. 「設定」タブで必須項目がそろっているか確認します
   テレグラム通知を使う場合はトークンを貼り付けて「保存して接続」し、
   Telegram で自分の Bot に /start を送ってください
   ※通知が届く部屋は「通知する部屋」のチェックで選び直せます
   (チェックなし = 全部屋を通知。走査・記録は常に全部屋)

5. 「走査」タブで「▶ 走査開始」を押せば監視が始まります

■ 終了について
・完全に終了する : 画面右上の「⏻ 終了」ボタン(または黒いウィンドウを閉じる)
・ブラウザのタブを閉じただけなら、監視は裏で動き続けます
　(画面をもう一度見たいときは exe をダブルクリックすれば開きます)
"@
Set-Content -Path dist/README.txt -Value $readme -Encoding UTF8

Compress-Archive -Force -Path dist/kc19-watch.exe, dist/README.txt -DestinationPath dist/kc19-watch.zip
Write-Host "`n=> dist\kc19-watch.zip (Releases にはこの zip だけを上げる)"
