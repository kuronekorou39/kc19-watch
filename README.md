# 空室監視 Bot（Telegram通知 + Webダッシュボード + 予約アシスト）

予約サイトの空室状況を定期的にチェックし、空きが出たら Telegram に通知するツールです。
Web ダッシュボードで状況・推移グラフ・ログを確認でき、空室検知時にはブラウザ自動化で
予約フォームを途中まで自動入力して人に渡す（handoff）機能もあります。

> 対象サイト・施設・部屋・プランなどの具体的な設定はリポジトリに含めず、
> `settings.local.json`（gitignore 済み）に記述します。実値は別途共有されるものを入れてください。

## 使い方は2通り

| | 対象 | 自動予約(handoff) |
|---|---|---|
| **exe版**（下記「配布版」） | Python が無くてもOK。ダウンロードして置くだけ | ✕（通知のリンクから手動予約） |
| **ソース版**（下記「セットアップ」） | Python/uv を使える人 | ○ |

## 配布版（exe・Python不要）

### 使う側の手順
1. Releases から `kc19-watch.exe` をダウンロードし、好きなフォルダに置く
2. 共有された `settings.local.json` を **exe と同じフォルダ** に置く
   （もらっていない場合も起動はでき、設定画面から手入力できます）
3. exe をダブルクリック → 自動でブラウザにダッシュボードが開く
   （Windows SmartScreen の警告が出たら「詳細情報」→「実行」）
4. [@BotFather](https://t.me/BotFather) で自分用 Bot を作り、トークンを設定画面
   「Telegram Bot Token」に貼って保存 → **exe を一度閉じて再起動**
5. Telegram で自分の Bot に `/start` → 監視開始＆通知先に登録される

設定・DB・ログはすべて exe と同じフォルダに作られます。

### 配る側（ビルド）
```powershell
./build_exe.ps1   # => dist/kc19-watch.exe (これを Releases 等で配布)
```

## セットアップ（uv）

### 前提
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)（未導入なら `pip install uv`）
- 自動予約を使う場合のみ: `uv run playwright install chromium`

### 手順
```bash
uv sync
cp settings.example.json settings.local.json   # コピーして値を埋める
uv run kemocon        # Webダッシュボード + Bot（推奨） → http://127.0.0.1:8000
uv run kemocon-bot    # Botのみ（ダッシュボードなし）
```

### 設定（`settings.local.json`）
- `telegram.bot_token` … BotFather のトークン（環境変数 `TELEGRAM_BOT_TOKEN` でも可）
- `monitor.site` … 予約サイトのエンドポイント（在庫API・予約カレンダー・予約フォームのURL）
- `monitor.yado_id` / `target_rooms` / `plans` / `menu_url` … 監視対象の施設・部屋・プラン
- `reservation.guest` … 予約フォームに自動入力する本人情報

`settings.local.json` は gitignore 済み（トークン・個人情報・対象サイトを含むため共有されない）。

## 主な機能

### Web ダッシュボード（http://127.0.0.1:8000）
- サーバー状況（監視ON/OFF・間隔・次回チェック・回数・メモリ）
- 空室状況（部屋 × プラン × 日付、空室数、予約ページへのリンク）
- 空室数の推移グラフ（部屋別・SQLiteに永続化）
- ログビューア（リアルタイム）
- 操作（監視の開始/停止・間隔変更・手動チェック・テスト通知）
- 解禁バースト（指定時間帯だけ間隔を狭め、時間外は自動で戻る）

### Telegram コマンド
- `/start` 監視開始 ／ `/end` 停止 ／ `/now` 現在の状況 ／ `/info` 情報 ／ `/help`

### 自動予約アシスト（handoff）
- 空室検知時、ブラウザ(Playwright)で予約フォームを自動入力し「確認画面」まで進めて残す。
- **最終確定（予約するボタン）は人間が押す**設計。Bot は確定しない。
- 各項目は独立入力（フォームが多少違っても壊れず、埋められた分だけ入れて報告）。

## 監視の仕組み（概要）
- 予約サイトの在庫APIを定期ポーリングし、部屋×プラン×日付ごとに空室数を取得。
- 前回との差分（満室→空室）を検知して Telegram 通知。初回はベースライン記録のみ。
- 状態・履歴は SQLite（`*.db`、gitignore）に永続化。ログは `logs/`（gitignore）。

## ファイル構成
```
kc19-watch/
├── pyproject.toml / uv.lock / .python-version
├── main.py                    # 起動: ダッシュボード + Bot
├── settings.example.json      # 設定テンプレ（コピーして settings.local.json に）
├── kemocon/                   # 本体パッケージ
│   ├── config.py              # 設定の読み書き
│   ├── state.py               # 共有ランタイム状態 + ログバッファ
│   ├── monitor.py             # 空室チェック + 変化検知 + コントローラ + バースト
│   ├── bot.py                 # Telegram Bot
│   ├── web.py                 # FastAPI ダッシュボード
│   ├── store.py               # SQLite永続化
│   ├── booking.py             # Playwright予約アシスト（handoff）
│   └── static/index.html      # ダッシュボード UI
├── settings.local.json        # 各自の設定（gitignore・共有されない）
├── logs/ , *.db               # ランタイム（gitignore）
└── README.md
```

## 注意
- 予約はキャンセル規定付きの本予約です。最終確定は人間が行う設計ですが、自己責任でご利用ください。
- 各サイトの利用規約に従い、過度なアクセス（短すぎる間隔）は避けてください。
- Bot（トークン）は各自専用に作成してください。個人チャットで /start した人は誰でも通知先に登録され、監視を開始/停止できます。Bot のユーザー名はむやみに公開しないでください。
