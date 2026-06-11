# -*- coding: utf-8 -*-
"""訂席資料表自動同步：Google Drive 上的 Excel → 機器版訂席總表。

流程：
1. 每小時檢查 Drive 資料夾裡最新的 .xlsx 有沒有更新
2. 有更新就下載、用 converter 解析、整份重寫「訂席總表」分頁
3. 每次轉換在「轉換報告」分頁留一筆紀錄
4. 台灣時間每天 20:00 檢查當天是否有上傳新檔，沒有就 LINE 提醒管理者
5. 管理者在 LINE 傳「同步」可立即手動觸發
"""
import io
import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from linebot import LineBotApi
from linebot.models import TextSendMessage

from converter import parse_workbook, records_to_rows

logger = logging.getLogger("ria.sync")

TAIPEI_TZ = timezone(timedelta(hours=8))
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

def _extract_id(value):
    """允許環境變數貼完整網址或純 ID。"""
    value = (value or "").strip().strip("'\"")
    match = re.search(r"/(?:d|folders)/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


DRIVE_FOLDER_ID = _extract_id(
    os.environ.get("DRIVE_FOLDER_ID", "17wp3xo9jDoeSqjuTWgcn_8OcqaAwn4pf")
)
MACHINE_SHEET_ID = _extract_id(os.environ.get("MACHINE_SHEET_ID", ""))
ADMIN_LINE_USER_ID = os.environ.get("ADMIN_LINE_USER_ID", "")

SYNC_INTERVAL_SECONDS = 3600
REMINDER_HOUR_TAIPEI = 20

DATA_TAB = "訂席總表"
REPORT_TAB = "轉換報告"
REPORT_HEADER = ["同步時間(台灣)", "來源檔名", "檔案更新時間", "宴席筆數", "公休筆數", "異常數", "異常摘要"]

_lock = threading.Lock()
_last_synced_modified_time = None
_last_reminder_date = None
_scheduler_started = False


def _load_credentials():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get(
        "GOOGLE_CREDENTIALS"
    )
    if not sa_json:
        return None
    return Credentials.from_service_account_info(
        json.loads(sa_json), scopes=SCOPES
    )


def _notify_admin(text):
    if not ADMIN_LINE_USER_ID:
        return
    try:
        token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
        LineBotApi(token).push_message(
            ADMIN_LINE_USER_ID, TextSendMessage(text=text)
        )
    except Exception:
        logger.exception("LINE 通知管理者失敗")


def _find_latest_xlsx(session):
    params = {
        "q": (
            f"'{DRIVE_FOLDER_ID}' in parents and trashed=false "
            f"and mimeType='{XLSX_MIME}'"
        ),
        "orderBy": "modifiedTime desc",
        "pageSize": 5,
        "fields": "files(id,name,modifiedTime)",
    }
    resp = session.get(
        "https://www.googleapis.com/drive/v3/files", params=params, timeout=30
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0] if files else None


def _download_file(session, file_id):
    resp = session.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"alt": "media"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def _get_or_create_tab(spreadsheet, title, rows=1000, cols=40):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def status_report():
    """回報同步系統的設定狀態（管理者 LINE 傳「狀態」時使用）。"""
    creds_ok = _load_credentials() is not None
    lines = [
        "🔧 同步系統狀態",
        f"機器版總表 ID：{MACHINE_SHEET_ID[:10] + '…' if MACHINE_SHEET_ID else '❌ 未設定'}",
        f"Drive 資料夾 ID：{DRIVE_FOLDER_ID[:10]}…",
        f"管理者 ID：{'✅ 已設定' if ADMIN_LINE_USER_ID else '❌ 未設定'}",
        f"服務帳戶金鑰：{'✅ 已設定' if creds_ok else '❌ 未設定'}",
        f"自動排程：{'✅ 運作中' if _scheduler_started else '❌ 未啟動'}",
        f"上次同步的檔案版本：{_last_synced_modified_time or '（尚未同步過）'}",
    ]
    return "\n".join(lines)


def run_sync(force=False):
    """執行一次同步。回傳 (success, message)，message 為人看的摘要。"""
    global _last_synced_modified_time
    logger.info("run_sync 被呼叫 force=%s", force)
    if not MACHINE_SHEET_ID:
        logger.warning("run_sync 中止：MACHINE_SHEET_ID 未設定")
        return False, "尚未設定 MACHINE_SHEET_ID，同步功能未啟用"
    creds = _load_credentials()
    if creds is None:
        logger.warning("run_sync 中止：服務帳戶金鑰未設定")
        return False, "尚未設定服務帳戶金鑰，無法同步"

    if not _lock.acquire(timeout=10):
        return False, "另一個同步作業正在執行中，請稍候一分鐘再試"
    try:
        try:
            logger.info("開始同步 force=%s", force)
            session = AuthorizedSession(creds)
            file_info = _find_latest_xlsx(session)
            if file_info is None:
                return False, "Drive 資料夾裡找不到任何 .xlsx 檔案"

            if not force and file_info["modifiedTime"] == _last_synced_modified_time:
                return True, "檔案沒有更新，略過本次同步"

            logger.info("下載檔案：%s", file_info["name"])
            content = _download_file(session, file_info["id"])
            records, issues = parse_workbook(io.BytesIO(content))
            rows = records_to_rows(records)
            closed = sum(1 for r in records if r.get("訂席狀態") == "公休")
            logger.info("解析完成 %d 筆，開始寫入總表", len(records))

            gc = gspread.Client(auth=creds)
            try:
                gc.http_client.set_timeout(120)
            except AttributeError:
                pass
            spreadsheet = gc.open_by_key(MACHINE_SHEET_ID)

            data_ws = _get_or_create_tab(spreadsheet, DATA_TAB)
            data_ws.clear()
            data_ws.update(values=rows, range_name="A1")

            report_ws = _get_or_create_tab(spreadsheet, REPORT_TAB, rows=2000, cols=10)
            if not report_ws.get_values("A1:A1"):
                report_ws.update(values=[REPORT_HEADER], range_name="A1")
            now_taipei = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")
            report_ws.append_row(
                [
                    now_taipei,
                    file_info["name"],
                    file_info["modifiedTime"],
                    len(records) - closed,
                    closed,
                    len(issues),
                    "；".join(issues)[:1000],
                ],
                value_input_option="RAW",
            )

            _last_synced_modified_time = file_info["modifiedTime"]
            summary = (
                f"✅ 訂席總表同步完成\n"
                f"檔案：{file_info['name']}\n"
                f"宴席 {len(records) - closed} 筆、公休 {closed} 筆"
            )
            if issues:
                summary += f"\n⚠️ 格式異常 {len(issues)} 筆：\n" + "\n".join(
                    issues[:5]
                )
                _notify_admin(summary)
            logger.info(
                "同步完成 file=%s records=%d issues=%d",
                file_info["name"], len(records), len(issues),
            )
            return True, summary
        except Exception:
            logger.exception("同步失敗")
            return False, "❌ 同步失敗，請查看系統 log"
    finally:
        _lock.release()


def run_sync_async(notify_user_id):
    """背景執行同步，完成後把結果推播給指定使用者（避免卡住 LINE webhook）。"""

    def _worker():
        _, message = run_sync(force=True)
        try:
            token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
            LineBotApi(token).push_message(
                notify_user_id, TextSendMessage(text=message)
            )
        except Exception:
            logger.exception("推播同步結果失敗")

    threading.Thread(target=_worker, daemon=True).start()


def push_test_async(notify_user_id):
    """測試主動推播功能是否正常（隔離診斷用）。"""

    def _worker():
        time.sleep(3)
        logger.info("推播測試：開始推播給 %s", notify_user_id)
        try:
            token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
            LineBotApi(token).push_message(
                notify_user_id,
                TextSendMessage(text="✅ 主動推播功能正常"),
            )
            logger.info("推播測試：成功")
        except Exception:
            logger.exception("推播測試：失敗")

    threading.Thread(target=_worker, daemon=True).start()


def _check_stale_and_remind():
    """台灣時間每天 20:00 檢查當天是否有上傳新檔。"""
    global _last_reminder_date
    now = datetime.now(TAIPEI_TZ)
    if now.hour != REMINDER_HOUR_TAIPEI or _last_reminder_date == now.date():
        return
    _last_reminder_date = now.date()
    creds = _load_credentials()
    if creds is None:
        return
    try:
        session = AuthorizedSession(creds)
        file_info = _find_latest_xlsx(session)
        if file_info is None:
            _notify_admin("⚠️ 提醒：Drive 資料夾裡還沒有訂席資料表檔案")
            return
        modified = datetime.fromisoformat(
            file_info["modifiedTime"].replace("Z", "+00:00")
        ).astimezone(TAIPEI_TZ)
        if modified.date() < now.date():
            _notify_admin(
                f"⚠️ 提醒：今天還沒上傳新的訂席資料表\n"
                f"目前最新檔案：{file_info['name']}\n"
                f"最後更新：{modified.strftime('%Y-%m-%d %H:%M')}"
            )
    except Exception:
        logger.exception("檢查檔案更新狀態失敗")


def _scheduler_loop():
    time.sleep(90)  # 等服務啟動穩定後再開始第一次同步
    last_sync_attempt = 0.0
    while True:
        try:
            if time.time() - last_sync_attempt >= SYNC_INTERVAL_SECONDS:
                last_sync_attempt = time.time()
                run_sync()
            _check_stale_and_remind()
        except Exception:
            logger.exception("排程迴圈發生未預期錯誤")
        time.sleep(60)


def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    if not MACHINE_SHEET_ID:
        logger.warning("MACHINE_SHEET_ID 未設定，訂席總表同步功能未啟用")
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    logger.info("訂席總表同步排程已啟動（每小時檢查、每天 %d:00 提醒）", REMINDER_HOUR_TAIPEI)
