"""ブラウザ自動予約(Playwright)。予約フォームを「行けるところまで」自動入力して人に渡す。

設計思想(堅牢・非破壊):
- 各項目は独立して入力し、1つ失敗しても全体は止めない(例外は握って記録)。
- **「確認画面へ / 予約完了」はBotが押さない**。＝先へ進む処理でコケることが無い。
- 埋めた/埋められなかった項目を報告し、画面は開いたまま残す(handoff)→ ユーザーが確認して確定。
- 満室・フォーム構造の相違も、落ちずに status で返す。

呼び出しは同期。asyncからは run_in_executor 経由で使う。
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from typing import Any

log = logging.getLogger("kemocon")


async def prepare_booking_async(*args, **kwargs) -> dict[str, Any]:
    """prepare_booking を毎回使い捨ての専用スレッドで実行する(async用)。

    Playwright同期APIは実行スレッドに自前のイベントループを作る。handoffで
    セッションを開いたまま残すとループがスレッドに残留し、共有スレッドプール
    (run_in_executor)がそのスレッドを再利用した2回目以降に
    「Sync API inside the asyncio loop」で失敗する。プールを使わず
    毎回新しいスレッドで実行することで回避する。

    handoffで窓を残した場合は、結果を返した後も同じスレッドが窓が閉じられる
    まで見届けて後片付けする(Playwrightオブジェクトは生成スレッドに紐づくため、
    他スレッドからは閉じられない)。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def deliver(kind: str, value: Any) -> None:
        """スレッドから結果を返す。呼び出し元がキャンセル済み/ループ終了後も落ちない。"""
        def apply() -> None:
            if fut.cancelled():
                if kind == "exc":
                    log.error("予約フォーム処理の結果は破棄されました(呼び出し元が停止): %s", value)
                return
            (fut.set_exception if kind == "exc" else fut.set_result)(value)
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:  # イベントループ終了後(シャットダウン中)
            if kind == "exc":
                log.error("予約フォーム処理が失敗しました(アプリ停止中): %s", value)

    def worker() -> None:
        try:
            rep = prepare_booking(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            deliver("exc", exc)
        else:
            session = rep.pop("_session", None)
            deliver("result", rep)
            if session:
                _watch_session(session)

    threading.Thread(target=worker, daemon=True, name="booking-handoff").start()
    return await fut


def _watch_session(s: dict) -> None:
    """handoffで残したブラウザ窓が閉じられるまで待ち、閉じられたら後片付けする。

    これが無いと、開いた窓ごとに Playwright ドライバ(nodeプロセス)が
    アプリ終了まで残り続ける。daemonスレッド上で動くのでアプリ終了は妨げない。
    """
    try:
        while not s["page"].is_closed():
            s["page"].wait_for_timeout(1000)
    except Exception:  # noqa: BLE001
        pass  # 窓が閉じられた/接続が切れた
    for closer in (s["browser"].close, s["p"].stop):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


def _launch_browser(p, headless: bool):
    """PCにあるChrome → Edge → Playwright同梱Chromium の順で起動を試す。

    exe版はChromiumを同梱しない(サイズと `playwright install` 不要のため)ので、
    利用者のPCに入っているブラウザを使う。Windows 10以降はEdgeが必ずある。
    """
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            if channel:
                return p.chromium.launch(headless=headless, channel=channel)
            return p.chromium.launch(headless=headless)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{channel or 'chromium'}: {(str(exc).splitlines() or [repr(exc)])[0]}")
    raise RuntimeError("ブラウザを起動できませんでした → " + " / ".join(errors))


def _assert_trusted_host(form_url: str, mon: dict) -> None:
    """予約フォームURLが在庫API/カレンダーと同じホスト・httpsか検証する。

    フォーム自動入力は代表者氏名・電話・住所・メール等の個人情報を入力するため、
    共有設定ファイル(第三者から受け取る場合がある)で form_url を攻撃者のサイトに
    差し替えられると、個人情報が別サイトに打ち込まれてしまう。監視対象サイト
    (api_url/date_url)と同じホストの https 以外は拒否する。
    """
    fp = urllib.parse.urlparse(form_url)
    if fp.scheme != "https" or not fp.netloc:
        raise ValueError(f"予約フォームURLが不正です(httpsのみ許可): {form_url}")
    site = mon.get("site", {})
    trusted = {urllib.parse.urlparse(site.get(k, "")).netloc
               for k in ("api_url", "date_url")}
    trusted.add(urllib.parse.urlparse(mon.get("menu_url", "")).netloc)
    trusted.discard("")
    if trusted and fp.netloc not in trusted:
        raise ValueError(
            f"予約フォームURLのホスト({fp.netloc})が監視対象サイトと一致しません。"
            "共有設定ファイルが改ざんされている可能性があります")


def build_form_url(mon: dict, plan_id: str, room_id: str, year: str, month: int, day: int) -> str:
    form_url = mon.get("site", {}).get("form_url", "")
    _assert_trusted_host(form_url, mon)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(mon.get("menu_url", "")).query)
    uid = q.get("uid", [""])[0]
    return (f"{form_url}?id={mon['yado_id']}&plan={plan_id}&room={room_id}"
            f"&year={year}&month={month}&day={day}&kid=&ty={mon['api_ty']}"
            f"&list=&lan=JPN&uid={uid}&mo=0&meo=0&prv=")


def _field_plan(res: dict, nights: int | None) -> list[dict]:
    """入力する項目の計画。required=Trueは本来必須(未入力なら手動入力を促す)。"""
    g = res.get("guest", {})
    plan = [
        {"label": "大人男性", "kind": "text", "sel": "input[name='man_1']", "val": res.get("adults_male"), "required": True},
        {"label": "大人女性", "kind": "text", "sel": "input[name='woman_1']", "val": res.get("adults_female"), "required": False},
        {"label": "部屋数", "kind": "select_value", "sel": "select[name='room_num']", "val": "1", "required": True},
        {"label": "泊数", "kind": "select_value", "sel": "select[name='stay_num']", "val": (str(nights) if nights else None), "required": True},
        {"label": "チェックイン時刻", "kind": "checkin", "sel": "select[name='ck_hourmin']", "val": res.get("checkin_time", "15:00"), "required": True},
        {"label": "代表者氏名", "kind": "text", "sel": "input[name='guest_name']", "val": g.get("name"), "required": True},
        {"label": "読みがな", "kind": "text", "sel": "input[name='guest_kana']", "val": g.get("kana"), "required": True},
        {"label": "電話", "kind": "text", "sel": "input[name='guest_tel']", "val": g.get("tel"), "required": True},
        {"label": "郵便番号(前)", "kind": "text", "sel": "input[name='guest_post1']", "val": g.get("post1"), "required": True},
        {"label": "郵便番号(後)", "kind": "text", "sel": "input[name='guest_post2']", "val": g.get("post2"), "required": True},
        {"label": "都道府県", "kind": "pref", "sel": "select[name='guest_pref_id']", "val": g.get("pref"), "required": True},
        {"label": "住所", "kind": "text", "sel": "input[name='guest_address']", "val": g.get("address"), "required": True},
        {"label": "メール", "kind": "text", "sel": "input[name='guest_mail1']", "val": g.get("email"), "required": True},
        {"label": "メール(確認)", "kind": "text", "sel": "input[name='guest_mail2']", "val": g.get("email"), "required": True},
        {"label": "支払い方法", "kind": "click_text", "sel": res.get("payment_method", "現地決済"), "val": True, "required": False},
        {"label": "メルマガ", "kind": "click_text", "sel": "希望しません", "val": True, "required": False},
        {"label": "会員登録オフ", "kind": "uncheck", "sel": "input[name='form_kaiin_true']", "val": True, "required": False},
    ]
    return plan


def _apply(page, item: dict, report: dict) -> None:
    """1項目を入力。見つからない/失敗しても例外は投げず記録するだけ。"""
    label, kind, sel, val = item["label"], item["kind"], item["sel"], item["val"]
    try:
        # 値の指定が無いものはスキップ(必須なら empty_required に)
        if val in (None, ""):
            if item.get("required"):
                report["empty_required"].append(label)
            return
        if kind in ("click_text",):
            loc = page.get_by_text(sel, exact=False)
            if loc.count() == 0:
                report["skipped_missing"].append(label)
                return
            loc.first.click()
        elif kind == "uncheck":
            if page.query_selector(sel) is None:
                report["skipped_missing"].append(label)
                return
            page.uncheck(sel)
        elif page.query_selector(sel) is None:
            report["skipped_missing"].append(label)     # フォームに項目が無い(構造相違)
            return
        elif kind == "text":
            page.fill(sel, str(val))
        elif kind == "select_value":
            page.select_option(sel, str(val))
        elif kind == "pref":
            page.select_option(sel, label=str(val))
            page.eval_on_selector(sel, "s=>s.dispatchEvent(new Event('change',{bubbles:true}))")
        elif kind == "checkin":
            hour = str(val).split(":")[0]
            page.eval_on_selector(sel,
                "(s,h)=>{for(const o of s.options){if(o.text.includes(h)){s.value=o.value;"
                "s.dispatchEvent(new Event('change',{bubbles:true}));return;}}"
                "if(s.options.length>1)s.selectedIndex=1;}", arg=hour)
        report["filled"].append(label)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"{label}: {exc}")


def prepare_booking(mon: dict, res: dict, plan_id: str, room_id: str, day: int,
                    year: str | None = None, month: int = 11, nights: int | None = None,
                    headless: bool | None = None, keep_open: bool = True,
                    proceed_to_confirm: bool = False) -> dict[str, Any]:
    """予約フォームを開いて行けるところまで自動入力し、画面を残して人に渡す。

    Botは確認/確定ボタンを押さない。フォーム構造が違っても落ちずに報告する。
    戻り値: {status, url, filled[], skipped_missing[], empty_required[], errors[]}
      status = "ready"(入力完了・要ユーザー確定) / "full"(満室) / "no_form" / "error"
    """
    from playwright.sync_api import sync_playwright

    year = year or mon["target_year"]
    if headless is None:
        headless = bool(res.get("headless", False))   # 実運用は画面ありで人に渡す
    report: dict[str, Any] = {"status": "", "url": "", "filled": [], "skipped_missing": [],
                              "empty_required": [], "errors": []}
    url = build_form_url(mon, plan_id, room_id, year, month, day)

    p = sync_playwright().start()
    try:
        browser = _launch_browser(p, headless)
    except Exception:
        p.stop()
        raise
    try:
        page = browser.new_page()
    except Exception:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        p.stop()
        raise
    try:
        page.goto(url, wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(1500)
        report["url"] = page.url

        if page.query_selector("input[name='guest_name']") is None:
            body = ""
            try:
                body = page.inner_text("body")
            except Exception:
                pass
            report["status"] = "full" if "満室" in body else "no_form"
        else:
            for item in _field_plan(res, nights):
                _apply(page, item, report)
            report["status"] = "ready"

            # テスト/検証用: 「確認画面へ」を押して内容確認ページまで進む(予約完了は絶対に押さない)
            if proceed_to_confirm:
                clicked = False
                for sel in ["input[value*='確認画面']", "button:has-text('確認画面')",
                            "a:has-text('確認画面')", "input[value*='確認']"]:
                    try:
                        loc = page.locator(sel)
                        if loc.count() > 0:
                            loc.first.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    page.wait_for_timeout(3500)
                    body2 = page.inner_text("body")
                    if any(k in body2 for k in ("この内容で予約", "予約を確定", "予約内容の確認", "上記の内容で")):
                        report["status"] = "confirm_ready"      # 内容確認ページに到達(あとは予約完了のみ)
                    elif "内容確認" in body2 and "予約完了" in body2:
                        report["status"] = "confirm_ready"
                    else:
                        report["status"] = "confirm_blocked"    # 入力不備等で止まった
                        report["confirm_hint"] = re.sub(r"\s+", " ", body2)[:200]
                else:
                    report["errors"].append("「確認画面へ」ボタンが見つからない")

    except Exception as exc:  # noqa: BLE001
        report["status"] = report["status"] or "error"
        report["errors"].append(str(exc))

    # handoff: フォームが出ていて keep_open なら画面を残す(確認画面まで進んだ/止まった場合も)。
    # セッションは prepare_booking_async のスレッドが窓が閉じられるまで見届けて片付ける
    if keep_open and report["status"] in ("ready", "confirm_ready", "confirm_blocked"):
        report["_session"] = {"p": p, "browser": browser, "page": page}
        report["handoff_open"] = True
    else:
        try:
            browser.close(); p.stop()
        except Exception:  # noqa: BLE001
            pass
        report["handoff_open"] = False
    return report
