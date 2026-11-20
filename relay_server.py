import os
import re
import json
import sqlite3
import datetime
from typing import Optional, Union
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx

app = FastAPI(title="TradingView Relay Server", version="2.0.0")

# ------------------------------------------------------------------------------
# 環境変数設定
# ------------------------------------------------------------------------------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "MY_SECRET_KEY_2026_ADMIN")
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
    "BTCUSDT": "BTCUSD#",
    "BTCUSD.P": "BTCUSD#",
    "USDJPY": "USDJPY#",
    "EURUSD": "EURUSD#",
    "GBPJPY": "GBPJPY#",
}

def convert_symbol_for_mt5(symbol: str) -> str:
    """TradingViewの銘柄名をMT5ブローカーの銘柄名へ安全に変換"""
    if not symbol:
        return "UNKNOWN"
    raw_sym = symbol.strip().upper()
    # 変換マップに存在する場合は適用
    if raw_sym in SYMBOL_MAP:
        return SYMBOL_MAP[raw_sym]
    # 特定パターンの自動判定（BTC系・XAU系）
    if raw_sym.startswith("BTC"):
        return "BTCUSD#"
    if raw_sym.startswith("XAU"):
        return "GOLD#"
    # 登録のない銘柄は絶対にGOLD#へ強制上書きせず、TradingViewの入力値をそのまま通過
    return raw_sym

def calc_point_value(symbol: str) -> float:
    """銘柄ごとのPips計算用ポイント値を最適化"""
    sym = symbol.upper()
    if "GOLD" in sym or "XAU" in sym:
        return 0.1
    elif "BTC" in sym or "ETH" in sym or "CRYPTO" in sym:
        return 1.0  # BTCなどの暗号資産用Pips単位 (1.0 $ = 1 Pip)
    elif "JPY" in sym:
        return 0.01
    return 0.0001

def format_price(price: float, symbol: str) -> str:
    """銘柄に応じた価格表示フォーマット"""
    sym = symbol.upper()
    if "GOLD" in sym or "XAU" in sym or "JPY" in sym or "BTC" in sym:
        return f"{price:.2f}"
    return f"{price:.5f}"

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
# リクエストスキーマ
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

def parse_raw_text_payload(body_text: str) -> Optional[dict]:
    """JSON以外のテキスト形式アラートから必要情報を正規表現で安全に抽出"""
    try:
        # action抽出
        action_match = re.search(r'\b(BUY|SELL|CLOSE_BUY|CLOSE_SELL|COUNTER_BUY|COUNTER_SELL)\b', body_text, re.IGNORECASE)
        if not action_match:
            return None
        action = action_match.group(1).upper()

        # secret抽出
        secret_match = re.search(r'(?:secret|key)[\"\':\s=]+([^\",\s\n]+)', body_text, re.IGNORECASE)
        secret = secret_match.group(1) if secret_match else ""

        # symbol抽出
        symbol_match = re.search(r'(?:symbol|通貨ペア)[\"\':\s=]+([A-Z0-9_\.#]+)', body_text, re.IGNORECASE)
        if not symbol_match:
            symbol_match = re.search(r'\((XAUUSD|GOLD|BTCUSD|USDJPY|[A-Z0-9_\.#]+)\)', body_text, re.IGNORECASE)
        symbol = symbol_match.group(1) if symbol_match else "XAUUSD"

        # price抽出
        price_match = re.search(r'(?:価格|price)[\"\':\s=]+([0-9]+\.?[0-9]*)', body_text, re.IGNORECASE)
        if not price_match:
            price_match = re.search(r'([0-9]+\.[0-9]+)', body_text)
        price = float(price_match.group(1)) if price_match else 0.0

        # reason抽出
        reason_match = re.search(r'(?:発動条件|理由|reason)[\"\':\s=]+([^\n,\"]+)', body_text, re.IGNORECASE)
        reason = reason_match.group(1).strip() if reason_match else "Signal"

        return {
            "secret": secret,
            "action": action,
            "symbol": symbol,
            "price": price,
            "reason": reason,
            "trigger_mode": "realtime" if "リアルタイム" in body_text else "confirmed"
        }
    except Exception as e:
        print(f"[WARN] テキスト抽出失敗: {e}")
        return None

@app.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8").strip()

    payload_dict = None

    # 1. JSON形式としてパース試行
    try:
        raw_json = json.loads(body_text)
        if isinstance(raw_json, dict):
            payload_dict = raw_json
    except Exception:
        pass

    # 2. JSONでパースできなかった場合、テキスト抽出を試行
    if not payload_dict:
        extracted = parse_raw_text_payload(body_text)
        if extracted:
            payload_dict = extracted

    # 3. 解析が完全に失敗した場合の安全なエラー処理
    if not payload_dict or "action" not in payload_dict or "symbol" not in payload_dict:
        dt_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
        err_msg = f"⚠️【緊急通知】Webhookパース失敗\n・受信日時 : {dt_now}\n・エラー    : JSON/テキスト解析不能\n・受信内容 : {body_text[:200]}"
        print(f"[ERROR] Webhookパース失敗: {body_text}")
        
        # 安全のため勝手な代替発注は行わず、Discordへ緊急エラー通知を送信
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_A or DISCORD_WEBHOOK_C, {"content": f"```text\n{err_msg}\n```"})
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "⚠️ [パース失敗] 有効なシグナル項目(action/symbol)を抽出できませんでした。データ構造を確認してください。"}
        )

    # Pydanticモデル化
    try:
        payload = AlertPayload(**payload_dict)
    except Exception as e:
        return JSONResponse(status_code=422, content={"status": "error", "message": f"フィールド検証エラー: {str(e)}"})

    # 4. 認証キー (WEBHOOK_SECRET) の厳格検証
    if WEBHOOK_SECRET and payload.secret != WEBHOOK_SECRET:
        dt_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[SECURITY] 認証キー不一致で拒否: 届いたキー='{payload.secret}'")
        err_msg = f"🚨【セキュリティ拒否】認証キー不一致\n・受信日時 : {dt_now}\n・届いたキー: '{payload.secret}'\n・銘柄/方向 : {payload.symbol} / {payload.action}"
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_A or DISCORD_WEBHOOK_C, {"content": f"```text\n{err_msg}\n```"})
        return JSONResponse(
            status_code=401,
            content={"status": "unauthorized", "message": f"🚨 [拒否] 認証キーが不一致です。(届いたキー: '{payload.secret}')"}
        )

    action_upper = payload.action.upper()
    dt_str = format_timestamp(payload.time)
    mode_str = "確定足" if payload.trigger_mode == "confirmed" else "リアルタイム"
    mt5_str = "ON" if MT5_ENDPOINT_URL else "OFF"
    point = payload.point_value or calc_point_value(payload.symbol)

    # MT5用に銘柄名を自動変換 (XAUUSD -> GOLD#, BTCUSD -> BTCUSD# 等。未知の銘柄は無変換で通過)
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
            ("価格", format_price(payload.price, display_symbol)),
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
        else:
            print(f"[WARN] DBにアクティブなエントリー記録がありませんでした (trade_id: {trade_id})。Render再起動等の可能性がありますが、決済処理を継続します。")
        conn.close()

        pips_str = "+0.0 Pips (エントリー未保持)"
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
            ("価格", format_price(payload.price, display_symbol)),
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
            (f"{pos_dir}価格", format_price(entry_price_val, display_symbol)),
            ("決済価格", format_price(payload.price, display_symbol)),
            ("決済理由", reason_clean),
            ("コード", "VIDYA Signal v8"),
            ("決済日時", dt_str),
            ("MT5連携", mt5_str)
        ]
        content_b = f"```text\n{header_b}\n{align_lines(items_b)}\n----------------------------------\n```"
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_B, {"content": content_b})

        # MT5へ必ず決済指示を転送
        background_tasks.add_task(forward_to_mt5, mt5_payload_dict)
        return {"status": "success", "message": "Exit processed & notifications sent", "trade_id": trade_id, "mt5_symbol": mt5_symbol}

    return {"status": "ignored", "message": f"Action '{payload.action}' not handled"}

@app.get("/")
def health_check():
    return {"status": "ok", "service": "TradingView Relay Server v2.0.0"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("relay_server:app", host="0.0.0.0", port=port, reload=True)
