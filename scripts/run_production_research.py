#!/usr/bin/env python3
"""
本番リサーチ一括実行:
  collect_reels → filter_buzz_reels →（5件未満なら追加キーワードで chase、条件は緩めない）
  → OpenAI でコンテンツ／スコア／インサイト → build_xlsx（Google Sheets 自動同期）

使い方:
  python3 run_production_research.py <キーワード...> [--period 1w|2w|1m|3m|6m]
                                                     [--min-views N | --buzz-ratio X]
  例:
    python3 run_production_research.py ダイエット
    python3 run_production_research.py ダイエット 食事管理 --period 2w
    python3 run_production_research.py ダイエット --period 1m --min-views 100000
    python3 run_production_research.py ダイエット --period 1m --buzz-ratio 5
  キーワードは1つ以上必須（引数なし・空のみの場合は終了）。
  --period は 1w / 2w / 1m / 3m / 6m から選択（既定 2w = 14日）。
  --min-views: 絶対値の再生回数閾値（既定 50000）
  --buzz-ratio: フォロワー比閾値（指定時は --min-views は無視）
  ※ --min-views と --buzz-ratio の同時指定は不可
  ・OpenAI 分析対象はバズ倍率TOP15件まで（環境変数 IG_REEL_TOP_N で変更可）。

環境変数:
  APIFY_TOKEN（必須）
  OPENAI_API_KEY（必須）
  GOOGLE_SHEETS_CREDENTIALS（推奨: ~/.config/gcloud/sheets-writer.json）
  OPENAI_MODEL（任意、既定 gpt-4o-mini）
  IG_REEL_OUTPUT_DIR（任意、xlsx 出力先）
  GSHEETS_WRITE_THROTTLE_SEC（任意、429 回避用秒）
  IG_REEL_TOP_N（任意、既定 15）
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from gsheets import (  # noqa: E402
    DEFAULT_SPREADSHEET_ID,
    cleanup_legacy_tabs,
    create_snapshot_sheet,
    get_watchlist,
    open_spreadsheet,
    resolve_snapshot_tab_name,
    write_usage_guide,
)
from pipeline import (  # noqa: E402
    _acquire_singleton_lock,
    _extract,
    collect_reels,
    enrich_followers_inplace,
    filter_buzz_reels,
    keyword_sourced_raw_count,
    passed_keyword_only,
    search_reels,
)
from transcribe import transcribe_reels_inplace  # noqa: E402

# 後追いリサーチ専用。元キーワードと重複しないよう別表現のみ（フィルタ条件は変更しない）
CHASE_KEYWORD_ROUNDS: List[List[str]] = [
    ["食事制限", "カロリー管理", "痩せる", "糖質オフ"],
    ["ボディメイク", "腸活", "ファスティング", "代謝アップ"],
    ["PFCバランス", "ダイエットレシピ", "健康食", "ルーティン"],
]

PERIOD_PRESETS: Dict[str, int] = {
    "1w": 7,
    "2w": 14,
    "1m": 30,
    "3m": 90,
    "6m": 180,  # ※ 自然言語ヒアリングでは出さないが、CLIでは引き続き受け付ける
}
DEFAULT_PERIOD = "2w"  # 既定 14日
DEFAULT_MIN_VIEWS = 50000  # 既定 5万再生以上
DEFAULT_MIN_BUZZ_RATIO: Optional[float] = None  # フォロワー比モードは明示時のみ
DEFAULT_TOP_N = 15


def _build_condition_label(period_days: int, min_views: int, min_buzz_ratio: Optional[float]) -> str:
    if min_buzz_ratio is not None:
        return f"{period_days}日以内・フォロワー×{min_buzz_ratio}倍以上"
    return f"{period_days}日以内・{min_views:,}再生以上"


def _canon_reel_url(url: str) -> str:
    if not url:
        return ""
    s = str(url).strip()
    if not s.startswith("http"):
        s = f"https://www.instagram.com/reel/{s.strip('/')}/"
    return s.rstrip("/") + "/"


def _merge_raw_items(all_reels: List[Any], items: List[Any], seen: Set[str]) -> int:
    """collect_reels と同様、重複URLを除いて raw item を all_reels に追加。"""
    n = 0
    for item in items:
        url = _extract(item, ["url", "webLink", "permalink", "shortCode"], "")
        nu = _canon_reel_url(url)
        if not nu or nu in seen:
            continue
        seen.add(nu)
        if isinstance(item, dict):
            item["_ig_reel_source"] = "keyword"
        all_reels.append(item)
        n += 1
    return n


def _openai_analyze(
    passed: List[Dict[str, Any]], keywords_label: str, collected_total: int,
    condition_label: str = "",
) -> Tuple[List[Dict], List[Dict], Dict[str, Any]]:
    """Returns (content, insights, summary). content = [{hook, genre}], insights = [{hypothesis, improvement}]."""
    from openai import OpenAI

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    reel_payload = []
    for r in passed:
        reel_payload.append(
            {
                "url": r.get("url", ""),
                "username": r.get("username", ""),
                "followers": r.get("followers", 0),
                "views": r.get("views", 0),
                "likes": r.get("likes", 0),
                "comments": r.get("comments", 0),
                "posted_date": r.get("posted_date", ""),
                "buzz_ratio": r.get("buzz_ratio", 0),
                "caption": (r.get("caption") or "")[:3500],
                "duration": str(r.get("duration", "")),
                # Whisper による音声文字起こし。空文字なら未取得（音声なし or DL失敗）
                "transcript": (r.get("transcript") or "")[:5000],
            }
        )

    n = len(reel_payload)
    system = (
        "あなたはInstagramリールのバズ分析の専門家である。\n"
        "出力は必ず有効なJSONのみ。日本語で記述。\n"
        "【重要】入力 reels はキーワード検索で収集したリールのみである。"
        "ウォッチリスト登録アカウント由来のデータは一切含まれない。"
        "分析・サマリー・仮説からウォッチリストの文脈を排除し、キーワード検索結果の範囲に限定すること。\n"
        "【重要・フック判定の優先順位】各リールには transcript フィールドがある。"
        "これは Whisper で文字起こしした実際の動画音声である。"
        "transcript が空でなければ、それを最優先でフック判定の根拠にすること。"
        "transcript が空の場合のみ、caption と数値からフックを推測する。"
        "transcript と caption が食い違う場合は transcript を信頼する（caption は宣伝文で動画と無関係なことが多い）。\n\n"
        f"入力リール数は {n} 件。必ず以下の形式のJSONを出力すること:\n"
        "{\n"
        f'  "content": [{n}個のオブジェクト。各リールのフック＋ジャンル],\n'
        f'  "insights": [{n}個のオブジェクト。各リールのバズ仮説＋改善提案],\n'
        '  "summary": {全体サマリー}\n'
        "}\n\n"
        "content の各要素の構造（簡素化版・以前の structure/cta/text_usage/caption_summary/hashtags/duration は出力不要）:\n"
        '{"hook": "冒頭フック要約。transcript があれば最初の1〜2文を必ず要約せよ。20〜80文字程度", '
        '"genre": "ジャンル分類（例: ダイエット/食事/筋トレ/料理 等）"}\n\n'
        "insights の各要素の構造（簡素化版・以前の comparison/trend/reproducible/remake は出力不要）:\n"
        '{"hypothesis": "バズ仮説（なぜ伸びたか・主要因と補助要因の2〜3文）", '
        '"improvement": "このリールをさらに伸ばすための改善提案（具体的に1〜2文）"}\n\n'
        "summary の構造（必須。キーワード検索・フィルタ通過リールのみを対象。ウォッチリストは含めない）:\n"
        "{\n"
        '  "trend_genre": "今回のリサーチキーワード周辺で最も伸びているジャンル・テーマとその理由（2〜3文）",\n'
        '  "trend_hook_top3": "フィルタ通過リールに共通するフック手法の傾向（必要ならTOP3形式でよい・3〜5文）",\n'
        '  "trend_structure_top3": "再生数が特に高いリールに共通するコンテンツ構成パターン（2〜3文）",\n'
        '  "trend_cta": "今のInstagramで効いているCTA手法の傾向（1〜2文）",\n'
        '  "trend_user_demand": "全体から読み取れるユーザーの関心・需要の方向性（2文）",\n'
        '  "common_factors": "共通バズ要因パターン（3〜5個・箇条書き風の改行入り1テキスト）",\n'
        '  "rank1": {"username": "", "url": "", "why_rank": "なぜこの順位か（1〜2文）"},\n'
        '  "rank2": {同構造},\n'
        '  "rank3": {同構造},\n'
        '  "actions": [\n'
        '    {"priority": 1, "what": "何をやるか（10〜30文字）", "how": "どうやってやるか（30〜80文字）", "expected": "期待される効果（20〜50文字）"},\n'
        "    ... 計5件（priority 1〜5）\n"
        "  ]\n"
        "}\n"
        "※ rank1〜3 の username/url は入力の views 上位3件と整合させること。"
        " アクションはトレンド分析を踏まえ、キーワード通過リールの分析結果にのみ基づくこと。"
    )
    user = {
        "keywords": keywords_label,
        "collected_total": collected_total,
        "filter_condition": condition_label or _build_condition_label(7),
        "reels": reel_payload,
        "scope": "keyword_search_only",
        "note": "ウォッチリスト由来のリールは入力に含まれない。サマリーにウォッチの記述を混ぜないこと。",
    }

    def _call_api(payload_reels: List[Dict], attempt_n: int) -> Dict:
        u = {**user, "reels": payload_reels}
        # 429/500/502/503 等の一時障害は指数バックオフで最大3回リトライ
        max_retries = 3
        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                r = client.chat.completions.create(
                    model=model,
                    temperature=0.3,
                    max_tokens=8000,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(u, ensure_ascii=False)},
                    ],
                )
                return json.loads((r.choices[0].message.content or "").strip())
            except Exception as e:  # openai.APIError, RateLimitError, APIConnectionError 等
                msg = str(e)
                # リトライ対象: rate limit / 5xx / connection / timeout
                retriable = any(
                    s in msg.lower()
                    for s in ["rate limit", "429", "500", "502", "503", "504",
                              "timeout", "timed out", "connection"]
                )
                if not retriable or attempt >= max_retries:
                    raise
                wait = 2 ** attempt  # 2, 4, 8 秒
                print(f"  [OpenAI retry] attempt {attempt} 失敗 → {wait}秒待機: {msg[:120]}",
                      file=sys.stderr)
                time.sleep(wait)
                last_err = e
        # 念のため
        if last_err:
            raise last_err
        raise RuntimeError("OpenAI 呼び出しが最大試行回数を超えました")

    # 全件一括で試みる
    data = _call_api(reel_payload, 1)
    content = data.get("content") or []
    insights = data.get("insights") or []
    summary_extra = data.get("summary") or {}

    # 件数不一致ならバッチ5件ずつで再取得
    if not (len(content) == len(insights) == n):
        print(f"[WARN] OpenAI 応答件数不一致 (content={len(content)} insights={len(insights)}/{n}) → バッチ分割リトライ")
        content, insights = [], []
        batch_size = 5
        for i in range(0, n, batch_size):
            batch = reel_payload[i:i + batch_size]
            print(f"  バッチ {i // batch_size + 1}: {len(batch)} 件")
            bd = _call_api(batch, 2)
            content.extend(bd.get("content") or [])
            insights.extend(bd.get("insights") or [])
            if i == 0:
                summary_extra = bd.get("summary") or summary_extra
        if not (len(content) == len(insights) == n):
            raise RuntimeError(
                f"OpenAI 応答件数不一致（バッチ後）: passed={n} content={len(content)} insights={len(insights)}"
            )

    # 再生数TOP3を計算
    top = sorted(passed, key=lambda x: int(x.get("views") or 0), reverse=True)[:3]

    def _merge_rank(i: int, reel: Dict[str, Any]) -> Dict[str, Any]:
        key = f"rank{i}"
        rk = summary_extra.get(key)
        out: Dict[str, Any] = rk if isinstance(rk, dict) else {}
        out.setdefault("username", str(reel.get("username") or "").strip())
        out.setdefault("url", str(reel.get("url") or "").strip())
        out.setdefault("why_rank", "")
        return out

    rank_objs: Dict[str, Any] = {}
    for i, reel in enumerate(top, 1):
        rank_objs[f"rank{i}"] = _merge_rank(i, reel)

    summary = {
        "date": date.today().strftime("%Y-%m-%d"),
        "keyword": keywords_label,
        "total": str(collected_total),
        "filtered": str(len(passed)),
        "common_factors": summary_extra.get("common_factors", ""),
        "trend_genre": summary_extra.get("trend_genre", ""),
        "trend_hook_top3": summary_extra.get("trend_hook_top3", ""),
        "trend_structure_top3": summary_extra.get("trend_structure_top3", ""),
        "trend_cta": summary_extra.get("trend_cta", ""),
        "trend_user_demand": summary_extra.get("trend_user_demand", ""),
        "rank1": rank_objs.get("rank1", {}),
        "rank2": rank_objs.get("rank2", {}),
        "rank3": rank_objs.get("rank3", {}),
        "actions": summary_extra.get("actions", ""),
    }
    return content, insights, summary


def chase_more_reels(
    all_reels: List[Any], keywords_used: List[str], apify_token: str, max_per_kw: int = 30,
    period_days: int = 14,
    min_views: int = DEFAULT_MIN_VIEWS,
    min_buzz_ratio: Optional[float] = DEFAULT_MIN_BUZZ_RATIO,
) -> Tuple[List[Any], int]:
    """キーワード検索由来のフィルタ通過が5件未満のときだけ追加キーワードで検索し、all_reels にマージする。"""
    filter_kwargs = dict(days=period_days, min_views=min_views, min_buzz_ratio=min_buzz_ratio)
    passed, _ = filter_buzz_reels(all_reels, **filter_kwargs)
    if len(passed_keyword_only(passed)) >= 5:
        return all_reels, 0

    seen: Set[str] = set()
    for reel in all_reels:
        u = _extract(reel, ["url", "webLink", "permalink", "shortCode"], "")
        nu = _canon_reel_url(u)
        if nu:
            seen.add(nu)

    used = set(keywords_used)
    added_total = 0
    for round_kws in CHASE_KEYWORD_ROUNDS:
        passed, _ = filter_buzz_reels(all_reels, **filter_kwargs)
        if len(passed_keyword_only(passed)) >= 5:
            break
        for kw in round_kws:
            if kw in used:
                continue
            used.add(kw)
            print(f"[CHASE] 追加検索: 「{kw}」 (max {max_per_kw})")
            items = search_reels(kw, apify_token, max_per_kw)
            n = _merge_raw_items(all_reels, items, seen)
            added_total += n
            print(f"  → +{n} 件（重複除去後マージ）")
            passed, _ = filter_buzz_reels(all_reels, **filter_kwargs)
            if len(passed_keyword_only(passed)) >= 5:
                break
        passed, _ = filter_buzz_reels(all_reels, **filter_kwargs)
        if len(passed_keyword_only(passed)) >= 5:
            break

    return all_reels, added_total


def _parse_cli() -> Tuple[List[str], int, int, Optional[float]]:
    """sys.argv[1:] からキーワード・期間・再生条件を取得。

    対応形式:
      - python3 run_production_research.py ダイエット 食事管理
      - python3 run_production_research.py ダイエット --period 1m
      - python3 run_production_research.py ダイエット --min-views 100000
      - python3 run_production_research.py ダイエット --buzz-ratio 5
    Returns:
      (keywords, period_days, min_views, min_buzz_ratio)
    """
    args = list(sys.argv[1:])
    if not args:
        print(
            "使い方: python3 run_production_research.py <キーワード...> "
            "[--period 1w|2w|1m|3m|6m] [--min-views N | --buzz-ratio X]",
            file=sys.stderr,
        )
        print("例:", file=sys.stderr)
        print("  python3 run_production_research.py ダイエット --period 2w --min-views 50000", file=sys.stderr)
        print("  python3 run_production_research.py ダイエット --period 1m --buzz-ratio 5", file=sys.stderr)
        sys.exit(1)

    period_key = DEFAULT_PERIOD
    min_views = DEFAULT_MIN_VIEWS
    min_buzz_ratio: Optional[float] = DEFAULT_MIN_BUZZ_RATIO
    explicit_min_views = False
    explicit_buzz_ratio = False
    cleaned: List[str] = []
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
        if a == "--min-views":
            if i + 1 >= len(args):
                print("[FATAL] --min-views の値がありません（例: 50000）", file=sys.stderr)
                sys.exit(1)
            try:
                min_views = max(0, int(args[i + 1]))
            except ValueError:
                print(f"[FATAL] --min-views は整数で指定してください（指定値: '{args[i+1]}'）", file=sys.stderr)
                sys.exit(1)
            explicit_min_views = True
            i += 2
            continue
        if a.startswith("--min-views="):
            try:
                min_views = max(0, int(a.split("=", 1)[1]))
            except ValueError:
                print(f"[FATAL] --min-views は整数で指定してください（指定値: '{a}'）", file=sys.stderr)
                sys.exit(1)
            explicit_min_views = True
            i += 1
            continue
        if a == "--buzz-ratio":
            if i + 1 >= len(args):
                print("[FATAL] --buzz-ratio の値がありません（例: 5）", file=sys.stderr)
                sys.exit(1)
            try:
                min_buzz_ratio = float(args[i + 1])
            except ValueError:
                print(f"[FATAL] --buzz-ratio は数値で指定してください（指定値: '{args[i+1]}'）", file=sys.stderr)
                sys.exit(1)
            explicit_buzz_ratio = True
            i += 2
            continue
        if a.startswith("--buzz-ratio="):
            try:
                min_buzz_ratio = float(a.split("=", 1)[1])
            except ValueError:
                print(f"[FATAL] --buzz-ratio は数値で指定してください（指定値: '{a}'）", file=sys.stderr)
                sys.exit(1)
            explicit_buzz_ratio = True
            i += 1
            continue
        if a and a.strip():
            cleaned.append(a.strip())
        i += 1

    if period_key not in PERIOD_PRESETS:
        print(
            f"[FATAL] --period は {sorted(PERIOD_PRESETS.keys())} のいずれかを指定してください（指定値: '{period_key}'）。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 排他チェック：絶対値とフォロワー比の両方を明示指定はNG
    if explicit_min_views and explicit_buzz_ratio:
        print(
            "[FATAL] --min-views と --buzz-ratio は同時に指定できません。どちらか1つを選んでください。",
            file=sys.stderr,
        )
        sys.exit(1)
    # フォロワー比モードに切り替わった場合は絶対値は無視
    if explicit_buzz_ratio:
        min_views = 0

    if not cleaned:
        print("[FATAL] キーワードが1つ以上必要です。", file=sys.stderr)
        sys.exit(1)

    # キーワード正規化: ハッシュ記号(#/＃)、全角空白、前後空白の除去。空になったキーワードは捨てる
    normalized: List[str] = []
    for raw in cleaned:
        s = raw.replace("　", " ").strip().lstrip("#＃").strip()
        if s:
            normalized.append(s)
    if not normalized:
        print("[FATAL] 有効なキーワードがありません（記号のみ等）。", file=sys.stderr)
        sys.exit(1)
    if len(normalized) > 5:
        print(
            f"[WARN] キーワード数が {len(normalized)} 件です。Apify/OpenAI のコストが大きくなるため "
            "5件以下を推奨します。続行します。",
            file=sys.stderr,
        )

    return normalized, PERIOD_PRESETS[period_key], min_views, min_buzz_ratio


def main() -> None:
    keywords, period_days, min_views, min_buzz_ratio = _parse_cli()
    _acquire_singleton_lock("ig-reel-research/keyword")
    condition_label = _build_condition_label(period_days, min_views, min_buzz_ratio)
    top_n = max(1, int(os.environ.get("IG_REEL_TOP_N", str(DEFAULT_TOP_N))))

    apify = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
    if not apify:
        print("[FATAL] APIFY_TOKEN（または APIFY_API_TOKEN）を設定してください。")
        sys.exit(1)
    if len(apify.strip()) < 20:
        print(
            f"[FATAL] APIFY_TOKEN が短すぎます（{len(apify.strip())} 文字）。"
            "ログインシェルで .zshrc が読み込まれていない可能性。"
            "`zsh -lic 'echo $APIFY_TOKEN'` で実体を確認してください。"
        )
        sys.exit(1)
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        print("[FATAL] OPENAI_API_KEY を設定してください。")
        sys.exit(1)
    if len(openai_key.strip()) < 20:
        print(f"[FATAL] OPENAI_API_KEY が短すぎます（{len(openai_key.strip())} 文字）。値を確認してください。")
        sys.exit(1)
    os.environ.setdefault("GOOGLE_SHEETS_CREDENTIALS", os.path.expanduser("~/.config/gcloud/sheets-writer.json"))
    if not os.path.exists(os.environ["GOOGLE_SHEETS_CREDENTIALS"]):
        print(f"[FATAL] GOOGLE_SHEETS_CREDENTIALS が見つかりません: {os.environ['GOOGLE_SHEETS_CREDENTIALS']}")
        sys.exit(1)
    os.environ.setdefault("GSHEETS_WRITE_THROTTLE_SEC", "5")
    kw_label = " / ".join(keywords)
    period_label = {7: '1週間', 14: '2週間', 30: '1ヶ月', 90: '3ヶ月', 180: '6ヶ月'}.get(period_days, f'{period_days}日')

    print(f"=== 期間設定: {period_days}日 ({period_label}) / 分析上限: TOP{top_n}件（バズ倍率順） ===")
    print(f"=== 再生条件: {condition_label} ===")

    # スプシを開く
    sp = open_spreadsheet()
    print("=== 旧フォーマットのタブを削除（5シート構造時代のもの） ===")
    n_legacy = cleanup_legacy_tabs()
    if n_legacy > 0:
        print(f"  → {n_legacy} 個の旧タブを削除")
    print("=== 使い方ガイドを再生成 ===")
    write_usage_guide(sp)

    watch = get_watchlist()
    if watch:
        print(f"=== ウォッチリスト登録アカウント {len(watch)} 件: {', '.join(watch)} ===")
    print(f"=== 収集: 各キーワード最大30件 / キーワード={keywords} ===")
    all_reels = collect_reels(keywords, apify, max_per_keyword=30, watchlist_usernames=watch, period_days=period_days)

    # フォロワー数補完（フォロワー比モードでは min_views=0 で全件補完）
    enrich_followers_inplace(all_reels, apify, min_views=min_views, days=period_days)

    collected_kw = keyword_sourced_raw_count(all_reels)
    passed, rejected = filter_buzz_reels(
        all_reels, days=period_days, min_views=min_views, min_buzz_ratio=min_buzz_ratio,
    )
    passed_kw = passed_keyword_only(passed)
    print(
        f"\n[FILTER] 第1回: キーワード由来通過 {len(passed_kw)} / 全通過 {len(passed)} / 除外 {len(rejected)} / "
        f"キーワード収集raw {collected_kw} / 全raw {len(all_reels)}"
    )

    if len(passed_kw) < 5:
        print(
            f"\n[CHASE] キーワード由来通過が5件未満 → 追加キーワード検索（条件は変更しません）。"
            f"現在キーワード通過={len(passed_kw)}"
        )
        all_reels, chase_added = chase_more_reels(
            all_reels, keywords, apify, 30,
            period_days=period_days, min_views=min_views, min_buzz_ratio=min_buzz_ratio,
        )
        enrich_followers_inplace(all_reels, apify, min_views=min_views, days=period_days)
        collected_kw = keyword_sourced_raw_count(all_reels)
        passed, rejected = filter_buzz_reels(
            all_reels, days=period_days, min_views=min_views, min_buzz_ratio=min_buzz_ratio,
        )
        passed_kw = passed_keyword_only(passed)
        print(
            f"[FILTER] chase後: キーワード由来通過 {len(passed_kw)} / 全通過 {len(passed)} / 除外 {len(rejected)} / "
            f"キーワード収集raw {collected_kw} / 全raw {len(all_reels)}（chase追加マージ {chase_added}）"
        )

    if not passed_kw:
        print(
            "[FATAL] キーワード検索由来のフィルタ通過が0件です。"
            "ウォッチリストのみ通過している場合も、通常リサーチの分析は実行できません。",
        )
        sys.exit(2)

    # OpenAI コスト・実行時間制御のため、バズ倍率上位 top_n 件のみを分析対象にする
    passed_kw = sorted(passed_kw, key=lambda x: float(x.get("buzz_ratio") or 0), reverse=True)
    if len(passed_kw) > top_n:
        print(f"[CAP] 分析対象を上位 {top_n} 件に制限（通過 {len(passed_kw)} 件 → 上位 {top_n} 件）")
        passed_kw = passed_kw[:top_n]

    # 音声文字起こし（ローカル Whisper）。スキップしたい場合は IG_REEL_DISABLE_TRANSCRIBE=1
    if os.environ.get("IG_REEL_DISABLE_TRANSCRIBE", "").strip() not in ("1", "true", "yes"):
        whisper_model = os.environ.get("IG_REEL_WHISPER_MODEL", "medium").strip() or "medium"
        print(f"\n=== Whisper 音声文字起こし（model={whisper_model}） ===")
        transcribe_reels_inplace(passed_kw, model_name=whisper_model)
    else:
        print("[INFO] IG_REEL_DISABLE_TRANSCRIBE=1 のため文字起こしをスキップ")
        for r in passed_kw:
            r.setdefault("transcript", "")

    print("\n=== OpenAI によるコンテンツ分析・インサイト・サマリー生成 ===")
    content, insights, summary = _openai_analyze(passed_kw, kw_label, collected_kw, condition_label)

    # サマリーに期間情報を追加
    summary["period"] = f"{period_label} ({period_days}日)"
    summary["condition_label"] = condition_label

    # スナップショットタブ名を解決（衝突時は日付サフィックス）
    tab_name = resolve_snapshot_tab_name(sp, keywords)
    print(f"\n=== スナップショットタブ作成: {tab_name} ===")
    sheet_url = create_snapshot_sheet(
        sp,
        tab_name=tab_name,
        summary=summary,
        reels=passed_kw,
        contents=content,
        insights=insights,
        watchlist_usernames=watch,
    )
    print(f"\n[DONE] スプレッドシート: {sheet_url}")


if __name__ == "__main__":
    main()
