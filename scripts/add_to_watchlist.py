#!/usr/bin/env python3
"""ウォッチリストへのアカウント追加 CLI。

使い方:
  python3 add_to_watchlist.py @user1 @user2 [--genre フィットネス] [--memo "編集が上手い"]

引数:
  - 1つ以上のアカウント名（@は付けても付けなくてもOK）
  - --genre / -g  : ジャンル（任意・全アカウント共通）
  - --memo / -m   : メモ（任意・全アカウント共通）
  - --spreadsheet-id : スプシID指定（既定は gsheets.DEFAULT_SPREADSHEET_ID）

仕様:
  - 重複（既登録済み）アカウントはスキップ
  - 不正な文字を含むユーザー名は警告して除外
  - 登録日は自動で本日の日付（YYYY-MM-DD）

環境変数:
  GOOGLE_SHEETS_CREDENTIALS（必須）
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from gsheets import add_to_watchlist  # noqa: E402


def _parse_cli() -> Tuple[List[str], str, str, Optional[str]]:
    """引数パース。

    Returns: (usernames, genre, memo, spreadsheet_id)
    """
    args = list(sys.argv[1:])
    if not args:
        print(
            "使い方: python3 add_to_watchlist.py @user1 @user2 [--genre ジャンル] [--memo メモ] [--spreadsheet-id ID]",
            file=sys.stderr,
        )
        print("例: python3 add_to_watchlist.py @tomoya_tore @rinochan.diet --genre フィットネス --memo \"編集が上手い\"", file=sys.stderr)
        sys.exit(1)

    usernames: List[str] = []
    genre = ""
    memo = ""
    spreadsheet_id: Optional[str] = None

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--genre", "-g"):
            if i + 1 >= len(args):
                print("[FATAL] --genre の値が指定されていません", file=sys.stderr)
                sys.exit(1)
            genre = args[i + 1]
            i += 2
            continue
        if a.startswith("--genre="):
            genre = a.split("=", 1)[1]
            i += 1
            continue
        if a in ("--memo", "-m"):
            if i + 1 >= len(args):
                print("[FATAL] --memo の値が指定されていません", file=sys.stderr)
                sys.exit(1)
            memo = args[i + 1]
            i += 2
            continue
        if a.startswith("--memo="):
            memo = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--spreadsheet-id":
            if i + 1 >= len(args):
                print("[FATAL] --spreadsheet-id の値が指定されていません", file=sys.stderr)
                sys.exit(1)
            spreadsheet_id = args[i + 1]
            i += 2
            continue
        if a.startswith("--spreadsheet-id="):
            spreadsheet_id = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("-"):
            print(f"[FATAL] 不明なオプション: {a}", file=sys.stderr)
            sys.exit(1)
        usernames.append(a)
        i += 1

    if not usernames:
        print("[FATAL] アカウント名が1つ以上必要です。", file=sys.stderr)
        sys.exit(1)

    return usernames, genre, memo, spreadsheet_id


def main() -> None:
    usernames, genre, memo, spreadsheet_id = _parse_cli()

    os.environ.setdefault(
        "GOOGLE_SHEETS_CREDENTIALS",
        os.path.expanduser("~/.config/gcloud/sheets-writer.json"),
    )
    if not os.path.exists(os.environ["GOOGLE_SHEETS_CREDENTIALS"]):
        print(
            f"[FATAL] GOOGLE_SHEETS_CREDENTIALS が見つかりません: {os.environ['GOOGLE_SHEETS_CREDENTIALS']}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[INFO] 追加対象: {', '.join('@' + u.lstrip('@＠') for u in usernames)}")
    if genre:
        print(f"[INFO] ジャンル: {genre}")
    if memo:
        print(f"[INFO] メモ: {memo}")

    added, already, invalid = add_to_watchlist(
        usernames,
        spreadsheet_id=spreadsheet_id,
        genre=genre,
        memo=memo,
    )

    print()
    if added:
        print(f"[OK] {added} 件を新規追加しました")
    if already:
        print(f"[SKIP] 既に登録済み: {', '.join('@' + u for u in already)}")
    if invalid:
        print(f"[WARN] 不正な形式（除外）: {', '.join(invalid)}", file=sys.stderr)

    if added == 0 and not already and not invalid:
        print("[WARN] 何も処理されませんでした")
        sys.exit(2)


if __name__ == "__main__":
    main()
