#!/usr/bin/env python3
"""
ウォッチリスト専用リサーチ（本番）:
  research_all_watchlist（period_days 以内・Apify）→ scrape_profiles 相当のフォロワー補完は gsheets 内で実行済み
  → OpenAI をリール数に応じて分割バッチで実行（JSON 欠損対策）
  → OpenAI でウォッチ専用ナラティブサマリー → build_gsheet（ウォッチリスト系＋「ウォッチリスト サマリー」。通常リサーチサマリー・横断トレンドは更新しない）

使い方:
  python3 run_watchlist_research.py [--period 1w|2w|1m|3m|6m]

  --period は 1w / 2w / 1m / 3m / 6m から選択（既定 1w）。
  ・期間に連動してアーカイブ閾値も同じ日数になる。
  ・OpenAI 分析対象はバズ倍率TOP15件まで（環境変数 IG_REEL_TOP_N で変更可）。

環境変数:
  APIFY_TOKEN または APIFY_API_TOKEN（必須）
  OPENAI_API_KEY（必須）
  GOOGLE_SHEETS_CREDENTIALS（推奨: ~/.config/gcloud/sheets-writer.json）
  OPENAI_MODEL（任意）
  GSHEETS_WRITE_THROTTLE_SEC（任意）
  IG_REEL_TOP_N（任意、既定 15）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from gsheets import (  # noqa: E402
    cleanup_legacy_tabs,
    create_snapshot_sheet,
    get_watchlist,
    get_watchlist_ordered_meta,
    open_spreadsheet,
    research_all_watchlist,
    resolve_watchlist_tab_name,
    write_usage_guide,
)
from pipeline import _acquire_singleton_lock  # noqa: E402
from transcribe import transcribe_reels_inplace  # noqa: E402
import run_production_research as rpr  # noqa: E402

DEFAULT_BATCH = 4
DEFAULT_TOP_N = 15


def _build_watch_condition(period_days: int) -> str:
    return f"ウォッチリスト専用・直近{period_days}日以内（再生・フォロワー比の条件なし）"


def _parse_period_arg() -> int:
    """sys.argv から --period を抜き出して日数に変換。指定なしなら 7。"""
    presets = {"1w": 7, "2w": 14, "1m": 30, "3m": 90, "6m": 180}
    args = list(sys.argv[1:])
    period_key = "1w"
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--period", "-p"):
            if i + 1 >= len(args):
                print("[FATAL] --period の値がありません。1w / 2w / 1m / 3m / 6m から指定してください。", file=sys.stderr)
                sys.exit(1)
            period_key = args[i + 1].strip().lower()
            i += 2
            continue
        if a.startswith("--period="):
            period_key = a.split("=", 1)[1].strip().lower()
            i += 1
            continue
        i += 1
    if period_key not in presets:
        print(
            f"[FATAL] --period は {sorted(presets.keys())} のいずれかを指定してください（指定値: '{period_key}'）。",
            file=sys.stderr,
        )
        sys.exit(1)
    return presets[period_key]


def _normalize_watch_reels_for_write(watch_reels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sheets書き込み前に最低限必要な項目がある行だけ残す。"""
    cleaned: List[Dict[str, Any]] = []
    skipped = 0
    for r in watch_reels:
        url = str(r.get("url") or "").strip()
        username = str(r.get("username") or "").strip().lstrip("@")
        if not url or not username:
            skipped += 1
            continue
        nr = dict(r)
        nr["username"] = username
        cleaned.append(nr)
    if skipped:
        print(f"[WARN] watchlist_reels 正規化: 不完全データ {skipped} 件を除外")
    return cleaned


def main() -> None:
    period_days = _parse_period_arg()
    _acquire_singleton_lock("ig-reel-research/watchlist")
    watch_condition = _build_watch_condition(period_days)
    top_n = max(1, int(os.environ.get("IG_REEL_TOP_N", str(DEFAULT_TOP_N))))

    apify = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
    if not apify:
        print("[FATAL] APIFY_TOKEN（または APIFY_API_TOKEN）を設定してください。", file=sys.stderr)
        sys.exit(1)
    if len(apify.strip()) < 20:
        print(
            f"[FATAL] APIFY_TOKEN が短すぎます（{len(apify.strip())} 文字）。"
            "ログインシェルで .zshrc が読み込まれていない可能性。"
            "`zsh -lic 'echo $APIFY_TOKEN'` で実体を確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        print("[FATAL] OPENAI_API_KEY を設定してください。", file=sys.stderr)
        sys.exit(1)
    if len(openai_key.strip()) < 20:
        print(
            f"[FATAL] OPENAI_API_KEY が短すぎます（{len(openai_key.strip())} 文字）。値を確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    os.environ.setdefault("GOOGLE_SHEETS_CREDENTIALS", os.path.expanduser("~/.config/gcloud/sheets-writer.json"))
    if not os.path.exists(os.environ["GOOGLE_SHEETS_CREDENTIALS"]):
        print(
            f"[FATAL] GOOGLE_SHEETS_CREDENTIALS が見つかりません: {os.environ['GOOGLE_SHEETS_CREDENTIALS']}",
            file=sys.stderr,
        )
        sys.exit(1)
    os.environ.setdefault("GSHEETS_WRITE_THROTTLE_SEC", "5")

    period_label = {7: '1週間', 14: '2週間', 30: '1ヶ月', 90: '3ヶ月', 180: '6ヶ月'}.get(period_days, f'{period_days}日')
    print(f"=== 期間設定: {period_days}日 ({period_label}) / 分析上限: TOP{top_n}件（バズ倍率順） ===")

    sp = open_spreadsheet()
    print("=== 旧フォーマットのタブを削除 ===")
    n_legacy = cleanup_legacy_tabs()
    if n_legacy > 0:
        print(f"  → {n_legacy} 個の旧タブを削除")
    print("=== 使い方ガイドを再生成 ===")
    write_usage_guide(sp)

    watch_usernames = get_watchlist()
    if not watch_usernames:
        print("[FATAL] ウォッチリストが空です。「ウォッチリスト」タブにアカウントを登録してください。", file=sys.stderr)
        sys.exit(2)
    print(f"=== ウォッチリスト登録アカウント {len(watch_usernames)} 件: {', '.join(watch_usernames)} ===")

    watch_reels = research_all_watchlist(apify, period_days=period_days)
    watch_reels = _normalize_watch_reels_for_write(watch_reels)
    print(f"[INFO] ウォッチリスト収集: {len(watch_reels)} 件")

    # 各リールに登録時のジャンル情報を付与
    meta_list = get_watchlist_ordered_meta(sp)
    genre_by_user = {m["username"]: m.get("genre", "") for m in meta_list}
    for r in watch_reels:
        u = str(r.get("username") or "").strip().lstrip("@")
        if u and u in genre_by_user:
            r["genre"] = genre_by_user[u]

    if not watch_reels:
        print(f"[WARN] 直近{period_days}日のリールが0件のため、OpenAI・Sheets のウォッチ分析はスキップします。")
        sys.exit(0)

    # OpenAI コスト・実行時間制御のため、バズ倍率上位 top_n 件のみを分析対象にする
    watch_reels = sorted(watch_reels, key=lambda x: float(x.get("buzz_ratio") or 0), reverse=True)
    if len(watch_reels) > top_n:
        print(f"[CAP] ウォッチリスト分析対象を上位 {top_n} 件に制限（収集 {len(watch_reels)} 件 → 上位 {top_n} 件）")
        watch_reels = watch_reels[:top_n]

    # 音声文字起こし（ローカル Whisper）
    if os.environ.get("IG_REEL_DISABLE_TRANSCRIBE", "").strip() not in ("1", "true", "yes"):
        whisper_model = os.environ.get("IG_REEL_WHISPER_MODEL", "medium").strip() or "medium"
        print(f"\n=== Whisper 音声文字起こし（model={whisper_model}） ===")
        transcribe_reels_inplace(watch_reels, model_name=whisper_model)
    else:
        print("[INFO] IG_REEL_DISABLE_TRANSCRIBE=1 のため文字起こしをスキップ")
        for r in watch_reels:
            r.setdefault("transcript", "")

    kw_base = "ウォッチリスト: " + ", ".join(
        sorted({str(r.get("username") or "").strip().lstrip("@") for r in watch_reels if r.get("username")})
    )
    n_total = len(watch_reels)
    batch_size = int(os.environ.get("WATCHLIST_OPENAI_BATCH", str(DEFAULT_BATCH)))

    contents: List[Dict[str, Any]] = []
    insightss: List[Dict[str, Any]] = []
    summary_first_batch: Dict[str, Any] = {}

    for off in range(0, n_total, batch_size):
        chunk = watch_reels[off : off + batch_size]
        bi = off // batch_size + 1
        c, ins, sm = rpr._openai_analyze(
            chunk,
            kw_base + f"（分割{bi}）",
            n_total,
            watch_condition,
        )
        if len(c) != len(chunk):
            raise RuntimeError(f"OpenAI 応答件数不一致: batch={bi} chunk={len(chunk)} content={len(c)}")
        contents.extend(c)
        insightss.extend(ins)
        if bi == 1:
            summary_first_batch = sm

    if not (len(contents) == len(insightss) == n_total):
        raise RuntimeError(
            f"マージ後の件数不一致: n={n_total} c={len(contents)} i={len(insightss)}"
        )

    # サマリーに期間情報・条件・件数を補強
    summary_first_batch["date"] = date.today().strftime("%Y-%m-%d")
    summary_first_batch["keyword"] = f"ウォッチリスト ({len(watch_usernames)}アカウント)"
    summary_first_batch["period"] = f"{period_label} ({period_days}日)"
    summary_first_batch["condition_label"] = watch_condition
    summary_first_batch["total"] = str(n_total)
    summary_first_batch["filtered"] = str(n_total)

    # スナップショットタブ作成
    tab_name = resolve_watchlist_tab_name(sp)
    print(f"\n=== スナップショットタブ作成: {tab_name} ===")
    sheet_url = create_snapshot_sheet(
        sp,
        tab_name=tab_name,
        summary=summary_first_batch,
        reels=watch_reels,
        contents=contents,
        insights=insightss,
        watchlist_usernames=watch_usernames,
    )
    print(f"\n[DONE] スプレッドシート: {sheet_url}")


if __name__ == "__main__":
    main()
