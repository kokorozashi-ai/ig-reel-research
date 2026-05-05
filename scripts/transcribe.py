"""
リール動画の総合解析モジュール（音声・カット・シーン・テロップ）。

機能:
  1. 音声文字起こし（ローカル Whisper・無料）
  2. 動画カット数検出（PySceneDetect・無料）
  3. シーン構成の言語化（gpt-4o-mini-vision・有料・約$0.0015/カット）
  4. テロップOCR（vision と同時実行・追加コストなし）

依存:
  - openai-whisper (`pip install openai-whisper`)
  - scenedetect[opencv] (`pip install scenedetect[opencv]`)
  - ffmpeg (`brew install ffmpeg`)

使い方:
  from transcribe import transcribe_reels_inplace
  transcribe_reels_inplace(reels)
  # → 各 reel に下記キーが追加される:
  #   - "transcript": 音声文字起こし
  #   - "cut_count": カット数
  #   - "scene_breakdown": [{start, end, description, telop}, ...] のリスト
  #   - "telop_full": 全テロップを連結したテキスト

キャッシュ:
  ~/.cache/ig-reel-research/analyses/<sha1(url)>.json に全結果を保存。
  同じURLを2回目に処理する時は API/Whisper を呼ばずキャッシュを返す。

環境変数:
  IG_REEL_VISION_MODEL    : vision分析モデル（既定 gpt-4o-mini）
  IG_REEL_DISABLE_VISION  : 1 で vision 解析を無効化（カット数は取る）
  IG_REEL_DISABLE_SCENES  : 1 でカット検出も無効化（音声のみ）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

CACHE_DIR_LEGACY = os.path.expanduser("~/.cache/ig-reel-research/transcripts")
CACHE_DIR_FULL = os.path.expanduser("~/.cache/ig-reel-research/analyses")
AUDIO_MAX_SECONDS = 90
DOWNLOAD_TIMEOUT = 60
SCENE_THRESHOLD = 27.0  # PySceneDetect の感度（27は標準・大きいほど鈍感）

_LOADED_WHISPER: Any = None
_LOADED_WHISPER_NAME: Optional[str] = None
_VISION_CLIENT: Any = None


# ====================================================================
# 動画URL抽出
# ====================================================================

def _extract_video_url(reel: Dict[str, Any]) -> str:
    """Apify の raw item から動画URLらしきものを取り出す。"""
    raw = reel.get("raw") if isinstance(reel.get("raw"), dict) else reel
    if not isinstance(raw, dict):
        return ""
    for k in ("videoUrl", "video_url", "videoURL", "downloadUrl"):
        v = raw.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
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

def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _full_cache_path(url: str) -> str:
    os.makedirs(CACHE_DIR_FULL, exist_ok=True)
    return os.path.join(CACHE_DIR_FULL, f"{_url_hash(url)}.json")


def _legacy_transcript_cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR_LEGACY, f"{_url_hash(url)}.txt")


def _read_full_cache(url: str) -> Optional[Dict[str, Any]]:
    p = _full_cache_path(url)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _read_legacy_transcript(url: str) -> Optional[str]:
    p = _legacy_transcript_cache_path(url)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _write_full_cache(url: str, data: Dict[str, Any]) -> None:
    p = _full_cache_path(url)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", mp4_path,
                "-vn", "-ac", "1", "-ar", "16000",
                "-t", str(AUDIO_MAX_SECONDS),
                wav_path,
            ],
            check=True, capture_output=True,
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

def _get_whisper(model_name: str = "medium"):
    global _LOADED_WHISPER, _LOADED_WHISPER_NAME
    if _LOADED_WHISPER is not None and _LOADED_WHISPER_NAME == model_name:
        return _LOADED_WHISPER
    import whisper
    print(f"  [INFO] Whisper モデルロード中: {model_name}")
    t0 = time.time()
    _LOADED_WHISPER = whisper.load_model(model_name)
    _LOADED_WHISPER_NAME = model_name
    print(f"  [INFO] Whisper ロード完了 ({time.time()-t0:.1f}秒)")
    return _LOADED_WHISPER


def _transcribe_audio(wav_path: str, model_name: str = "medium") -> str:
    model = _get_whisper(model_name)
    try:
        result = model.transcribe(wav_path, language="ja", fp16=False)
        return (result.get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] Whisper 文字起こし失敗: {e}", file=sys.stderr)
        return ""


# ====================================================================
# シーン検出（PySceneDetect）
# ====================================================================

def _detect_scenes(mp4_path: str) -> List[Tuple[float, float]]:
    """シーン境界を検出して (start_sec, end_sec) のリストを返す。

    検出失敗時 or シーン未分割時は [(0.0, total_duration)] を返す。
    """
    try:
        from scenedetect import detect, ContentDetector
        scene_list = detect(mp4_path, ContentDetector(threshold=SCENE_THRESHOLD))
        if not scene_list:
            # 全体を1シーンとして扱う
            duration = _get_video_duration(mp4_path)
            return [(0.0, duration)] if duration > 0 else []
        return [(scene[0].get_seconds(), scene[1].get_seconds()) for scene in scene_list]
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] シーン検出失敗: {e}", file=sys.stderr)
        duration = _get_video_duration(mp4_path)
        return [(0.0, duration)] if duration > 0 else []


def _get_video_duration(mp4_path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", mp4_path],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0.0


def _extract_keyframe(mp4_path: str, time_sec: float, output_jpg: str, width: int = 480) -> bool:
    """指定秒のフレームを JPEG で抽出。"""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{time_sec:.2f}", "-i", mp4_path,
             "-frames:v", "1", "-q:v", "3",
             "-vf", f"scale={width}:-1", output_jpg],
            check=True, capture_output=True,
        )
        return os.path.exists(output_jpg) and os.path.getsize(output_jpg) > 0
    except subprocess.CalledProcessError:
        return False


# ====================================================================
# Vision 分析（GPT-4o-mini-vision でシーン記述＋テロップOCR）
# ====================================================================

VISION_PROMPT = """この画像はInstagramリール動画から抽出した1フレームです。以下の2点を日本語で簡潔に出力してください。
必ず有効なJSONのみで返答してください。

{
  "description": "画像に何が写っているかを15〜60文字で1文記述（被写体・構図・背景）",
  "telop": "画像内に表示されているテロップ・字幕・テキストがあればそのまま書き起こし。文字がない場合は空文字"
}

備考:
- description は「人物の動作」「場所」「重要なオブジェクト」を含めると良い
- telop は装飾文字・絵文字・ハッシュタグもそのまま書き起こす
- 余計な説明・前置きは一切不要、JSONのみ"""


def _get_vision_client():
    global _VISION_CLIENT
    if _VISION_CLIENT is not None:
        return _VISION_CLIENT
    from openai import OpenAI
    _VISION_CLIENT = OpenAI()
    return _VISION_CLIENT


def _vision_analyze_image(image_path: str, model: str = "gpt-4o-mini") -> Dict[str, str]:
    """画像1枚を vision モデルに渡してシーン記述とテロップOCRを取得。"""
    if not os.path.exists(image_path) or os.path.getsize(image_path) <= 0:
        return {"description": "", "telop": ""}
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        client = _get_vision_client()
        # GPT-5 系は max_completion_tokens / 既定 temperature を使う
        is_new_api = (
            model.startswith("gpt-5") or model.startswith("o1")
            or model.startswith("o3") or model.startswith("o4")
        )
        kwargs: Dict[str, Any] = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        }
        if is_new_api:
            kwargs["max_completion_tokens"] = 500
        else:
            kwargs["max_tokens"] = 500
            kwargs["temperature"] = 0.2
        # リトライ
        for attempt in range(3):
            try:
                r = client.chat.completions.create(**kwargs)
                txt = (r.choices[0].message.content or "").strip()
                data = json.loads(txt)
                return {
                    "description": str(data.get("description", "")).strip(),
                    "telop": str(data.get("telop", "")).strip(),
                }
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if any(s in msg for s in ["429", "500", "502", "503", "rate limit", "timeout"]) and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                print(f"  [WARN] vision 分析失敗: {str(e)[:160]}", file=sys.stderr)
                return {"description": "", "telop": ""}
        return {"description": "", "telop": ""}
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] vision 処理エラー: {str(e)[:160]}", file=sys.stderr)
        return {"description": "", "telop": ""}


def _analyze_scenes_in_video(
    mp4_path: str,
    work_dir: str,
    vision_model: str,
    enable_vision: bool = True,
) -> Tuple[int, List[Dict[str, Any]]]:
    """動画のシーン分割と各シーンの vision 分析。

    Returns: (cut_count, scene_breakdown)
      scene_breakdown: [{start, end, description, telop}, ...]
    """
    scenes = _detect_scenes(mp4_path)
    cut_count = len(scenes)
    if cut_count == 0:
        return 0, []

    if not enable_vision:
        return cut_count, [
            {"start": round(s, 1), "end": round(e, 1), "description": "", "telop": ""}
            for (s, e) in scenes
        ]

    breakdown: List[Dict[str, Any]] = []
    for i, (start, end) in enumerate(scenes):
        midtime = (start + end) / 2.0
        kf = os.path.join(work_dir, f"keyframe_{i:02d}.jpg")
        if not _extract_keyframe(mp4_path, midtime, kf):
            breakdown.append({"start": round(start, 1), "end": round(end, 1), "description": "", "telop": ""})
            continue
        result = _vision_analyze_image(kf, model=vision_model)
        breakdown.append({
            "start": round(start, 1),
            "end": round(end, 1),
            "description": result.get("description", ""),
            "telop": result.get("telop", ""),
        })
    return cut_count, breakdown


# ====================================================================
# 統合解析（Whisper + PySceneDetect + Vision）
# ====================================================================

def analyze_video_full(
    url: str,
    whisper_model: str = "medium",
    vision_model: str = "gpt-4o-mini",
    enable_vision: bool = True,
    enable_scenes: bool = True,
) -> Dict[str, Any]:
    """1動画を全機能で解析する。

    Returns:
      {
        "transcript": str,
        "cut_count": int,
        "scene_breakdown": [{start, end, description, telop}, ...],
      }
    """
    empty: Dict[str, Any] = {"transcript": "", "cut_count": 0, "scene_breakdown": []}
    if not url or not url.startswith("http"):
        return empty

    # 完全キャッシュヒット（結果に必要なフィールドが揃っていれば）
    cached = _read_full_cache(url)
    if cached is not None and "scene_breakdown" in cached:
        return cached

    # 旧 .txt キャッシュから transcript だけは引き継げる
    legacy_transcript = _read_legacy_transcript(url)

    with tempfile.TemporaryDirectory(prefix="ig-reel-") as td:
        mp4 = os.path.join(td, "v.mp4")
        if not _download_video(url, mp4):
            empty["transcript"] = legacy_transcript or ""
            _write_full_cache(url, empty)
            return empty

        # Whisper（旧キャッシュがあれば再利用）
        if legacy_transcript is not None:
            transcript = legacy_transcript
        else:
            wav = os.path.join(td, "a.wav")
            transcript = ""
            if _extract_audio(mp4, wav):
                transcript = _transcribe_audio(wav, model_name=whisper_model)

        # シーン検出 & vision 分析
        cut_count = 0
        scene_breakdown: List[Dict[str, Any]] = []
        if enable_scenes:
            cut_count, scene_breakdown = _analyze_scenes_in_video(
                mp4, td, vision_model=vision_model, enable_vision=enable_vision,
            )

    result = {
        "transcript": transcript,
        "cut_count": cut_count,
        "scene_breakdown": scene_breakdown,
    }
    _write_full_cache(url, result)
    return result


# ====================================================================
# 公開関数（reels リスト一括処理）
# ====================================================================

def transcribe_reels_inplace(
    reels: List[Dict[str, Any]],
    model_name: str = "medium",
    vision_model: Optional[str] = None,
) -> None:
    """各リールに以下のキーを注入する:
      - transcript    : Whisper 結果
      - cut_count     : PySceneDetect 結果
      - scene_breakdown : [{start, end, description, telop}, ...]
      - telop_full    : 全シーンのテロップを連結

    既存のテキスト transcript キャッシュ（旧 .txt）は自動で引き継がれる。
    新規分のみ Whisper + シーン検出 + vision を実行する。
    """
    if not reels:
        return
    n = len(reels)
    enable_vision = os.environ.get("IG_REEL_DISABLE_VISION", "").strip() not in ("1", "true", "yes")
    enable_scenes = os.environ.get("IG_REEL_DISABLE_SCENES", "").strip() not in ("1", "true", "yes")
    vmodel = vision_model or os.environ.get("IG_REEL_VISION_MODEL", "gpt-4o-mini")

    print(
        f"[ANALYZE] {n} 件のリールを解析開始（whisper={model_name} / vision={vmodel} / "
        f"scenes={'on' if enable_scenes else 'off'} / vision={'on' if enable_vision else 'off'}）"
    )
    success_audio = success_visual = no_url = cache_hits = 0
    t_start = time.time()

    for i, reel in enumerate(reels, 1):
        url = _extract_video_url(reel)
        username = reel.get("username", "?")
        if not url:
            reel["transcript"] = ""
            reel["cut_count"] = 0
            reel["scene_breakdown"] = []
            reel["telop_full"] = ""
            no_url += 1
            print(f"  [{i}/{n}] @{username}: 動画URL取得不可 → スキップ")
            continue

        was_cached = _read_full_cache(url) is not None
        result = analyze_video_full(
            url,
            whisper_model=model_name,
            vision_model=vmodel,
            enable_vision=enable_vision,
            enable_scenes=enable_scenes,
        )
        reel["transcript"] = result.get("transcript", "")
        reel["cut_count"] = result.get("cut_count", 0)
        reel["scene_breakdown"] = result.get("scene_breakdown", [])
        # 全テロップを連結
        telops = [s.get("telop", "") for s in reel["scene_breakdown"] if s.get("telop")]
        reel["telop_full"] = "\n".join(telops)

        if reel["transcript"]:
            success_audio += 1
        if reel["cut_count"] > 0:
            success_visual += 1
        if was_cached:
            cache_hits += 1

        tag = "cache" if was_cached else "new"
        preview = (reel["transcript"] or "")[:30].replace("\n", " ") or "(無音)"
        print(
            f"  [{i}/{n}] @{username} ({tag}): cuts={reel['cut_count']} / "
            f"transcript={preview}..."
        )

    elapsed = time.time() - t_start
    print(
        f"[ANALYZE] 完了: 音声OK {success_audio}/{n} / 映像OK {success_visual}/{n} / "
        f"キャッシュヒット {cache_hits} / 動画URL不可 {no_url} / 所要 {elapsed:.0f}秒"
    )


# 後方互換: 旧API（transcribeのみ実行・別途呼び出されているケース対応）
def transcribe_video_url(url: str, model_name: str = "medium") -> str:
    """単一URLの音声のみ文字起こし（旧API）。"""
    result = analyze_video_full(
        url, whisper_model=model_name,
        enable_vision=False, enable_scenes=False,
    )
    return result.get("transcript", "")
