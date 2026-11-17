# TradingView Relay Server 運用＆統合仕様書 (v1.1.0)

TradingViewのPine Script v180と連携し、Discord（A/B/C各チャンネル）およびMT5へ自動判定配信を行う中継サーバー（relay_server.py）の最終仕様書です。

---

## 1. Discord通知チャンネル構成 & KEYマッピング

Render等の環境変数（KEY）に設定したWebhook URLに応じて、シグナルを全自動振り分け配信します。

| Render環境変数 (KEY) | Discordチャンネル役割 | 通知形式 | 主な配信タイミング |
| :--- | :--- | :--- | :--- |
| **`DISCORD_WEBHOOK_C_URL`** | **【チャンネルC】** エントリー通知 | 即時通知 | エントリー発生時 (`BUY` / `SELL`) |
| **`DISCORD_WEBHOOK_A_URL`** | **【チャンネルA】** 決済通知 | 即時通知 | 決済検出時 (`CLOSE_BUY` / `CLOSE_SELL`) ※Pips損益付き |
| **`DISCORD_WEBHOOK_B_URL`** | **【チャンネルB】** トレード完了成績カード | 確定カード | DB照合完了時 (エントリー価格・決済価格・Pips・保有条件) |
| **`MT5_ENDPOINT_URL`** | **【MT5連携】** 自動売買端末転送 | JSONシグナル | エントリーおよび決済発生時に即時転送 |

---

## 2. Discord通知デザイン & レイアウト仕様

すべての通知はコードブロック（```text ... ```）で囲まれ、Python側で表示幅が自動計算されるため、スマホ・PC問わず **`:`（コロン）の縦ラインが一直線に美しく整列** します。

### 🟢 【チャンネルC】エントリー通知 (即時)

```text
🟩【BUY 検知】
・銘柄  : GOLD
・価格  : 2680.50
・条件  : 確定足
・コード: VIDYA Signal v8
・日時  : 2026-11-17 01:00:00
```
*(※ SELL時は `🟥【SELL 検知】` に自動切り替え)*

---

### 🟦 【チャンネルA】決済通知 (即時)

```text
🟦【決済 検知】
・銘柄  : GOLD (BUY)
・価格  : 2695.20
・損益  : +147.0 Pips
・理由  : Band
・コード: VIDYA Signal v8
・日時  : 2026-11-17 02:15:00
```

---

### 🏆 【チャンネルB】トレード完了成績カード

```text
🏆【トレード完了】結果: +147.0 Pips
----------------------------------
・銘柄    : GOLD
・BUY価格 : 2680.50
・決済価格: 2695.20
・決済理由: Band
・コード  : VIDYA Signal v8
・決済日時: 2026-11-17 02:15:00
・MT5連携 : ON
----------------------------------
```
*(※ 負けトレード時は `⚠️【トレード完了】結果: -30.0 Pips` に自動切り替え)*

---

## 3. 422エラー＆タイムアウト完全回避ロジック

1. **HTTP 422 (Unprocessable Content) 回避**
   - Pine Script v180から送られるJSON (`action`, `symbol`, `price`, `time`, `trigger_mode`, `reason`) に合わせた受取処理を実装。
   - `alert_type` や `trade_id` が送られない場合でも、`action` 文字列からエントリー／決済を自動判別し、`銘柄+売買方向` でデータベース照合を完了させます。
   - テキスト通知やpingテストなどの非JSON通信が届いた場合でもエラーを返さず、`200 OK` で安全に流します。

2. **タイムアウト (Cold Start) 回避**
   - Render無料プランの15分スリープ対策として、外部の無料監視サービス等から Render URL へ定期的なPing設定を推奨します。

---

## 4. GitHub更新 & Renderデプロイ手順

1. ローカルの `relay_server.py` を本リポジトリの最新版コードへ差し替えます。
2. Gitコミット＆プッシュを実行：
   ```bash
   git add relay_server.py
   git commit -m "Update relay_server v1.1.0 (Align layout, fix 422 error, updated channels A/B/C)"
   git push origin main
   ```
3. Renderが自動検知してデプロイが完了したら、環境変数 `DISCORD_WEBHOOK_A_URL`, `DISCORD_WEBHOOK_B_URL`, `DISCORD_WEBHOOK_C_URL` を設定の上、運用を開始してください。
