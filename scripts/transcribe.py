"""
リール音声の文字起こしモジュール（ローカル Whisper・無料）。

依存:
  - openai-whisper（pip 済み）
  - ffmpeg（brew install ffmpeg 済み）

使い方:
  from transcribe import transcribe_reels_inplace
  transcribe_reels_inplace(passed_kw, model_name="medium")
  # → 各 reel に "transcript" キーが追加される

キャッシュ:
  ~/.cache/ig-reel-research/transcripts/<sha1(url)>.txt にテキストを保存。
  同じURLを2回目に処理する時は Whisper を呼ばずキャッシュを返す。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import requests

CACHE_DIR = os.path.expanduser("~/.cache/ig-reel-research/transcripts")
AUDIO_MAX_SECONDS = 90  # 念のため90秒で頭打ち（リールはほぼ60秒以内）
DOWNLOAD_TIMEOUT = 60   # 動画DLの最大秒数
_LOADED_MODEL: Any = None
_LOADED_MODEL_NAME: Optional[str] = None


# ====================================================================
# 動画URL抽出
# ====================================================================

def _extract_video_url(reel: Dict[str, Any]) -> str:
    """Apify の raw item から動画URLらしきものを取り出す。"""
    raw = reel.get("raw") if isinstance(reel.get("raw"), dict) else reel
    if not isinstance(raw, dict):
        return ""
    # よく出てくるフィールド候補
    for k in ("videoUrl", "video_url", "videoURL", "downloadUrl"):
        v = raw.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    # 配列タイプ
    for k in ("videoUrls", "video_urls", "videos"):
        v = raw.get(k)
        if isinstance(v, list) and v:
            for item in v:
                if isinstance(item, str) and item.startswith("http"):
                    return item
                if isinstance(item, dict):
                    u = item.get("url") or item.get("src")
                    if isinstance(u, str) and u.startswith("http"):
                        return u
    return ""


# ====================================================================
# キャッシュ
# ====================================================================

def _cache_path_for(url: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.txt")


def _read_cache(url: str) -> Optional[str]:
    p = _cache_path_for(url)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None
    return None


def _write_cache(url: str, text: str) -> None:
    p = _cache_path_for(url)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text or "")
    except OSError as e:
        print(f"  [WARN] キャッシュ書き込み失敗: {e}", file=sys.stderr)


# ====================================================================
# 動画ダウンロード & 音声抽出
# ====================================================================

def _download_video(url: str, dest_path: str) -> bool:
    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            if r.status_code != 200:
                return False
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 15):
                    if chunk:
                        f.write(chunk)
        return os.path.getsize(dest_path) > 0
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] 動画DL失敗: {e}", file=sys.stderr)
        return False


def _extract_audio(mp4_path: str, wav_path: str) -> bool:
    """ffmpeg で 16kHz mono の wav を作る（Whisper の入力に最適化）。"""
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", mp4_path,
                "-vn",                    # 動画は捨てる
                "-ac", "1",               # モノラル
                "-ar", "16000",           # 16kHz
                "-t", str(AUDIO_MAX_SECONDS),  # 90秒上限
                wav_path,
            ],
            check=True,
            capture_output=True,
        )
        return os.path.exists(wav_path) and os.path.getsize(wav_path) > 0
    except subprocess.CalledProcessError as e:
        print(f"  [WARN] 音声抽出失敗: {e.stderr.decode('utf-8','replace')[:200]}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("  [FATAL] ffmpeg がインストールされていません。`brew install ffmpeg`", file=sys.stderr)
        return False


# ====================================================================
# Whisper 文字起こし
# ====================================================================

def _get_model(model_name: str = "medium"):
    """モデルを1度だけロードして使い回す。"""
    global _LOADED_MODEL, _LOADED_MODEL_NAME
    if _LOADED_MODEL is not None and _LOADED_MODEL_NAME == model_name:
        return _LOADED_MODEL
    import whisper  # 遅延 import（モジュール import 時のロードを避ける）
    print(f"  [INFO] Whisper モデルロード中: {model_name}")
    t0 = time.time()
    _LOADED_MODEL = whisper.load_model(model_name)
    _LOADED_MODEL_NAME = model_name
    print(f"  [INFO] Whisper ロード完了 ({time.time()-t0:.1f}秒)")
    return _LOADED_MODEL


def _transcribe_audio(wav_path: str, model_name: str = "medium") -> str:
    model = _get_model(model_name)
    try:
        result = model.transcribe(wav_path, language="ja", fp16=False)
        return (result.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] Whisper 文字起こし失敗: {e}", file=sys.stderr)
        return ""


# ====================================================================
# 公開関数
# ====================================================================

def transcribe_video_url(url: str, model_name: str = "medium") -> str:
    """単一URLの動画を文字起こしする。キャッシュヒット時は即返す。"""
    if not url or not url.startswith("http"):
        return ""
    cached = _read_cache(url)
    if cached is not None:
        return cached
    with tempfile.TemporaryDirectory(prefix="ig-reel-") as td:
        mp4 = os.path.join(td, "v.mp4")
        wav = os.path.join(td, "a.wav")
        if not _download_video(url, mp4):
            _write_cache(url, "")
            return ""
        if not _extract_audio(mp4, wav):
            _write_cache(url, "")
            return ""
        text = _transcribe_audio(wav, model_name=model_name)
    _write_cache(url, text)
    return text


def transcribe_reels_inplace(
    reels: List[Dict[str, Any]],
    model_name: str = "medium",
) -> None:
    """passed_kw など enriched dict のリストに 'transcript' キーを追加する。

    動画URLが取れなかったリールは transcript="" になる。
    """
    if not reels:
        return
    n = len(reels)
    print(f"[TRANSCRIBE] {n} 件の音声を Whisper で文字起こし開始（model={model_name}）")
    success = 0
    no_url = 0
    cache_hits = 0
    t_start = time.time()
    for i, reel in enumerate(reels, 1):
        video_url = _extract_video_url(reel)
        username = reel.get("username", "?")
        if not video_url:
            reel["transcript"] = ""
            no_url += 1
            print(f"  [{i}/{n}] @{username}: 動画URL取得不可 → スキップ")
            continue
        # キャッシュ判定（読み込み前）
        from_cache = _read_cache(video_url) is not None
        text = transcribe_video_url(video_url, model_name=model_name)
        reel["transcript"] = text
        if text:
            success += 1
            tag = "cache" if from_cache else "new"
            preview = text[:40].replace("\n", " ")
            print(f"  [{i}/{n}] @{username} ({tag}): {preview}...")
            if from_cache:
                cache_hits += 1
        else:
            print(f"  [{i}/{n}] @{username}: 文字起こし結果が空")
    elapsed = time.time() - t_start
    print(
        f"[TRANSCRIBE] 完了: 成功 {success}/{n} 件 / "
        f"動画URL不可 {no_url} 件 / キャッシュヒット {cache_hits} 件 / "
        f"所要 {elapsed:.0f}秒"
    )
