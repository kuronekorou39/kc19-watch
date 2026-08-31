"""FastAPI Webダッシュボード。Botも同じプロセス・同じ非同期ループで起動する。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from telegram import Update

from kemocon import __version__

from . import booking, config, store
from .bot import build_application, build_status_text
from .monitor import controller, setup_logging
from .state import STATE

log = logging.getLogger("kemocon")

# exe化(frozen)時は PyInstaller の展開先(_MEIPASS)に同梱された static を使う
if getattr(sys, "frozen", False):
    _STATIC_DIR = Path(getattr(sys, "_MEIPASS")) / "kemocon" / "static"
else:
    _STATIC_DIR = Path(__file__).resolve().parent / "static"
_PROC = psutil.Process()


def _restore_history() -> None:
    """DBに残っている過去の変化イベントを履歴パネルに復元する。"""
    for e in reversed(store.get_events(50)):
        STATE.history.append({
            "at": (e["ts"] or "").replace("T", " "),
            "room_label": e["room"], "plan_label": e["plan_label"],
            "date": e["target_date"], "type": e["type"],
            "guest_nums": e["guest_nums"].split(",") if e["guest_nums"] else [],
            "aki_num": e["aki_num"],
        })


# start/stop は途中に複数の await を挟むため、保存ボタン連打や
# 「保存と共有ファイル取り込みの同時実行」で二重起動(同一トークンの二重
# ポーリング=Telegram 409)にならないよう直列化する
_BOT_LOCK = asyncio.Lock()


async def _teardown_application(application) -> None:
    """PTB Application を段階ごとに確実に畳む(1段の失敗で残りを飛ばさない)。"""
    for step in (application.updater.stop, application.stop, application.shutdown):
        try:
            await step()
        except Exception as exc:  # noqa: BLE001
            log.error("Bot停止中エラー(%s): %s", getattr(step, "__name__", step), exc)


async def start_bot(app: FastAPI) -> tuple[bool, str]:
    """Telegram Bot を起動して常駐させる。(成功?, ユーザー向けメッセージ) を返す。

    設定画面でトークンを保存した直後にも呼ばれる(再起動なしで接続できるように)。
    """
    async with _BOT_LOCK:
        if getattr(app.state, "ptb", None) is not None:
            return True, "接続済みです"
        settings = config.load_settings()
        if not settings["telegram"].get("bot_token"):
            return False, "トークンが未設定です"
        application = None
        try:
            application = build_application(settings)
            await application.initialize()
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram Bot の起動に失敗しました(トークン不正?): %s", exc)
            controller.bot = None
            # 途中まで起動したPTBを畳む(放置するとポーリングやHTTPクライアントが
            # 参照不能なまま生き残り、再試行のたびに積み上がる)
            if application is not None:
                await _teardown_application(application)
            return False, "接続に失敗しました。トークンを確認してください"
        controller.bot = application.bot
        app.state.ptb = application
        log.info("✅ Telegram Bot 接続完了")
        return True, "Telegramに接続しました"


async def stop_bot(app: FastAPI) -> None:
    """Bot を停止する(トークン差し替え時・シャットダウン時)。"""
    async with _BOT_LOCK:
        application = getattr(app.state, "ptb", None)
        app.state.ptb = None
        controller.bot = None
        if application:
            await _teardown_application(application)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    store.init_db()
    store.close_dangling_sessions(store.now_iso())  # 前回の開きっぱなしセッションを閉じる
    _restore_history()
    _PROC.cpu_percent()  # cpu_percent の初回呼び出し(次回から実値)
    imported = config.auto_import_share()
    if imported:
        log.info("📄 共有設定ファイル %s を取り込みました", imported)
    settings = config.load_settings()
    STATE.interval = config.clamp_interval(settings["monitor"]["interval_seconds"], settings["monitor"])
    # トークン未設定/不正でもダッシュボードは起動する(設定画面から入力すれば再起動なしで接続)
    app.state.ptb = None
    app.state.shutting_down = False
    ok, msg = await start_bot(app)
    if not ok:
        log.warning("Telegram未接続で起動します(%s)。設定タブから接続できます。通知は届きません", msg)
    log.info("🚀 ダッシュボード起動完了")
    # PTBの post_init は run_polling 経由でしか呼ばれないため、webモードの auto_start はここで行う
    if settings["monitor"].get("auto_start"):
        await controller.start()
    try:
        yield
    finally:
        app.state.shutting_down = True   # SSEループを速やかに止める
        await controller.stop()
        await stop_bot(app)


app = FastAPI(title="Vacancy Watch Dashboard", lifespan=lifespan)


def _system_metrics() -> dict:
    return {
        "memory_mb": round(_PROC.memory_info().rss / (1024 * 1024), 1),
        "cpu_percent": _PROC.cpu_percent(),
    }


# 万一スクリプトが差し込まれても外部へデータを送れないようにする多層防御。
# 通信・画像は自分自身のみ(connect-src/img-src 'self')に限定。予約サイトへの
# リンクはタブ遷移(top-level navigation)なので影響しない。inline script/style は
# このダッシュボードの構成上 'unsafe-inline' が必要。
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # ブラウザに古いページをキャッシュさせない(コード更新後の表示ズレ防止)
    return HTMLResponse(
        (_STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache",
                 "Content-Security-Policy": _CSP},
    )


def _build_status() -> dict:
    """/api/status と /api/stream で共通の状態スナップショットを作る。"""
    settings = config.load_settings()
    snap = STATE.snapshot()
    snap["system"] = _system_metrics()
    snap["version"] = __version__
    snap["status_text"] = build_status_text(settings)
    snap["notify_chat_count"] = len(settings["telegram"]["notify_chat_ids"])
    # 代表者情報などの保存先(設定タブに表示。どこに何が保存されるか利用者が確認できるように)
    snap["settings_file"] = str(config.SETTINGS_FILE)
    # 予約アシストの対象選択(設定タブのチェックボックス用)
    res = settings["reservation"]
    snap["reservation_cfg"] = {
        "auto_book_room_ids": [str(x) for x in (res.get("auto_book_room_ids") or [])],
        "auto_book_plan_ids": [str(x) for x in (res.get("auto_book_plan_ids") or [])],
        "auto_confirm": bool(res.get("auto_confirm")),
        "auto_confirm_max": int(res.get("auto_confirm_max", 1)),
        "auto_confirmed_count": int(res.get("auto_confirmed_count", 0)),
    }
    # セットアップ状況(設定タブのチェックリスト・走査開始ボタンのゲートに使う)
    snap["setup"] = {
        "required": config.required_status(settings),
        "missing": config.missing_required(settings),
        "token_set": bool(settings["telegram"].get("bot_token")),
        "bot_running": controller.bot is not None,
    }
    m = settings["monitor"]
    snap["monitor_cfg"] = {
        "yado_id": m["yado_id"],
        "plans": m.get("plans", []),
        "scan_room_ids": m.get("scan_room_ids", []),
        "notify_room_ids": m.get("notify_room_ids", []),
        "known_rooms": m.get("known_rooms", []),
        "guest_nums": m.get("guest_nums", []),
        "interval_seconds": m["interval_seconds"],
        "interval_min": int(m.get("interval_min", 30)),
        "interval_max": int(m.get("interval_max", 3600)),
        "menu_url": m["menu_url"],
    }
    return snap


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(_build_status())


@app.get("/api/logs")
async def api_logs(source: str = "all", after: int = 0, limit: int = 500) -> JSONResponse:
    return JSONResponse({"logs": STATE.logs_since(after, source, limit)})


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    async def gen():
        last_seq = STATE._log_seq
        while not getattr(request.app.state, "shutting_down", False):
            if await request.is_disconnected():
                break
            snap = _build_status()
            new_logs = STATE.logs_since(last_seq)
            if new_logs:
                last_seq = new_logs[-1]["seq"]
            payload = {"status": snap, "logs": new_logs}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/control/start")
async def api_start(request: Request) -> JSONResponse:
    missing = config.missing_required(config.load_settings())
    if missing:
        return JSONResponse({"ok": False, "reason": "missing", "missing": missing,
                             "monitoring": STATE.monitoring})
    body = await _json(request)
    interval = body.get("interval")
    if interval:
        mon = config.load_settings()["monitor"]
        interval = config.clamp_interval(interval, mon)
        STATE.interval = interval
        config.merge_and_save({"monitor": {"interval_seconds": interval}})
    ok = await controller.start(interval)
    return JSONResponse({"ok": ok, "monitoring": STATE.monitoring, "interval": STATE.interval})


@app.post("/api/control/interval")
async def api_interval(request: Request) -> JSONResponse:
    """稼働中でも間隔を変更(次サイクルから反映)。min/maxに丸める。"""
    body = await _json(request)
    mon = config.load_settings()["monitor"]
    v = config.clamp_interval(body.get("interval"), mon)
    STATE.interval = v
    config.merge_and_save({"monitor": {"interval_seconds": v}})
    return JSONResponse({"ok": True, "interval": v,
                         "min": int(mon.get("interval_min", 30)),
                         "max": int(mon.get("interval_max", 3600))})


@app.post("/api/control/stop")
async def api_stop() -> JSONResponse:
    ok = await controller.stop()
    return JSONResponse({"ok": ok, "monitoring": STATE.monitoring})


@app.post("/api/control/check")
async def api_check() -> JSONResponse:
    await controller.check_once()
    return JSONResponse({"ok": True, "room_status": STATE.room_status})


@app.get("/api/history")
async def api_history(limit: int = 8000, hours: float = 24) -> JSONResponse:
    """空室数の推移(グラフ用)。直近 hours 時間ぶんを部屋ごとに返す(hours<=0で全期間)。

    サンプルは変化時のみ記録されるため、窓の開始時点の値は窓より前の最後の
    記録から引き継ぐ。全期間0室の部屋は series から省いて件数だけ返す
    (全部屋走査では0室の線が大半で、肝心の空室が見えなくなるため)。
    """
    samples = store.get_samples(limit)
    cutoff = ((datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
              if hours > 0 else "")  # ""はすべてのISO時刻より小さい=全期間
    rooms: list[str] = []
    per_ts: dict[str, dict[str, int]] = {}
    for s in samples:
        room = s["room"] or "(不明)"
        if room not in rooms:
            rooms.append(room)
        pt = per_ts.setdefault(s["ts"], {})
        pt[room] = max(pt.get(room, 0), s["aki_num"])
    carry: dict[str, int] = {}
    plist: list[dict] = []
    for ts in sorted(per_ts):  # ISO文字列なので辞書順=時刻順
        if ts < cutoff:
            carry.update(per_ts[ts])
        else:
            plist.append({"ts": ts, **per_ts[ts]})
    # 引き継ぎは「今も走査している部屋」だけ。走査から外れた部屋に古い値の
    # 起点を作ると、確認していないのに空室が続いているような線が描かれてしまう
    if STATE.room_status:
        current = {e["room_label"] for e in STATE.room_status.values()}
        carry = {room: v for room, v in carry.items() if room in current}
    if carry:
        plist.insert(0, {"ts": cutoff, **carry})
    # 「現在」の点(部屋ごとの最大空室数)を足す
    if STATE.room_status:
        now_pt: dict = {"ts": store.now_iso()}
        for e in STATE.room_status.values():
            room = e["room_label"]
            mx = max([0, *e["counts"].values()])
            now_pt[room] = max(now_pt.get(room, 0), mx)
            if room not in rooms:
                rooms.append(room)
        plist.append(now_pt)
    nonzero = [r for r in rooms if any(p.get(r) for p in plist)]
    ymax = max([v for p in plist for k, v in p.items() if k != "ts"], default=0)
    series = [{"key": r, "label": r} for r in nonzero]
    return JSONResponse({"series": series, "points": plist, "ymax": ymax,
                         "zero_rooms": len(rooms) - len(nonzero),
                         "window_hours": hours, "stats": store.stats()})


@app.get("/api/events")
async def api_events(limit: int = 100) -> JSONResponse:
    return JSONResponse({"events": store.get_events(limit)})


@app.get("/api/sessions")
async def api_sessions(limit: int = 50) -> JSONResponse:
    return JSONResponse({"sessions": store.get_sessions(limit)})


@app.post("/api/control/test-notify")
async def api_test_notify() -> JSONResponse:
    """登録済みチャットへテスト通知を送り、Telegram疎通を確認する。"""
    settings = config.load_settings()
    chat_ids = settings["telegram"]["notify_chat_ids"]
    if not chat_ids:
        return JSONResponse({"ok": False, "reason": "no_chat", "chat_count": 0})
    if controller.bot is None:
        return JSONResponse({"ok": False, "reason": "no_bot", "chat_count": len(chat_ids)})
    sent = 0
    for cid in chat_ids:
        try:
            await controller.bot.send_message(
                chat_id=cid,
                text="✅ テスト通知：監視Botは正常に通知できています。",
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.error("テスト通知失敗 chat_id=%s: %s", cid, exc)
    return JSONResponse({"ok": sent > 0, "sent": sent, "chat_count": len(chat_ids)})


@app.post("/api/booking/open")
async def api_booking_open(request: Request) -> JSONResponse:
    """走査タブの空室セルから、その部屋×プラン×日付の予約フォームを開く(handoff)。

    ブラウザを起動して自動入力し「確認画面」まで進めて画面を残す。
    予約の確定は絶対にしない(最後の「予約する」は人間が押す)。
    reservation.enabled(空室検知時の自動起動)がOFFでも手動起動はできる。
    """
    settings = config.load_settings()
    mon, res = settings["monitor"], settings["reservation"]
    body = await _json(request)
    room_id = str(body.get("room_id") or "")
    plan_id = str(body.get("plan_id") or "")
    date = str(body.get("date") or "")
    target = STATE.room_status.get(f"{room_id}|{plan_id}")
    if not target or date not in (target.get("dates") or []):
        return JSONResponse({"ok": False, "error": "対象が見つかりません。画面を更新してください"})
    month, day = (int(x) for x in date.split("/"))
    auto_c = booking.auto_confirm_allowed(res)
    log.info("🧾 予約フォーム手動起動%s: %s [%s] %s",
             "(確定まで)" if auto_c else "", target["room_label"], target["plan_label"], date)
    try:
        rep = await booking.prepare_booking_async(
            mon, res, plan_id, room_id, day, year=mon["target_year"], month=month,
            nights=target.get("nights"), headless=False, keep_open=True,
            proceed_to_confirm=res.get("proceed_to_confirm", True),
            auto_confirm=auto_c)
    except Exception as exc:  # noqa: BLE001
        log.error("予約フォーム起動失敗: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)})
    if rep.get("status") == "booked":
        cnt = int(res.get("auto_confirmed_count", 0)) + 1
        config.merge_and_save({"reservation": {"auto_confirmed_count": cnt}})
        log.info("✅ 予約確定(手動起動) (%d/%d件)", cnt, int(res.get("auto_confirm_max", 1)))
        # Telegramにも知らせる(重要イベントなので画面の前にいなくても分かるように)
        if controller.bot:
            msg = (f"🎉✅ 予約を確定しました！\n\n◆{target['room_label']}\n"
                   f"　[{target['plan_label']}] {date}\n\n"
                   "ブラウザ窓と確認メールで内容を確認してください。")
            for cid in settings["telegram"]["notify_chat_ids"]:
                try:
                    await controller.bot.send_message(chat_id=cid, text=msg)
                except Exception as exc:  # noqa: BLE001
                    log.error("Telegram送信失敗 chat_id=%s: %s", cid, exc)
    return JSONResponse({"ok": True, "report": {k: rep.get(k) for k in
        ("status", "url", "filled", "empty_required", "skipped_missing", "errors",
         "handoff_open", "clicked_final", "confirm_hint")}})


@app.post("/api/control/test-booking")
async def api_test_booking(request: Request) -> JSONResponse:
    """予約アシスト(Playwright)を1回だけ実際に動かして挙動を確認する。

    先頭の部屋×プランの予約フォームを開いて自動入力し、画面を開いたまま残す。
    「確認画面へ」には進まず、予約の確定も絶対にしない。
    """
    settings = config.load_settings()
    mon, res = settings["monitor"], settings["reservation"]
    missing = config.missing_required(settings)
    if missing:
        return JSONResponse({"ok": False, "error": "必須項目が未設定です: " + "、".join(missing)})
    body = await _json(request)
    plans = mon["plans"]
    # 対象部屋: 指定 > 通知対象の先頭 > 発見済みの先頭(全部屋走査では部屋は自動発見のため)
    known = mon.get("known_rooms") or []
    room_id = str(body.get("room_id") or
                  next(iter(mon.get("notify_room_ids") or []), "") or
                  (known[0]["room_id"] if known else ""))
    if not room_id:
        return JSONResponse({"ok": False, "error":
            "対象の部屋がまだ分かりません。先に「🔄 今すぐチェック」を1回実行してください"})
    # プラン設定は共有ファイル/手入力由来なので形式不備でも500にせず説明を返す
    try:
        plan = next((p for p in plans if str(p.get("id")) == str(body.get("plan_id"))), plans[0])
        date = str(plan["dates"][0])
        month, day = (int(x) for x in date.split("/"))
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"プラン設定の形式が不正です({exc})。"
                             "設定タブの「プラン設定」を確認してください"})
    log.info("🧪 予約アシストのテスト起動: room=%s plan=%s date=%s", room_id, plan["id"], date)
    try:
        rep = await booking.prepare_booking_async(
            mon, res, str(plan["id"]), room_id, day, year=mon["target_year"],
            month=month, nights=plan.get("nights"),
            headless=False, keep_open=True, proceed_to_confirm=False)
    except Exception as exc:  # noqa: BLE001
        log.error("予約アシストのテスト失敗: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({"ok": True, "report": {k: rep.get(k) for k in
        ("status", "url", "filled", "empty_required", "skipped_missing", "errors", "handoff_open")}})


@app.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    return JSONResponse(config.masked(config.load_settings()))


@app.post("/api/settings")
async def api_save_settings(request: Request) -> JSONResponse:
    patch = await _json(request)
    # マスク値(…を含むトークン)が送られてきたら無視して上書きしない
    tel = patch.get("telegram", {})
    if isinstance(tel, dict) and "bot_token" in tel:
        tok = str(tel["bot_token"])
        if "…" in tok or tok == "設定済み" or not tok.strip():
            tel.pop("bot_token")
    token_changed = isinstance(tel, dict) and bool(tel.get("bot_token"))
    updated = config.merge_and_save(patch)
    if "monitor" in patch and "interval_seconds" in patch["monitor"]:
        STATE.interval = config.clamp_interval(updated["monitor"]["interval_seconds"], updated["monitor"])
    # トークンが入力されたら、その場でBotをつなぎ直す(再起動不要)
    bot_message = None
    if token_changed:
        await stop_bot(request.app)
        _, bot_message = await start_bot(request.app)
    return JSONResponse({"ok": True, "settings": config.masked(updated),
                         "missing": config.missing_required(updated),
                         "bot": {"running": controller.bot is not None, "message": bot_message}})


@app.post("/api/shutdown")
async def api_shutdown(request: Request) -> JSONResponse:
    """画面の「⏻ 終了」ボタンから、アプリ全体をグレースフル終了する。"""
    log.info("⏻ 画面から終了要求を受けました")
    server = getattr(request.app.state, "server", None)
    if server is None:
        return JSONResponse({"ok": False, "error": "server handle not found"})
    server.should_exit = True   # uvicornのループが検知 → lifespanのfinallyで監視/Botも停止
    return JSONResponse({"ok": True})


@app.post("/api/settings/import")
async def api_import_settings(request: Request) -> JSONResponse:
    """共有設定ファイル(YAML/JSON)の中身を受け取り、設定に取り込む。"""
    body = await _json(request)
    text = str(body.get("text", ""))
    try:
        data = config.parse_share_text(text)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    # exeの隣に同じ内容の共有ファイルが置かれている場合は取り込みマーカーも記録し、
    # 次回起動時の自動再取り込みで画面上の修正が巻き戻されるのを防ぐ
    for name in config.SHARE_FILE_NAMES:
        path = config.BASE_DIR / name
        try:
            if path.exists() and path.read_text(encoding="utf-8") == text:
                data["share_import"] = {"file": name, "mtime": int(path.stat().st_mtime)}
                break
        except OSError:
            pass
    updated = config.merge_and_save(data)
    STATE.interval = config.clamp_interval(updated["monitor"]["interval_seconds"], updated["monitor"])
    # ファイルにトークンが含まれていた場合は接続も試みる
    if updated["telegram"].get("bot_token") and controller.bot is None:
        await start_bot(request.app)
    return JSONResponse({"ok": True, "settings": config.masked(updated),
                         "missing": config.missing_required(updated),
                         "bot": {"running": controller.bot is not None, "message": None}})


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _is_our_dashboard(host: str, port: int) -> bool:
    """指定ポートで応答しているのが、この空室監視ダッシュボード自身か確認する。"""
    import requests
    try:
        r = requests.get(f"http://{host}:{port}/api/status", timeout=2)
        return r.ok and "status_text" in r.json()
    except Exception:  # noqa: BLE001
        return False


def run() -> None:
    """`uv run kemocon` のエントリポイント。"""
    import socket
    import time
    import webbrowser

    import uvicorn

    config.setup_console()
    host = os.environ.get("KEMOCON_HOST", "127.0.0.1")
    preferred = int(os.environ.get("KEMOCON_PORT", "8000"))

    # ポートが埋まっている場合: 自分がすでに起動済みなら画面を開くだけ、
    # 他のアプリが使っているなら次のポートに退避する
    port = None
    for cand in range(preferred, preferred + 10):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, cand))
            port = cand
            break
        except OSError:
            if _is_our_dashboard(host, cand):
                print(f"\n  すでに起動しています → http://{host}:{cand}")
                print("  既存の画面をブラウザで開きます(この窓は自動で閉じます)\n")
                webbrowser.open(f"http://{host}:{cand}")
                time.sleep(4)
                return
    if port is None:
        print(f"\n  空きポートが見つかりません({preferred}〜{preferred + 9})。"
              "他のアプリを終了してから再実行してください\n")
        time.sleep(8)
        return
    if port != preferred:
        print(f"  ※ポート {preferred} は使用中のため {port} を使います")

    print(f"\n  空室監視ダッシュボード v{__version__} → http://{host}:{port}")
    print("  終了する時は画面右上の「終了」ボタン、またはこの窓を閉じる")
    print("  走査の経過はブラウザ画面の「稼働ログ」欄と logs フォルダで見られます"
          "(この窓には警告・エラーだけ出ます)\n")
    if getattr(sys, "frozen", False):
        # exe版はダブルクリック起動が前提なので、ブラウザでダッシュボードを自動で開く
        import threading
        threading.Timer(2.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    # SSEの常時接続で終了が固まらないよう、グレースフル終了を数秒で打ち切る
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           timeout_graceful_shutdown=5))
    app.state.server = server   # /api/shutdown が should_exit を立てるためのハンドル
    server.run()
    print("\n  終了しました。この窓は閉じてOKです")


if __name__ == "__main__":
    run()
