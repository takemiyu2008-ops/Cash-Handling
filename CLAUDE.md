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

- 状態は `state = { version, gen, nextId, baseAmount, baseAmountAt, counts, damaged, tax, transactions }` の1オブジェクト。localStorage キーは `kinko-cash-v1`
- `baseAmount` は金庫の基準額（デフォルト500,000円、残高タブから変更可）。残高・実査の両タブで基準額との差額を表示する
- `counts` は金種→帳簿上の枚数。`transactions[].delta` は金種→**符号付き枚数**（＋が金庫に入る方向）で、残高は取引の適用結果として更新される
- **金庫の総額 = `bookTotal()`（金種別合計）+ `state.damaged`（損傷金）**。ヘッダーの「金庫総額」・残高タブの「総額」・基準額との比較はすべてこの総額で行う
- 取引種別は `TYPE_LABELS` に定義。入出金タブで選べるのは入金・出金それぞれ「売掛金 / ニチイ / ガソリン代 / オーナー持ち出し分 / 消費税 / その他」の6項目（`ar_*`・`nichii_*`・`gas_*`・`owner_*`・`tax_*`・`misc_*` の `_in` / `_out`。ただし `tax_*` は金額のみ入力の別枠扱い＝後述）＋レジ内の入出金「現金仕入 / 売掛金 / 売掛金回収」の3項目（`reg_purchase_*`・`reg_ar_*`・`reg_collect_*` の `_in` / `_out`。金額のみ入力の記録専用＝後述）＋システム生成の `audit`・`reversal`。旧種別（`sales_in`・`change_supply`・`other_in`・`change_out`・`bank_deposit`・`other_out`）は**過去の取引を履歴・CSVに正しく表示するためだけに残してある**（新規には選べない）。新「その他」に旧 `other_in`/`other_out` を再利用しないのはこのため（過去取引のラベルが書き換わってしまう）。入金/出金の符号とバッジは `IN_TYPES` から導出するので、旧種別も同 Set に含めておくこと
- 取引の削除はしない方針。誤記録は「取消」＝逆方向の取引（`reversal`）を追加して打ち消す（監査証跡を残すため）
- 実査は実枚数を入力→帳簿との差異（過不足）を取引 `audit` として記録し、帳簿を実査値に合わせる
- 実査の硬貨は棒金（`ROLL_SIZE`=50枚/本）＋バラ枚数の2入力。`readCounts` が `{prefix}-roll-{金種}` の存在を見て自動合算する
- **直近の実査は修正できる**（実査タブの「前回の実査を修正する」→ 修正モード）。修正は「元 `audit` の取消（`reversal`）＋新しい `audit`」の2取引で行う（直接書き換えはしない＝上記の削除禁止方針どおり）。実査時に数えた実枚数は保存していないが、不変条件から導出する（`actual = counts − Σ(実査より後の取引の delta)`、損傷金も同型。`buildAuditSnapshot`）。新 audit の delta は「修正後の実枚数 − 実査時点の帳簿」＝正しく数えていたら記録されたはずの過不足。ガードは reversal 単体ではなく**2取引適用後の最終状態**で行う（counts / damaged がマイナスにならないこと）
  - 新 audit には `fixes: 元auditのuid` を付け、`mergeStates` がオフライン2端末の同時修正による補正の二重適用を先勝ちで排除する（取消の `reverses` dedup と同型）
  - 修正モードの状態はメモリ上の `auditEdit`（対象 uid のみ）。同期で対象が取消された・より新しい実査が入った場合は `renderAuditEditUI` が自動解除する
- 「損傷金」（破損札・変形硬貨）は金種別枚数とは**別枠**で管理する。実査時に合計額を入力する運用で、金種別枚数には**含めない**
  - `state.damaged` が損傷金の帳簿残高（円）、`tx.damagedDelta` がその取引による符号付き増減。**`state.damaged` = 全 `damagedDelta` の総和**という不変条件を `counts` と同じ形で持たせ、`mergeStates` が同じやり方で再計算できるようにしてある
  - 実査は「入力値 − `state.damaged`」を `damagedDelta` として記録する。取消（`reversal`）は `-tx.damagedDelta` を持つ
  - 旧形式の `tx.damaged`（金種別枚数に含めて数えていた頃の参考値）は履歴・CSVの表示互換のためだけに読む。遡って解釈し直すと当時の帳簿が壊れるため移行はしない
- 「消費税」（スタッフから徴収する国への預り金）も金種別枚数とは**別枠**で管理する。損傷金との違いは2点: (1) **金庫総額に含めない**（国に納めるお金のため、ヘッダー・残高タブ・基準額比較・実査の照合計算のすべてから除外し、実査・残高タブに参考表示するだけ）、(2) 増減は実査ではなく**入出金タブの種別 `tax_in`/`tax_out`** で記録する（選ぶと金種別入力が金額のみの入力欄 `entryTaxAmount` に切り替わる。`TAX_TYPES` で判定）
  - `state.tax` が消費税の帳簿残高（円）、`tx.taxDelta` が符号付き増減。**`state.tax` = 全 `taxDelta` の総和**の不変条件・`mergeStates` 再計算・`normalizeState` の0落ち補完・取消時の `-tx.taxDelta` は損傷金とすべて同型。取引の `delta` は空 `{}` で金種別枚数は動かさない
  - 出金（`tax_out`）は `state.tax` を超えられない（バリデーションあり）
- 「レジ内の入出金」（レジでの現金仕入・売掛金・売掛金回収）は**記録専用の別枠種別**（`REG_TYPES` で判定）。金庫の金種別枚数・損傷金・消費税・金庫総額・実査照合のいずれにも影響せず、**残高も持たない**（`state` に対応フィールドなし。レジには売上金など本アプリで扱わない出入りがあるため残高は意味を持たない）
  - 取引は `delta: {}` で、符号付き金額 `tx.regAmount` のみを持つ（`tx.amount` には算入される）。入力は消費税と同じ金額のみ方式（`entryTaxWrap`／`entryTaxAmount` を共用し、説明文だけ `entryTaxHint`／`entryRegHint` で切替）
  - 取消（`reversal`）は `-tx.regAmount`。残高が無いためマイナス化ガードは不要。state に寄与しないため `mergeStates`／`normalizeState` は無変更で対応できている。CSVには「レジ増減」列として出力
- 出金は金種ごとに帳簿枚数を超えられない（バリデーションあり）
- CSV書き出し（UTF-8 BOM付き）、JSONバックアップ／復元機能あり

### クラウド同期（Firebase Realtime Database）

- Firebase SDK は使わず **REST API（fetch のみ）** で同期。DB URL はソース内定数 `SYNC_DB_URL`（空文字なら同期無効＝完全ローカル動作）。セットアップ手順は `SETUP.md`
- 保存先は `/stores/<storeId>`。`storeId` は共有パスコードの SHA-256（`deriveStoreId`）。端末には storeId のみ localStorage キー `kinko-sync-v1` に保存（パスコード原文は保存しない）
- RTDB ノードは `{ meta: {rev, at}, data: state }` の2層。60秒ポーリングは軽量な `meta` のみ GET し、`rev` が変わった時だけ全体同期（無料枠の転送量対策）
- 同期は「GET（ETag付き）→ `mergeStates` で統合 → 差分があれば `if-match` 条件付き PUT、412 なら再試行（最大3回）」
- **counts = 全 transactions の delta の総和**（および **damaged = 全 damagedDelta の総和**）という不変条件を利用し、マージは「取引 `uid` の和集合 → at順ソート → `id` 振り直し → counts / damaged 再計算」。取消の相互参照は連番 `id` ではなく `uid` で行う（`reverses`/`reversedBy`）
- 取引の `at` は**端末内で単調増加を保証**する（`addTransaction` が直前の取引と同時刻以下なら +1ms する）。実査修正のように2取引を連続追加すると同一ミリ秒になり、マージの at 順ソートで順序が入れ替わって実枚数の導出が狂うため
- `state.gen`（世代番号）が異なる場合は新しい方が全取り。全置換操作（`resetAll`・`importJson`）は新しい `gen` を採番して全端末に伝播させる
- RTDB は空オブジェクト・空配列を保存しない仕様のため、`normalizeState()` で `tx.delta` / `transactions` / `damaged`（0のとき落ちる）等の欠落を必ず補完してから使う（照合一致の取引は `delta: {}`）
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
