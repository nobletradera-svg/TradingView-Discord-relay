import os
import json
import sqlite3
import datetime
import unicodedata
from typing import Optional, Union
from fastapi import FastAPI, Request, BackgroundTasks
import httpx

app = FastAPI(
    title="TradingView Relay Server",
    description="TradingViewのWebhookアラートを受信し、Discord各チャンネル(A/B/C)およびMT5へ並行配信・自動売買照合を行う中継サーバー",
    version="1.1.0"
)

# 環境変数からのWebhook URL設定 (Render等での運用に対応)
DISCORD_WEBHOOK_A = os.getenv("DISCORD_WEBHOOK_A_URL", "")  # 決済通知(即時)
DISCORD_WEBHOOK_B = os.getenv("DISCORD_WEBHOOK_B_URL", "")  # 成績カード
DISCORD_WEBHOOK_C = os.getenv("DISCORD_WEBHOOK_C_URL", "")  # エントリー通知(即時)
MT5_ENDPOINT_URL = os.getenv("MT5_ENDPOINT_URL", "")        # MT5発注連携用

DB_PATH = os.getenv("DB_PATH", "trades.db")

# ------------------------------------------------------------------------------
# データベース初期化 (エントリー情報の一次保持用)
# ------------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_trades (
            trade_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            volume REAL DEFAULT 0.1,
            point_value REAL DEFAULT 0.01,
            trigger_mode TEXT DEFAULT 'confirmed'
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------------------------
# 文字幅計算・縦揃えパディング関数
# ------------------------------------------------------------------------------
def get_east_asian_width_count(text: str) -> int:
    """全角文字=2, 半角文字=1 として表示幅を計算"""
    count = 0
    for ch in text:
        w = unicodedata.east_asian_width(ch)
        if w in ('F', 'W', 'A'):
            count += 2
        else:
            count += 1
    return count

def pad_label(label: str, target_width: int) -> str:
    """指定された表示幅になるよう末尾に半角スペースを追加"""
    current_width = get_east_asian_width_count(label)
    padding_spaces = max(0, target_width - current_width)
    return label + (" " * padding_spaces)

# ------------------------------------------------------------------------------
# ユーティリティ関数
# ------------------------------------------------------------------------------
def format_time_jst(raw_time: Optional[Union[str, int, float]]) -> str:
    """タイムスタンプまたはISO文字列をJST (YYYY-MM-DD HH:MM:SS) 形式に整形"""
    jst = datetime.timezone(datetime.timedelta(hours=9))
    if not raw_time:
        return datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        if isinstance(raw_time, (int, float)) or (isinstance(raw_time, str) and raw_time.replace('.', '', 1).isdigit()):
            ts = float(raw_time)
            if ts > 1e11:  # ミリ秒タイムスタンプの補正
                ts /= 1000.0
            dt = datetime.datetime.fromtimestamp(ts, tz=jst)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        
        if isinstance(raw_time, str):
            dt = datetime.datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            return dt.astimezone(jst).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
        
    return str(raw_time)

def get_point_value(symbol: str, user_point: Optional[float] = None) -> float:
    """1 Pips相当の価格単位を判定"""
    if user_point and user_point > 0:
        return user_point
    sym = symbol.upper()
    if "JPY" in sym:
        return 0.01
    elif "XAU" in sym or "GOLD" in sym:
        return 0.1
    else:
        return 0.0001

# ------------------------------------------------------------------------------
# 配信用ヘルパー関数
# ------------------------------------------------------------------------------
async def send_discord_webhook(url: str, payload: dict):
    """Discord Webhookへ非同期でPOSTリクエストを送信"""
    if not url:
        print("[WARN] Discord Webhook URLが設定されていません。")
        return
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=10.0)
            res.raise_for_status()
        except Exception as e:
            print(f"[ERROR] Discord送信失敗 ({url}): {e}")

async def forward_to_mt5(payload_dict: dict):
    """MT5側中継/EAへシグナルを転送する関数"""
    if not MT5_ENDPOINT_URL:
        print("[INFO] MT5連携URLが未設定のため、MT5転送はスキップされました。")
        return
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(MT5_ENDPOINT_URL, json=payload_dict, timeout=5.0)
            print(f"[MT5] 応答ステータス: {res.status_code}")
        except Exception as e:
            print(f"[ERROR] MT5転送失敗: {e}")

# ------------------------------------------------------------------------------
# メインWebhookエンドポイント (422エラー回避のためRequest直接受取)
# ------------------------------------------------------------------------------
@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8").strip()
    except Exception as e:
        return {"status": "error", "message": f"Failed to read request body: {e}"}

    # JSONパース試行
    try:
        data = json.loads(body_str)
    except Exception:
        print(f"[INFO] 非JSON形式の通知を受信(自動スキップ): {body_str[:100]}...")
        return {"status": "ignored", "message": "Non-JSON alert received & ignored gracefully"}

    if not isinstance(data, dict):
        return {"status": "ignored", "message": "Invalid JSON format"}

    # 主要データの取得・初期化
    action = str(data.get("action", "")).upper()
    symbol = str(data.get("symbol", "UNKNOWN")).upper()
    
    try:
        price = float(data.get("price", 0.0))
    except (ValueError, TypeError):
        price = 0.0

    if not action or price <= 0:
        return {"status": "ignored", "message": "Missing required fields (action or price)"}

    # アラート種別の判定 (entry / exit)
    raw_alert_type = data.get("alert_type")
    if raw_alert_type:
        alert_type = str(raw_alert_type).lower()
    elif "CLOSE" in action:
        alert_type = "exit"
    elif "BUY" in action or "SELL" in action:
        alert_type = "entry"
    else:
        alert_type = "entry"

    # 売買方向の判定
    position_type = "BUY" if "BUY" in action else "SELL"
    
    # 照合用trade_idの自動設定
    trade_id = data.get("trade_id")
    if not trade_id:
        trade_id = f"{symbol}_{position_type}"

    time_str = format_time_jst(data.get("time"))
    trigger_mode = str(data.get("trigger_mode", "confirmed")).lower()
    cond_name = "確定足" if "confirm" in trigger_mode else "リアルタイム"
    reason_str = str(data.get("reason", "")).strip() or "Band"
    volume = float(data.get("volume", 0.1)) if data.get("volume") is not None else 0.1
    point = get_point_value(symbol, data.get("point_value"))

    # --------------------------------------------------------------------------
    # 1. エントリーアラートの処理 (【チャンネルC】通知)
    # --------------------------------------------------------------------------
    if alert_type == "entry":
        # SQLiteへ保存
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """INSERT OR REPLACE INTO active_trades 
               (trade_id, symbol, action, entry_price, entry_time, volume, point_value, trigger_mode) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, symbol, action, price, time_str, volume, point, trigger_mode)
        )
        conn.commit()
        conn.close()

        # 【チャンネルC】エントリー通知 (即時)
        icon = "🟩" if position_type == "BUY" else "🟥"
        msg_c_lines = [
            f"{icon}【{position_type} 検知】",
            f"{pad_label('・銘柄', 8)}: {symbol}",
            f"{pad_label('・価格', 8)}: {price:.2f}",
            f"{pad_label('・条件', 8)}: {cond_name}",
            f"{pad_label('・コード', 8)}: VIDYA Signal v8",
            f"{pad_label('・日時', 8)}: {time_str}"
        ]
        payload_c = {"content": "```text\n" + "\n".join(msg_c_lines) + "\n```"}
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_C, payload_c)

        # MT5への転送
        background_tasks.add_task(forward_to_mt5, data)

        return {"status": "success", "message": "Entry processed", "trade_id": trade_id}

    # --------------------------------------------------------------------------
    # 2. 決済アラートの処理 (【チャンネルA】および【チャンネルB】通知)
    # --------------------------------------------------------------------------
    elif alert_type == "exit":
        # SQLiteからエントリー情報を取得
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT symbol, action, entry_price, entry_time, volume, point_value FROM active_trades WHERE trade_id = ?",
            (trade_id,)
        )
        row = cursor.fetchone()

        entry_price = price
        pips = 0.0
        if row:
            entry_action = row[1]
            entry_price = float(row[2])
            p_val = float(row[5]) or point

            # Pips計算
            if "BUY" in entry_action.upper():
                pips = (price - entry_price) / p_val
            else:
                pips = (entry_price - price) / p_val

            # レコード削除
            cursor.execute("DELETE FROM active_trades WHERE trade_id = ?", (trade_id,))
            conn.commit()
        conn.close()

        pips_str = f"+{pips:.1f}" if pips >= 0 else f"{pips:.1f}"

        # 【チャンネルA】決済通知 (即時)
        msg_a_lines = [
            "🟦【決済 検知】",
            f"{pad_label('・銘柄', 8)}: {symbol} ({position_type})",
            f"{pad_label('・価格', 8)}: {price:.2f}",
            f"{pad_label('・損益', 8)}: {pips_str} Pips",
            f"{pad_label('・理由', 8)}: {reason_str}",
            f"{pad_label('・コード', 8)}: VIDYA Signal v8",
            f"{pad_label('・日時', 8)}: {time_str}"
        ]
        payload_a = {"content": "```text\n" + "\n".join(msg_a_lines) + "\n```"}
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_A, payload_a)

        # 【チャンネルB】トレード完了成績カード (Embed / Card)
        card_icon = "🏆" if pips >= 0 else "⚠️"
        mt5_status = "ON" if MT5_ENDPOINT_URL else "OFF"
        price_label = f"・{position_type}価格"

        msg_b_lines = [
            f"{card_icon}【トレード完了】結果: {pips_str} Pips",
            "----------------------------------",
            f"{pad_label('・銘柄', 10)}: {symbol}",
            f"{pad_label(price_label, 10)}: {entry_price:.2f}",
            f"{pad_label('・決済価格', 10)}: {price:.2f}",
            f"{pad_label('・決済理由', 10)}: {reason_str}",
            f"{pad_label('・コード', 10)}: VIDYA Signal v8",
            f"{pad_label('・決済日時', 10)}: {time_str}",
            f"{pad_label('・MT5連携', 10)}: {mt5_status}",
            "----------------------------------"
        ]
        payload_b = {"content": "```text\n" + "\n".join(msg_b_lines) + "\n```"}
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_B, payload_b)

        # MT5への転送
        background_tasks.add_task(forward_to_mt5, data)

        return {"status": "success", "message": "Exit processed & notifications sent", "trade_id": trade_id}

    return {"status": "ignored", "message": "Unhandled request"}

# ------------------------------------------------------------------------------
# サーバー起動チェック用ヘルスチェック
# ------------------------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "service": "TradingView Relay Server"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("relay_server:app", host="0.0.0.0", port=port, reload=True)
