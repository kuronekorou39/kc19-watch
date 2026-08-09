"""空室監視のコアロジックと監視コントローラ(部屋×プラン)。

- 複数の対象部屋(target_rooms, room_idで指定) × 複数プラン を監視する。
- 1プランのAPI応答に全部屋の在庫が含まれるので、部屋を増やしてもAPI回数は増えない。
- プランごとに見るチェックイン日(dates)と泊数(nights)を持つ。3連泊は初日だけ見る。
- 人数(2〜5)は別々に検索(実測では空室数は人数非依存。差が出たら警告)。

状態(targets, フラット): key="{room_id}|{plan_id}"
  { key: { room_label, room_id, plan_id, plan_label, dates, nights, book_url,
           matrix:{人数(str):{日付:bool}}, counts:{日付:int} } }
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any

import requests

from . import booking, config, store
from .state import STATE, DashboardLogHandler

log = logging.getLogger("kemocon")
http_log = logging.getLogger("kemocon.http")

_MAX_RESPONSE_LOG_SIZE = 4000


def setup_logging() -> None:
    """ファイル(日付別) + ダッシュボードの両方にログを出す。"""
    log_dir = os.path.join(config.BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        fh = logging.FileHandler(os.path.join(log_dir, f"kemocon_bot_{stamp}.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(logging.StreamHandler())
        log.addHandler(DashboardLogHandler("bot"))

    http_log.setLevel(logging.INFO)
    http_log.propagate = False
    if not http_log.handlers:
        hh = logging.FileHandler(os.path.join(log_dir, f"http_requests_{stamp}.log"), encoding="utf-8")
        hh.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        http_log.addHandler(hh)
        http_log.addHandler(DashboardLogHandler("http"))


def _date_key(d: str):
    mo, da = d.split("/")
    return (int(mo), int(da))


def _effective_interval(mon: dict) -> tuple[int, bool]:
    """解禁ウィンドウ中ならバースト間隔を返す。(interval, burst_active)"""
    b = mon.get("burst", {})
    if b.get("enabled") and b.get("start") and b.get("end"):
        try:
            now = datetime.now()
            s = datetime.strptime(b["start"], "%Y-%m-%d %H:%M")
            e = datetime.strptime(b["end"], "%Y-%m-%d %H:%M")
            if s <= now <= e:
                return max(5, int(b.get("interval_seconds", 20))), True
        except Exception:  # noqa: BLE001
            pass
    return STATE.interval, False


def _plans(mon: dict) -> list[dict]:
    if mon.get("plans"):
        return mon["plans"]
    dates = mon.get("target_dates", ["11/6", "11/7", "11/8"])
    return [{"id": str(p), "label": str(p), "dates": dates} for p in mon.get("plan_ids", ["517"])]


def _target_rooms(mon: dict) -> list[dict]:
    if mon.get("target_rooms"):
        return mon["target_rooms"]
    rid = mon.get("booking_room_id")
    return [{"label": "対象部屋", "room_id": rid}] if rid else []


def _booking_url(mon: dict, plan_id: str, room_id: str) -> str:
    date_url = mon.get("site", {}).get("date_url", "")
    if not date_url:
        return mon.get("menu_url", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(mon.get("menu_url", "")).query)
    uid = q.get("uid", [""])[0]
    lmp = q.get("plan", [""])[0]
    return (f"{date_url}?id={mon['yado_id']}"
            f"&room={room_id}&plan={plan_id}&year={mon['target_year']}&month=11"
            f"&lan=JPN&ty={mon['api_ty']}&uid={uid}&m_menu=1&lmp={lmp}&dt=4")


def _fetch_stock(mon: dict, plan_id: str, guest_num: int) -> str:
    year = mon["target_year"]
    start, end = f"{year}/11/1", f"{year}/11/30"
    input_data = (
        f"id=stock_calendar_1,start_date={start},end_date={end},select_room=,"
        f"select_plan={plan_id},user_num={guest_num},init_flag=0,disp_cal_room=1,"
        f"disp_cal_plan=,disp_cal_plan_btn=1,init_plan_num=0,kid="
    )
    params = {
        "id": mon["yado_id"], "planId": plan_id, "startDate": start, "endDate": end,
        "input_data": input_data, "iflg": "1", "ty": mon["api_ty"], "lan": "JPN",
    }
    api_url = mon.get("site", {}).get("api_url", "")
    if not api_url:
        raise RuntimeError("site.api_url が未設定です。settings.local.json を設定してください")
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Referer": mon.get("menu_url", "").split("?")[0],
    }
    http_log.info("REQ plan=%s user_num=%s", plan_id, guest_num)
    r = requests.get(api_url, params=params, headers=headers, timeout=15)
    r.encoding = "utf-8"
    http_log.info("RES plan=%s user_num=%s -> %s (%d chars)\n%s",
                  plan_id, guest_num, r.status_code, len(r.text), r.text[:_MAX_RESPONSE_LOG_SIZE])
    # 異常応答を「全満室」と誤解釈しないため、このサイクルごと中断する(呼び出し元でスキップ)
    if r.status_code != 200 or "rooms" not in r.text:
        raise RuntimeError(f"在庫APIの応答が異常です (HTTP {r.status_code}, {len(r.text)} chars)")
    return r.text


def _parse_target_rooms(text: str, year: str, dates: list[str], want_ids: set[str]) -> dict[str, dict[str, int]]:
    """対象room_idの {room_id: {date: aki_num}} を返す(-1=データなし)。"""
    out: dict[str, dict[str, int]] = {}
    if "rooms" not in text:
        return out
    parts = re.split(r'"room_id":\s*[\'"]?(\d+)', text)  # [pre, id, block, id, block, ...]
    for i in range(1, len(parts), 2):
        rid = parts[i]
        if rid not in want_ids:
            continue
        block = parts[i + 1] if i + 1 < len(parts) else ""
        aki_match = re.search(r'"aki"\s*:\s*\[(.*?)\]', block, re.DOTALL)
        ds = {d: -1 for d in dates}
        if aki_match:
            sec = aki_match.group(1)
            for d in dates:
                m = re.search(rf'"aki_date"\s*:\s*[\'"]{year}/{d}[\'"][^{{]*?"aki_num"\s*:\s*[\'"](\d+)[\'"]', sec)
                if m:
                    ds[d] = int(m.group(1))
        out[rid] = ds
    return out


def check_availability(mon: dict[str, Any]) -> dict[str, dict]:
    """部屋×プランを検索して targets(フラット) を返す。"""
    year = mon["target_year"]
    guest_nums = [int(g) for g in (mon.get("guest_nums") or [2, 3, 4, 5])]
    plans = _plans(mon)
    rooms = _target_rooms(mon)
    want_ids = {str(r["room_id"]) for r in rooms}

    targets: dict[str, dict] = {}
    for r in rooms:
        for pl in plans:
            key = f'{r["room_id"]}|{pl["id"]}'
            targets[key] = {
                "room_label": r["label"], "room_id": str(r["room_id"]),
                "plan_id": str(pl["id"]), "plan_label": pl.get("label", str(pl["id"])),
                "dates": pl["dates"], "nights": pl.get("nights"),
                "book_url": _booking_url(mon, pl["id"], r["room_id"]),
                "matrix": {}, "counts": {d: 0 for d in pl["dates"]},
            }

    log.info("🔎 検索: 部屋%d × プラン%s × 人数%s",
             len(rooms), [p["id"] for p in plans], guest_nums)
    for pl in plans:
        for gn in guest_nums:
            text = _fetch_stock(mon, pl["id"], gn)
            per_room = _parse_target_rooms(text, year, pl["dates"], want_ids)
            for r in rooms:
                key = f'{r["room_id"]}|{pl["id"]}'
                akis = per_room.get(str(r["room_id"]), {})
                e = targets[key]
                gg = e["matrix"].setdefault(str(gn), {d: False for d in pl["dates"]})
                for d in pl["dates"]:
                    n = akis.get(d, -1)
                    if n > 0:
                        gg[d] = True
                    if n > e["counts"][d]:
                        e["counts"][d] = n
    log.info("📊 対象 %d 件を取得", len(targets))
    return targets


def detect_changes(old: dict, new: dict) -> list[dict]:
    """前回/今回の targets を比較し、(部屋×プラン×日付)ごとの変化イベントを返す。"""
    events = []
    for key, e in new.items():
        old_e = old.get(key)
        if not old_e:
            continue
        gmap, old_g = e["matrix"], old_e.get("matrix", {})
        for d in e["dates"]:
            new_gns = [g for g in gmap if gmap[g].get(d)]
            old_gns = [g for g in old_g if old_g.get(g, {}).get(d)]
            base = {"room_label": e["room_label"], "room_id": e["room_id"],
                    "plan_id": e["plan_id"], "plan_label": e["plan_label"],
                    "date": d, "book_url": e.get("book_url"), "nights": e.get("nights")}
            if new_gns and not old_gns:
                events.append({**base, "type": "available", "guest_nums": new_gns,
                               "aki_num": e["counts"].get(d, 0)})
            elif not new_gns and old_gns:
                events.append({**base, "type": "full", "guest_nums": old_gns, "aki_num": 0})
    return events


def build_messages(events: list[dict]) -> list[str]:
    msgs = []
    avail = [e for e in events if e["type"] == "available"]
    full = [e for e in events if e["type"] == "full"]
    if avail:
        text = "🎉 空室が出ました！\n\n"
        for e in avail:
            gns = "・".join(f"{g}名" for g in e["guest_nums"])
            text += f"◆{e['room_label']}\n　[{e['plan_label']}] {e['date']} 空室（{e['aki_num']}室 / {gns}）\n"
            if e.get("book_url"):
                text += f"　→ 予約: {e['book_url']}\n"
        if any("3連泊" in e["plan_label"] for e in avail):
            text += "\n🔥 3連泊のチェックイン日が空きました！\n"
        msgs.append(text)
    if full:
        text = "😢 満室になりました\n\n"
        for e in full:
            text += f"◆{e['room_label']} [{e['plan_label']}] {e['date']} 満室\n"
        msgs.append(text)
    return msgs


def _autobook_message(e: dict, rep: dict) -> str:
    """自動起動した予約フォームの結果メッセージ。"""
    status = rep.get("status")
    nights = e.get("nights")
    text = ("🧾 予約フォームを自動で開きました（要確認）\n\n"
            f"◆{e['room_label']}\n　[{e['plan_label']}] {e['date']}"
            f"（{str(nights) + '泊' if nights else ''}）\n")
    if status in ("ready", "confirm_ready", "confirm_blocked"):
        text += f"\n✅ 自動入力: {len(rep.get('filled', []))}項目"
        empty = rep.get("empty_required", [])
        if empty:
            text += f"\n⚠️ 手動で入力: {'・'.join(empty)}"
        missing = rep.get("skipped_missing", [])
        if missing:
            text += f"\n（フォームに無かった項目: {'・'.join(missing)}）"
        if status == "confirm_ready":
            text += "\n\n▶ 確認画面まで自動で進みました。ブラウザで「予約する」を押すだけです（Botは押しません）。"
        elif status == "confirm_blocked":
            text += "\n\n⚠️ 確認画面で止まりました。ブラウザ画面で内容を確認して完了してください。"
        else:
            text += "\n\nブラウザ画面で内容を確認し「確認画面へ」→「予約する」で完了してください（Botは確定しません）。"
    elif status == "full":
        text += "\n（開いた瞬間に満室になっていました）"
    elif status == "no_form":
        text += "\n（予約フォームが表示されませんでした。手動で確認してください）"
    else:
        text += f"\n（エラー: {rep.get('errors')}）"
    if e.get("book_url"):
        text += f"\n→ 予約ページ: {e['book_url']}"
    return text


class MonitorController:
    def __init__(self) -> None:
        self.bot = None
        self._task: asyncio.Task | None = None
        self.session_id: int | None = None
        self._last_counts: dict | None = None
        self._autobooked: set = set()    # 自動起動済みの (room|plan|date) 重複防止

    async def start(self, interval: int | None = None) -> bool:
        if STATE.monitoring:
            return False
        if interval:
            STATE.interval = config.clamp_interval(interval, config.load_settings()["monitor"])
        STATE.monitoring = True
        STATE.monitor_started_at = time.time()
        STATE.last_error = None
        self.session_id = store.start_session(store.now_iso())
        self._last_counts = None
        self._task = asyncio.create_task(self._run_loop())
        log.info("▶️ 監視を開始 (間隔 %d 秒)", STATE.interval)
        return True

    async def stop(self) -> bool:
        if not STATE.monitoring:
            return False
        STATE.monitoring = False
        STATE.next_check_ts = None
        store.stop_session(self.session_id, store.now_iso())
        self.session_id = None
        if self._task:
            self._task.cancel()
            self._task = None
        log.info("⏹️ 監視を停止")
        return True

    async def check_once(self) -> dict:
        await self._check_iteration()
        return STATE.room_status

    async def _run_loop(self) -> None:
        try:
            while STATE.monitoring:
                await self._check_iteration()
                mon = config.load_settings()["monitor"]
                interval, burst = _effective_interval(mon)
                STATE.effective_interval, STATE.burst_active = interval, burst
                STATE.next_check_ts = time.time() + interval
                slept = 0
                while STATE.monitoring and slept < interval:
                    await asyncio.sleep(1)
                    slept += 1
        except asyncio.CancelledError:
            pass

    async def _check_iteration(self) -> None:
        settings = config.load_settings()
        mon = settings["monitor"]
        try:
            loop = asyncio.get_running_loop()
            targets = await loop.run_in_executor(None, check_availability, mon)
            if not targets:
                STATE.last_error = "対象が取得できませんでした(応答異常?)"
                log.error(STATE.last_error)
                return
            STATE.check_count += 1
            ts = store.now_iso()
            STATE.last_checked_ts = time.time()
            STATE.last_checked_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            STATE.last_error = None

            # 永続化(部屋×プラン×日付, 変化時のみ)
            counts_snap = {k: e["counts"] for k, e in targets.items()}
            if counts_snap != self._last_counts:
                rows = [(e["room_label"], e["plan_id"], d, n)
                        for e in targets.values() for d, n in e["counts"].items()]
                store.record_sample(ts, rows)
                self._last_counts = {k: dict(c) for k, c in counts_snap.items()}

            if not STATE.room_status:
                STATE.room_status = targets
                log.info("初回チェック完了 - %d 件記録(通知なし)", len(targets))
                return

            events = detect_changes(STATE.room_status, targets)
            STATE.room_status = targets
            for e in events:
                STATE.add_history(e)
            if events:
                store.record_events(ts, events)
                await self._notify(events, settings)
                await self._maybe_autobook(events, settings)
        except Exception as exc:  # noqa: BLE001
            STATE.last_error = str(exc)
            log.error("チェック中エラー: %s", exc)

    async def _maybe_autobook(self, events: list[dict], settings: dict) -> None:
        """空室検知時に予約フォームを自動で開いて入力し、人に渡す(handoff)。"""
        res = settings["reservation"]
        if not res.get("enabled"):
            return
        mon = settings["monitor"]
        room_filter = {str(x) for x in (res.get("auto_book_room_ids") or [])}
        plan_filter = {str(x) for x in (res.get("auto_book_plan_ids") or [])}
        max_windows = int(res.get("max_windows", 4))
        loop = asyncio.get_running_loop()
        opened = 0
        for e in [ev for ev in events if ev["type"] == "available"]:
            if opened >= max_windows:
                break
            if room_filter and e["room_id"] not in room_filter:
                continue
            if plan_filter and e["plan_id"] not in plan_filter:
                continue
            tkey = f'{e["room_id"]}|{e["plan_id"]}|{e["date"]}'
            if tkey in self._autobooked:
                continue
            self._autobooked.add(tkey)
            month, day = (int(x) for x in e["date"].split("/"))
            log.info("🧾 予約フォーム自動起動: %s [%s] %s", e["room_label"], e["plan_label"], e["date"])
            try:
                rep = await loop.run_in_executor(None, lambda ev=e: booking.prepare_booking(
                    mon, res, ev["plan_id"], ev["room_id"], day, year=mon["target_year"],
                    month=month, nights=ev.get("nights"),
                    headless=res.get("headless", False), keep_open=True,
                    proceed_to_confirm=res.get("proceed_to_confirm", True)))
            except Exception as exc:  # noqa: BLE001
                log.error("予約フォーム起動失敗: %s", exc)
                continue
            opened += 1
            await self._send(settings, _autobook_message(e, rep))

    async def _send(self, settings: dict, text: str) -> None:
        chat_ids = settings["telegram"]["notify_chat_ids"]
        if not self.bot or not chat_ids:
            return
        for chat_id in chat_ids:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text)
            except Exception as exc:  # noqa: BLE001
                log.error("Telegram送信失敗 chat_id=%s: %s", chat_id, exc)

    async def _notify(self, events: list[dict], settings: dict) -> None:
        messages = build_messages(events)
        chat_ids = settings["telegram"]["notify_chat_ids"]
        for msg in messages:
            log.info("🔔 通知: %s", msg.splitlines()[0])
            if not self.bot or not chat_ids:
                continue
            for chat_id in chat_ids:
                try:
                    await self.bot.send_message(chat_id=chat_id, text=msg)
                except Exception as exc:  # noqa: BLE001
                    log.error("Telegram送信失敗 chat_id=%s: %s", chat_id, exc)


controller = MonitorController()
