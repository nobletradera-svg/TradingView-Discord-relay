import os
import sqlite3
import datetime
import asyncio
from typing import Optional, Union, Any
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, Field, field_validator
import httpx

app = FastAPI(
    title="TradingView Relay Server",
    description="TradingViewのWebhookアラートを受信し、Discord各チャンネルおよびMT5へ並行配信・自動売買照合を行う中継サーバー",
    version="1.8.1"
)

# 環境変数からのWebhook URL設定 ( Render等での運用に対応 )
DISCORD_WEBHOOK_A = os.getenv("DISCORD_WEBHOOK_A_URL", "")  # 決済通知用 (即時)
DISCORD_WEBHOOK_B = os.getenv("DISCORD_WEBHOOK_B_URL", "")  # 成績カード(Embed)用
DISCORD_WEBHOOK_C = os.getenv("DISCORD_WEBHOOK_C_URL", "")  # エントリー通知用 (即時)
MT5_ENDPOINT_URL = os.getenv("MT5_ENDPOINT_URL", "")        # MT5発注連携用

DB_PATH = os.getenv("DB_PATH", "trades.db")

# ------------------------------------------------------------------------------
# データベース初期化
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
            point_value REAL DEFAULT 0.01
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------------------------
# リクエストスキーマ (TradingView Pine Script v181からの柔軟なJSON受取)
# ------------------------------------------------------------------------------
class AlertPayload(BaseModel):
    secret: Optional[str] = Field(default="", description="認証キー")
    action: str = Field(..., description="BUY, SELL, CLOSE_BUY, CLOSE_SELL など")
    symbol: str = Field(..., description="通貨ペア・銘柄名 (例: GOLD, USDJPY)")
    price: float = Field(..., description="約定価格")
    time: Optional[Union[int, float, str]] = Field(default=None, description="発生日時")
    trigger_mode: Optional[str] = Field(default="confirmed", description="confirmed または realtime")
    reason: Optional[str] = Field(default="Band", description="決済理由 (例: Band, Center, TP, SL)")
    alert_type: Optional[str] = Field(default=None, description="'entry' または 'exit'")
    trade_id: Optional[str] = Field(default=None, description="エントリー識別子")
    volume: Optional[float] = Field(default=0.1, description="ロット数")
    point_value: Optional[float] = Field(default=None, description="1Pipの単位")

def format_timestamp(raw_time: Optional[Union[int, float, str]]) -> str:
    """タイムスタンプをJST (YYYY-MM-DD HH:MM:SS) 形式にフォーマット"""
    if not raw_time:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        return now.strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        if isinstance(raw_time, (int, float)):
            # ミリ秒単位の判定
            ts = raw_time / 1000.0 if raw_time > 1e11 else float(raw_time)
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=9)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(raw_time, str):
            if raw_time.isdigit():
                ts = float(raw_time) / 1000.0 if float(raw_time) > 1e11 else float(raw_time)
                dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=9)))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return raw_time.split(".")[0].replace("T", " ")
    except Exception:
        pass
    
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return now.strftime("%Y-%m-%d %H:%M:%S")

def align_lines(items: list[tuple[str, str]]) -> str:
    """項目と値を全自動でコロン(:)位置が揃うように整形"""
    max_len = 0
    for label, _ in items:
        # 全角文字は幅2、半角文字は幅1として計算
        w = sum(2 if ord(c) > 127 else 1 for c in label)
        if w > max_len:
            max_len = w

    lines = []
    for label, val in items:
        w = sum(2 if ord(c) > 127 else 1 for c in label)
        pad = max_len - w
        # 半角スペースで調整
        lines.append(f"・{label}{' ' * pad} : {val}")
    return "\n".join(lines)

def calc_point_value(symbol: str) -> float:
    sym = symbol.upper()
    if "GOLD" in sym or "XAU" in sym:
        return 0.1
    elif "JPY" in sym:
        return 0.01
    else:
        return 0.0001

# ------------------------------------------------------------------------------
# 配信用ヘルパー関数
# ------------------------------------------------------------------------------
async def send_discord_webhook(url: str, payload: dict):
    if not url:
        print("[WARN] Discord Webhook URLが未設定のため送信スキップ")
        return
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=10.0)
            res.raise_for_status()
        except Exception as e:
            print(f"[ERROR] Discord送信失敗 ({url}): {e}")

async def forward_to_mt5(payload_dict: dict):
    if not MT5_ENDPOINT_URL:
        print("[INFO] MT5連携URLが未設定のため転送スキップ")
        return
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(MT5_ENDPOINT_URL, json=payload_dict, timeout=5.0)
            print(f"[MT5] 応答ステータス: {res.status_code}")
        except Exception as e:
            print(f"[ERROR] MT5転送失敗: {e}")

# ------------------------------------------------------------------------------
# メインWebhookエンドポイント
# ------------------------------------------------------------------------------
@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    # 非JSON形式のアラート（TV標準テキスト等）の自動判定・フォールバック
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8").strip()
        import json
        json_data = json.loads(body_str)
        payload = AlertPayload(**json_data)
    except Exception as e:
        print(f"[INFO] 非JSON形式の通知を受信(自動スキップ): {body_str[:100]}...")
        return {"status": "ignored", "message": "Non-JSON alert received & ignored gracefully"}

    action_upper = payload.action.upper()
    dt_str = format_timestamp(payload.time)
    mode_str = "確定足" if payload.trigger_mode == "confirmed" else "リアルタイム"
    mt5_str = "ON" if MT5_ENDPOINT_URL else "OFF"
    point = payload.point_value or calc_point_value(payload.symbol)

    # --------------------------------------------------------------------------
    # A. エントリー通知 (BUY / SELL / COUNTER_BUY / COUNTER_SELL) ➔ チャンネルC
    # --------------------------------------------------------------------------
    if action_upper in ["BUY", "SELL", "COUNTER_BUY", "COUNTER_SELL"]:
        is_buy = "BUY" in action_upper
        act_clean = "BUY" if is_buy else "SELL"
        trade_id = payload.trade_id or f"{payload.symbol}_{act_clean}"

        # DBへエントリー情報を記録
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO active_trades (trade_id, symbol, action, entry_price, entry_time, volume, point_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, payload.symbol, act_clean, payload.price, dt_str, payload.volume, point)
        )
        conn.commit()
        conn.close()

        # 【チャンネルC】エントリー通知 (即時)
        icon = "🟩" if is_buy else "🟥"
        header = f"{icon}【{act_clean} 検知】"
        items = [
            ("銘柄", payload.symbol),
            ("価格", f"{payload.price:.2f}" if "GOLD" in payload.symbol.upper() or "JPY" in payload.symbol.upper() else f"{payload.price:.5f}"),
            ("条件", mode_str),
            ("コード", "VIDYA Signal v8"),
            ("日時", dt_str)
        ]
        msg_body = align_lines(items)
        content_text = f"```text\n{header}\n{msg_body}\n```"

        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_C, {"content": content_text})
        background_tasks.add_task(forward_to_mt5, payload.model_dump())

        return {"status": "success", "message": "Entry processed", "trade_id": trade_id}

    # --------------------------------------------------------------------------
    # B. 決済通知 (CLOSE_BUY / CLOSE_SELL) ➔ チャンネルA (即時) & チャンネルB (成績カード)
    # --------------------------------------------------------------------------
    elif action_upper in ["CLOSE_BUY", "CLOSE_SELL"]:
        pos_dir = "BUY" if "BUY" in action_upper else "SELL"
        trade_id = payload.trade_id or f"{payload.symbol}_{pos_dir}"

        # DBからエントリー情報を検索
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, action, entry_price, entry_time, volume, point_value FROM active_trades WHERE trade_id = ?", (trade_id,))
        row = cursor.fetchone()

        entry_info = None
        if row:
            entry_info = {
                "symbol": row[0],
                "action": row[1],
                "entry_price": row[2],
                "entry_time": row[3],
                "volume": row[4],
                "point_value": row[5]
            }
            cursor.execute("DELETE FROM active_trades WHERE trade_id = ?", (trade_id,))
            conn.commit()
        conn.close()

        # Pips損益の計算
        pips_str = "+0.0 Pips"
        pips_val = 0.0
        entry_price_val = payload.price
        if entry_info:
            entry_price_val = entry_info["entry_price"]
            p_val = entry_info["point_value"] or point
            if entry_info["action"] == "BUY":
                pips_val = (payload.price - entry_price_val) / p_val
            else:
                pips_val = (entry_price_val - payload.price) / p_val
            pips_str = f"+{pips_val:.1f} Pips" if pips_val >= 0 else f"{pips_val:.1f} Pips"

        reason_clean = payload.reason or "Band"

        # 【チャンネルA】決済通知 (即時)
        header_a = f"🟦【決済 検知】"
        items_a = [
            ("銘柄", f"{payload.symbol} ({pos_dir})"),
            ("価格", f"{payload.price:.2f}" if "GOLD" in payload.symbol.upper() or "JPY" in payload.symbol.upper() else f"{payload.price:.5f}"),
            ("損益", pips_str),
            ("理由", reason_clean),
            ("コード", "VIDYA Signal v8"),
            ("日時", dt_str)
        ]
        msg_body_a = align_lines(items_a)
        content_a = f"```text\n{header_a}\n{msg_body_a}\n```"
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_A, {"content": content_a})

        # 【チャンネルB】トレード完了成績カード (Embed)
        trophy = "🏆" if pips_val >= 0 else "⚠️"
        header_b = f"{trophy}【トレード完了】結果: {pips_str}\n----------------------------------"
        items_b = [
            ("銘柄", payload.symbol),
            (f"{pos_dir}価格", f"{entry_price_val:.2f}" if "GOLD" in payload.symbol.upper() or "JPY" in payload.symbol.upper() else f"{entry_price_val:.5f}"),
            ("決済価格", f"{payload.price:.2f}" if "GOLD" in payload.symbol.upper() or "JPY" in payload.symbol.upper() else f"{payload.price:.5f}"),
            ("決済理由", reason_clean),
            ("コード", "VIDYA Signal v8"),
            ("決済日時", dt_str),
            ("MT5連携", mt5_str)
        ]
        msg_body_b = align_lines(items_b)
        content_b = f"```text\n{header_b}\n{msg_body_b}\n----------------------------------\n```"
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_B, {"content": content_b})

        background_tasks.add_task(forward_to_mt5, payload.model_dump())

        return {"status": "success", "message": "Exit processed & notifications sent", "trade_id": trade_id}

    else:
        return {"status": "ignored", "message": f"Action '{payload.action}' not handled"}

@app.get("/")
def health_check():
    return {"status": "ok", "service": "TradingView Relay Server"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("relay_server:app", host="0.0.0.0", port=port, reload=True)
