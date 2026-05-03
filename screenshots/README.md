# スクショ撮影リスト

このフォルダにスクショ画像を入れて、配布パッケージを完成させてください。
README_社内向け.md / セットアップ手順書.md / 運用ガイド.md の各所で参照されています。

## 撮影リスト（推奨：すべてPNG・幅1200px程度・モザイク要らない範囲）

### 必須（最小セット・10枚）

- [ ] **`0_環境準備完了.png`**
  ターミナルで `which ffmpeg && python3 -c "import whisper; print('whisper OK')"` を実行した結果
  （`/opt/homebrew/bin/ffmpeg` と `whisper OK` が並んで見える）

- [ ] **`1_apify_token.png`**
  Apify の "Create a new personal API token" ダイアログ
  （実トークンは映らないように。Description入力欄が見える状態）

- [ ] **`2_リサーチ結果タブ.png`** ⭐ 最重要
  スプシで「ダイエット」など実際のスナップショットタブを開いた状態
  - 行1のサマリーバナー
  - 行2のヘッダー（リール1〜5見える）
  - 行3〜の属性データ部分（最低でもURL〜再生回数まで）
  - 列幅・色がわかる
  - 個人特定可能なリール画像が含まれていれば必要に応じてモザイク

- [ ] **`2b_オプション_使い方ガイドタブ.png`**
  使い方ガイドタブの上半分（タブ構成説明あたり）

- [ ] **`3a_gcp_project_create.png`**
  Google Cloud Console の「新しいプロジェクト」作成画面

- [ ] **`3b_gcp_apis_enabled.png`**
  Sheets API・Drive API が有効化された状態の画面

- [ ] **`3c_service_account_role.png`**
  サービスアカウント作成時のロール選択（編集者）画面

- [ ] **`3d_create_key_json.png`**
  サービスアカウントの「キー」タブで「新しい鍵を作成」を選んだ状態

- [ ] **`4_spreadsheet_share.png`**
  Google スプレッドシートで「共有」ダイアログを開き、サービスアカウントメールに編集者権限を付与している状態

- [ ] **`6_test_run_complete.png`**
  Claude Code チャットで「ダイエットでリサーチして」→ 完了「リサーチ完了！○件…」 メッセージが表示された状態

### 推奨（あると親切・5枚）

- [ ] **`4_ウォッチリスト登録.png`**
  ウォッチリストタブにアカウントが2-3件登録されている状態

- [ ] **`5_star_marker.png`**
  キーワードリサーチ結果でウォッチ済みアカウントの列に ★ が付いた状態（紫背景の列ヘッダー）

- [ ] **`2_openai_key.png`**
  OpenAI Platform の API keys 一覧画面（キー本体は隠れている状態でOK）

- [ ] **`スプシ全体.png`**
  スプシのタブ一覧が見える状態（複数スナップショットが並んでいる）

- [ ] **`実行ログ.png`**
  ターミナルで実行中のログ（[FILTER] [TRANSCRIBE] [DONE] が出ている状態）

## 撮影のコツ

- **ウインドウサイズ**：1200x800 程度を推奨（縮小しても見やすい）
- **個人情報の扱い**：
  - APIキー・トークンの実値が映る場合は **モザイク必須**（マスク漏れ事故防止）
  - スプシID は判別できないように一部マスクするか、テスト用スプシのIDなら問題なし
  - サービスアカウントのメールアドレスは社内共有なら通常OK（外部公開時はマスク）
- **macOS スクショ**：
  - `Cmd + Shift + 4` → ウィンドウ単位で `Space + クリック`
  - スクショ範囲を `Cmd + Shift + 5` で動画録画も可（Loom / Quicktime も併用OK）
- **画像形式**：PNG推奨（圧縮ノイズなし）

## 完了したら

すべて撮影できたら、このフォルダに置いた状態で配布パッケージを zip 化してください：

```bash
cd ~/Downloads
zip -r ig-reel-research-share-20260504.zip share-ig-reel-research/ -x "share-ig-reel-research/ig-reel-research/scripts/__pycache__/*" -x "share-ig-reel-research/ig-reel-research/scripts/output/*"
```

→ Google Drive の社内共有フォルダにアップロード → リンクをチームに送付。
