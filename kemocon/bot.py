"""Telegram Bot。ハンドラは共有コントローラを叩くので、GUI操作と状態が一致する。"""
from __future__ import annotations

import logging
import warnings

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.warnings import PTBUserWarning

# frozen(exe)環境でビルダー経由でも出る「ApplicationBuilderを使え」警告を抑止
# (実際にビルダー経由で構築しており、利用者のコンソールに出るだけのノイズ)
warnings.filterwarnings("ignore", category=PTBUserWarning)

from . import config, store
from .monitor import controller, notify_room_ids, setup_logging
from .state import STATE

log = logging.getLogger("kemocon")


async def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """個人チャットは常に許可、グループは管理者のみ許可。"""
    chat = update.effective_chat
    if chat is None:
        return False
    if chat.type == "private":
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        return member.status in ("administrator", "creator")
    except Exception:  # noqa: BLE001
        return False


def _remember_chat(chat_id: int) -> None:
    # merge_and_save 経由で保存する(load→saveの丸ごと保存だと、環境変数
    # TELEGRAM_BOT_TOKEN で注入されたトークンまでファイルに書かれてしまう)
    ids = config.load_settings(apply_env=False)["telegram"]["notify_chat_ids"]
    if chat_id not in ids:
        config.merge_and_save({"telegram": {"notify_chat_ids": ids + [chat_id]}})


def build_status_text(settings: dict | None = None) -> str:
    """/now 用の状況テキスト(部屋→プラン別)。

    全部屋走査でも読める長さに保つ: 通知対象(🔔)の部屋を先頭に並べ、
    Telegramの上限(4096字)に収まらない分は部屋数だけにまとめる。
    settings は読み込み済みがあれば渡す(毎秒のSSEで二重読みしないため)。
    """
    text = "空室状況\n\n"
    if STATE.room_status:
        want = notify_room_ids((settings or config.load_settings())["monitor"])
        by_room: dict = {}
        for e in STATE.room_status.values():
            by_room.setdefault((e["room_id"], e["room_label"]), []).append(e)
        keys = sorted(by_room, key=lambda k: 0 if (not want or k[0] in want) else 1)
        blocks = []
        for key in keys:
            rid, room_label = key
            mark = "🔔 " if want and rid in want else ""
            block = f"■ {mark}{room_label}\n"
            for e in by_room[key]:
                counts = e.get("counts", {})
                cells = " ".join(
                    f"{d}:{('空' + str(counts.get(d, 0)) + '室') if counts.get(d, 0) > 0 else '×'}"
                    for d in e.get("dates", [])
                )
                block += f"  [{e['plan_label']}] {cells}\n"
            blocks.append(block)
        omitted = 0
        for i, block in enumerate(blocks):
            # 上限に達したら以降は全部省略する(途中を飛ばして後ろの小さい部屋だけ
            # 載る、という優先度の逆転をしない。blocksは通知対象が先頭)
            if len(text) + len(block) > 3500:
                omitted = len(blocks) - i
                break
            text += block
        if omitted:
            text += f"…ほか{omitted}部屋(ダッシュボードで確認できます)\n"
    else:
        text += "まだ確認していません\n"
    text += f"\n監視: {'稼働中' if STATE.monitoring else '停止中'}"
    text += f"\n最終確認: {STATE.last_checked_at or '未実行'}"
    return text


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "コマンド一覧\r\n"
        "/start - 空室監視を開始\r\n"
        "/end - 空室監視を停止\r\n"
        "/now - 現在の状況を確認\r\n"
        "/info - 監視情報を表示\r\n"
        "/help - ヘルプ表示\r\n"
    )
    await update.message.reply_text(text)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mon = config.load_settings()["monitor"]
    scan_ids = [str(x) for x in mon.get("scan_room_ids", [])]
    known = {str(r.get("room_id")): r.get("label", "") for r in mon.get("known_rooms", [])}
    want = notify_room_ids(mon)
    notify = "、".join(known.get(rid) or f"部屋{rid}" for rid in sorted(want)) if want else "全部屋"
    dates = sorted({d for p in mon.get("plans", []) for d in p.get("dates", [])})
    text = (
        f"空室監視\r\n"
        f"走査部屋：{'限定 ' + '、'.join(scan_ids) if scan_ids else '全部屋(自動)'}\r\n"
        f"通知部屋：{notify}\r\n"
        f"監視対象日：{'、'.join(dates) or '(未設定)'}\r\n"
        f"監視間隔：{mon['interval_seconds']}秒\r\n"
    )
    await update.message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        await update.message.reply_text("このコマンドは管理者のみが使用できます。")
        return
    _remember_chat(update.effective_chat.id)
    missing = config.missing_required(config.load_settings())
    if missing:
        await update.message.reply_text(
            "通知先に登録しました！\n"
            "ただし設定が未完了のため、まだ監視は始められません。\n"
            f"ダッシュボードの設定タブで入力してください: {'、'.join(missing)}")
        return
    started = await controller.start()
    if started:
        await update.message.reply_text(f"監視を開始しました！(間隔 {STATE.interval} 秒)")
    else:
        await update.message.reply_text("すでに動いてます！")


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_allowed(update, context):
        await update.message.reply_text("このコマンドは管理者のみが使用できます。")
        return
    stopped = await controller.stop()
    if stopped:
        await update.message.reply_text("監視を停止しました。")
    else:
        await update.message.reply_text("まだ動いてません。 /start してね")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(build_status_text())


def build_application(settings: dict) -> Application:
    """PTB Application を組み立てる。"""
    STATE.interval = config.clamp_interval(settings["monitor"]["interval_seconds"], settings["monitor"])

    async def _post_init(app: Application) -> None:
        # run_polling(単体モード)時のみPTBが呼ぶ。webモードの auto_start は web.lifespan 側で行う
        controller.bot = app.bot
        if settings["monitor"].get("auto_start"):
            await controller.start()

    app = (
        Application.builder()
        .token(settings["telegram"]["bot_token"])
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler("now", cmd_now))
    return app


def run_bot_only() -> None:
    """ダッシュボードなしでBotだけ起動(去年と同じ挙動)。"""
    config.setup_console()
    setup_logging()
    store.init_db()
    store.close_dangling_sessions(store.now_iso())  # 前回の開きっぱなしセッションを閉じる
    settings = config.load_settings()
    app = build_application(settings)
    log.info("Bot(単体)を起動します")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot_only()
