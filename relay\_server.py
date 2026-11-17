import os
import sqlite3
import datetime
import asyncio
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, Field
import httpx

app = FastAPI(
    title="TradingView Relay Server",
    description="TradingViewのWebhookアラートを受信し、Discord各チャンネルおよびMT5へ並行配信・自動売買照合を行う中継サーバー",
    version="1.0.0"
)

# 環境変数からのWebhook URL設定 ( Render等での運用に対応 )
DISCORD_WEBHOOK_A = os.getenv("DISCORD_WEBHOOK_A_URL", "")  # エントリー通知用
DISCORD_WEBHOOK_B = os.getenv("DISCORD_WEBHOOK_B_URL", "")  # 決済通知用
DISCORD_WEBHOOK_C = os.getenv("DISCORD_WEBHOOK_C_URL", "")  # 成績カード(Embed)用
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
            point_value REAL DEFAULT 0.01
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------------------------
# リクエストスキーマ (TradingView Pine Scriptから送信される標準JSON)
# ------------------------------------------------------------------------------
class AlertPayload(BaseModel):
    alert_type: str = Field(..., description="'entry' または 'exit'")
    trade_id: str = Field(..., description="エントリーと決済を照合する一意の識別子")
    symbol: str = Field(..., description="通貨ペア・銘柄名 (例: USDJPY, EURUSD)")
    action: str = Field(..., description="BUY, SELL, CLOSE_BUY, CLOSE_SELL など")
    price: float = Field(..., description="約定価格")
    time: Optional[str] = Field(default=None, description="発生日時 (ISO形式等)")
    volume: Optional[float] = Field(default=0.1, description="ロット数")
    reason: Optional[str] = Field(default="", description="決済理由 (例: TP, SL, Signal)")
    point_value: Optional[float] = Field(default=None, description="1Pipsの単位 (クロス円は0.01, ドルストレートは0.0001)")

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
# メインWebhookエンドポイント
# ------------------------------------------------------------------------------
@app.post("/webhook")
async def handle_webhook(payload: AlertPayload, background_tasks: BackgroundTasks):
    now_str = payload.time or datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # 1. Pips計算用ポイント決定 (未指定時の自動判別)
    point = payload.point_value
    if point is None or point <= 0:
        point = 0.01 if "JPY" in payload.symbol.upper() else 0.0001

    # --------------------------------------------------------------------------
    # A. エントリーアラートの処理
    # --------------------------------------------------------------------------
    if payload.alert_type.lower() == "entry":
        # SQLiteへ保存
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO active_trades (trade_id, symbol, action, entry_price, entry_time, volume, point_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload.trade_id, payload.symbol, payload.action, payload.price, now_str, payload.volume, point)
        )
        conn.commit()
        conn.close()

        # Discord チャンネルAへの通知 (エントリー即時通知)
        discord_payload_a = {
            "content": f"🚀 **【エントリー通知】**\n"
                       f"• **銘柄**: `{payload.symbol}`\n"
                       f"• **売買**: `{payload.action}`\n"
                       f"• **価格**: `{payload.price}`\n"
                       f"• **数量**: `{payload.volume} Lot`\n"
                       f"• **ID**: `{payload.trade_id}`\n"
                       f"• **時刻**: `{now_str}`"
        }
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_A, discord_payload_a)
        
        # MT5への転送
        background_tasks.add_task(forward_to_mt5, payload.model_dump())

        return {"status": "success", "message": "Entry processed", "trade_id": payload.trade_id}

    # --------------------------------------------------------------------------
    # B. 決済アラートの処理
    # --------------------------------------------------------------------------
    elif payload.alert_type.lower() == "exit":
        # SQLiteから対応するエントリー情報を取得
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, action, entry_price, entry_time, volume, point_value FROM active_trades WHERE trade_id = ?", (payload.trade_id,))
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
            # データベースから削除
            cursor.execute("DELETE FROM active_trades WHERE trade_id = ?", (payload.trade_id,))
            conn.commit()
        conn.close()

        # Discord チャンネルBへの通知 (決済即時通知)
        discord_payload_b = {
            "content": f"🏁 **【決済通知】**\n"
                       f"• **銘柄**: `{payload.symbol}`\n"
                       f"• **決済種別**: `{payload.action}`\n"
                       f"• **決済価格**: `{payload.price}`\n"
                       f"• **理由**: `{payload.reason or '指定なし'}`\n"
                       f"• **ID**: `{payload.trade_id}`\n"
                       f"• **時刻**: `{now_str}`"
        }
        background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_B, discord_payload_b)

        # Discord チャンネルCへの通知 (成績カード Embed表示)
        if entry_info:
            entry_price = entry_info["entry_price"]
            exit_price = payload.price
            p_val = entry_info["point_value"] or point

            # Pips損益計算 (BUYかSELLかに応じて算出)
            if "BUY" in entry_info["action"].upper():
                pips = (exit_price - entry_price) / p_val
            else:
                pips = (entry_price - exit_price) / p_val

            # 保有時間計算
            try:
                t_entry = datetime.datetime.fromisoformat(entry_info["entry_time"].replace("Z", "+00:00"))
                t_exit = datetime.datetime.fromisoformat(now_str.replace("Z", "+00:00"))
                duration_sec = int((t_exit - t_entry).total_seconds())
                duration_str = f"{duration_sec // 3600}時間 {(duration_sec % 3600) // 60}分 {duration_sec % 60}秒"
            except Exception:
                duration_str = "計算不可"

            color = 0x2ecc71 if pips >= 0 else 0xe74c3c  # 勝ち: 緑, 負け: 赤
            pips_symbol = "🟢 +" if pips >= 0 else "🔴 "

            embed_c = {
                "embeds": [
                    {
                        "title": f"📊 トレード完結成績カード [{payload.symbol}]",
                        "color": color,
                        "fields": [
                            {"name": "トレードID", "value": f"`{payload.trade_id}`", "inline": True},
                            {"name": "売買方向", "value": f"`{entry_info['action']}`", "inline": True},
                            {"name": "数量", "value": f"`{entry_info['volume']} Lot`", "inline": True},
                            {"name": "エントリー価格", "value": f"`{entry_price}`", "inline": True},
                            {"name": "決済価格", "value": f"`{exit_price}`", "inline": True},
                            {"name": "決済理由", "value": f"`{payload.reason or 'Signal'}`", "inline": True},
                            {"name": "損益 (Pips)", "value": f"**{pips_symbol}{pips:.1f} Pips**", "inline": False},
                            {"name": "保有時間", "value": duration_str, "inline": True},
                            {"name": "決済日時", "value": now_str, "inline": True}
                        ],
                        "footer": {"text": "TradingView × Python Relay Server"}
                    }
                ]
            }
            background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_C, embed_c)
        else:
            # エントリー履歴が見つからなかった場合のフォールバック Embed
            embed_c_fallback = {
                "embeds": [{
                    "title": f"⚠️ トレード完結成績カード [{payload.symbol}]",
                    "description": f"トレードID `{payload.trade_id}` のエントリー記録がDBで見つかりませんでした。単体決済として記録します。\n決済価格: `{payload.price}`",
                    "color": 0xf1c40f
                }]
            }
            background_tasks.add_task(send_discord_webhook, DISCORD_WEBHOOK_C, embed_c_fallback)

        # MT5への転送
        background_tasks.add_task(forward_to_mt5, payload.model_dump())

        return {"status": "success", "message": "Exit processed & performance card posted", "trade_id": payload.trade_id}

    else:
        raise HTTPException(status_code=400, detail="Invalid alert_type. Must be 'entry' or 'exit'.")

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
