# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

コンビニ店舗の**事務所内金庫の現金**を管理するアプリ。

想定する機能の軸:

- 金種別（10000 / 5000 / 1000 / 500 / 100 / 50 / 10 / 5 / 1 円）の枚数管理
- 入出金の記録（釣銭準備金の払い出し、売上金の入金・回収 など）
- 残高照合と過不足チェック（帳簿残高 vs 実査カウントの突き合わせ）

実装は `index.html` 1ファイルのみ。タブは「実査／入出金／残高／履歴」の4画面構成（起動時は実査タブ。毎日の実査が最初の作業のため）。ヘッダーに現在日時（曜日・秒付き）と同期状態チップを常時表示。

GitHub Pages（https://takemiyu2008-ops.github.io/Cash-Handling/ ）で公開し、店舗スタッフが各自のスマホ・PCで開いて使う。

## アーキテクチャ

- 状態は `state = { version, nextId, baseAmount, counts, transactions }` の1オブジェクト。localStorage キーは `kinko-cash-v1`
- `baseAmount` は金庫の基準額（デフォルト500,000円、残高タブから変更可）。残高・実査の両タブで基準額との差額を表示する
- `counts` は金種→帳簿上の枚数。`transactions[].delta` は金種→**符号付き枚数**（＋が金庫に入る方向）で、残高は取引の適用結果として更新される
- 取引種別は `TYPE_LABELS` に定義（売上金入金・釣銭調達・釣銭払い出し・銀行預け入れ・その他入出金・実査調整・取消）
- 取引の削除はしない方針。誤記録は「取消」＝逆方向の取引（`reversal`）を追加して打ち消す（監査証跡を残すため）
- 実査は実枚数を入力→帳簿との差異（過不足）を取引 `audit` として記録し、帳簿を実査値に合わせる
- 実査の硬貨は棒金（`ROLL_SIZE`=50枚/本）＋バラ枚数の2入力。`readCounts` が `{prefix}-roll-{金種}` の存在を見て自動合算する
- 実査時に「損傷金」（破損札・変形硬貨の合計額、参考記録）を入力可。`tx.damaged` に保存され、履歴・CSV・残高タブ（前回実査分）に表示。金種別枚数には損傷分も含めて数える運用
- 出金は金種ごとに帳簿枚数を超えられない（バリデーションあり）
- CSV書き出し（UTF-8 BOM付き）、JSONバックアップ／復元機能あり

### クラウド同期（Firebase Realtime Database）

- Firebase SDK は使わず **REST API（fetch のみ）** で同期。DB URL はソース内定数 `SYNC_DB_URL`（空文字なら同期無効＝完全ローカル動作）。セットアップ手順は `SETUP.md`
- 保存先は `/stores/<storeId>`。`storeId` は共有パスコードの SHA-256（`deriveStoreId`）。端末には storeId のみ localStorage キー `kinko-sync-v1` に保存（パスコード原文は保存しない）
- RTDB ノードは `{ meta: {rev, at}, data: state }` の2層。60秒ポーリングは軽量な `meta` のみ GET し、`rev` が変わった時だけ全体同期（無料枠の転送量対策）
- 同期は「GET（ETag付き）→ `mergeStates` で統合 → 差分があれば `if-match` 条件付き PUT、412 なら再試行（最大3回）」
- **counts = 全 transactions の delta の総和**という不変条件を利用し、マージは「取引 `uid` の和集合 → at順ソート → `id` 振り直し → counts 再計算」。取消の相互参照は連番 `id` ではなく `uid` で行う（`reverses`/`reversedBy`）
- `state.gen`（世代番号）が異なる場合は新しい方が全取り。全置換操作（`resetAll`・`importJson`）は新しい `gen` を採番して全端末に伝播させる
- RTDB は空オブジェクト・空配列を保存しない仕様のため、`normalizeState()` で `tx.delta` / `transactions` 等の欠落を必ず補完してから使う（照合一致の取引は `delta: {}`）
- 各操作は「先に localStorage 保存 → 後から同期」。オフラインでも従来どおり動き、復帰時（online イベント / visibilitychange / ポーリング / 次回保存）に追いつく

## 技術方針（ユーザーと合意済みの制約）

- 単一の `index.html` に HTML / CSS / JS をすべてインラインで書く。ビルドツール・外部依存・CDN は使わない（Firebase も SDK は読み込まず REST + fetch のみ）
- データ永続化は `localStorage`（JSON）＋ Firebase RTDB への同期（設定時のみ）。自前サーバーは持たない
- UI は日本語
- 金額は**円の整数**で扱う（浮動小数点で金額計算をしない）

## 実行方法

```bash
open index.html
```

ブラウザで開くだけ。ビルド・テスト・lint の仕組みは存在しない。
