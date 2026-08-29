"""設定の読み書き。

年ごとに変わる監視設定・Telegram設定・(将来の)予約設定をまとめて
`settings.local.json` に保存する。このファイルは .gitignore 済み
(Telegramトークンや個人情報を含むため)。ファイルが無ければ既定値で自動生成する。

サイトURL・施設ID・部屋/プランなどの実値は「共有設定ファイル」
(settings.share.yaml / .yml / .json。リポジトリには含めず別途配布)から取り込む。
exeと同じフォルダに置けば起動時に自動で取り込まれ、GUIからも読み込める。
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

# exe化(PyInstaller等のfrozen)時は、設定・DB・ログをexeと同じフォルダに置く
# (__file__ は一時展開先を指すため使えない)
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = BASE_DIR / "settings.local.json"

# Botのトークンは settings.local.json もしくは環境変数 TELEGRAM_BOT_TOKEN で設定する。
# 共有リポジトリにトークンを含めないため、既定は空。
_DEFAULT_BOT_TOKEN = ""

DEFAULT_SETTINGS: dict[str, Any] = {
    "telegram": {
        "bot_token": _DEFAULT_BOT_TOKEN,
        # /start したチャットIDがここに追加され、通知先になる
        "notify_chat_ids": [],
    },
    "monitor": {
        # 対象サイト・施設の情報は settings.local.json に入れる(共有リポには含めない)
        "yado_id": "",
        "api_ty": "lim,sp",
        "target_year": "2026",
        # サイトのエンドポイント(settings.local.json で設定)
        "site": {
            "api_url": "",    # 在庫検索API
            "date_url": "",   # 予約カレンダーページ
            "form_url": "",   # 予約入力フォーム
        },
        # 走査はAPI応答に含まれる全部屋が対象(部屋名も応答から自動取得)。
        # scan_room_ids に room_id を列挙した場合だけ、その部屋に限定する
        "scan_room_ids": [],
        # Telegram通知する部屋(room_id)。空 = 走査した全部屋を通知
        "notify_room_ids": [],
        # 部屋の表示名の上書き [{label, room_id}](任意)。旧設定との互換も兼ねる
        "target_rooms": [],
        # 走査で発見した部屋 [{room_id, label}]。アプリが自動更新する(GUIの選択肢用)
        "known_rooms": [],
        # 検索する人数(2〜5名)。人数ごとに別々に検索する
        "guest_nums": [2, 3, 4, 5],
        # 監視プラン [{id, label, dates, nights}]。settings.local.json で設定
        "plans": [],
        # メニューページURL(通知リンク/予約URL生成に使用)。settings.local.json で設定
        "menu_url": "",
        # 監視間隔(秒)。1チェックで (プラン数×人数) 回リクエストする点に注意
        # (既定 3プラン×4人数=12リクエスト)。負荷が高ければ延ばす
        "interval_seconds": 100,
        "interval_min": 30,               # 手動で設定できる間隔の下限
        "interval_max": 3600,             # 同 上限
        "auto_start": False,              # 起動時に自動で監視を開始するか
        # 解禁ウィンドウ: 指定時間帯だけ間隔を狭める(公開直後を素早く拾う)。時間外は通常間隔に自動復帰
        "burst": {
            "enabled": False,
            "start": "",                  # 例 "2026-08-04 19:50" (YYYY-MM-DD HH:MM)
            "end": "",                    # 例 "2026-08-04 20:45"
            "interval_seconds": 20,       # バースト中の間隔
        },
    },
    # ブラウザ自動予約(Playwright)用の設定。予約フォームの実フィールド名に対応
    "reservation": {
        # Trueで、空室検知時に予約フォームを自動で開いて入力し人に渡す(handoff)
        "enabled": False,
        # 自動起動の対象は常に「通知する部屋(notify_room_ids)」と同じ範囲。
        # そこからさらに絞る場合だけ room_id / plan_id を列挙する(空=絞らない)
        "auto_book_room_ids": [],
        "auto_book_plan_ids": [],
        "max_windows": 4,                 # 1チェックで自動起動するフォーム窓の上限(取りすぎ防止)
        # Trueで「確認画面へ」まで自動で進める(残りは「予約する」1クリックのみ)。Botは予約完了は押さない
        "proceed_to_confirm": True,
        "checkin_time": "15:00",          # ck_hourmin(チェックイン時刻)
        # 予約する大人の人数(man_1 + woman_1 = 予約人数)。合計が対象プランの許容人数内で
        "adults_male": 2,
        "adults_female": 0,
        # 代表者情報(予約フォームの guest_* 項目に対応)
        "guest": {
            "name": "",       # 代表者氏名 guest_name(例: 山田 太郎)
            "kana": "",       # 読みがな guest_kana(全角かな/カナ 例: やまだ たろう)
            "tel": "",        # 電話 guest_tel(例: 09000000000)
            "post1": "",      # 郵便番号 前3桁 guest_post1
            "post2": "",      # 郵便番号 後4桁 guest_post2
            "pref": "",       # 都道府県 guest_pref_id(名称 例: 静岡県)
            "address": "",    # 住所 guest_address
            "email": "",      # Eメール guest_mail1/2(両方に入る)
        },
        "payment_method": "現地決済",       # 現地決済 / クレジットカード事前決済
        # handoff(画面を残して人が仕上げる)には表示ありが必須なので既定 False
        "headless": False,
    },
}

# GUIに出さない/マスクする秘密キー(パス表記)
SECRET_PATHS = {("telegram", "bot_token")}

# 共有設定ファイルの探索順(BASE_DIR = exeと同じフォルダ)
SHARE_FILE_NAMES = ("settings.share.yaml", "settings.share.yml", "settings.share.json")

# 走査(監視)開始に最低限必要な項目。ラベルはGUIのチェックリストにそのまま出す
REQUIRED_FIELDS: list[tuple[str, tuple[str, ...]]] = [
    ("施設ID", ("monitor", "yado_id")),
    ("在庫検索APIのURL", ("monitor", "site", "api_url")),
    ("メニューページのURL", ("monitor", "menu_url")),
    ("監視プラン", ("monitor", "plans")),
]
# 予約カレンダー/フォームのURL(site.date_url / site.form_url)は予約アシスト用で、
# 監視自体には不要なので必須にしない(無ければリンクは menu_url にフォールバック)


def setup_console() -> None:
    """Windowsコンソールの文字化け対策。

    日本語WindowsのコンソールはCP932のため、UTF-8のログ(日本語・絵文字)が
    化ける。コードページをUTF-8(65001)にし、stdout/stderrもUTF-8に揃える。
    (絵文字はフォント次第で□になることはあるが、化けはしない)
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _deep_merge(base: dict, override: dict) -> dict:
    """base を override で再帰的に上書きした新しい dict を返す。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_settings(apply_env: bool = True) -> dict[str, Any]:
    """設定を読み込む。既定値に保存済みの値をマージして返す。

    apply_env=False は保存経路用: 環境変数のトークンを実行時だけ効かせ、
    ファイルには書き込まない(誤って永続化しないため)。
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings = _deep_merge(settings, saved)
        except (json.JSONDecodeError, OSError) as exc:
            # 壊れていても既定値で動けるようにする
            print(f"[config] settings.local.json 読み込み失敗: {exc}")
    # 環境変数によるトークン上書き(最優先・実行時のみ)
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if apply_env and env_token:
        settings["telegram"]["bot_token"] = env_token
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    """設定を settings.local.json に保存する。"""
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def merge_and_save(patch: dict[str, Any]) -> dict[str, Any]:
    """現在の設定に patch をマージして保存し、新しい設定を返す。"""
    current = load_settings(apply_env=False)  # 環境変数のトークンはファイルに書かない
    updated = _deep_merge(current, patch)
    save_settings(updated)
    return updated


def clamp_interval(value: Any, mon: dict) -> int:
    """間隔を [interval_min, interval_max] に丸める。"""
    mn = int(mon.get("interval_min", 30))
    mx = int(mon.get("interval_max", 3600))
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = mn
    return max(mn, min(mx, v))


def _get_path(settings: dict, path: tuple[str, ...]) -> Any:
    node: Any = settings
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def required_status(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """必須項目ごとの充足状況 [{label, ok}] を返す(GUIのチェックリスト用)。"""
    return [{"label": label, "ok": bool(_get_path(settings, path))}
            for label, path in REQUIRED_FIELDS]


def missing_required(settings: dict[str, Any]) -> list[str]:
    """未設定の必須項目ラベル一覧。空リストなら走査を開始できる。"""
    return [r["label"] for r in required_status(settings) if not r["ok"]]


def _prune_empty(node: Any) -> Any:
    """空値("" / [] / {} / None)を再帰的に取り除く。

    共有ファイルの空欄(例: bot_token: "")で、利用者が画面から設定済みの値を
    上書きして消してしまわないため。0 や False は意味があるので残す。
    """
    if isinstance(node, dict):
        pruned = {k: _prune_empty(v) for k, v in node.items()}
        return {k: v for k, v in pruned.items()
                if not (v is None or (isinstance(v, (str, list, dict)) and len(v) == 0))}
    return node


def parse_share_text(text: str) -> dict[str, Any]:
    """共有設定ファイルの中身(YAML/JSON)を辞書にする。不正なら ValueError。

    空欄の項目は「指定なし」とみなして取り込まない(既存設定を消さない)。
    """
    try:
        data = yaml.safe_load(text)  # YAMLはJSONの上位互換なのでどちらの形式も読める
    except yaml.YAMLError as exc:
        raise ValueError(f"設定ファイルとして読めません: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("設定ファイルの形式が正しくありません(最上位が辞書ではありません)")
    known = _prune_empty({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    if not known:
        raise ValueError("有効な設定が見つかりません(monitor / telegram / reservation がありません)")
    return known


def auto_import_share() -> str | None:
    """BASE_DIR の共有設定ファイルを、前回取り込み後に更新されていれば取り込む。

    取り込んだファイル名を返す(取り込み不要/失敗は None)。GUIで直した値を
    毎回上書きしないよう、ファイル名+mtime をマーカーとして設定に記録する。
    """
    for name in SHARE_FILE_NAMES:
        path = BASE_DIR / name
        if not path.exists():
            continue
        mtime = int(path.stat().st_mtime)
        mark = load_settings().get("share_import", {})
        if mark.get("file") == name and mark.get("mtime") == mtime:
            return None
        try:
            data = parse_share_text(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            print(f"[config] {name} の取り込みに失敗: {exc}")
            return None
        data["share_import"] = {"file": name, "mtime": mtime}
        merge_and_save(data)
        return name
    return None


def masked(settings: dict[str, Any]) -> dict[str, Any]:
    """秘密情報をマスクした設定(GUI表示用)を返す。"""
    out = copy.deepcopy(settings)
    for path in SECRET_PATHS:
        node = out
        for key in path[:-1]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        last = path[-1]
        if isinstance(node, dict) and node.get(last):
            value = str(node[last])
            node[last] = value[:6] + "…" + value[-4:] if len(value) > 12 else "設定済み"
    return out
