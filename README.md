# ええAIdea（ee-aidea）

※「AIdea」という名前の別のアプリ・サービスとは無関係です。

> **ee-aidea** ("ええAIdea") is a tiny kit (Python standard library only, files only) for running AI sub-agents as "departments" with a hash-chained usage ledger, a record-only bidding log, and human approval (green / yellow / red, STOP).
> It never moves real money, has nothing to do with crypto assets, and never touches your AI account credentials.
> The main documentation below is in Japanese; the bidding part is experimental and its usefulness is unverified.

AI のサブエージェントを「課」に分け、**消費の帳簿**・**入札の記録**・**人間の承認（緑/黄/赤・STOP）**で動かすための、Python 標準ライブラリだけ・ファイルだけで動く最小キットです。
Claude Code のサブエージェントと一緒に使う例を同梱していますが、他の道具でも同じ考え方で使えます。
名前は「AI」と「アイディア」を関西弁の「ええアイディア」にかけたものです。配布名は `ee-aidea`、Python のパッケージ名は `ee_aidea`（ハイフンが使えないため）です。

## これは何か・何でないか

| する | しない |
|---|---|
| 課ごとの「持ち分」（内部の目安の点数、単位は既定 `pt`）を、追記のみの帳簿で管理する | 現実のお金を動かす。点数を現実のお金・外部のポイント・暗号資産と交換する（譲渡・換金・売買の機能はない） |
| サブエージェントを1回動かしたら、使ったトークン数から「API の従量課金で払ったらいくらか」の参考換算を出し、持ち分から引く | AI サービスの認証情報（ログイン・APIキー）を読む・保管する・中継する。利用者は自分のアカウントで自分の AI 道具にログインして使う |
| 次に動かす候補を「入札」として記録する（**記録だけ。試験的で、効果は未検証**） | 落札に従ってサブエージェントを自動で起動する |
| 変更の提案を色分けし、人間の返事（4文字・CANCEL・STOP）に従って、練習用フォルダ（git 管理）に反映する | 練習用フォルダ以外を書き換える。ネットに出る（git を呼ぶだけ） |

## 前提

- **Python 3.10 以上**（3.10.12 と 3.13 でテストを確認）。追加のパッケージは不要
- **git**（承認係が練習用フォルダへの反映に使う）
- AI の道具（例: Claude Code）は、利用者が自分のアカウントで使う。このキットはその道具を起動しない

## 全体の流れ

```
 課（設定で命名）            帳簿 ledger              入札 auction（記録だけ）
 research / engineering /    grant-month で配る        decide: 次の回の候補を点数順に記録
 qa ...                      spend: 参考換算を引く      actual: 実際に動かしたものを記録
     │                       summary: 最終ハッシュ      settle: 結果の答え合わせ
     │  サブエージェントを1回動かす   ▲                         │
     └──────────────────────────────┘                         │（作業は人と司令塔が決める）
     │
     ▼  変更があれば
 承認係 approval（ファイルモード）
   new / send で提案 → 人間が reply（4文字 / CANCEL / STOP）→ run が照合
   緑: 知らせたら自動で反映 ／ 黄: 4文字で反映 ／ 赤: 4文字＋待機（既定24時間）のあと反映
     │
     ▼
 練習用フォルダ（deploy_root。git で1件ずつコミット。戻すのも git で）
```

## 導入

1. このフォルダを好きな場所にコピーする（以下、このフォルダで作業する）。
2. 設定のひな形をコピーする。
   ```
   cp ee-aidea.example.json ee-aidea.json
   cp rates.example.json rates.json
   cp budgets.example.json budgets.json
   cp levels.example.json levels.json
   ```
   Windows の場合は `copy ee-aidea.example.json ee-aidea.json` のように読み替える。
3. `ee-aidea.json` の `departments` に課の名前を書く（英小文字で始まり、英小文字・数字・`_` `-` だけ、32文字まで。`treasury` は予約名）。
   日本語の表示名は `labels` に書ける。他へ移せない・減らせない課は `protected_depts` に書く（例: 検証担当）。
4. `rates.json` の単価は**すべて例**です。使うモデルの公式の料金ページで確認して書き換えてください。`fx_rate` は 1 USD を自分の通貨に換える率（これも例）。
5. `budgets.json` に月ごとの配分を書く（`total` を発行し、`protected_depts` の各課に `protected_min_per_case × protected_cases` を先に配り、残りを `split_depts` で等分。端数は `treasury` に残る）。
6. 承認係の準備（練習用フォルダ `sandbox/` と封印用の鍵ファイル `data/seal.key` を作る）。
   ```
   python -m ee_aidea.approval_cli setup
   python -m ee_aidea.approval_cli check
   ```
   `check` がすべて OK になれば準備完了です。`ee-aidea.json`・`data/`・`sandbox/`・`*.key` は同梱の `.gitignore` で git の管理から外してあります。

## 1サイクルの手順

以下は `ee-aidea.json` のあるフォルダで実行します（月や番号は例。月は `budgets.json` に書いた月と合わせ、ふだんは今月にする）。

1. **月初の発行**（同じ月は1回だけ）
   ```
   python -m ee_aidea.ledger grant-month 2030-01
   ```
2. **サブエージェントを1回動かす**（例: Claude Code で `agents/researcher.md` を使った調べもの）。終わったら、道具の表示や利用記録で入力・出力のトークン数を確認する。
3. **消費を記録する**（参考換算の分を、その課の持ち分から引く。足りなければ引けるだけ引いて不足を記録）
   ```
   python -m ee_aidea.ledger spend --dept research --model example-large --tokens-in 41000 --tokens-out 3200 --ref R-1 --note 競合調査
   ```
   サブスクの枠内で動かした場合も、参考換算は「API で払ったらいくらか」の目安です。実際に払った額があれば `--real-cost` に書く。
4. **集計と最終ハッシュ**
   ```
   python -m ee_aidea.ledger summary
   ```
   `--month 2030-01` のように付けると、その月の実績を出します（省くと今月）。最後の行の「帳簿の最終ハッシュ」を、**PC の外**（日報・Issue・チャットなど）に写しておく（理由は「正直な限界」）。
5. **入札（記録だけ・試験的）**: 各課の「次にやりたいこと」を入札ファイルに書き、点数順の候補を記録する。
   ```
   python -m ee_aidea.auction decide --round 20300101_0900 --slots 1 --bids bids.json
   python -m ee_aidea.auction actual --round 20300101_0900 --ran research:R-1
   python -m ee_aidea.auction report
   ```
   入札ファイルの形: `[{"dept":"research","ref":"R-1","task":"競合調査","max_bid":120,"expected_value":3000,"basis":"類似品の価格から見積もり"}]`（`basis` は5文字以上。短いと「根拠がない」で不可になります）
   点数 = 期待値 × 信用度 ÷ 上限。信用度は `settle` で記録した実績から計算します。**作業はこの結果に従いません**（何を動かすかは人と司令塔が決め、`actual` で記録して見比べるだけ）。
6. **変更を提案する**（まずは見本で試すのがおすすめ）
   ```
   python -m ee_aidea.approval_cli samples
   ```
   緑・黄・赤の見本が1件ずつでき、画面と `data/outbox.log` に内容が出ます。自分の変更なら:
   ```
   python -m ee_aidea.approval_cli new --diff change.diff --fields fields.json --send
   ```
   `change.diff` は練習用フォルダの中で `git diff` した出力そのまま、`fields.json` は `levels.json` の `required_fields` に挙げた項目（例: `{"summary": "...", "rollback": "...", "risk": "...", "reviewer_objection": "..."}`）。
7. **返事をして反映する**
   ```
   python -m ee_aidea.approval_cli reply c7e2        # 黄・赤の提案の先頭4文字（画面と outbox に出ている）
   python -m ee_aidea.approval_cli run --once        # 返事を読んで照合・反映（常駐させるなら --once を外す）
   python -m ee_aidea.approval_cli status
   ```
   緑は `run` のときに自動で反映されます。赤は4文字を返事したあと待機（既定24時間）し、待機中は `reply CANCEL` で破棄できます。15分（`max_message_age_minutes`）より古い承認の返事は処理しません。
8. **練習用フォルダに反映されたか確認する**: `sandbox/` の中のファイルと `git log`（1提案1コミット）。
9. **STOP と再開を試す**
   ```
   python -m ee_aidea.approval_cli reply STOP
   python -m ee_aidea.approval_cli run --once        # 再開用の4文字が画面と outbox に出る
   python -m ee_aidea.approval_cli reply <再開用の4文字>
   python -m ee_aidea.approval_cli run --once
   ```
   STOP で止まるのは**承認の反映だけ**です。ほかのプログラムは止まりません。停止をまたいだ赤の提案は、承認前の状態に戻ります。

`levels.json` を書き換えたら、内容を確認してから `python -m ee_aidea.approval_cli pin-levels` を実行するまで承認係は動きません。

## サブエージェント定義・運用ルールのひな形

- `agents/researcher.md`・`agents/engineer.md`・`agents/qa.md`: 課ごとの指示書の例。Claude Code では、プロジェクトの `.claude/agents/` に置くとサブエージェントとして使える形にしてあります（先頭の設定部分の書式は Claude Code の公式文書で確認してください）。普通の Markdown なので、他の道具では本文を指示として渡せます。
- `ORG.example.md`: 親のエージェント（司令塔）に渡す運用ルールの例。

## テストの動かし方

```
python -m unittest discover -s tests -v
```

ネットは使いません（git は使います）。テストは一時フォルダの中で動き、このフォルダには何も残しません（`__pycache__` を作りたくない場合は `python -B -m unittest discover -s tests` または環境変数 `PYTHONDONTWRITEBYTECODE=1`）。

## 正直な限界

- **帳簿の改ざん検知には限界があります。** 各行が直前の行のハッシュを持ち、最後の行のハッシュを `<帳簿>.head` に残すので、途中の行の書き換え・削除・最終行の差し替え・`head` と合わない追加は `verify` で見つかります。しかし、**ファイルを書ける者が帳簿と head を両方作り直すと、このPCの中だけでは検知できません**。対策は、`summary` が出す最終ハッシュを PC の外（日報・Issue・チャットなど）に写しておき、あとで突き合わせることです。中身の決まり（持ち分がマイナス、守られた課からの移動、二重発行など）は、作り直された帳簿でも `verify` が見ます。
- **`protected_depts` は過去の行にも当てはめて検査します。** 後から課を守る側に加えると、過去にその課から移した行が `verify` で異常になります。増やすのは月初の発行の前にしてください。課を `departments` から外した場合は、過去の行は読めますが、新しい追記はできません。
- **ファイルモードの限界**: 同じ PC でファイルを書けるもの（AI のサブエージェントを含む）は、`data/inbox.txt` に承認の返事を書けてしまいます。封印用の鍵 `data/seal.key` を読めるものは、提案や状態の記録を作り直せます。常駐していない間に `data/` をまるごと古い写しに戻されると、巻き戻しに気づけません。ファイルモードは「**一人で、AI に inbox・鍵・設定を書かせない設定にして使う練習用**」と考えてください。
  - 対策の例（Claude Code の場合）: プロジェクトの権限設定で、`data/`・`ee-aidea.json`・`levels.json` への書き込みと、承認係のコマンド（`approval_cli reply` など）の実行を禁止する。書き方の例は下のとおりですが、**書式・効き方は Claude Code の公式文書で確認してください（このキットでは未検証）**。シェルのコマンドを許すと、そこからファイルを書けてしまう点にも注意してください。
    ```json
    {
      "permissions": {
        "deny": ["Edit(data/**)", "Write(data/**)", "Edit(ee-aidea.json)", "Edit(levels.json)",
                 "Bash(python -m ee_aidea.approval_cli reply:*)"]
      }
    }
    ```
- **入札は試験的で、効果は未検証です。** 記録して見比べるための仕組みで、入札に従うと良い結果になるという根拠はまだありません。
- **参考換算は目安です。** 単価は利用者が書いた値で計算します。実際の請求額とは一致しません。
- 時刻は `utc_offset_hours`（既定 +9）で記録します。途中で変えると、月の区切りがずれます。

## 戻し方・やめ方

- 練習用フォルダの変更は1提案1コミットです。戻すときは `sandbox/` で `git log` を見て `git revert <コミット>` など（承認係の外での操作になるので、戻したあと `status` で状態を確認する）。
- 承認係を止める: 常駐中なら画面で Ctrl+C。承認だけ止めたいなら `reply STOP`。
- やめる: このフォルダごと消せば終わりです（外部への登録やサービスはありません）。帳簿・記録は `data/` にあります。残したい場合は先にコピーしてください。

## ファイル一覧

| ファイル | 内容 |
|---|---|
| `ee_aidea/ledger.py` | 帳簿（verify / summary / seal / grant-month / spend / transfer） |
| `ee_aidea/auction.py` | 入札の記録（decide / actual / settle / report） |
| `ee_aidea/approval.py` | 承認係の本体（差分の検査、色の判定、封印、STOP・CANCEL・待機・期限、反映） |
| `ee_aidea/approval_cli.py` | 承認係のコマンド（check / setup / samples / new / send / status / reply / run / pin-levels） |
| `ee_aidea/file_io.py` | ファイルだけで返事をやり取りする層（inbox / outbox） |
| `ee_aidea/config.py` | `ee-aidea.json` の読み込みと検査 |
| `*.example.json` | 設定のひな形（値はすべて例） |
| `agents/*.md`・`ORG.example.md` | 指示書・運用ルールのひな形 |
| `tests/` | テスト（標準ライブラリの unittest） |

## ライセンス

MIT（`LICENSE` を参照）。
