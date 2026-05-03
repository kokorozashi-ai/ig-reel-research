---
name: ig-reel-research
description: "Instagramリールのバズ分析。キーワード本番 run_production_research.py／ウォッチ専用 run_watchlist_research.py／ウォッチリスト追加 add_to_watchlist.py。Apify→フィルタ→Whisper音声文字起こし→OpenAI→Sheets スナップショット形式（タブ=キーワード名、転置レイアウト・行1サマリー/行2ヘッダー/行3以降データ）。【ヒアリング先行型】キーワードリサーチのトリガー検出時、条件（キーワード/期間/再生条件）が揃っていなければまずユーザーに質問してから実行。期間プリセット: --period 1w/2w/1m/3m（既定2w=14日、ヒアリングでは6mは出さない）。再生条件: --min-views N（既定50000）または --buzz-ratio X（フォロワー比モード）から選択、両者排他。OpenAI分析は上位15件まで（IG_REEL_TOP_N）。フックはWhisper transcript を最優先で判定。文字起こしを止めるには IG_REEL_DISABLE_TRANSCRIBE=1。実行前に export APIFY_TOKEN=$APIFY_TOKEN（.zshrc の変数名）。ウォッチリスト登録シートは絶対にクリアしない。常駐タブは「使い方ガイド」「ウォッチリスト」のみ、それ以外は累積スナップショット。トリガー:「○○でリサーチして」「リサーチして」「@user をウォッチリストに追加」等。ユーザーに手順を見せず Bash 実行（ただしヒアリングは先行）。"
allowed-tools: Bash(export* && cd* && python3 run_production_research.py* && python3 run_watchlist_research.py* && python3 add_to_watchlist.py*)
---

# Instagram リール バズリサーチ スキル

## 概要

キーワード or ウォッチリスト指定 → Apify収集 → フィルタ → Whisper音声文字起こし → OpenAI分析 → **スナップショット形式で Google Sheets に1枚タブ追加**。
全工程が自動化されており、ユーザーはキーワード（と任意で期間プリセット）を伝えるだけでよい。**リサーチ依頼に対しては Claude Code が Bash ツールで本番コマンドを直接実行する。ユーザーに手順・コマンドの説明やコピペ用の手順書を見せてはいけない。**

## 新スキーマ（スナップショット形式）

### スプシのタブ構成

| タブ | 役割 | クリア対象 |
|---|---|---|
| `使い方ガイド` | 仕様の解説（毎回再生成） | クリア対象外（再生成のみ） |
| `ウォッチリスト` | アカウント登録（手動運用） | **絶対にクリアしない** |
| `<キーワード名>` | 通常リサーチの結果スナップショット（リサーチごとに新規追加） | 累積（明示削除しない限り残る） |
| `<キーワード名> YYYYMMDD` | 同名キーワードの2回目以降は日付サフィックス自動付与 | 累積 |
| `ウォッチリスト YYYYMMDD` | ウォッチリストモードの結果スナップショット | 累積 |

旧フォーマット（リール一覧 / コンテンツ分析 / バズ要因スコアリング / ディープインサイト / 過去〜系等）のタブは、各リサーチ実行時に `cleanup_legacy_tabs()` で **自動的に削除** される。

### スナップショット内のレイアウト（転置）

```
       |  A (列1)        |   B    |   C    |   D    | ...
-------|-----------------|--------|--------|--------|
 1     | 【リサーチサマリー】 | リール1 | リール2 | リール3 |   ← 列ヘッダー（ウォッチ中アカウントは "★ リール" で紫背景）
 2     | 実施日: …         |        |        |        |
 ...   | サマリー本文       |        |        |        |
       | (空行)            |        |        |        |
       | リールURL         | https. | https. | https. |   ← データ部
       | アカウント名       | @user1 | @user2 | @user3 |
       | フォロワー数       | 12,400 | 8,500  | 5,200  |
       | 再生回数           | 152万  | 85万   | 42万   |
       | …（16項目）       |        |        |        |
```

### リール1件あたりの抽出項目（16項目）

1. リールURL ／ 2. アカウント名 ／ 3. フォロワー数 ／ 4. 再生回数 ／ 5. いいね数 ／ 6. コメント数 ／
7. 投稿日 ／ 8. エンゲージメント率 ／ 9. 動画尺 ／ 10. ジャンル ／ 11. ハッシュタグ ／
12. フック（Whisper 最優先で判定）／ 13. 全文文字起こし（Whisper） ／ 14. キャプション全文 ／
15. バズ仮説 ／ 16. 改善提案

廃止項目（明示）：バズ倍率／バズ要因スコア（7項目+総合）／同ジャンル比較／自分用リメイク案／構成・CTA・テロップ別分析（→ 全文文字起こしに統合）／キャプション要約（→ 全文に変更）

### ウォッチリスト連携（双方向）

- **キーワードリサーチ → ウォッチリスト**：CLI `python3 add_to_watchlist.py @user1 @user2 [--genre ...] [--memo ...]` で発見アカウントをウォッチリストに昇格
- **ウォッチリスト → キーワードリサーチ**：登録済みアカウントがキーワードリサーチで含まれていれば、そのリール列ヘッダーに **★（紫背景）** で表示

### ウォッチリスト登録シートの保護（必須）

- スプレッドシートの **「ウォッチリスト」** シート（ユーザーが手動でアカウント・ジャンル等を登録するタブ）は、**絶対にクリアしないこと**。`clear` 系処理の対象から**必ず除外**する。
- 手動登録データは消えると**復旧できない**。`clear_research_sheets()` の対象外であり、`clear_watchlist_data()` は本番では no-op（実装済み）。テストで登録行を空にする必要がある場合のみ `clear_watchlist_registration_rows_for_testing()` を使う。

### Apify トークンと Bash 実行（必須）

- 本番スクリプトが読む環境変数は **`APIFY_TOKEN`** である。一方、ユーザー環境の `~/.zshrc` 等では **`APIFY_TOKEN`**（APIFY の誤記）として登録されていることがある。**変数名を取り違えないこと。**
- **リサーチ実行時、Claude Code は必ず** `export APIFY_TOKEN=$APIFY_TOKEN` を設定してから `run_production_research.py` および `run_watchlist_research.py` を実行する（未設定の `APIFY_TOKEN` をそのまま渡すと空になるので、ログインシェルで読み込む必要がある場合は `zsh -lic 'export APIFY_TOKEN=$APIFY_TOKEN && ...'` 等で対応する）。
- 上記を**ユーザーに手順として表示せず**、**エージェントが Bash で実行する**。

## 既知のバグ防止チェックリスト（全コード変更・リサーチ実行時に必読必守）

このチェックリストは過去の開発で実際に発生した全バグの再発防止策である。コード修正・新機能追加・テスト・リサーチ実行の前後に必ず全項目を確認すること。

---

### 1. 日付表示（発生回数: 5回以上）
【問題】Google Sheetsが日付文字列を数値（シリアル値: 45200, 46124等）に自動変換してしまう
【対策】日付を書き込む際は先頭にアポストロフィを付けるか、書き込み後にnumberFormat: {type: "TEXT"}を設定
【対象】全シートの日付列すべて:
- リール一覧・過去リサーチリールの投稿日
- ウォッチリスト リール一覧・過去ウォッチリスト リールの投稿日
- リサーチサマリーの実施日
- ウォッチリスト サマリーの分析日
- リサーチ横断トレンドのリサーチ日
- ウォッチリストの登録日
【確認方法】書き込み後にシートを開き、日付が「2026-04-18」形式で表示されていること。5桁の数字になっていたらバグ

### 2. エンゲージメント率の表示（発生回数: 3回以上）
【問題1】小数のまま表示される（例: 0.0118）
【問題2】計算式が間違い80〜99%等の異常値が出る
【正しい計算式】（いいね数＋コメント数）÷ 再生回数 × 100
【絶対にやってはいけない計算】（いいね数＋コメント数）÷ フォロワー数 × 100（これはバズ倍率に近い計算であり、エンゲージメント率ではない）
【正常値の範囲】0.5〜10%程度。10%を大幅に超える値が出た場合は計算式のバグ
【表示形式】必ず%付き文字列（例: "1.18%"）
【対象】全シートのエンゲージメント率列（リール一覧、過去リサーチリール、ウォッチリスト リール一覧、過去ウォッチリスト リール）

### 3. バズ倍率の表示
【正しい計算式】再生回数 ÷ フォロワー数
【表示形式】必ずx付き文字列（例: "5.0x"）
【対象】全シートのバズ倍率列

### 4. 動画尺の表示（発生回数: 1回）
【問題】数字だけで単位がない
【対策】60秒未満は「○秒」、60秒以上は「○分○秒」の形式
【対象】コンテンツ分析、ウォッチリスト コンテンツ分析

### 5. 数値のカンマ区切り
【対象】フォロワー数、再生回数、いいね数、コメント数（全シート共通）

### 6. データなし行の背景色（発生回数: 5回以上）
【問題】スコア色分け（赤黄緑）やゼブラストライプがデータなし行に残る
【対策】データ最終行より下の全行、アカウントブロック間の空行2行の背景色を白(#FFFFFF)にリセット
【罫線】維持してよいが背景色は必ず白
【対象シート】
- バズ要因スコアリング、過去バズスコアリング
- ウォッチリスト スコアリング、過去ウォッチリスト スコアリング
- 全てのウォッチリスト系分析シート（リール一覧、コンテンツ分析、インサイト含む）
【確認方法】書き込み後にデータ最終行の下と空行を目視確認。色が残っていたらバグ

### 7. アカウントブロック間の空行（発生回数: 3回以上）
【問題】空行にNo.だけ残る、ゼブラストライプが残る、書式が残る
【対策】空行2行には一切のデータ・No.・書式を入れない。背景色は白、セル内容は空
【対象】ウォッチリスト系の全分析シート・全過去シート

### 8. ウォッチリストシートの保護（発生回数: 2回）
【問題】リサーチ再実行時やクリア処理でウォッチリストシートのユーザー登録データが消える
【対策】clear処理・初期化処理・全シートクリアの対象から「ウォッチリスト」シートを必ず除外
【影響】違反するとユーザーが手動登録したアカウントデータが消失し復旧できない
【確認方法】クリア関数のコードを確認し、ウォッチリストシートが除外されていることを毎回確認

### 9. ウォッチリスト アカウント別ブロック分け（発生回数: 2回）
【問題】ブロック分けが消えてフラットな一覧になる
【正しい構成】各アカウントごとに:
  1. アカウントヘッダー行（📌 @アカウント名 ｜ ジャンル ｜ 注目ポイント、背景 #4A148C、白文字、太字、フォントサイズ13）
  2. 列ヘッダー行（背景 #7B1FA2、白文字）
  3. データ行（ゼブラストライプ）
  4. 空行2行（完全空白・白背景）
【対象】ウォッチリスト リール一覧、コンテンツ分析、スコアリング、インサイト、過去系も全て
【確認方法】書き込み後にブロック分けが存在することを目視確認

### 10. アーカイブ後の空行残り（発生回数: 1回）
【問題】7日超リールを過去シートに移動した後、元シートにNo.と空行が残る
【対策】アーカイブ処理後にデータを詰め直し、No.を振り直す。空行を一切残さない
【連動】コンテンツ分析・スコアリング・インサイトも連動で空行除去

### 11. リンク化（発生回数: 2回）
【問題】コード修正後にリールURLやアカウント名のハイパーリンクが消える
【対策】
- リールURL（B列）: 全シートでクリッカブルなハイパーリンクにする
- ウォッチリスト系のアカウント名: https://www.instagram.com/{アカウント名}/ へのリンク
【対象】通常リサーチ全シート、ウォッチリスト全シート、過去系全シート
【確認方法】コード修正後にリンクが機能することをクリックして確認

### 12. リサーチサマリーとウォッチリストの完全分離（発生回数: 2回）
【問題】リサーチサマリーにウォッチリストのデータが混入する
【対策】
- リサーチサマリー: 通常リサーチのフィルタ通過リールのみを対象
- ウォッチリスト サマリー: ウォッチリストのリールのみを対象
- OpenAIへのプロンプトを別々のAPI呼び出しで生成
- リサーチ横断トレンドも通常リサーチのデータのみで集計
- トップ3にウォッチリストアカウントが入らないようにする

### 13. セクションヘッダーの文字色（発生回数: 1回）
【問題】背景色付きヘッダーの文字が暗い色で見にくい
【対策】リサーチサマリー、ウォッチリストサマリーのセクションヘッダー文字色は常に白

### 14. ウォッチリストのフォロワー数補完（発生回数: 1回）
【問題】Apifyのリールスクレイパーがフォロワー数を返さず、フォロワー数0・バズ倍率0.0xになる
【対策】ウォッチリストリサーチ時にscrape_profiles()で各アカウントのフォロワー数を別途取得して補完

### 15. 環境変数（発生回数: 3回以上）
【APIFY_TOKEN】~/.zshrcにAPIFY_TOKENとして登録。実行時はexport APIFY_TOKEN=$APIFY_TOKEN
【実行方法】Claude Codeはユーザーに手順を表示せず、自分でbashを実行する
【OPENAI_API_KEY・GOOGLE_SHEETS_CREDENTIALS】~/.zshrcに登録済み

### 16. コンテンツ生成時の禁止事項（発生回数: 1回）
【問題】フックや同ジャンル比較に[P1] [P2]等のプレフィックスが付く
【対策】テストデータ・コンテンツ生成時に機械的なラベルを絶対に含めない

### 17. フィルタ条件（重要ルール）
- 通常リサーチ: **指定期間以内** ＋ **再生条件**（絶対値 OR フォロワー比のどちらか1つ）。両方の同時指定不可
- 既定: 期間 14日（`--period 2w`）／ 再生 5万以上（`--min-views 50000`）
- 期間プリセット: `1w / 2w / 1m / 3m / 6m`（ヒアリング時は 6m を出さず4択提示。CLIでは引き続き受付）
- 再生条件モード:
  - **絶対値モード**：`--min-views N`（既定 50000）。フォロワー比は判定しない
  - **フォロワー比モード**：`--buzz-ratio X`（指定すると絶対値は無視）
- ウォッチリスト: 指定期間以内の全リール（再生数・フォロワー比の条件なし）
- 通過5件未満の場合: 条件緩和ではなく追加キーワードで母数を増やす
- OpenAI 分析対象は **バズ倍率上位 15件** まで（環境変数 `IG_REEL_TOP_N` で変更可）。長期間でもコスト・実行時間が爆発しないようキャップする

### 17a. ユーザー依頼の拒否ルール（誤動作防止）

以下のユーザー依頼が来たら **スクリプト実行せず、短く拒否して代替案を1行で提示** する：

| 依頼パターン | 例 | 対応 |
|---|---|---|
| **複数期間の同時指定** | 「1週間と1ヶ月、両方リサーチして」 | 「1回の実行は1期間のみ対応です。先にどちらを実行しますか？」と聞き返す |
| **再生数閾値の緩和** | 「再生2万でいいから」「閾値下げて」 | 「3万再生以上は固定仕様です。代わりに追加キーワードで母数を増やす運用を推奨」 |
| **期間外への拡張** | 「2年分見せて」 | 「最大は6ヶ月です（`--period 6m`）。それ以上は仕様外」 |
| **再生≧フォロワー条件の解除** | 「フォロワー比は気にしないで」 | 「ウォッチリストモードならその条件なしで動きます。`run_watchlist_research.py` を提案」 |
| **既存スプシの破壊** | 「過去シート全部消して」「ウォッチリスト全消し」 | 「ウォッチリスト登録シートは絶対にクリアしません。テスト用関数のみ別途あります」 |

### 18. Google Sheets API レート制限（発生回数: 2回）
【対策】各シート書き込みの間にtime.sleep()を入れて429エラーを回避

### 19. スコア色分け5段階
- 1点: 濃い赤 #FF9999
- 2点: 薄い赤 #FFCCCC
- 3点: 黄色 #FFFFCC
- 4点: 薄い緑 #CCFFCC
- 5点: 濃い緑 #99FF99
【対象】バズ要因スコアリング、過去バズスコアリング、ウォッチリスト スコアリング、過去ウォッチリスト スコアリング、リサーチ横断トレンドのスコア項目別

### 20. リサーチ横断トレンドの「バズの共通強み」空欄対策（発生回数: 1回）
【対策】該当なしの場合「該当なし（全項目の平均が3.5未満）」と1行表示。完全に空欄にしない

### 21. ウォッチリスト サマリー件数ズレ（発生回数: 1回）
【問題】「収集リール数（分析対象）」が実データ件数と不一致（例: 30表示だが実際は22）
【対策】ウォッチリスト サマリーの件数・日付はモデル出力を盲信せず、実データ件数で上書きする
【確認方法】ウォッチリスト リール一覧の実データ件数と、ウォッチリスト サマリーの件数が一致していること

### 22. 既存merge残骸によるデータ欠落（発生回数: 1回）
【問題】ブロックシート再描画時、以前の結合セルが残っていると一部セルだけ空白化する
【対策】シート再書き込み前に必ずunmergeを実行してからclear/writeする
【対象】ウォッチリスト系シート全て（現行・過去）

### 23. APIFYトークン空上書き（発生回数: 2回）
【問題】`export APIFY_TOKEN=$APIFY_TOKEN` 実行時に `APIFY_TOKEN` が未展開だと `APIFY_TOKEN` が空になり実行失敗
【対策】ログインシェルで読み込むか、既存 `APIFY_TOKEN` を退避して空上書きを防ぐ
【確認方法】実行直前に `APIFY_TOKEN` が空でないこと

### 24. Google Sheets一時障害（発生回数: 2回）
【問題】429だけでなく500 Internal errorでも書き込みが失敗する
【対策】429/500/502/503で待機付きリトライを実装し、即失敗にしない
【確認方法】一時障害発生時に再試行後成功すること

### 25. セクションヘッダー修正時の背景維持（発生回数: 1回）
【問題】文字色だけ変える修正で背景色まで変えてしまう
【対策】ヘッダー修正時は `fields` を最小化し、背景色は既存テーマ色を維持する
【対象】リサーチサマリー、ウォッチリスト サマリー

### 26. 過去シートが累積していない（発生回数: 1回・要修正）
【問題】`clear_research_sheets()` が過去シート（過去リサーチリール／過去バズスコアリング／過去ディープインサイト）も毎回クリアしていた。さらに、メイン4シートをクリアした後に `_archive_old_reels()` が走るため、アーカイブ対象を読めない順序になっていた
【対策】① `SHEET_TITLES_TO_CLEAR_KEYWORD` から過去シート3つを除外。②`run_production_research.py` の `clear_research_sheets()` 呼び出しを削除し、`build_gsheet()` 内の `_archive_old_reels()` → `_clear_and_write()` の順序に任せる
【確認方法】2回目のリサーチ実行後、過去シートに前回分の7日超リールが残っていること。空のままならまだ壊れている

### 27. キーワード入力の正規化（誤動作防止）
【問題】「#ダイエット」「ダイエット　ご飯」（全角空白）等の入力が Apify URL 構築時に壊れる
【対策】CLI 受け取り時に `lstrip("#＃")` と `replace("　", " ")` を適用、空文字キーワードは除外
【対象】`run_production_research.py` の `_parse_cli`

### 28. 並列実行による2重書き込み（誤動作防止）
【問題】別ターミナルで2本同時起動するとスプシが破損し得る
【対策】`/tmp/ig-reel-research.lock` でPIDロック。既存PIDが生きていれば即終了
【確認方法】2本同時起動でFATAL終了することを目視確認

### 29. APIFY_TOKEN/OPENAI_API_KEY 空ガード
【問題】環境変数が空文字や数文字しかない場合、実行は始まるがApifyから0件・OpenAIで認証エラー
【対策】`len(token.strip()) >= 20` を実行直後に検証し、満たなければFATAL
【対象】`run_production_research.py` `run_watchlist_research.py`

### 30. OpenAI 一時障害でのリトライ
【問題】OpenAIが 429/500/502/503/タイムアウトを返したとき即落ちる
【対策】`_call_api` 内に最大3回・指数バックオフ（2/4/8秒）のリトライ実装
【確認方法】一時障害時に再試行後成功すること

### 31. 音声文字起こしによるフック精度向上（重要仕様）
【問題】キャプション+数値だけだとフック判定が動画の実内容とズレる（キャプションは宣伝文で動画と無関係なことが多い）
【対策】TOP15キャップ後・OpenAI分析前に、ローカル Whisper（medium）で各リールの音声を文字起こしし、OpenAI に `transcript` フィールドとして渡す
【スキップ方法】`IG_REEL_DISABLE_TRANSCRIBE=1` でスキップ可（コスト・時間優先したい時）
【モデル変更】`IG_REEL_WHISPER_MODEL=tiny|base|small|medium|large`（既定 medium）
【依存】`brew install ffmpeg` と `pip install openai-whisper`
【キャッシュ】`~/.cache/ig-reel-research/transcripts/<sha1(video_url)>.txt` に永続化。同じURLは2度処理しない
【プロンプト連動】`_openai_analyze` のシステムプロンプトで「transcriptが空でなければ最優先でフック・構成判定の根拠にする」と明示
【処理時間】M1 Mac で15件・medium で3〜8分追加。コストは$0
【確認方法】実行ログに `[TRANSCRIBE] 完了: 成功 X/30 件` が出て、リサーチ後の「コンテンツ分析」シートのフックが動画の実台詞と整合すること

---

## コード修正完了時の最終確認チェックリスト
修正後、以下を全て確認してから完了とすること：
□ 全シートの日付がテキスト表示（シリアル値でない）
□ エンゲージメント率が%付き・正常値（0.5〜10%程度）
□ バズ倍率がx付き
□ 動画尺に単位付き
□ 数値にカンマ区切り
□ データなし行・空行の背景色が白
□ ウォッチリストシートが保護されている（クリアされていない）
□ アカウント別ブロック分けが維持されている
□ リールURL・アカウント名がリンク化されている
□ リサーチサマリーにウォッチリストのデータが混入していない
□ セクションヘッダーの文字色が白
□ フォロワー数が0でない（ウォッチリスト）
□ スコア色分けが5段階で正しい
□ ウォッチリスト サマリー件数が実データ件数と一致している
□ `APIFY_TOKEN` が空でないことを確認して実行している
□ 429/500/502/503発生時にリトライ経路が有効である

---

## トリガー条件（このスキルを自動実行する）

ユーザーが次のようなメッセージを送ったら、このスキルを **発火** する。**ただし条件が揃うまで実行しない**（後述の「ヒアリング先行ルール」参照）。

### キーワードリサーチ（`run_production_research.py`）

- 「○○、○○でリサーチして」
- 「○○でリール調べて」
- 「○○のリサーチお願い」
- 「○○で2週間／1ヶ月／3ヶ月分リサーチして」（期間指定あり）
- 「リサーチして」（キーワード未指定）

### ウォッチリストリサーチ（`run_watchlist_research.py`）

- 「ウォッチリストで分析して」「ウォッチリストの最新を確認して」
- 「ウォッチリストで2週間分リサーチして」

### ウォッチリスト追加（`add_to_watchlist.py`）

- 「@user_name をウォッチリストに追加して」
- 「@user1 と @user2 をフィットネスジャンルでウォッチリストに登録」
- 「ウォッチリストに @xxx を追加（メモ：編集が上手い）」

## ヒアリング先行ルール（キーワードリサーチ・必須）

キーワードリサーチのトリガーが発火したら、 **すぐに Bash 実行せず、まずユーザーに条件を聞く**。

### Step 1: 条件ヒアリング

ユーザー発話に「キーワード+期間+再生条件」が **すべて明示的に揃っている場合は即実行**（Step 2へスキップ）。

そうでない場合は、次のテンプレートをそのまま送って **ユーザーの返答を待つ**：

```
リサーチを始めるね！以下を教えて：

- **キーワード**（必須）：何を調べる？
- **期間**：7日 / 14日（既定） / 30日 / 90日
- **再生回数の条件**：
  - 絶対値: 5万以上（既定）/ 10万以上 / 30万以上 など
  - フォロワー比: 5倍以上 / 10倍以上 など（こんな検索もできるよ！）

特になければ **キーワードだけ** 教えてくれれば、デフォルト（14日間・5万再生以上）で実行するよ！
```

### Step 2: 発話マッピング → CLI引数に変換

ユーザー返答から以下のとおり引数を抽出：

| 発話例 | CLI引数 |
|---|---|
| 「ダイエット」だけ | `ダイエット` |
| 「ダイエットで14日」「14日間で」 | `--period 2w` |
| 「7日」「1週間」 | `--period 1w` |
| 「30日」「1ヶ月」 | `--period 1m` |
| 「90日」「3ヶ月」 | `--period 3m` |
| 「5万以上」「5万再生」 | `--min-views 50000` |
| 「10万以上」 | `--min-views 100000` |
| 「30万以上」 | `--min-views 300000` |
| 「フォロワー5倍」「5倍以上」 | `--buzz-ratio 5` |
| 「フォロワー10倍」 | `--buzz-ratio 10` |
| 「デフォルトで」「特になし」「いつものでOK」 | 引数なし（既定 `--period 2w --min-views 50000`） |

注意：
- **絶対値とフォロワー比は同時指定不可**（CLI側でエラー）。ユーザーがどちらか1つだけ選んでいることを確認
- 「5万以上 + フォロワー10倍」のように両方混ざる発話があれば、 **どちらにしますか？** と再確認
- 期間が連続値（例: 21日）の場合は最寄りプリセットへ丸めて確認なし実行（21日→14日 等）

### Step 3: Bash 実行

抽出した引数で本番ワンライナーを実行（後述）。

### 期間の自動マッピング

ユーザー発話に期間表現があれば次のように `--period` 引数に変換して渡す（指定がなければ既定 `1w`）:

| 発話例 | 引数 | 日数 |
|---|---|---|
| 「直近1週間」「いつもの」「1週間分」 | `--period 1w` | 7日 |
| 「2週間分」「直近2週間」「半月」 | `--period 2w` | 14日 |
| 「1ヶ月分」「直近1ヶ月」「30日」 | `--period 1m` | 30日 |
| 「3ヶ月分」「四半期」「90日」 | `--period 3m` | 90日 |
| 「半年分」「6ヶ月分」「180日」 | `--period 6m` | 180日 |

期間が連続値（例:「3週間」「45日」）の場合は **直近のプリセット** に丸めて確認なしで実行（3週間→1m、45日→1m など）。

## Claude Code が行うこと（必須・Bash 直接実行）

トリガーに該当したら、**確認や手順の提示なしで**、目的に応じて以下の Bash コマンドを **そのまま実行** する。

### キーワードリサーチ

```bash
export APIFY_TOKEN=$APIFY_TOKEN && export OPENAI_API_KEY="$OPENAI_API_KEY" && export GOOGLE_SHEETS_CREDENTIALS="$HOME/.config/gcloud/sheets-writer.json" && cd ~/.claude/skills/ig-reel-research/scripts && python3 -u run_production_research.py キーワード1 キーワード2 ... [--period 1w|2w|1m|3m] [--min-views N | --buzz-ratio X]
```

例:
- 「ダイエット」（条件なし） → `python3 -u run_production_research.py ダイエット` （既定 14日・5万再生以上）
- 「ダイエットで30日、10万以上」 → `python3 -u run_production_research.py ダイエット --period 1m --min-views 100000`
- 「ダイエットで90日、フォロワー5倍以上」 → `python3 -u run_production_research.py ダイエット --period 3m --buzz-ratio 5`
- 「美容と節約で2週間」 → `python3 -u run_production_research.py 美容 節約 --period 2w`

### ウォッチリストリサーチ

```bash
export APIFY_TOKEN=$APIFY_TOKEN && export OPENAI_API_KEY="$OPENAI_API_KEY" && export GOOGLE_SHEETS_CREDENTIALS="$HOME/.config/gcloud/sheets-writer.json" && cd ~/.claude/skills/ig-reel-research/scripts && python3 -u run_watchlist_research.py [--period 1w|2w|1m|3m|6m]
```

### ウォッチリスト追加

```bash
export GOOGLE_SHEETS_CREDENTIALS="$HOME/.config/gcloud/sheets-writer.json" && cd ~/.claude/skills/ig-reel-research/scripts && python3 -u add_to_watchlist.py @user1 @user2 [--genre ジャンル] [--memo "メモ"]
```

### 共通ルール

- **`-u` フラグ必須**：Python の stdout バッファリングを無効化し、進捗をリアルタイム表示
- 上記は **エージェント用** の実行定義。コマンド全文をユーザーに貼ってはいけない
- **`export APIFY_TOKEN=$APIFY_TOKEN` は必須**（`.zshrc` の変数名）
- キーワードは1つ以上必須。引数なしでは実行しない
- `--period` 省略時は **直近1週間（7日）**

### 完了後のユーザーへの報告（この形式のみ）

「リサーチ完了！○件収集→○件分析。スプレッドシートに反映済み: URL」

※○件はログの `[FILTER]` / `[TOTAL]` 等で確認して具体的な数値に置き換える。

## 前提条件

**Apify API キー・OpenAI API キー・Google Sheets 用認証 JSON** が環境で利用可能であること。推奨: `GOOGLE_SHEETS_CREDENTIALS=$HOME/.config/gcloud/sheets-writer.json`。不足で失敗した場合は、**手順の羅列やコンソール操作の説明はせず**、不足している前提（どのキーか）だけを短く伝える。

**シェルにトークンが載っていない場合:** ユーザーが `~/.zshrc` 等に **`export APIFY_TOKEN=...`**（変数名注意）だけしているとき、非対話 Bash では変数が空になり得る。そのときは **ログインシェルで読み込んでから** `export APIFY_TOKEN=$APIFY_TOKEN` 付きで実行する（例: `zsh -lic 'export APIFY_TOKEN=$APIFY_TOKEN && cd ... && python3 ...'`）。ユーザーに手順を書かず、エージェント側でそう実行する。

---

## ウォッチリスト登録・ウォッチリスト専用リサーチ

**スプレッドシート「ウォッチリスト」シート**にアカウント（ユーザー名・ジャンル・注目ポイント等）を追記する依頼、または **登録済みアカウントだけを対象に分析・シート更新する**依頼では、次を実行する（**ユーザーにコマンドや手順を見せない**）。

```bash
export APIFY_TOKEN=$APIFY_TOKEN && export OPENAI_API_KEY="$OPENAI_API_KEY" && export GOOGLE_SHEETS_CREDENTIALS="$HOME/.config/gcloud/sheets-writer.json" && cd ~/.claude/skills/ig-reel-research/scripts && python3 run_watchlist_research.py [--period 1w|2w|1m|3m|6m]
```

- 期間指定があれば末尾に `--period <値>` を付ける（既定 `1w`）。期間マッピングは「期間の自動マッピング」表に従う。
- シートへの**追記**が必要なときは、**gspread 等で `ウォッチリスト` に行を追加してから**上記を実行するか、ユーザーがシートに追記済みであることを前提に実行する（**追記方法をチャットで長く説明しない**）。
- 完了報告の例: 「ウォッチリストリサーチ完了。○件収集（指定期間内）。スプレッドシート反映済み: `https://docs.google.com/spreadsheets/d/YOUR_SPREADSHEET_ID_HERE/edit`」（件数はログの `[INFO]` 等から）。

---

## 実装済み仕様（以後の再フィードバック不要）

以下は **コードに既に反映済み**。同内容の修正依頼を出さなくてよい。

| 項目 | 内容 |
|------|------|
| キーワード本番 | `run_production_research.py` が `collect_reels`→フィルタ→chase→OpenAI→`build_xlsx`→Sheets。 |
| ウォッチ取得 | `instagram-scraper` の **`/ユーザー名/reels/` + resultsType:reels**（プロフィールURLを `reel-scraper` に渡さない）。 |
| ウォッチ・フォロワー | `research_all_watchlist` 後に **`scrape_profiles` 相当でフォロワー補完**し、**バズ倍率・エンゲージ率を再計算**（`pipeline.enrich_watchlist_reels_followers_from_profiles`）。 |
| キーワード側ウォッチマージ | `filter_watchlist_reels` 通過リールに **`raw` を付与**し、`collect_reels` から本編へマージ可能に。 |
| アカウント名表示 | ウォッチ関連シート・登録シートで **`=HYPERLINK("https://www.instagram.com/ユーザー名/", "ユーザー名")`**。読み取りは **`_extract_username_from_cell`** で HYPERLINK／プレーン両対応。 |
| OpenAI 件数 | ウォッチ件数が多いとき **`run_watchlist_research.py` が `_openai_analyze` を分割バッチ**（既定4件、`WATCHLIST_OPENAI_BATCH` で変更可）。 |
| リサーチサマリー / ウォッチサマリー | **完全分離**。通常リサーチは「リサーチサマリー」、ウォッチは「ウォッチリスト サマリー」シートのみ更新。ウォッチ実行時は通常サマリー・横断トレンドは**書き換えない**。通常リサーチの OpenAI 入力は **キーワード検索由来のフィルタ通過リールのみ**（`reel_source=keyword`）。ウォッチ由来は `collect_reels` でマージされても分析・シート・サマリーから除外。 |
| ウォッチリスト登録シート | **絶対にクリアしない**。clear 対象外。手動登録は失うと復旧不可（上記「ウォッチリスト登録シートの保護」参照）。 |
| Apify 実行前 | **`export APIFY_TOKEN=$APIFY_TOKEN` 必須**（`.zshrc` は `APIFY_TOKEN` のことがある）。 |

---


## 全体パイプライン

```
[ユーザー] キーワード指定（例:「ダイエット」）
    ↓
[Phase 1] Apify API でリール大量収集（キーワード＋関連語で50-100件）
    ↓
[Phase 2] 自動フィルタリング ← ★ここが核
    ├─ 条件1: 投稿日が直近7日以内
    ├─ 条件2: 再生回数 ≧ 30,000
    └─ 条件3: 再生回数 ≧ フォロワー数
    ↓
[Phase 3] フィルタ通過リールのみディープ分析
    ├─ コンテンツ分析（フック・構成・CTA・音源・テロップ）
    ├─ バズ要因スコアリング（7項目×5段階＋加重平均）
    ├─ 競合比較・トレンド文脈
    └─ 改善提案・リメイク案
    ↓
[Phase 4] 5シート構成xlsx自動出力
    ↓
[Phase 4b] `scripts/gsheets.py` の `build_gsheet()` で Google Sheets に自動書き込み（スプレッドシートID固定）
    ↓
[ユーザー] xlsx とスプレッドシートURLを受け取るだけ
```

---

## Phase 1: データ収集（Apify API）

### Step 1: キーワード展開

ユーザーのキーワードから類義語・関連語を自動生成し、検索範囲を広げる。

例（キーワード=「ダイエット」）:
| 検索回 | キーワード |
|--------|-----------|
| 1回目 | ダイエット |
| 2回目 | 痩せる |
| 3回目 | 体脂肪 減らす |
| 4回目 | ダイエットレシピ |

### Step 2: Apify でリール検索

**メインアクター**: `patient_discovery/instagram-search-reels`（ログイン不要・キーワード検索）

```python
import requests, time, os
from datetime import datetime, timedelta

def search_reels(keyword, apify_token, max_results=30):
    url = "https://api.apify.com/v2/acts/patient_discovery~instagram-search-reels/runs"
    headers = {"Authorization": f"Bearer {apify_token}", "Content-Type": "application/json"}
    payload = {"keyword": keyword, "maxResults": max_results}
    resp = requests.post(url, json=payload, headers=headers, params={"waitForFinish": 120})
    if resp.status_code != 201:
        return []
    dataset_id = resp.json()["data"]["defaultDatasetId"]
    items = requests.get(
        f"https://api.apify.com/v2/datasets/{dataset_id}/items",
        headers=headers
    ).json()
    return items
```

### ウォッチリスト連携（新機能）

**ウォッチ専用の本番一括**は `scripts/run_watchlist_research.py`（前章の Bash 参照）。以下はシート運用と内部の役割の参考。

スプレッドシートの **`ウォッチリスト`** シートに、参考にしたいアカウントをユーザーが直接追記して運用する。

- 追記場所: `ウォッチリスト` シート
- 入力例:
  - `アカウント名（Instagramユーザー名）`: `fitness_pro`（`@`なし）
  - `ジャンル`: 美容 / 料理 / ビジネス など
  - `注目ポイント`: フックの作り方が上手い、編集が参考になる など
  - `登録日`、`メモ` は任意入力

スキル実行時は、`gsheets.py` の `get_watchlist()` でアカウント一覧を取得し、`collect_reels()` の `watchlist_usernames` に渡す。

```python
from scripts.gsheets import get_watchlist
from scripts.pipeline import collect_reels

watchlist = get_watchlist()  # ['fitness_pro', 'cook_lab', ...]
all_reels = collect_reels(keywords, apify_token, max_per_keyword=30, watchlist_usernames=watchlist)
```

これにより、キーワード検索結果に加えてウォッチ対象アカウント由来の最新リールも自動でマージされる（URL重複は自動除去）。

#### ウォッチリスト専用フィルタ（`filter_watchlist_reels`）

`collect_reels()` 内のウォッチリスト経路では、通常の `filter_buzz_reels` とは別に **`filter_watchlist_reels`** を適用する。

| 項目 | 通常リサーチ（`filter_buzz_reels`） | ウォッチリスト（`filter_watchlist_reels`） |
|------|-------------------------------------|---------------------------------------------|
| 投稿日 | 直近7日以内 | 直近7日以内 |
| 再生数 | 3万以上 など | **基準なし（0再生でも可）** |
| フォロワー比 | 再生≧フォロワー など | **比較なし** |

7日を超えた投稿は、Google シート側の `build_gsheet()` で **過去ウォッチリスト** 系シートへ自動移動する（通常の「過去リサーチ」と同様の7日ルール）。

#### 単体・一括リサーチAPI

- **`research_watchlist_account(username, apify_token)`**（`pipeline.py`）: 指定アカウントの最新リールを Apify で取得し、`filter_watchlist_reels` で7日以内のみ返す。
- **`research_all_watchlist(apify_token, spreadsheet_id=None)`**（`gsheets.py`）: ウォッチリストシートの全アカウントに対して上記を実行し、**URL を重複除去した enriched リスト**を返す。Claude Code 側でコンテンツ分析・スコアリング・インサイトを生成し、`build_gsheet(..., watchlist_reels=..., watchlist_content=..., watchlist_scoring=..., watchlist_insights=...)` に渡す。

#### スキル実行時: 通常リサーチとウォッチリストリサーチの両方

1. **キーワード検索**: `collect_reels(keywords, apify_token, watchlist_usernames=get_watchlist())` でキーワード＋ウォッチリスト由来をマージ（ウォッチ分は `filter_watchlist_reels` 済みの raw のみマージ）。
2. **通常条件で絞る**: `passed, rejected = filter_buzz_reels(all_reels, ...)` でバズリサーチ対象を決定。
3. **ウォッチリストを別枠でフル分析する場合**: `watch_enriched = research_all_watchlist(apify_token)` で取得 → 分析結果を `build_gsheet` の `watchlist_*` 引数に渡す（紫ヘッダーのウォッチリスト分析シートを更新）。

```python
from scripts.gsheets import get_watchlist, research_all_watchlist
from scripts.pipeline import collect_reels, filter_buzz_reels

# 1+2: 通常 + ウォッチリストマージ後、バズ条件でフィルタ
all_reels = collect_reels(keywords, apify_token, watchlist_usernames=get_watchlist())
passed, rejected = filter_buzz_reels(all_reels, days=7, min_views=30000)

# 3: ウォッチリスト専用（再生・フォロワー条件なし、7日以内のみ）
watch_enriched = research_all_watchlist(apify_token)
# → 以降は Claude が watch_enriched を分析し build_gsheet(..., watchlist_reels=...) に渡す
```

### ウォッチリスト分析シート（自動）

`build_gsheet()` 実行時に、通常リサーチとは別枠でウォッチリスト分析シート群を自動更新する。

- `ウォッチリスト リール一覧`
- `ウォッチリスト コンテンツ分析`
- `ウォッチリスト スコアリング`
- `ウォッチリスト インサイト`
- `ウォッチリスト アカウント別サマリー`
- `ウォッチリスト サマリー`（OpenAI によるナラティブ。`run_watchlist_research.py` が更新）

これらは **紫ヘッダー（#4A148C）** で通常リサーチと視覚的に区別される。  
またウォッチリスト系にも7日経過ルールが適用され、古い投稿は以下へ連動移動される。

- `過去ウォッチリスト リール`
- `過去ウォッチリスト スコアリング`
- `過去ウォッチリスト インサイト`

### Step 3: 詳細データ補完（フォロワー数・再生回数が欠けている場合のみ）

**補完アクター1**: `apify/instagram-reel-scraper`（リールURL → 詳細データ）
```python
def scrape_reel_details(reel_urls, apify_token):
    url = "https://api.apify.com/v2/acts/apify~instagram-reel-scraper/runs"
    headers = {"Authorization": f"Bearer {apify_token}", "Content-Type": "application/json"}
    resp = requests.post(url, json={"urls": reel_urls}, headers=headers, params={"waitForFinish": 180})
    if resp.status_code != 201:
        return []
    dataset_id = resp.json()["data"]["defaultDatasetId"]
    return requests.get(f"https://api.apify.com/v2/datasets/{dataset_id}/items", headers=headers).json()
```

**補完アクター2**: `apify/instagram-scraper`（プロフィール → フォロワー数）
```python
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
```

---

## Phase 2: 自動フィルタリング ★核心部分

収集した全リールに対して、Pythonで3条件を**自動判定**し、通過したものだけを分析対象にする。
ユーザーの判断・手動確認は一切不要。

### フィルタリングスクリプト

```python
from datetime import datetime, timedelta

def extract_field(item, field_names, default=None):
    """Apifyのフィールド名がアクターによって異なるため、複数候補から取得"""
    for name in field_names:
        val = item.get(name)
        if val is not None:
            return val
    return default

def filter_buzz_reels(all_reels, days=7, min_views=30000):
    """
    3条件フィルタリング:
    1. 直近{days}日以内に投稿
    2. 再生回数 ≧ {min_views}
    3. 再生回数 ≧ フォロワー数
    
    Returns: (通過リスト, 除外リスト（理由付き）)
    """
    cutoff_date = datetime.now() - timedelta(days=days)
    passed = []
    rejected = []
    
    for reel in all_reels:
        # --- 各フィールドを柔軟に取得 ---
        views = extract_field(reel, [
            'videoPlayCount', 'plays', 'viewCount', 'video_play_count',
            'playCount', 'views', 'videoViews'
        ], 0)
        
        followers = extract_field(reel, [
            'followersCount', 'followers', 'followerCount',
            'ownerFollowers', 'user_followers', 'follower_count'
        ], 0)
        
        timestamp_raw = extract_field(reel, [
            'timestamp', 'takenAt', 'taken_at', 'createdAt',
            'created_at', 'publishedAt', 'postedAt', 'date'
        ])
        
        reel_url = extract_field(reel, [
            'url', 'webLink', 'permalink', 'shortCode'
        ], '')
        if reel_url and not reel_url.startswith('http'):
            reel_url = f"https://www.instagram.com/reel/{reel_url}/"
        
        username = extract_field(reel, [
            'ownerUsername', 'username', 'owner_username', 'user'
        ], '不明')
        
        # --- 日付パース ---
        posted_date = None
        if timestamp_raw:
            if isinstance(timestamp_raw, (int, float)):
                posted_date = datetime.fromtimestamp(timestamp_raw)
            elif isinstance(timestamp_raw, str):
                for fmt in ['%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ',
                            '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                    try:
                        posted_date = datetime.strptime(timestamp_raw, fmt)
                        break
                    except ValueError:
                        continue
        
        # --- 数値を安全にint化 ---
        try:
            views = int(views) if views else 0
        except (ValueError, TypeError):
            views = 0
        try:
            followers = int(followers) if followers else 0
        except (ValueError, TypeError):
            followers = 0
        
        # --- 3条件フィルタ ---
        rejection_reasons = []
        
        # 条件1: 直近7日以内
        if posted_date is None:
            rejection_reasons.append("投稿日不明")
        elif posted_date < cutoff_date:
            rejection_reasons.append(f"投稿日が7日超前({posted_date.strftime('%Y-%m-%d')})")
        
        # 条件2: 再生 ≧ 30,000
        if views < min_views:
            rejection_reasons.append(f"再生{views:,} < {min_views:,}")
        
        # 条件3: 再生 ≧ フォロワー数
        if followers > 0 and views < followers:
            rejection_reasons.append(f"再生{views:,} < フォロワー{followers:,}")
        elif followers == 0:
            rejection_reasons.append("フォロワー数不明")
        
        # --- 結果振り分け ---
        enriched = {
            'url': reel_url,
            'username': username,
            'views': views,
            'followers': followers,
            'likes': extract_field(reel, ['likesCount', 'likes', 'like_count'], 0),
            'comments': extract_field(reel, ['commentsCount', 'comments', 'comment_count'], 0),
            'posted_date': posted_date.strftime('%Y-%m-%d') if posted_date else '不明',
            'caption': extract_field(reel, ['caption', 'text', 'description'], ''),
            'hashtags': extract_field(reel, ['hashtags', 'tags'], []),
            'music': extract_field(reel, ['musicInfo', 'audioTitle', 'music', 'audio'], ''),
            'duration': extract_field(reel, ['videoDuration', 'duration', 'video_duration'], ''),
            'buzz_ratio': round(views / followers, 1) if followers > 0 else 0,
            'engagement_rate': round((int(extract_field(reel, ['likesCount','likes','like_count'], 0) or 0)
                                    + int(extract_field(reel, ['commentsCount','comments','comment_count'], 0) or 0))
                                    / views * 100, 2) if views > 0 else 0,
            'raw_data': reel  # 元データも保持
        }
        
        if not rejection_reasons:
            passed.append(enriched)
        else:
            enriched['rejection_reasons'] = rejection_reasons
            rejected.append(enriched)
    
    # バズ倍率降順でソート
    passed.sort(key=lambda x: x['buzz_ratio'], reverse=True)
    
    return passed, rejected
```

### フィルタ結果が5件未満の場合 → 条件は絶対に変えない。後追いリサーチで候補を増やす

**条件の緩和は絶対にしない。** 7日以内・3万再生以上・再生≧フォロワーの3条件は不変。
代わりに、追加の検索キーワードでリール候補の母数を増やし、同じ条件で再フィルタする。

```python
def chase_research(current_reels, current_passed, keywords_used, apify_token, max_rounds=3):
    """
    後追いリサーチ: フィルタ通過が5件未満なら追加検索→同条件で再フィルタ
    条件は絶対に緩和しない。
    """
    all_reels = list(current_reels)
    passed = list(current_passed)
    seen_urls = {r.get('url','') for r in all_reels}
    used_keywords = set(keywords_used)
    
    round_num = 0
    while len(passed) < 5 and round_num < max_rounds:
        round_num += 1
        
        # 追加キーワードを自動生成（Claude側で生成する）
        # 例: 元が「ダイエット」→ ラウンド1:「食事制限」「カロリー」
        #     ラウンド2:「ボディメイク」「宅トレ」
        #     ラウンド3:「糖質制限」「ファスティング」
        new_keywords = generate_additional_keywords(keywords_used, round_num)
        new_keywords = [kw for kw in new_keywords if kw not in used_keywords]
        
        if not new_keywords:
            break
        
        # 追加検索
        for kw in new_keywords:
            new_items = search_reels(kw, apify_token, max_results=30)
            used_keywords.add(kw)
            for item in new_items:
                url = extract_field(item, ['url','webLink','permalink','shortCode'], '')
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_reels.append(item)
        
        # 同じ条件で再フィルタ（条件変更なし）
        passed, rejected = filter_buzz_reels(all_reels, days=7, min_views=30000)
    
    return passed, rejected, all_reels, round_num
```

**後追いリサーチのキーワード展開ルール**（Claudeが実行時に判断）:

| ラウンド | 展開方法 | 例（元キーワード:「ダイエット」） |
|---------|---------|-------------------------------|
| 1 | 同ジャンルの類義語 | 食事制限、カロリー管理 |
| 2 | 周辺ジャンル | ボディメイク、宅トレ、有酸素運動 |
| 3 | 切り口を変える | 糖質制限、ファスティング、腸活ダイエット |

**最大3ラウンドまで後追い。** それでも5件未満なら、そのキーワードでは条件を満たすバズリールが少ないことをユーザーに正直に報告し、取得できた件数で分析を実行する。

### フィルタリング結果のレポート

**本番一括（`run_production_research.py` を Bash で実行した場合）**は、ユーザーへの返答はスキル冒頭の **「リサーチ完了！○件収集→○件通過。スプレッドシートに反映済み: URL」** のみとし、下記の長文ブロックは使わない。

手動・フォールバックでフィルタのみ説明する場合の参考フォーマット:

```
📊 フィルタリング結果:
━━━━━━━━━━━━━━━
収集リール数: 87件
  ↓ 直近7日以内: 34件
  ↓ 再生3万以上: 18件
  ↓ 再生≧フォロワー: 12件 ✅
━━━━━━━━━━━━━━━
分析対象: 12件（バズ倍率順）
適用条件: 標準条件（7日以内・3万再生以上・再生≧フォロワー）
```

---

## Phase 3: 伸び要素の分解・抽出（フィルタ通過リールのみ）

> ⚠️ **以下は旧スキーマ（5シート構造）の説明。現在は廃止され、すべてのデータが1枚のスナップショットタブに転置レイアウトで書き込まれる。「新スキーマ（スナップショット形式）」セクションを参照のこと。**

フィルタを通過したリールのみを対象に、以下の分析を実行する。
スコアリング詳細基準は `references/scoring_guide.md` を参照。

### Sheet 1: リール一覧（基本データ）
| カラム | 内容 |
|--------|------|
| No. | バズ倍率順の連番 |
| リールURL | ハイパーリンク |
| アカウント名 | @username |
| フォロワー数 | 数値 |
| 再生回数 | 数値 |
| いいね数 | 数値 |
| コメント数 | 数値 |
| 投稿日 | YYYY-MM-DD |
| バズ倍率 | 再生 ÷ フォロワー（〇〇x） |
| エンゲージメント率 | (いいね+コメント) ÷ 再生 ×100（%） |

### Sheet 2: コンテンツ分析
Apifyで取得したキャプション・音源・動画尺を元にClaude が分析:
| カラム | 分析内容 |
|--------|---------|
| フック（冒頭3秒） | キャプション冒頭 + 動画構成から推測 |
| 動画構成 | 問題提起→解決→CTA等のフロー |
| CTA | フォロー・保存・コメント誘導の有無と手法 |
| 使用音源 | 音源名＋トレンド音源かの判定 |
| テキスト/テロップ | キャプションから推測される活用法 |
| 動画尺 | 秒数 |
| キャプション要約 | 要点を3行以内に |
| ハッシュタグ | 全タグ列挙 |
| ジャンル | 自動分類 |

### Sheet 3: バズ要因スコアリング
各項目を1-5で採点し、加重平均で総合スコアを算出:

| 評価項目 | 重み | 判定基準の要点 |
|----------|------|---------------|
| フックの強さ | ×1.5 | 冒頭で衝撃・矛盾・緊急性があるか |
| 共感・自分ごと化 | ×1.3 | 「自分のことだ」と感じさせるか |
| 情報の希少性 | ×1.2 | 「初めて知った」と思わせるか |
| 保存したくなる度 | ×1.2 | ハウツー・チェックリスト等の実用性 |
| シェアしたくなる度 | ×1.0 | 「誰かに教えたい」と思うか |
| 編集クオリティ | ×0.8 | テンポ・SE・テロップの品質 |
| トレンド活用度 | ×1.0 | 旬のトレンドを巧みに活用しているか |

**総合スコア** = (フック×1.5 + 共感×1.3 + 希少性×1.2 + 保存×1.2 + シェア×1.0 + 編集×0.8 + トレンド×1.0) ÷ 8.0

**バズ仮説**: なぜこのリールが伸びたか（主要因・補助要因・タイミング要因の3文）

### Sheet 4: ディープインサイト
| カラム | 内容 |
|--------|------|
| 同ジャンル比較 | フィルタ通過リール全体の中での突出点 |
| トレンド文脈 | キーワードが今伸びている背景（季節・社会的関心・プラットフォーム動向） |
| 再現可能な要素 | ユーザーのアカウントで即応用できるポイント |
| 改善提案 | このリールをさらに伸ばすには |
| 自分用リメイク案 | ユーザーが同テーマで作る場合の具体的な構成（冒頭→中盤→終盤→CTA） |

### Sheet 5: リサーチサマリー（通常キーワードリサーチ専用）
- **ウォッチリストの内容は含めない**（トレンド分析がジャンル混在でぼやけるのを防ぐ）
- セクション1: 実施日・キーワード・対象期間・適用条件・収集数・フィルタ通過数
- セクション2: **今回のトレンド分析**（ジャンル別伸び／急上昇フックTOP3／構成パターンTOP3／CTA／アルゴリズム傾向）
- セクション3: **バズ倍率TOP3**ごとの詳細（なぜその順位か、フック→構成→CTA、再現ステップ）
- セクション4: 共通バズ要因パターン（3〜5個）
- セクション5: アクション5件（何を／どうやって／期待効果）

### Sheet: ウォッチリスト サマリー（ウォッチリスト専用リサーチで更新）
- セクション1: 登録アカウント数・収集リール数・分析日
- セクション2: アカウント別（リンク、強み、取り入れる要素TOP3、注意点）
- セクション3: 横断パターン＋自分のジャンル（例: ダイエット・健康）への応用アイデア3つ

### Sheet: リサーチ横断トレンド
- **通常リサーチの「リール一覧」「過去リサーチリール」由来のみ**で集計。ウォッチリストのリールは含まない。

---

## Phase 4: xlsx出力

xlsx スキル（`/mnt/skills/public/xlsx/SKILL.md`）を参照して作成。
スキル内の `scripts/generate_xlsx.py` をテンプレートとして活用。

**フォーマット**:
- ヘッダー: 太字・#1F4E79背景・白文字
- データ行: ゼブラストライプ
- URL: ハイパーリンク
- スコア: 条件付き色分け（1-2=赤 #FFCCCC、3=黄 #FFFFCC、4-5=緑 #CCFFCC）
- バズ倍率: 高い順にソート
- ファイル名: `IG_Reel_Research_{キーワード}_{YYYYMMDD}.xlsx`

### Google Sheets への自動書き込み（xlsx 直後）

`scripts/pipeline.py` の `build_xlsx()` は、xlsx 保存後に `scripts/gsheets.py` の `build_gsheet()` を**内部で自動呼び出し**する。  
つまり、**`build_xlsx()` を呼べば Google Sheets も自動同期されるため、`gsheets.py` を別途呼ぶ必要はない**。

この自動同期で、同じシート構成（リール一覧・コンテンツ分析・バズ要因スコアリング・ディープインサイト・リサーチサマリー）を Google スプレッドシートへ書き込む。

- **スプレッドシートID（固定）**: `YOUR_SPREADSHEET_ID_HERE`
- **呼び出し例**: `build_gsheet(keyword, reels, content, scoring, insights, summary, condition_label, spreadsheet_id="YOUR_SPREADSHEET_ID_HERE")`  
  `spreadsheet_id` を省略した場合も、スクリプト側のデフォルトが上記IDと一致する。
- **前提**: 環境変数 `GOOGLE_SHEETS_CREDENTIALS` にサービスアカウントJSONのパスを設定（`gsheets.py` が参照）。依存: `gspread`、Google Sheets API 有効化済みの認証情報。

**過去リサーチのアーカイブ（自動）**

- `build_gsheet()` を実行するたび、**リサーチ日（当日）**を基準に、「リール一覧」の**投稿日が7日より前**のリールを検出し、リールURL単位で **「過去リサーチリール」** シートへ移動する（メインの「リール一覧」「コンテンツ分析」「バズ要因スコアリング」「ディープインサイト」の4シートから該当行を削除）。
- 「過去リサーチリール」は**既に移動済みのURLは重複追加せず**、新規分のみ追記して**累積**する。スプレッドシート上で**リサーチ履歴として参照**できる。

書き込み成功時は `build_gsheet()` がスプレッドシートの編集URLを返すので、ユーザーに xlsx とあわせて共有する。

---

## フォールバック: Apify APIキー未設定時

**通常はまず本番ワンライナー（Bash）を試す。** 実行が不可能なときだけ、代替として `web_search` 等を検討する。ユーザーに**手順の列挙やコンソール操作の説明はしない**（不足している前提だけ短く伝える）。

---

## 実行時チェックリスト

### 本番一括（`run_production_research.py`・推奨）

1. [ ] ユーザーからキーワードを受け取った（1語以上）
2. [ ] **Bash ツール**でスキル記載のワンライナーを実行した（ユーザーにコマンドや手順を見せていない）
3. [ ] ログから収集件数・通過件数を読み取り、**「リサーチ完了！○件収集→○件通過。スプレッドシートに反映済み: URL」**で報告した

### 参考（スクリプト内部・手動実装時の工程メモ）

4. [ ] 類義語・関連語を3-4個自動生成した
5. [ ] Apify APIでリール候補を収集した
6. [ ] フォロワー数が欠けているリールがあれば補完した
7. [ ] **自動フィルタリング**を実行した（7日以内 / 3万再生以上 / 再生≧フォロワー）
8. [ ] 通過リールのコンテンツ分析を実行した
9. [ ] バズ要因スコアリングを実行した
10. [ ] ディープインサイト（競合比較・改善提案・リメイク案）を作成した
11. [ ] リサーチサマリーを作成した
12. [ ] 5シート構成xlsxを出力した
13. [ ] `build_xlsx()` 実行で Google Sheets まで自動同期された（`gsheets.py` の別呼び出しは不要）
14. [ ] xlsx スキルの参照を確認した
