# Instagram リール バズリサーチ スキル

> Claude Code 拡張用スキル。Instagram のバズリールを自動収集 → AI 分析 → Google スプレッドシートに出力。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/Platform-macOS-blue.svg)]()
[![Python: 3.9+](https://img.shields.io/badge/Python-3.9+-green.svg)]()

## このスキルで何ができる？

**指定キーワードでInstagramのバズリールを自動収集・分析し、Googleスプレッドシートに1枚のスナップショットタブとしてまとめます。**

- 動画の音声を自動文字起こし（ローカル Whisper・無料）
- バズった理由・改善案を AI が分析（OpenAI gpt-4o-mini）
- 気になるアカウントは「ウォッチリスト」に登録して定点観測
- macOS の launchd で **2週間ごとの自動実行** も可能

![リサーチ結果タブ](screenshots/2_リサーチ結果タブ.png)

## 実行イメージ

Claude Code のチャットでこう打つだけ：

```
ダイエットでリサーチして
```

すると Claude が条件をヒアリング：

```
リサーチを始めるね！以下を教えて：

- キーワード（必須）：何を調べる？
- 期間：7日 / 14日（既定） / 30日 / 90日
- 再生回数の条件：
  - 絶対値: 5万以上（既定）/ 10万以上 / 30万以上 など
  - フォロワー比: 5倍以上 / 10倍以上 など

特になければキーワードだけ教えてくれれば、デフォルト（14日間・5万再生以上）で実行するよ！
```

→ 条件が揃ったら裏で **15〜30分** 処理して、スプシに新タブを追加して完了。

## 構成（ざっくり）

```
キーワード入力
    ↓
Apify （Instagram スクレイピング・$0.30/run）
    ↓
フィルタ（期間 + 再生条件）
    ↓
Whisper（ローカル・無料）で音声文字起こし
    ↓
OpenAI gpt-4o-mini で分析（$0.05〜0.15/run）
    ↓
Google Sheets にスナップショット書き込み
```

## セットアップ全体所要時間

- **約45〜60分**（初回のみ）
- 内訳：Apify登録10分・OpenAIキー10分・GCPサービスアカウント20分・スプシ作成5分・コード設定10分

→ 詳細は **[セットアップ手順書.md](セットアップ手順書.md)** を参照

## 日常的な使い方

→ **[運用ガイド.md](運用ガイド.md)** を参照

主な機能：
- キーワードリサーチ（「ダイエットでリサーチして」）
- ウォッチリスト追加（「@user1 をウォッチリストに追加」）
- ウォッチリスト定点観測（毎月1日・15日に自動実行可）

## 月額コスト目安

| 項目 | 月額 |
|---|---|
| Apify（無料枠 $5/月含む） | $0〜10 |
| OpenAI（gpt-4o-mini） | $1〜5 |
| Whisper（ローカル実行） | **$0** |
| Google Sheets API | **$0** |
| **合計（週1リサーチ運用想定）** | **$5〜15/月** |

## ファイル構成

```
ig-reel-research/
├── SKILL.md              # スキル定義（Claude Code が読み込む）
├── SETUP_COMPLETE_GUIDE.md  # 詳細セットアップガイド（旧版・参考）
└── scripts/              # 実行スクリプト群
    ├── run_production_research.py   # キーワードリサーチ
    ├── run_watchlist_research.py    # ウォッチリスト分析
    ├── add_to_watchlist.py          # ウォッチリスト追加CLI
    ├── transcribe.py                # Whisper文字起こし
    ├── pipeline.py                  # データ収集・フィルタ
    └── gsheets.py                   # スプシ書き込み
```

## 動作環境（必須）

- **macOS**（Apple Silicon 推奨。M1/M2/M3でWhisperが速い）
- **Python 3.9+**
- **ffmpeg**（`brew install ffmpeg`）
- **Claude Code 拡張**（`~/.claude/skills/` にスキル配置）

Windows でも動かせる可能性はありますが、 **macOS前提で動作確認済み** です。

## 質問・トラブル

- 質問・バグ報告：[GitHub Issues](https://github.com/kokorozashi-ai/ig-reel-research/issues) で報告してください
- ログ場所：`~/.cache/ig-reel-research/cron.stdout.log`（自動実行時）
- 主なトラブルシュート：[運用ガイド.md](運用ガイド.md) の「よくある問題」セクションへ

## ライセンス

[MIT License](LICENSE) — 自由に使ってください。改変・商用利用OK。改善PR歓迎します。
