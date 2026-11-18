import os
import sqlite3
import datetime
from typing import Optional, Union
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel, Field
import httpx

app = FastAPI(title="TradingView Relay Server", version="1.8.2")

# 環境変数からのWebhook URL設定
DISCORD_WEBHOOK_A = os.getenv("DISCORD_WEBHOOK_A_URL", "")  # 決済通知用 (即時)
DISCORD_WEBHOOK_B = os.getenv("DISCORD_WEBHOOK_B_URL", "")  # 成績カード用
DISCORD_WEBHOOK_C = os.getenv("DISCORD_WEBHOOK_C_URL", "")  # エントリー通知用 (即時)
MT5_ENDPOINT_URL = os.getenv("MT5_ENDPOINT_URL", "")

DB_PATH = os.getenv("DB_PATH", "trades.db")

# ------------------------------------------------------------------------------
# 銘柄名 (シンボル) 自動変換マップ (TV側銘柄 -> MT5ブローカー銘柄)
# ------------------------------------------------------------------------------
SYMBOL_MAP = {
    "XAUUSD": "GOLD#",
    "GOLD": "GOLD#",
    "BTCUSD": "BTCUSD#",
    "USDJPY": "USDJPY#",
}

def convert_symbol_for_mt5(symbol: str) -> str:
    """TradingViewの銘柄名をMT5ブローカーの銘柄名へ変換"""
    raw_sym = symbol.strip().upper()
    return SYMBOL_MAP.get(raw_sym, raw_sym)

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
# リクエストスキーマ (TradingView Pine Script v181/v187 柔軟対応)
# ------------------------------------------------------------------------------
class AlertPayload(BaseModel):
    secret: Optional[str] = Field(default="")
    action: str = Field(..., description="BUY, SELL, CLOSE_BUY, CLOSE_SELL など")
    symbol: str = Field(...)
    price: float = Field(...)
    time: Optional[Union[int, float, str]] = Field(default=None)
    trigger_mode: Optional[str] = Field(default="confirmed")
    reason: Optional[str] = Field(default="Band")
    alert_type: Optional[str] = Field(default=None)
    trade_id: Optional[str] = Field(default=None)
    volume: Optional[float] = Field(default=0.1)
    point_value: Optional[float] = Field(default=None)

def format_timestamp(raw_time: Optional[Union[int, float, str]]) -> str:
    if not raw_time:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        return now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if isinstance(raw_time, (int, float)):
            ts = raw_time / 1000.0 if raw_time > 1e11 else float(raw_time)
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=9)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(raw_time, str) and raw_time.isdigit():
            ts = float(raw_time) / 1000.0 if float(raw_time) > 1e11 else float(raw_time)
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone(datetime.timedelta(hours=9)))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return now.strftime("%Y-%m-%d %H:%M:%S")

def align_lines(items: list[tuple[str, str]]) -> str:
    """全角・半角文字幅を考慮してコロン(:)の位置を縦揃え整列"""
    max_len = max(sum(2 if ord(c) > 127 else 1 for c in label) for label, _ in items)
    lines = []
    for label, val in items:
        w = sum(2 if ord(c) > 127 else 1 for c in label)
        lines.append(f"・{label}{' ' * (max_len - w)} : {val}")
    return "\n".join(lines)

def calc_point_value(symbol: str) -> float:
    sym = symbol.upper()
    if "GOLD" in sym or "XAU" in sym:
        return 0.1
    elif "JPY" in sym:
        return 0.01
    return 0.0001

async def send_discord_webhook(url: str, payload: dict):
    if not url:
        return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload, timeout=10.0)
        except Exception as e:
            print(f"[ERROR] Discord送信失敗: {e}")

async def forward_to_mt5(payload_dict: dict):
    if not MT5_ENDPOINT_URL:
        print("[INFO] MT5連携URLが未設定のため、MT5転送はスキップされました。")
        return
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(MT5_ENDPOINT_URL, json=payload_dict, timeout=5.0)
            print(f"[MT5] 転送成功 応答コード: {res.status_code}")
        except Exception as e:
            print(f"[ERROR] MT5転送失敗: {e}")

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body_bytes = await request.body()
        import json
        payload = AlertPayload(**json.loads(body_bytes.decode("utf-8").strip()))
    except Exception:
        return {"status": "ignored", "message": "Non-JSON alert received & ignored gracefully"}

    action_upper = payload.action.upper()
    dt_str = format_timestamp(payload.time)
    mode_str = "確定足" if payload.trigger_mode == "confirmed" else "リアルタイム"
    mt5_str = "ON" if MT5_ENDPOINT_URL else "OFF"
    point = payload.point_value or calc_point_value(payload.symbol)

    # MT5用に銘柄名を自動変換 (例: XAUUSD -> GOLD#)
    mt5_symbol = convert_symbol_for_mt5(payload.symbol)
    mt5_payload_dict = payload.model_dump()
    mt5_payload_dict["symbol"] = mt5_symbol

    display_symbol = "GOLD" if "XAU" in payload.symbol.upper() or "GOLD" in payload.symbol.upper() else payload.symbol

    # --------------------------------------------------------------------------
    # A. エントリーアラートの処理 (BUY / SELL) -> チャンネルCへ送信
    # --------------------------------------------------------------------------
    if action_upper in ["BUY", "SELL", "COUNTER_BUY", "COUNTER_SELL"]:
        is_buy = "BUY" in action_upper
        act_clean = "BUY" if is_buy else "SELL"
        trade_id = payload.trade_id or f"{display_symbol}_{act_clean}"

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO active_trades (trade_id, symbol, action, entry_price, entry_time, volume, point_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, display_symbol, act_clean, payload.price, dt_str, payload.volume, point)
        )
        conn.commit()
        conn.close()

        items = [
            ("銘柄", display_symbol),
            ("価格", f"{payload.price:.2f}" if "GOLD" in display_symbol.upper() or "JPY" in display_symbol.upper() else f"{payload.price:.5f}"),
            ("条件", mode_str),
            ("コード", "VIDYA Signal v8"),
            ("日時", dt_str)
        ]
        icon = "🟩" if is_buy else "🟥"
        content = f"```text\n{icon}【{act_clean} 検知】\n{align_lines(items)}\n```"

        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_C, {"content": content})
        background_tasks.add_task(forward_to_mt5, mt5_payload_dict)
        return {"status": "success", "message": "Entry processed", "trade_id": trade_id, "mt5_symbol": mt5_symbol}

    # --------------------------------------------------------------------------
    # B. 決済アラートの処理 (CLOSE_BUY / CLOSE_SELL) -> チャンネルA & Bへ送信
    # --------------------------------------------------------------------------
    elif action_upper in ["CLOSE_BUY", "CLOSE_SELL"]:
        pos_dir = "BUY" if "BUY" in action_upper else "SELL"
        trade_id = payload.trade_id or f"{display_symbol}_{pos_dir}"

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, action, entry_price, entry_time, volume, point_value FROM active_trades WHERE trade_id = ?", (trade_id,))
        row = cursor.fetchone()

        entry_info = None
        if row:
            entry_info = {"symbol": row[0], "action": row[1], "entry_price": row[2], "entry_time": row[3], "volume": row[4], "point_value": row[5]}
            cursor.execute("DELETE FROM active_trades WHERE trade_id = ?", (trade_id,))
            conn.commit()
        conn.close()

        pips_str = "+0.0 Pips"
        pips_val = 0.0
        entry_price_val = payload.price
        if entry_info:
            entry_price_val = entry_info["entry_price"]
            p_val = entry_info["point_value"] or point
            pips_val = (payload.price - entry_price_val) / p_val if entry_info["action"] == "BUY" else (entry_price_val - payload.price) / p_val
            pips_str = f"+{pips_val:.1f} Pips" if pips_val >= 0 else f"{pips_val:.1f} Pips"

        reason_clean = payload.reason or "Band"

        # 1. チャンネルA (決済即時通知)
        items_a = [
            ("銘柄", f"{display_symbol} ({pos_dir})"),
            ("価格", f"{payload.price:.2f}" if "GOLD" in display_symbol.upper() or "JPY" in display_symbol.upper() else f"{payload.price:.5f}"),
            ("損益", pips_str),
            ("理由", reason_clean),
            ("コード", "VIDYA Signal v8"),
            ("日時", dt_str)
        ]
        content_a = f"```text\n🟦【決済 検知】\n{align_lines(items_a)}\n```"
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_A, {"content": content_a})

        # 2. チャンネルB (トレード完了成績カード)
        trophy = "🏆" if pips_val >= 0 else "⚠️"
        header_b = f"{trophy}【トレード完了】結果: {pips_str}\n----------------------------------"
        items_b = [
            ("銘柄", display_symbol),
            (f"{pos_dir}価格", f"{entry_price_val:.2f}" if "GOLD" in display_symbol.upper() or "JPY" in display_symbol.upper() else f"{entry_price_val:.5f}"),
            ("決済価格", f"{payload.price:.2f}" if "GOLD" in display_symbol.upper() or "JPY" in display_symbol.upper() else f"{payload.price:.5f}"),
            ("決済理由", reason_clean),
            ("コード", "VIDYA Signal v8"),
            ("決済日時", dt_str),
            ("MT5連携", mt5_str)
        ]
        content_b = f"```text\n{header_b}\n{align_lines(items_b)}\n----------------------------------\n```"
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_B, {"content": content_b})

        background_tasks.add_task(forward_to_mt5, mt5_payload_dict)
        return {"status": "success", "message": "Exit processed & notifications sent", "trade_id": trade_id, "mt5_symbol": mt5_symbol}

    return {"status": "ignored", "message": f"Action '{payload.action}' not handled"}

@app.get("/")
def health_check():
    return {"status": "ok", "service": "TradingView Relay Server"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("relay_server:app", host="0.0.0.0", port=port, reload=True)
