#!/usr/bin/env python3
"""
ウォッチリスト登録シートに行を書き込む（本番・手動復旧用）。

既存のデータ行（2行目以降）をクリアしてから、指定アカウントを再登録する。
アカウント名列は Instagram プロフィールへの =HYPERLINK(...) とする。

使い方:
  export GOOGLE_SHEETS_CREDENTIALS=$HOME/.config/gcloud/sheets-writer.json
  python3 register_watchlist_accounts.py
"""
from __future__ import annotations

import os
import sys
from datetime import date

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from gsheets import SHEET_WATCHLIST, _ensure_watchlist_sheet, open_spreadsheet  # noqa: E402


def _hyperlink_username(username: str) -> str:
    u = (username or "").strip().lstrip("@")
    return f'=HYPERLINK("https://www.instagram.com/{u}/", "{u}")'


def main() -> None:
    os.environ.setdefault(
        "GOOGLE_SHEETS_CREDENTIALS",
        os.path.expanduser("~/.config/gcloud/sheets-writer.json"),
    )
    if not os.path.exists(os.environ["GOOGLE_SHEETS_CREDENTIALS"]):
        print(f"[FATAL] GOOGLE_SHEETS_CREDENTIALS が見つかりません: {os.environ['GOOGLE_SHEETS_CREDENTIALS']}", file=sys.stderr)
        sys.exit(1)

    today = date.today().strftime("%Y-%m-%d")
    rows = [
        [
            1,
            _hyperlink_username("watch_account_1"),
            "Instagramマーケティング",
            "フック・構成が上手い、編集・テロップが参考になる、マーケティング戦略が学べる",
            today,
            "",
        ],
        [
            2,
            _hyperlink_username("watch_account_2"),
            "Instagramマーケティング",
            "フック・構成が上手い、編集・テロップが参考になる、マーケティング戦略が学べる",
            today,
            "",
        ],
        [
            3,
            _hyperlink_username("watch_account_3"),
            "ダイエット・健康",
            "フック・構成が上手い",
            today,
            "",
        ],
    ]

    sp = open_spreadsheet()
    _ensure_watchlist_sheet(sp)
    ws = sp.worksheet(SHEET_WATCHLIST)
    n = min(ws.row_count, 500)
    if n >= 2:
        ws.batch_clear([f"A2:F{n}"])
    ws.update(range_name="A2:F4", values=rows, value_input_option="USER_ENTERED")
    sid = sp.id
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    print(f"[OK] ウォッチリストに3件を登録しました: {url}")


if __name__ == "__main__":
    main()
