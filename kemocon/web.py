"""FastAPI Webダッシュボード。Botも同じプロセス・同じ非同期ループで起動する。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from telegram import Update

from . import config, store
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    store.init_db()
    store.close_dangling_sessions(store.now_iso())  # 前回の開きっぱなしセッションを閉じる
    _restore_history()
    _PROC.cpu_percent()  # cpu_percent の初回呼び出し(次回から実値)
    settings = config.load_settings()
    STATE.interval = config.clamp_interval(settings["monitor"]["interval_seconds"], settings["monitor"])
    # トークン未設定/不正でもダッシュボードは起動する(設定画面から入力→再起動で有効化)
    application = None
    if settings["telegram"].get("bot_token"):
        try:
            application = build_application(settings)
            controller.bot = application.bot
            await application.initialize()
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram Bot の起動に失敗しました(トークン不正?): %s", exc)
            log.error("ダッシュボードのみで続行します。設定画面でトークンを確認し、アプリを再起動してください")
            application = None
            controller.bot = None
    else:
        log.warning("Telegram トークン未設定のためダッシュボードのみで起動します(通知は送られません)。"
                    "設定画面でトークンを保存し、アプリを再起動してください")
    app.state.ptb = application
    app.state.shutting_down = False
    log.info("🚀 ダッシュボード + Bot 起動完了")
    # PTBの post_init は run_polling 経由でしか呼ばれないため、webモードの auto_start はここで行う
    if settings["monitor"].get("auto_start"):
        await controller.start()
    try:
        yield
    finally:
        app.state.shutting_down = True   # SSEループを速やかに止める
        await controller.stop()
        if application:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception as exc:  # noqa: BLE001
                log.error("シャットダウン中エラー: %s", exc)


app = FastAPI(title="Vacancy Watch Dashboard", lifespan=lifespan)


def _system_metrics() -> dict:
    return {
        "memory_mb": round(_PROC.memory_info().rss / (1024 * 1024), 1),
        "cpu_percent": _PROC.cpu_percent(),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # ブラウザに古いページをキャッシュさせない(コード更新後の表示ズレ防止)
    return HTMLResponse(
        (_STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


def _build_status() -> dict:
    """/api/status と /api/stream で共通の状態スナップショットを作る。"""
    settings = config.load_settings()
    snap = STATE.snapshot()
    snap["system"] = _system_metrics()
    snap["status_text"] = build_status_text()
    snap["notify_chat_count"] = len(settings["telegram"]["notify_chat_ids"])
    m = settings["monitor"]
    snap["monitor_cfg"] = {
        "yado_id": m["yado_id"],
        "plans": m.get("plans", []),
        "target_rooms": m.get("target_rooms", []),
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
async def api_history(limit: int = 8000) -> JSONResponse:
    """空室数の推移(グラフ用)。部屋ごとに、その部屋の最大空室数を時系列で返す。"""
    samples = store.get_samples(limit)
    rooms: list[str] = []
    points: dict[str, dict] = {}
    for s in samples:
        room = s["room"] or "(不明)"
        if room not in rooms:
            rooms.append(room)
        pt = points.setdefault(s["ts"], {"ts": s["ts"]})
        pt[room] = max(pt.get(room, 0), s["aki_num"])
    plist = list(points.values())
    # 「現在」の点(部屋ごとの最大空室数)を足す
    now_vals = []
    if STATE.room_status:
        now_pt = {"ts": store.now_iso()}
        for e in STATE.room_status.values():
            room = e["room_label"]
            mx = max([0, *e["counts"].values()])
            now_pt[room] = max(now_pt.get(room, 0), mx)
            now_vals.append(mx)
            if room not in rooms:
                rooms.append(room)
        plist.append(now_pt)
    ymax = max([s["aki_num"] for s in samples] + now_vals, default=0)
    series = [{"key": r, "label": r} for r in rooms]
    return JSONResponse({"series": series, "points": plist, "ymax": ymax, "stats": store.stats()})


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
    updated = config.merge_and_save(patch)
    if "monitor" in patch and "interval_seconds" in patch["monitor"]:
        STATE.interval = config.clamp_interval(updated["monitor"]["interval_seconds"], updated["monitor"])
    return JSONResponse({"ok": True, "settings": config.masked(updated)})


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def run() -> None:
    """`uv run kemocon` のエントリポイント。"""
    import uvicorn

    host = os.environ.get("KEMOCON_HOST", "127.0.0.1")
    port = int(os.environ.get("KEMOCON_PORT", "8000"))
    print(f"\n  空室監視ダッシュボード → http://{host}:{port}\n  (終了する時は Ctrl+C)\n")
    if getattr(sys, "frozen", False):
        # exe版はダブルクリック起動が前提なので、ブラウザでダッシュボードを自動で開く
        import threading
        import webbrowser
        threading.Timer(2.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    # SSEの常時接続で終了が固まらないよう、グレースフル終了を数秒で打ち切る
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    run()
