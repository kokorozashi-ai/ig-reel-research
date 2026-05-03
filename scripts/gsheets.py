"""Google Sheets書き込みモジュール（スナップショット形式・転置レイアウト）。

新スキーマ:
- 「使い方ガイド」「ウォッチリスト」（登録シート）の2タブのみ常駐
- リサーチごとにスナップショットタブを新規作成
- 列1=サマリー、列2以降=各リール（転置）
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gspread
from google.oauth2.service_account import Credentials

# ====================================================================
# 設定・定数
# ====================================================================

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
DEFAULT_SPREADSHEET_ID = 'YOUR_SPREADSHEET_ID_HERE'

SHEET_GUIDE = '使い方ガイド'
SHEET_WATCHLIST = 'ウォッチリスト'
PROTECTED_SHEETS = (SHEET_GUIDE, SHEET_WATCHLIST)

# 旧フォーマット（5シート構造時代）のタブ名一覧。新スナップショット化のたびに自動削除。
LEGACY_TAB_NAMES = (
    'リール一覧', 'コンテンツ分析', 'バズ要因スコアリング', 'ディープインサイト',
    'リサーチサマリー', 'リサーチ横断トレンド',
    '過去リサーチリール', '過去バズスコアリング', '過去ディープインサイト',
    'ウォッチリスト リール一覧', 'ウォッチリスト コンテンツ分析', 'ウォッチリスト スコアリング',
    'ウォッチリスト インサイト', 'ウォッチリスト アカウント別サマリー', 'ウォッチリスト サマリー',
    '過去ウォッチリスト リール', '過去ウォッチリスト スコアリング', '過去ウォッチリスト インサイト',
)

# 抽出項目（転置レイアウトの行順序）
ATTRIBUTE_LABELS: List[str] = [
    'リールURL',
    'アカウント名',
    'フォロワー数',
    '再生回数',
    'いいね数',
    'コメント数',
    '投稿日',
    'エンゲージメント率',
    '動画尺',
    'ジャンル',
    'ハッシュタグ',
    'フック',
    '全文文字起こし',
    'キャプション全文',
    'バズ仮説',
    '改善提案',
]
NUMERIC_LABELS = {'フォロワー数', '再生回数', 'いいね数', 'コメント数'}
LONG_TEXT_LABELS = {'全文文字起こし', 'キャプション全文', 'バズ仮説', '改善提案', 'フック', 'ハッシュタグ'}


# ====================================================================
# カラー
# ====================================================================

def _hex_to_rgb(h: str) -> Dict[str, float]:
    h = h.lstrip('#')
    return {
        'red': int(h[0:2], 16) / 255.0,
        'green': int(h[2:4], 16) / 255.0,
        'blue': int(h[4:6], 16) / 255.0,
    }


C_WHITE = {'red': 1.0, 'green': 1.0, 'blue': 1.0}
C_BORDER = {'red': 0.85, 'green': 0.85, 'blue': 0.85}
C_HEADER = _hex_to_rgb('1F4E79')         # ネイビー（属性ラベル列）
C_SUMMARY_HEADER = _hex_to_rgb('2E75B6')  # 中ブルー（サマリー見出し）
C_REEL_HEADER = _hex_to_rgb('5B9BD5')     # 明ブルー（リール列ヘッダー）
C_WATCH_REEL_HEADER = _hex_to_rgb('7B1FA2')  # 紫（ウォッチ中アカウントの列ヘッダー）
C_LABEL_BG = _hex_to_rgb('E8EEF4')        # 薄ブルー（属性ラベル背景）
C_ZEBRA = _hex_to_rgb('F2F7FB')           # 行ゼブラ
C_GUIDE_HEADER = _hex_to_rgb('388E3C')    # ガイドはグリーン


# ====================================================================
# 認証・接続
# ====================================================================

def get_gspread_client():
    creds_path = os.environ.get('GOOGLE_SHEETS_CREDENTIALS') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '')
    if not creds_path or not os.path.exists(creds_path):
        raise FileNotFoundError(
            "認証JSONのパスが見つかりません。"
            " GOOGLE_SHEETS_CREDENTIALS にサービスアカウントJSONの絶対パスを設定してください。"
            f" (現在: {creds_path!r})"
        )
    return gspread.authorize(Credentials.from_service_account_file(creds_path, scopes=SCOPES))


def open_spreadsheet(spreadsheet_id: Optional[str] = None):
    return get_gspread_client().open_by_key(spreadsheet_id or DEFAULT_SPREADSHEET_ID)


# ====================================================================
# スロットリング・リトライ
# ====================================================================

def _write_throttle_pause() -> None:
    raw = (os.environ.get('GSHEETS_WRITE_THROTTLE_SEC') or '').strip()
    if not raw:
        return
    try:
        sec = float(raw)
    except ValueError:
        return
    if sec > 0:
        time.sleep(sec)


def _batch_update_with_retry(sp_or_ws, body: Dict[str, Any]) -> None:
    """429/500/502/503 で再試行。"""
    for attempt in range(8):
        try:
            if hasattr(sp_or_ws, 'spreadsheet_id'):
                sp_or_ws.client.batch_update(sp_or_ws.spreadsheet_id, body)
            else:
                sp_or_ws.batch_update(body)
            return
        except gspread.exceptions.APIError as e:
            msg = str(e)
            if any(c in msg for c in ('429', '500', '502', '503')) and attempt < 7:
                time.sleep(15 + attempt * 30)
                continue
            raise


# ====================================================================
# 値・書式ヘルパー
# ====================================================================

def _col_letter(n: int) -> str:
    r = ''
    while n > 0:
        n -= 1
        r = chr(65 + n % 26) + r
        n //= 26
    return r


def _safe_str(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _int_cell(v: Any) -> int:
    try:
        return int(float(str(v).replace(',', ''))) if v not in (None, '') else 0
    except (ValueError, TypeError):
        return 0


def _format_date_cell(v: Any) -> str:
    """日付を ' プレフィックス付きテキストにする（シリアル化防止）。"""
    s = str(v or '').strip()
    if not s:
        return ''
    if s.startswith("'"):
        return s
    return "'" + s


def _format_engagement(rate: Any) -> str:
    try:
        v = float(rate)
    except (TypeError, ValueError):
        return ''
    return f'{v:.2f}%'


def _format_duration(d: Any) -> str:
    try:
        sec = int(float(str(d).strip()))
    except (TypeError, ValueError):
        return str(d) if d else ''
    if sec <= 0:
        return ''
    if sec < 60:
        return f'{sec}秒'
    return f'{sec // 60}分{sec % 60}秒'


def _hyperlink_formula(url: str, label: Optional[str] = None) -> str:
    u = (url or '').strip().replace('"', '""')
    if not u:
        return ''
    label = (label or url).replace('"', '""')
    return f'=HYPERLINK("{u}","{label}")'


def _hyperlink_username_formula(username: str) -> str:
    u = str(username or '').strip().lstrip('@')
    if not u:
        return ''
    url = f'https://www.instagram.com/{u}/'
    return f'=HYPERLINK("{url}","{u}")'


def _extract_username_from_cell(s: Any) -> str:
    t = str(s or '').strip()
    if not t:
        return ''
    if t.upper().startswith('=HYPERLINK'):
        m = re.search(r'HYPERLINK\s*\(\s*"([^"]+)"\s*,\s*"([^"]*)"', t, re.I)
        if m:
            lab = (m.group(2) or '').strip()
            if lab:
                return lab.lstrip('@')
            url = m.group(1)
            if 'instagram.com/' in url:
                return url.split('instagram.com/')[-1].split('/')[0].lstrip('@')
        return ''
    return t.lstrip('@')


def _format_hashtags(tags: Any) -> str:
    if isinstance(tags, list):
        return ' '.join(f'#{t.lstrip("#")}' for t in tags if t)
    if isinstance(tags, str):
        return tags
    return ''


# ====================================================================
# シート構造ヘルパー
# ====================================================================

def _try_worksheet(sp, title: str):
    try:
        return sp.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return None


def _base_cell_format() -> Dict[str, Any]:
    return {
        'wrapStrategy': 'WRAP',
        'verticalAlignment': 'TOP',
        'borders': {
            'top': {'style': 'SOLID', 'width': 1, 'color': C_BORDER},
            'bottom': {'style': 'SOLID', 'width': 1, 'color': C_BORDER},
            'left': {'style': 'SOLID', 'width': 1, 'color': C_BORDER},
            'right': {'style': 'SOLID', 'width': 1, 'color': C_BORDER},
        },
    }


def _header_cell_format(background: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    f = _base_cell_format()
    f['backgroundColor'] = background or C_HEADER
    f['horizontalAlignment'] = 'CENTER'
    f['textFormat'] = {'foregroundColor': C_WHITE, 'bold': True, 'fontSize': 11}
    return f


def _label_cell_format() -> Dict[str, Any]:
    f = _base_cell_format()
    f['backgroundColor'] = C_LABEL_BG
    f['textFormat'] = {'bold': True, 'fontSize': 10}
    return f


def _summary_header_cell_format() -> Dict[str, Any]:
    f = _base_cell_format()
    f['backgroundColor'] = C_SUMMARY_HEADER
    f['horizontalAlignment'] = 'LEFT'
    f['textFormat'] = {'foregroundColor': C_WHITE, 'bold': True, 'fontSize': 12}
    return f


def _repeat_cell(ws, sr: int, sc: int, er: int, ec: int, fmt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'repeatCell': {
            'range': {
                'sheetId': ws.id,
                'startRowIndex': sr - 1,
                'endRowIndex': er,
                'startColumnIndex': sc - 1,
                'endColumnIndex': ec,
            },
            'cell': {'userEnteredFormat': fmt},
            'fields': 'userEnteredFormat',
        }
    }


def _set_column_widths(ws, widths: Sequence[Optional[int]]) -> None:
    if not widths:
        return
    requests = []
    for i, px in enumerate(widths):
        if not px or px <= 0:
            continue
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': i, 'endIndex': i + 1},
                'properties': {'pixelSize': int(px)},
                'fields': 'pixelSize',
            }
        })
    if requests:
        _batch_update_with_retry(ws, {'requests': requests})


def _set_row_heights(ws, row_heights: Dict[int, int]) -> None:
    """{row_1based: pixel_height} で個別に行高を指定。"""
    if not row_heights:
        return
    requests = []
    for r, px in row_heights.items():
        requests.append({
            'updateDimensionProperties': {
                'range': {'sheetId': ws.id, 'dimension': 'ROWS', 'startIndex': r - 1, 'endIndex': r},
                'properties': {'pixelSize': int(px)},
                'fields': 'pixelSize',
            }
        })
    if requests:
        _batch_update_with_retry(ws, {'requests': requests})


def _freeze(ws, rows: int = 0, cols: int = 0) -> None:
    body = {
        'requests': [{
            'updateSheetProperties': {
                'properties': {
                    'sheetId': ws.id,
                    'gridProperties': {'frozenRowCount': rows, 'frozenColumnCount': cols},
                },
                'fields': 'gridProperties.frozenRowCount,gridProperties.frozenColumnCount',
            }
        }]
    }
    _batch_update_with_retry(ws, body)


# ====================================================================
# ウォッチリスト登録シート（手動運用）
# ====================================================================

WATCHLIST_HEADERS = ['No.', 'アカウント名（Instagramユーザー名）', 'ジャンル', '注目ポイント', '登録日', 'メモ']
WATCHLIST_COL_WIDTHS = [40, 180, 120, 300, 110, 300]


def _ensure_watchlist_sheet(sp) -> None:
    ws = _try_worksheet(sp, SHEET_WATCHLIST)
    if ws:
        return
    ws = sp.add_worksheet(title=SHEET_WATCHLIST, rows=200, cols=8)
    ws.update(range_name='A1:F1', values=[WATCHLIST_HEADERS], value_input_option='USER_ENTERED')
    _set_column_widths(ws, WATCHLIST_COL_WIDTHS)
    _batch_update_with_retry(ws, {
        'requests': [_repeat_cell(ws, 1, 1, 1, 6, _header_cell_format())]
    })
    _freeze(ws, rows=1)


def get_watchlist(spreadsheet_id: Optional[str] = None) -> List[str]:
    """ウォッチリスト登録シートからアカウント名（@なし）を取得。"""
    sp = open_spreadsheet(spreadsheet_id)
    _ensure_watchlist_sheet(sp)
    ws = _try_worksheet(sp, SHEET_WATCHLIST)
    if not ws:
        return []
    vals = ws.get_all_values()
    if len(vals) < 2:
        return []
    out: List[str] = []
    for row in vals[1:]:
        u = _extract_username_from_cell(row[1] if len(row) > 1 else '')
        if u and u not in out:
            out.append(u)
    return out


def get_watchlist_ordered_meta(sp) -> List[Dict[str, str]]:
    """No.順で {username, genre, focus} を返す（ウォッチリストモード用）。"""
    ws = _try_worksheet(sp, SHEET_WATCHLIST)
    if not ws:
        return []
    vals = ws.get_all_values()
    if len(vals) < 2:
        return []
    h = vals[0]

    def _idx(name: str) -> int:
        try:
            return h.index(name)
        except ValueError:
            return -1

    i_user = _idx('アカウント名（Instagramユーザー名）')
    i_no = _idx('No.')
    i_genre = _idx('ジャンル')
    i_focus = _idx('注目ポイント')

    rows_meta: List[Tuple[int, Dict[str, str]]] = []
    for row in vals[1:]:
        u = _extract_username_from_cell(row[i_user] if 0 <= i_user < len(row) else '')
        if not u:
            continue
        try:
            no = int(float(str(row[i_no] if 0 <= i_no < len(row) else '').strip()))
        except (ValueError, TypeError):
            no = 10**9
        rows_meta.append((no, {
            'username': u,
            'genre': (row[i_genre] if 0 <= i_genre < len(row) else '').strip(),
            'focus': (row[i_focus] if 0 <= i_focus < len(row) else '').strip(),
        }))
    rows_meta.sort(key=lambda x: (x[0], x[1]['username']))
    return [m[1] for m in rows_meta]


def add_to_watchlist(
    usernames: Sequence[str],
    spreadsheet_id: Optional[str] = None,
    genre: str = '',
    memo: str = '',
) -> Tuple[int, List[str], List[str]]:
    """アカウント名を既存ウォッチリストに追加（重複は無視）。

    Returns:
        (added, already_existing, invalid)
    """
    sp = open_spreadsheet(spreadsheet_id)
    _ensure_watchlist_sheet(sp)
    ws = _try_worksheet(sp, SHEET_WATCHLIST)
    existing = {u.lower() for u in get_watchlist(spreadsheet_id)}

    today = date.today().strftime('%Y-%m-%d')
    new_rows: List[List[Any]] = []
    invalid: List[str] = []
    already: List[str] = []
    seen_in_batch: set = set()
    next_no = len(existing) + 1
    for raw in usernames:
        u = str(raw or '').strip().lstrip('@＠')
        if not u or not re.match(r'^[A-Za-z0-9._]{1,30}$', u):
            invalid.append(str(raw))
            continue
        key = u.lower()
        if key in existing or key in seen_in_batch:
            already.append(u)
            continue
        seen_in_batch.add(key)
        new_rows.append([
            next_no,
            _hyperlink_username_formula(u),
            genre,
            memo,
            "'" + today,
            '',
        ])
        next_no += 1

    if new_rows:
        ws.append_rows(new_rows, value_input_option='USER_ENTERED')
    return (len(new_rows), already, invalid)


# ====================================================================
# クリーンアップ・タブ名解決
# ====================================================================

def cleanup_legacy_tabs(spreadsheet_id: Optional[str] = None) -> int:
    """旧フォーマット（5シート構造時代）のタブのみを削除。新スナップショットは保持。

    各リサーチ実行時に自動呼び出しすることで、旧形式から新形式への移行を自動化する。

    Returns: 削除したタブ数
    """
    sp = open_spreadsheet(spreadsheet_id)
    _ensure_watchlist_sheet(sp)
    deleted = 0
    requests: List[Dict[str, Any]] = []
    for ws in sp.worksheets():
        if ws.title in LEGACY_TAB_NAMES:
            requests.append({'deleteSheet': {'sheetId': ws.id}})
            deleted += 1
    if requests:
        _batch_update_with_retry(sp, {'requests': requests})
    return deleted


def cleanup_all_except_defaults(spreadsheet_id: Optional[str] = None) -> int:
    """使い方ガイドとウォッチリスト以外のすべてのタブを削除（手動リセット用）。

    通常運用では呼ばない。リサーチ履歴をまっさらにしたい時だけ使う。

    Returns: 削除したタブ数
    """
    sp = open_spreadsheet(spreadsheet_id)
    _ensure_watchlist_sheet(sp)
    deleted = 0
    requests: List[Dict[str, Any]] = []
    for ws in sp.worksheets():
        if ws.title in PROTECTED_SHEETS:
            continue
        requests.append({'deleteSheet': {'sheetId': ws.id}})
        deleted += 1
    if requests:
        _batch_update_with_retry(sp, {'requests': requests})
    return deleted


def _list_existing_titles(sp) -> List[str]:
    return [ws.title for ws in sp.worksheets()]


def resolve_snapshot_tab_name(sp, keywords: Sequence[str]) -> str:
    """通常リサーチのタブ名を生成。

    - 単一: 'ダイエット'
    - 複数: 'ダイエット_食事管理'
    - 衝突時: 'ダイエット 20260510'
    """
    base = '_'.join(str(k).strip() for k in keywords if str(k).strip())
    base = base[:80]  # シート名上限100文字に余裕を持たせる
    if not base:
        base = 'リサーチ'
    titles = set(_list_existing_titles(sp))
    if base not in titles:
        return base
    today = date.today().strftime('%Y%m%d')
    candidate = f'{base} {today}'[:99]
    if candidate not in titles:
        return candidate
    # それでも衝突する場合は連番
    for i in range(2, 100):
        c = f'{base} {today} ({i})'[:99]
        if c not in titles:
            return c
    raise RuntimeError('シート名の生成に失敗しました（衝突が多すぎ）')


def resolve_watchlist_tab_name(sp) -> str:
    """ウォッチリスト分析タブ名: 'ウォッチリスト 20260503'"""
    today = date.today().strftime('%Y%m%d')
    base = f'ウォッチリスト {today}'
    titles = set(_list_existing_titles(sp))
    if base not in titles:
        return base
    for i in range(2, 100):
        c = f'{base} ({i})'
        if c not in titles:
            return c
    raise RuntimeError('シート名の生成に失敗しました')


# ====================================================================
# スナップショットシート作成（転置レイアウト）
# ====================================================================

def _build_summary_text(summary: Dict[str, Any]) -> str:
    """summary dict から行1バナー用の改行入り単一テキストを生成。"""

    def _g(*keys: str) -> str:
        for k in keys:
            v = summary.get(k)
            if v not in (None, '', []):
                return _safe_str(v)
        return ''

    parts: List[str] = ['【リサーチサマリー】']

    head_bits: List[str] = []
    if _g('date'):
        head_bits.append(f"実施日: {_g('date')}")
    if _g('keyword'):
        head_bits.append(f"キーワード: {_g('keyword')}")
    if _g('period'):
        head_bits.append(f"期間: {_g('period')}")
    if _g('total'):
        head_bits.append(f"収集: {_g('total')}件")
    if _g('filtered'):
        head_bits.append(f"分析対象: {_g('filtered')}件")
    if head_bits:
        parts.append(' ｜ '.join(head_bits))
    if _g('condition_label', 'filter_condition'):
        parts.append(f"適用条件: {_g('condition_label', 'filter_condition')}")

    rank_lines: List[str] = []
    for i in (1, 2, 3):
        rk = summary.get(f'rank{i}') or {}
        if isinstance(rk, dict):
            un = (rk.get('username') or '').strip()
            why = (rk.get('why_rank') or '').strip()
            if un:
                txt = f'{i}位: @{un}'
                if why:
                    txt += f' — {why[:140]}'
                rank_lines.append(txt)
    if rank_lines:
        parts.append('▼ 再生数TOP3\n' + '\n'.join(rank_lines))

    trend_bits: List[str] = []
    if _g('trend_genre', 'trend_analysis'):
        trend_bits.append(f"ジャンル動向: {_g('trend_genre', 'trend_analysis')}")
    if _g('trend_hook_top3'):
        trend_bits.append(f"フック傾向: {_g('trend_hook_top3')}")
    if _g('trend_structure_top3'):
        trend_bits.append(f"構成パターン: {_g('trend_structure_top3')}")
    if _g('trend_cta'):
        trend_bits.append(f"CTA手法: {_g('trend_cta')}")
    if _g('trend_user_demand'):
        trend_bits.append(f"ユーザー需要: {_g('trend_user_demand')}")
    if trend_bits:
        parts.append('▼ トレンド分析\n' + '\n'.join(trend_bits))

    if _g('common_factors'):
        parts.append(f"▼ 共通バズ要因\n{_g('common_factors')}")

    actions = summary.get('actions')
    if actions:
        act_lines: List[str] = []
        if isinstance(actions, list):
            for i, a in enumerate(actions, 1):
                if isinstance(a, dict):
                    pr = a.get('priority', i)
                    w = a.get('what', '')
                    h = a.get('how', '')
                    ex = a.get('expected', '')
                    act_lines.append(f"【{pr}】{w} ／ どう: {h} ／ 期待: {ex}")
                else:
                    act_lines.append(f"{i}. {a}")
        elif isinstance(actions, str) and actions.strip():
            act_lines = [actions]
        if act_lines:
            parts.append('▼ アクション（優先度順）\n' + '\n'.join(act_lines))

    return '\n\n'.join(parts)


def create_snapshot_sheet(
    sp,
    tab_name: str,
    summary: Dict[str, Any],
    reels: List[Dict[str, Any]],
    contents: List[Dict[str, Any]],
    insights: List[Dict[str, Any]],
    watchlist_usernames: Optional[Sequence[str]] = None,
) -> str:
    """新規スナップショットタブを作成し、転置レイアウトで全データを書き込む。

    レイアウト:
      行1: リサーチサマリー（A1:lastCol1 を結合した単一バナー、改行入り）
      行2: 列ヘッダー（A2='属性'、B2以降='リール1', 'リール2', …、ウォッチ中アカウントは '★ リール'＋紫背景）
      行3〜: 各属性ラベル（A列）+ 各リールデータ（B列以降）
      固定: 行1〜2、列A

    Returns: 作成したシートのエディタURL
    """
    watchlist_set = {u.lower() for u in (watchlist_usernames or [])}

    summary_text = _build_summary_text(summary)
    n_attr_rows = len(ATTRIBUTE_LABELS)
    n_reels = len(reels)

    HEADER_ROW = 2
    DATA_START_ROW = 3
    total_rows = DATA_START_ROW - 1 + n_attr_rows  # 1(summary) + 1(header) + N attrs
    total_cols = 1 + n_reels                       # 1(label) + N(reels)

    # シート作成
    ws = sp.add_worksheet(title=tab_name, rows=max(total_rows + 5, 25), cols=max(total_cols + 2, 8))

    # ===== マトリクス組み立て =====
    matrix: List[List[Any]] = [[''] * total_cols for _ in range(total_rows)]

    # 行1: A1=ラベル、B1=サマリー本文（B1:lastCol1 を結合する）
    # ※ 結合セルは列A（凍結列）と重なってはいけない（Sheets制約）。
    matrix[0][0] = '【リサーチサマリー】'
    if total_cols >= 2:
        matrix[0][1] = summary_text
    else:
        matrix[0][0] = '【リサーチサマリー】\n' + summary_text

    # 行2: ヘッダー
    matrix[1][0] = '属性'
    star_columns: List[int] = []
    for i, reel in enumerate(reels):
        col_1based = 2 + i
        username = str(reel.get('username') or '').strip().lstrip('@')
        is_watch = username.lower() in watchlist_set if username else False
        if is_watch:
            matrix[1][col_1based - 1] = f'★ リール{i + 1}'
            star_columns.append(col_1based)
        else:
            matrix[1][col_1based - 1] = f'リール{i + 1}'

    # 行3〜: 属性ラベル + 各リールデータ
    for j, lab in enumerate(ATTRIBUTE_LABELS):
        matrix[DATA_START_ROW - 1 + j][0] = lab
    for i, reel in enumerate(reels):
        col = 2 + i
        c = contents[i] if i < len(contents) else {}
        ins = insights[i] if i < len(insights) else {}

        username = str(reel.get('username') or '').strip().lstrip('@')
        url = str(reel.get('url') or '').strip()
        followers = _int_cell(reel.get('followers'))
        views = _int_cell(reel.get('views'))
        likes = _int_cell(reel.get('likes'))
        comments = _int_cell(reel.get('comments'))
        posted = str(reel.get('posted_date') or '')
        eng = reel.get('engagement_rate')
        duration = reel.get('duration')
        genre = c.get('genre', '')
        hashtags = _format_hashtags(reel.get('hashtags') or c.get('hashtags'))
        hook = c.get('hook', '')
        transcript = reel.get('transcript') or ''
        caption = reel.get('caption') or ''
        hypothesis = ins.get('hypothesis') or ''
        improvement = ins.get('improvement') or ''

        col_values: Dict[str, Any] = {
            'リールURL': _hyperlink_formula(url, label=url) if url else '',
            'アカウント名': _hyperlink_username_formula(username) if username else '',
            'フォロワー数': followers,
            '再生回数': views,
            'いいね数': likes,
            'コメント数': comments,
            '投稿日': _format_date_cell(posted),
            'エンゲージメント率': _format_engagement(eng),
            '動画尺': _format_duration(duration),
            'ジャンル': genre,
            'ハッシュタグ': hashtags,
            'フック': hook,
            '全文文字起こし': transcript,
            'キャプション全文': caption,
            'バズ仮説': hypothesis,
            '改善提案': improvement,
        }
        for j, lab in enumerate(ATTRIBUTE_LABELS):
            matrix[DATA_START_ROW - 1 + j][col - 1] = col_values.get(lab, '')

    # シートに書き込み
    last_col_letter = _col_letter(total_cols)
    rng = f'A1:{last_col_letter}{total_rows}'
    ws.update(range_name=rng, values=matrix, value_input_option='USER_ENTERED')
    _write_throttle_pause()

    # ===== 書式 =====
    requests: List[Dict[str, Any]] = []

    # 全体ベース罫線
    requests.append(_repeat_cell(ws, 1, 1, total_rows, total_cols, _base_cell_format()))

    # 行1: A1（ラベル）と B1:lastCol1（サマリー本文・結合）。
    # 列Aを凍結する都合上、結合範囲は B1 から開始する必要がある（Sheets制約）。
    if total_cols >= 2:
        requests.append({
            'mergeCells': {
                'range': {
                    'sheetId': ws.id,
                    'startRowIndex': 0,
                    'endRowIndex': 1,
                    'startColumnIndex': 1,  # B列から
                    'endColumnIndex': total_cols,
                },
                'mergeType': 'MERGE_ALL',
            }
        })
    # A1: ラベル（縦書きっぽいヘッダー、ネイビー）
    a1_fmt = _summary_header_cell_format()
    a1_fmt['horizontalAlignment'] = 'CENTER'
    a1_fmt['verticalAlignment'] = 'MIDDLE'
    requests.append(_repeat_cell(ws, 1, 1, 1, 1, a1_fmt))
    # B1: サマリー本文バナー
    if total_cols >= 2:
        summary_banner_fmt = _base_cell_format()
        summary_banner_fmt['backgroundColor'] = C_SUMMARY_HEADER
        summary_banner_fmt['horizontalAlignment'] = 'LEFT'
        summary_banner_fmt['textFormat'] = {'foregroundColor': C_WHITE, 'bold': False, 'fontSize': 10}
        requests.append(_repeat_cell(ws, 1, 2, 1, total_cols, summary_banner_fmt))

    # 行2: 列ヘッダー
    requests.append(_repeat_cell(ws, HEADER_ROW, 1, HEADER_ROW, total_cols, _header_cell_format(C_REEL_HEADER)))
    # A2（属性ヘッダー）はネイビー（C_HEADER）で上書き
    requests.append(_repeat_cell(ws, HEADER_ROW, 1, HEADER_ROW, 1, _header_cell_format(C_HEADER)))
    # ★ ウォッチ中の列ヘッダーは紫
    for col in star_columns:
        requests.append(_repeat_cell(ws, HEADER_ROW, col, HEADER_ROW, col, _header_cell_format(C_WATCH_REEL_HEADER)))

    # 列A（属性ラベル）データ部分
    requests.append(
        _repeat_cell(ws, DATA_START_ROW, 1, DATA_START_ROW + n_attr_rows - 1, 1, _label_cell_format())
    )

    # 数値行（フォロワー数・再生回数・いいね数・コメント数）：右寄せ・カンマ区切り
    if n_reels > 0:
        num_fmt = _base_cell_format()
        num_fmt['horizontalAlignment'] = 'RIGHT'
        num_fmt['numberFormat'] = {'type': 'NUMBER', 'pattern': '#,##0'}
        for j, lab in enumerate(ATTRIBUTE_LABELS):
            if lab in NUMERIC_LABELS:
                row = DATA_START_ROW + j
                requests.append(_repeat_cell(ws, row, 2, row, total_cols, num_fmt))

    # 列幅: A=180px（属性ラベル幅）、リール列=280px
    widths: List[Optional[int]] = [200] + [280] * n_reels
    _set_column_widths(ws, widths)

    # 行高: 行1（サマリー）は大きめ、長文行も少し広げる
    row_heights: Dict[int, int] = {1: 240}
    for j, lab in enumerate(ATTRIBUTE_LABELS):
        if lab in LONG_TEXT_LABELS:
            row_heights[DATA_START_ROW + j] = 110
    _set_row_heights(ws, row_heights)

    # 書式適用
    if requests:
        _batch_update_with_retry(ws, {'requests': requests})

    # 固定: 行1〜2、列A
    _freeze(ws, rows=2, cols=1)

    return f'https://docs.google.com/spreadsheets/d/{sp.id}/edit#gid={ws.id}'


# ====================================================================
# 使い方ガイド（毎回再生成）
# ====================================================================

def write_usage_guide(sp) -> None:
    ws = _try_worksheet(sp, SHEET_GUIDE)
    if ws:
        ws.clear()
    else:
        ws = sp.add_worksheet(title=SHEET_GUIDE, rows=200, cols=3)

    rows: List[List[str]] = []
    rows.append(['Instagram リール バズリサーチ｜使い方ガイド', '', ''])
    rows.append(['', '', ''])

    rows.append(['## このスプシで何ができるか', '', ''])
    rows.append(['キーワードリサーチ', '指定キーワードで直近期間のバズリールを15件まで分析。タブ「キーワード名」が新規追加される', ''])
    rows.append(['ウォッチリストリサーチ', '事前登録した特定アカウント（ウォッチリストタブ参照）の最新リールを分析。タブ「ウォッチリスト YYYYMMDD」が新規追加される', ''])
    rows.append(['', '', ''])

    rows.append(['## タブの種類と命名規則', '', ''])
    rows.append(['使い方ガイド', 'このシート。常駐・毎回再生成', ''])
    rows.append(['ウォッチリスト', '参考にしたいアカウントの登録シート（手動運用）。常駐・絶対にクリアされない', ''])
    rows.append(['<キーワード名>', '通常リサーチの結果スナップショット。例: 「ダイエット」「ダイエット_食事管理」（複数キーワードはアンダースコア連結）', ''])
    rows.append(['<キーワード名> YYYYMMDD', '同名キーワードの2回目以降は日付サフィックスが付く', ''])
    rows.append(['ウォッチリスト YYYYMMDD', 'ウォッチリストモードの結果スナップショット', ''])
    rows.append(['', '', ''])

    rows.append(['## スナップショット内のレイアウト', '', ''])
    rows.append(['列A', 'リサーチサマリー（実施日・キーワード・期間・通過数・トレンド・共通バズ要因・アクション） + 属性ラベル', ''])
    rows.append(['列B以降', '各リールのデータ。1列=1リール。バズ倍率順', ''])
    rows.append(['1行目', 'リールヘッダー。ウォッチリスト登録済みアカウントは ★（紫背景）で表示', ''])
    rows.append(['', '', ''])

    rows.append(['## リール1件あたりの抽出項目（16項目）', '', ''])
    for i, lab in enumerate(ATTRIBUTE_LABELS, 1):
        if lab == 'リールURL':
            rows.append([f'{i}. {lab}', 'リールへの直リンク（クリックでInstagramへ）', ''])
        elif lab == 'アカウント名':
            rows.append([f'{i}. {lab}', 'クリックでInstagramのプロフィールへ', ''])
        elif lab == 'フォロワー数':
            rows.append([f'{i}. {lab}', '投稿時点のフォロワー数（カンマ区切り）', ''])
        elif lab == '再生回数':
            rows.append([f'{i}. {lab}', '再生数（カンマ区切り）', ''])
        elif lab == 'いいね数':
            rows.append([f'{i}. {lab}', 'いいね数（カンマ区切り）', ''])
        elif lab == 'コメント数':
            rows.append([f'{i}. {lab}', 'コメント数（カンマ区切り）', ''])
        elif lab == '投稿日':
            rows.append([f'{i}. {lab}', 'YYYY-MM-DD', ''])
        elif lab == 'エンゲージメント率':
            rows.append([f'{i}. {lab}', '（いいね＋コメント）÷再生数 × 100。0.5〜10%が正常範囲', ''])
        elif lab == '動画尺':
            rows.append([f'{i}. {lab}', '○秒 または ○分○秒', ''])
        elif lab == 'ジャンル':
            rows.append([f'{i}. {lab}', 'OpenAIによる自動分類', ''])
        elif lab == 'ハッシュタグ':
            rows.append([f'{i}. {lab}', '使われていたタグ全列挙', ''])
        elif lab == 'フック':
            rows.append([f'{i}. {lab}', '冒頭3秒のフック要約。Whisper文字起こしを最優先で判定', ''])
        elif lab == '全文文字起こし':
            rows.append([f'{i}. {lab}', 'ローカル Whisper（medium）で動画音声を自動文字起こし', ''])
        elif lab == 'キャプション全文':
            rows.append([f'{i}. {lab}', 'Apifyから取得したキャプション原文', ''])
        elif lab == 'バズ仮説':
            rows.append([f'{i}. {lab}', 'なぜ伸びたかをOpenAIが言語化', ''])
        elif lab == '改善提案':
            rows.append([f'{i}. {lab}', 'このリールをさらに伸ばすには。応用検討用', ''])
    rows.append(['', '', ''])

    rows.append(['## 期間プリセット（4択）', '', ''])
    rows.append(['7日', '--period 1w', ''])
    rows.append(['14日（既定）', '--period 2w', ''])
    rows.append(['30日', '--period 1m', ''])
    rows.append(['90日', '--period 3m', ''])
    rows.append(['', '', ''])

    rows.append(['## フィルタ条件（絶対に緩和されない）', '', ''])
    rows.append(['通常リサーチ', '指定期間以内 ＋ 再生条件（絶対値 or フォロワー比のどちらか1つ）', ''])
    rows.append(['  絶対値モード（既定）', '--min-views N（既定 50000）。views ≥ N で判定。フォロワー比は判定しない', ''])
    rows.append(['  フォロワー比モード', '--buzz-ratio X。views ≥ followers × X で判定。絶対値は無視される', ''])
    rows.append(['  両者の同時指定', '不可。どちらか1つを選ぶ', ''])
    rows.append(['ウォッチリスト', '指定期間以内のリール全件（再生数・フォロワー比は無視）', ''])
    rows.append(['分析上限', 'バズ倍率上位15件まで（IG_REEL_TOP_N で変更可）', ''])
    rows.append(['', '', ''])

    rows.append(['## ウォッチリストの運用', '', ''])
    rows.append(['追加方法1: シート編集', '「ウォッチリスト」タブにアカウント名・ジャンル・注目ポイントを追記', ''])
    rows.append(['追加方法2: CLI', 'python3 add_to_watchlist.py @user1 @user2 --genre フィットネス --memo "メモ"', ''])
    rows.append(['実行', 'python3 run_watchlist_research.py [--period 1w|2w|1m|3m|6m]', ''])
    rows.append(['★マーカー', 'キーワードリサーチでウォッチリスト登録済みアカウントが含まれていると、リールヘッダーに★（紫背景）が表示される', ''])
    rows.append(['', '', ''])

    rows.append(['## 使用ツール', '', ''])
    rows.append(['Apify (Instagram スクレイパー)', 'リール・プロフィールの収集', ''])
    rows.append(['Whisper (medium・ローカル実行)', '音声文字起こし。コスト$0', ''])
    rows.append(['OpenAI (gpt-4o-mini)', 'コンテンツ分析・スコアリング・サマリー生成', ''])
    rows.append(['Google Sheets API', 'このスプシへの書き込み', ''])

    n = len(rows)
    ws.update(range_name=f'A1:C{n}', values=rows, value_input_option='USER_ENTERED')

    # 書式
    requests: List[Dict[str, Any]] = []
    requests.append(_repeat_cell(ws, 1, 1, 1, 3, _header_cell_format(C_GUIDE_HEADER)))
    # ## 見出し行を強調
    title_fmt = _base_cell_format()
    title_fmt['backgroundColor'] = C_LABEL_BG
    title_fmt['textFormat'] = {'bold': True, 'fontSize': 11}
    for i, row in enumerate(rows, 1):
        if row and isinstance(row[0], str) and row[0].startswith('##'):
            requests.append(_repeat_cell(ws, i, 1, i, 3, title_fmt))
    # 全体の罫線・wrap
    base = _base_cell_format()
    requests.append(_repeat_cell(ws, 2, 1, n, 3, base))
    if requests:
        _batch_update_with_retry(ws, {'requests': requests})

    _set_column_widths(ws, [200, 480, 100])
    _freeze(ws, rows=1)


# ====================================================================
# ウォッチリストリサーチ用の収集関数
# ====================================================================

def research_all_watchlist(
    apify_token: str,
    spreadsheet_id: Optional[str] = None,
    period_days: int = 7,
) -> List[Dict[str, Any]]:
    """全ウォッチリストアカウントの直近 period_days 以内のリールを収集。"""
    from pipeline import (  # 遅延 import
        enrich_watchlist_reels_followers_from_profiles,
        research_watchlist_account,
    )
    usernames = get_watchlist(spreadsheet_id)
    out: List[Dict[str, Any]] = []
    seen_urls: set = set()
    for uname in usernames:
        for item in research_watchlist_account(uname, apify_token, period_days=period_days):
            url = str(item.get('url') or '').strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                out.append(item)
    enrich_watchlist_reels_followers_from_profiles(out, apify_token)
    print(f"[INFO] research_all_watchlist: 合計 {len(out)} 件 / アカウント {len(usernames)} 個 / 期間 {period_days}日")
    return out
