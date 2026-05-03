"""
ig-reel-research パイプラインスクリプト

Phase 1: Apify APIでリール収集
Phase 2: 自動フィルタリング（指定期間以内・3万再生以上・再生≧フォロワー）
Phase 3: 各リール音声の Whisper 文字起こし（呼び出し側）
Phase 4: OpenAI でコンテンツ分析・サマリー生成（呼び出し側）
Phase 5: gsheets.create_snapshot_sheet で転置レイアウトのスナップショットを Sheets に書き込み

スキル実行時にこのスクリプトの関数を呼び出して使う。
"""

import atexit
import errno
import os
import requests
import sys
import tempfile
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
# ====================================================================
# 並列実行ロック（同じスプレッドシートへの2重書き込み防止）
# ====================================================================

LOCK_PATH = os.path.join(tempfile.gettempdir(), "ig-reel-research.lock")


def _acquire_singleton_lock(label: str = "ig-reel-research") -> None:
    """同名スクリプトの2重起動を阻止する単純なPIDロック。

    既存ロックがあれば、そのPIDが生きているか確認:
      - 生きていれば FATAL で即終了
      - 死んでいれば（古いロック）取り直す
    """
    my_pid = os.getpid()
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            old_pid_str, _, old_label = content.partition("|")
            old_pid = int(old_pid_str) if old_pid_str.isdigit() else 0
        except (OSError, ValueError):
            old_pid = 0
            old_label = ""

        alive = False
        if old_pid > 0:
            try:
                os.kill(old_pid, 0)  # シグナル0は存在確認のみ
                alive = True
            except OSError as e:
                alive = (e.errno == errno.EPERM)  # 権限不足=別ユーザーで起動中

        if alive:
            print(
                f"[FATAL] 別の {old_label or label} プロセス (PID {old_pid}) が実行中です。"
                f" ロックファイル: {LOCK_PATH}",
                file=sys.stderr,
            )
            sys.exit(3)
        # 古いロックは取り直す
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass

    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(f"{my_pid}|{label}")
    except OSError as e:
        print(f"[FATAL] ロックファイル作成失敗: {e}", file=sys.stderr)
        sys.exit(3)

    def _release_lock() -> None:
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            cur_pid = int(content.partition("|")[0]) if content else 0
            if cur_pid == my_pid:
                os.remove(LOCK_PATH)
        except (OSError, ValueError):
            pass

    atexit.register(_release_lock)


# ====================================================================
# Phase 1: Apify APIでデータ収集
# ====================================================================

def search_reels(keyword, apify_token, max_results=30):
    """ハッシュタグ検索でリールを収集（apify~instagram-scraper + resultsType:reels）"""
    url = "https://api.apify.com/v2/acts/apify~instagram-scraper/runs"
    headers = {"Authorization": f"Bearer {apify_token}", "Content-Type": "application/json"}
    tag_url = f"https://www.instagram.com/explore/tags/{keyword}/"
    payload = {
        "directUrls": [tag_url],
        "resultsType": "reels",
        "resultsLimit": max_results,
    }
    resp = requests.post(url, json=payload, headers=headers, params={"waitForFinish": 180})
    if resp.status_code not in (200, 201):
        print(f"[WARN] search_reels failed for '{keyword}': {resp.status_code}")
        return []
    dataset_id = resp.json().get("data", {}).get("defaultDatasetId", "")
    if not dataset_id:
        return []
    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers=headers,
        params={"limit": 200},
    )
    items = items_resp.json() if items_resp.status_code == 200 else []
    result = items if isinstance(items, list) else []
    print(f"[OK] '#{keyword}' → {len(result)}件取得")
    return result


def scrape_reel_details(reel_urls, apify_token):
    url = "https://api.apify.com/v2/acts/apify~instagram-reel-scraper/runs"
    headers = {"Authorization": f"Bearer {apify_token}", "Content-Type": "application/json"}
    resp = requests.post(url, json={"urls": reel_urls}, headers=headers, params={"waitForFinish": 180})
    if resp.status_code != 201:
        return []
    dataset_id = resp.json()["data"]["defaultDatasetId"]
    return requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items", headers=headers).json()


def fetch_reels_from_profile(username: str, apify_token: str, max_results: int = 30):
    """
    プロフィールの /reels/ からリール一覧を取得（apify~instagram-scraper + resultsType:reels）。
    instagram-reel-scraper はリールURL専用のため、ウォッチリスト用途ではこちらを使う。
    """
    u = str(username or "").strip().lstrip("@")
    if not u:
        return []
    api_url = "https://api.apify.com/v2/acts/apify~instagram-scraper/runs"
    headers = {"Authorization": f"Bearer {apify_token}", "Content-Type": "application/json"}
    reels_page = f"https://www.instagram.com/{u}/reels/"
    payload = {
        "directUrls": [reels_page],
        "resultsType": "reels",
        "resultsLimit": max_results,
    }
    resp = requests.post(api_url, json=payload, headers=headers, params={"waitForFinish": 180})
    if resp.status_code not in (200, 201):
        print(f"[WARN] fetch_reels_from_profile @{u}: HTTP {resp.status_code}")
        return []
    dataset_id = resp.json().get("data", {}).get("defaultDatasetId", "")
    if not dataset_id:
        return []
    items_resp = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers=headers,
        params={"limit": 200},
    )
    items = items_resp.json() if items_resp.status_code == 200 else []
    return items if isinstance(items, list) else []


def scrape_profiles(usernames, apify_token):
    url = "https://api.apify.com/v2/acts/apify~instagram-scraper/runs"
    headers = {"Authorization": f"Bearer {apify_token}", "Content-Type": "application/json"}
    payload = {
        "directUrls": [f"https://www.instagram.com/{u}/" for u in usernames],
        "resultsType": "details",
        "resultsLimit": 1
    }
    resp = requests.post(url, json=payload, headers=headers, params={"waitForFinish": 120})
    if resp.status_code != 201:
        return []
    dataset_id = resp.json()["data"]["defaultDatasetId"]
    return requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items", headers=headers).json()


REEL_SOURCE_KEYWORD = "keyword"
REEL_SOURCE_WATCHLIST = "watchlist"


def keyword_sourced_raw_count(all_reels: List[Any]) -> int:
    """収集リストのうちキーワード検索由来の raw 件数（ウォッチリスト除外）。"""
    return sum(1 for r in all_reels if (r.get("_ig_reel_source") or REEL_SOURCE_KEYWORD) == REEL_SOURCE_KEYWORD)


def passed_keyword_only(passed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """filter_buzz_reels 通過のうちキーワード検索由来のみ（通常リサーチ用）。"""
    return [r for r in passed if r.get("reel_source") == REEL_SOURCE_KEYWORD]


def collect_reels(keywords, apify_token, max_per_keyword=30, watchlist_usernames=None, period_days: int = 7):
    """
    キーワード検索 + ウォッチリストアカウント由来の最新リールを収集してマージ。

    ウォッチリスト分は Apify 取得後に filter_watchlist_reels（period_days 以内のみ・再生/フォロワー条件なし）で絞り、
    通過分の raw のみ all_reels に追加する。

    Args:
        keywords: 検索キーワード配列
        apify_token: Apifyトークン
        max_per_keyword: キーワードごとの最大取得数
        watchlist_usernames: 監視アカウントのユーザー名配列（@なし）
        period_days: 対象期間（日数）。ウォッチリスト由来リールのフィルタに使う
    """
    watchlist_usernames = watchlist_usernames or []
    all_reels = []
    seen_urls = set()

    def _norm_url(u):
        if not u:
            return ''
        s = str(u).strip()
        return s.rstrip('/') + '/' if s.startswith('http') else s

    for kw in keywords:
        items = search_reels(kw, apify_token, max_per_keyword)
        for item in items:
            url = _extract(item, ['url', 'webLink', 'permalink', 'shortCode'], '')
            nurl = _norm_url(url)
            if nurl and nurl not in seen_urls:
                seen_urls.add(nurl)
                if isinstance(item, dict):
                    item["_ig_reel_source"] = REEL_SOURCE_KEYWORD
                all_reels.append(item)

    if watchlist_usernames:
        try:
            merged = 0
            total_raw = 0
            total_pass = 0
            for uname in watchlist_usernames:
                u = str(uname or "").strip().lstrip("@")
                if not u:
                    continue
                watch_items = fetch_reels_from_profile(u, apify_token, max_per_keyword)
                total_raw += len(watch_items)
                passed_w, _rej_w = filter_watchlist_reels(watch_items, days=period_days)
                total_pass += len(passed_w)
                for r in passed_w:
                    raw = r.get("raw")
                    nurl = _norm_url(r.get("url") or "")
                    if not nurl:
                        continue
                    if not raw:
                        raw = r.get("raw_data") or r
                    if not raw:
                        continue
                    if isinstance(raw, dict):
                        raw["_ig_reel_source"] = REEL_SOURCE_WATCHLIST
                    if nurl not in seen_urls:
                        seen_urls.add(nurl)
                        all_reels.append(raw)
                        merged += 1
            print(
                f"[OK] ウォッチリスト由来: Apify計 {total_raw}件 → {period_days}日以内計 {total_pass}件 → マージ {merged}件（重複除去後）"
            )
        except Exception as e:
            print(f"[WARN] ウォッチリスト収集失敗: {e}")

    print(f"\n[TOTAL] 重複除去後: {len(all_reels)}件")
    return all_reels


# ====================================================================
# Phase 2: 自動フィルタリング
# ====================================================================

def _extract(item, field_names, default=None):
    for name in field_names:
        val = item.get(name)
        if val is not None:
            return val
    return default


def _safe_int(val):
    try:
        return int(val) if val else 0
    except (ValueError, TypeError):
        return 0


def _parse_date(raw):
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw)
    if isinstance(raw, str):
        for fmt in ['%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ',
                     '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return None


def _enrich_reel(reel):
    src = reel.get("_ig_reel_source") if isinstance(reel, dict) else None
    if src not in (REEL_SOURCE_KEYWORD, REEL_SOURCE_WATCHLIST):
        src = REEL_SOURCE_KEYWORD
    views = _safe_int(_extract(reel, ['videoPlayCount','plays','viewCount','video_play_count','playCount','views','videoViews']))
    followers = _safe_int(_extract(reel, ['followersCount','followers','followerCount','ownerFollowers','user_followers','follower_count']))
    likes = _safe_int(_extract(reel, ['likesCount','likes','like_count']))
    comments = _safe_int(_extract(reel, ['commentsCount','comments','comment_count']))
    posted = _parse_date(_extract(reel, ['timestamp','takenAt','taken_at','createdAt','created_at','publishedAt','postedAt','date']))
    url = _extract(reel, ['url','webLink','permalink','shortCode'], '')
    if url and not url.startswith('http'):
        url = f"https://www.instagram.com/reel/{url}/"
    username = _extract(reel, ['ownerUsername','username','owner_username','user'], '不明')
    return {
        'url': url,
        'username': username,
        'reel_source': src,
        'views': views,
        'followers': followers,
        'likes': likes,
        'comments': comments,
        'posted_date': posted.strftime('%Y-%m-%d') if posted else '不明',
        'posted_datetime': posted,
        'caption': _extract(reel, ['caption','text','description'], ''),
        'hashtags': _extract(reel, ['hashtags','tags'], []),
        'music': str(_extract(reel, ['musicInfo','audioTitle','music','audio'], '')),
        'duration': _extract(reel, ['videoDuration','duration','video_duration'], ''),
        'buzz_ratio': round(views / followers, 1) if followers > 0 else 0,
        'engagement_rate': round((likes + comments) / views * 100, 2) if views > 0 else 0,
        'raw': reel,
    }


def enrich_followers_inplace(all_reels, apify_token, min_views=30000, days=7):
    """
    apify~instagram-scraper (resultsType:reels) はフォロワー数を返さないため、
    再生数・日付の事前条件を満たしそうな候補のプロフィールを別途スクレイプして
    raw item に follower_count を注入する。
    """
    cutoff = datetime.now() - timedelta(days=days)
    # 候補のみ絞ってプロフィール取得コストを最小化
    candidates = []
    for reel in all_reels:
        views = _safe_int(_extract(reel, ['videoPlayCount','plays','viewCount','video_play_count','playCount','views','videoViews']))
        posted = _parse_date(_extract(reel, ['timestamp','takenAt','taken_at','createdAt','created_at','publishedAt','postedAt','date']))
        if views >= min_views and posted and posted >= cutoff:
            candidates.append(reel)

    if not candidates:
        print("[enrich_followers] 候補なし – プロフィール取得スキップ")
        return

    usernames = list({
        str(_extract(r, ['ownerUsername','username','owner_username'], '') or '')
        for r in candidates
        if _extract(r, ['ownerUsername','username','owner_username'], '')
    })
    if not usernames:
        return

    print(f"[enrich_followers] {len(usernames)} アカウントのフォロワー数を取得中...")
    profiles = scrape_profiles(usernames, apify_token)
    follower_map = {}
    for p in profiles:
        uname = p.get("username") or p.get("ownerUsername") or ""
        fc = (p.get("followersCount") or p.get("follower_count") or
              (p.get("edge_followed_by") or {}).get("count") or 0)
        if uname:
            follower_map[uname] = int(fc) if fc else 0
    print(f"[enrich_followers] 取得完了: {len(follower_map)} 件")

    # raw item に注入
    for reel in all_reels:
        uname = str(_extract(reel, ['ownerUsername','username','owner_username'], '') or '')
        if uname in follower_map:
            reel['follower_count'] = follower_map[uname]


def enrich_watchlist_reels_followers_from_profiles(reels: List[Dict[str, Any]], apify_token: str) -> None:
    """
    ウォッチリスト用 enriched リールにフォロワー数を scrape_profiles で補完し、
    バズ倍率・エンゲージメント率を再計算する（reels 出力は Apify 単体では followers が空のため）。
    """
    if not reels:
        return
    usernames: List[str] = []
    seen: Set[str] = set()
    for r in reels:
        u = str(r.get('username') or '').strip().lstrip('@')
        if u and u not in seen:
            seen.add(u)
            usernames.append(u)
    if not usernames:
        return
    print(f"[enrich_watchlist_followers] {len(usernames)} アカウントのフォロワー数を取得中...")
    profiles = scrape_profiles(usernames, apify_token)
    follower_map: Dict[str, int] = {}
    for p in profiles:
        uname = str(p.get('username') or p.get('ownerUsername') or '').strip()
        fc = (p.get('followersCount') or p.get('follower_count') or
              (p.get('edge_followed_by') or {}).get('count') or 0)
        if uname:
            follower_map[uname] = int(fc) if fc else 0
    print(f"[enrich_watchlist_followers] 取得完了: {len(follower_map)} 件")

    for r in reels:
        u = str(r.get('username') or '').strip().lstrip('@')
        if not u:
            continue
        fc = follower_map.get(u, 0)
        if fc <= 0:
            fc = _safe_int(r.get('followers'))
        r['followers'] = fc
        views = _safe_int(r.get('views'))
        likes = _safe_int(r.get('likes'))
        comments = _safe_int(r.get('comments'))
        r['buzz_ratio'] = round(views / fc, 1) if fc > 0 else 0.0
        r['engagement_rate'] = round((likes + comments) / views * 100, 2) if views > 0 else 0.0
        raw = r.get('raw')
        if isinstance(raw, dict):
            raw['followersCount'] = fc
            raw['follower_count'] = fc


def filter_buzz_reels(all_reels, days: int = 14, min_views: int = 50000, min_buzz_ratio: Optional[float] = None):
    """再生条件モード切替対応のフィルタ。

    - days: 投稿が直近 days 日以内であること（必須）
    - min_buzz_ratio が指定されていれば: views ≧ followers × min_buzz_ratio で判定（絶対値は無視）
    - min_buzz_ratio が None なら: views ≧ min_views で判定（フォロワー比は無視）

    どちらのモードでも「投稿日不明」は除外する。
    """
    cutoff = datetime.now() - timedelta(days=days)
    passed, rejected = [], []
    use_ratio_mode = min_buzz_ratio is not None
    for reel in all_reels:
        r = _enrich_reel(reel)
        reasons = []
        if r['posted_datetime'] is None:
            reasons.append("投稿日不明")
        elif r['posted_datetime'] < cutoff:
            reasons.append(f"投稿日が{days}日超前({r['posted_date']})")
        if use_ratio_mode:
            # フォロワー比モード
            if r['followers'] > 0:
                actual = r['views'] / r['followers']
                if actual < float(min_buzz_ratio):
                    reasons.append(
                        f"バズ倍率 {actual:.1f}x < {min_buzz_ratio}x"
                    )
            else:
                reasons.append("フォロワー数不明")
        else:
            # 絶対値モード
            if r['views'] < min_views:
                reasons.append(f"再生{r['views']:,} < {min_views:,}")
        if not reasons:
            passed.append(r)
        else:
            r['rejection_reasons'] = reasons
            rejected.append(r)
    passed.sort(key=lambda x: x['buzz_ratio'], reverse=True)
    return passed, rejected


def filter_watchlist_reels(all_reels, days=7):
    """
    ウォッチリスト専用フィルタ（filter_buzz_reels とは別）。

    - 再生数の下限なし（0再生でも可）
    - フォロワー数との比較なし
    - 唯一の条件: 直近 days 日以内に投稿されたリールであること
    - 投稿日不明は除外

    7日超のリールは Google シート側の build_gsheet アーカイブで
    「過去ウォッチリスト *」へ移動される想定。
    """
    cutoff = datetime.now() - timedelta(days=days)
    passed, rejected = [], []
    for reel in all_reels:
        r = _enrich_reel(reel)
        reasons = []
        if r['posted_datetime'] is None:
            reasons.append("投稿日不明")
        elif r['posted_datetime'] < cutoff:
            reasons.append(f"投稿日が{days}日超前({r['posted_date']})")
        if not reasons:
            r["raw"] = reel
            passed.append(r)
        else:
            r['rejection_reasons'] = reasons
            rejected.append(r)
    passed.sort(key=lambda x: x['posted_datetime'] or datetime.min, reverse=True)
    return passed, rejected


def research_watchlist_account(username, apify_token, period_days: int = 7):
    """
    指定アカウントの最新リールを Apify で取得し、filter_watchlist_reels で period_days 以内のみ返す。

    Returns:
        通過リールの enriched dict リスト（_enrich_reel 形式、raw キー付き）
    """
    u = str(username or '').strip().lstrip('@')
    if not u:
        return []
    raw_items = fetch_reels_from_profile(u, apify_token, max_results=30)
    if not raw_items:
        print(f"[INFO] ウォッチリスト @{u}: Apify取得0件")
        return []
    passed, rejected = filter_watchlist_reels(raw_items, days=period_days)
    print(f"[OK] ウォッチリスト @{u}: {period_days}日以内 {len(passed)}件 / 除外 {len(rejected)}件")
    return passed


def chase_research(
    all_reels, keywords_used, apify_token,
    max_rounds=3, period_days: int = 14,
    min_views: int = 50000, min_buzz_ratio: Optional[float] = None,
):
    """
    後追いリサーチ: フィルタ通過が5件未満なら追加検索→同じ条件で再フィルタ。
    条件は絶対に緩和しない。

    Returns:
        (passed, rejected, all_reels, rounds_executed, chase_keywords_used)
    """
    if min_buzz_ratio is not None:
        FILTER_CONDITION = f"{period_days}日以内・フォロワー×{min_buzz_ratio}倍以上"
    else:
        FILTER_CONDITION = f"{period_days}日以内・{min_views:,}再生以上"

    passed, rejected = filter_buzz_reels(
        all_reels, days=period_days, min_views=min_views, min_buzz_ratio=min_buzz_ratio,
    )
    
    if len(passed) >= 5:
        return passed, rejected, all_reels, 0, []
    
    seen_urls = set()
    for reel in all_reels:
        url = _extract(reel, ['url','webLink','permalink','shortCode'], '')
        if url:
            seen_urls.add(url)
    
    used = set(keywords_used)
    chase_keywords_all = []
    rounds = 0
    
    for round_num in range(1, max_rounds + 1):
        rounds = round_num
        # 追加キーワードはClaude側が実行時に生成してこの関数に渡す
        # ここでは空リストを返し、Claude側で追加検索→再呼び出しする設計
        # （スクリプト単体では追加キーワード生成はできないため）
        print(f"[CHASE] ラウンド{round_num}: 通過{len(passed)}件 < 5件 → 追加検索が必要")
        break  # Claude側で追加キーワード生成→search_reels→再フィルタのループを回す
    
    return passed, rejected, all_reels, rounds, chase_keywords_all


def chase_filter_after_additional_search(
    all_reels, period_days: int = 14,
    min_views: int = 50000, min_buzz_ratio: Optional[float] = None,
):
    """追加検索後に同じ条件で再フィルタ（条件は絶対に変えない）"""
    passed, rejected = filter_buzz_reels(
        all_reels, days=period_days, min_views=min_views, min_buzz_ratio=min_buzz_ratio,
    )
    passed.sort(key=lambda x: x['buzz_ratio'], reverse=True)
    return passed, rejected


