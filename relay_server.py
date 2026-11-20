import os
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

# ------------------------------------------------------------------------------
# 1. 設定項目
# ------------------------------------------------------------------------------
# ★ ご自身専用の秘密の認証キー
WEBHOOK_SECRET = "MY_SECRET_KEY_2026_ADMIN"

# ★ Mac上の MT5 の MQL5/Files フォルダパス
MT5_FILES_DIR = os.path.expanduser(
    "~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files"
)

# ------------------------------------------------------------------------------
# 2. 受信エンドポイント
# ------------------------------------------------------------------------------
@app.route("/mt5-webhook", methods=["POST"])
def mt5_webhook():
    try:
        payload = request.json or {}
        action = payload.get("action", "UNKNOWN")
        symbol = payload.get("symbol", "UNKNOWN")
        print(f"📥 [受信] Renderからリクエスト受領: {action} ({symbol})")
        
        # 🔒 認証キーのチェック
        incoming_secret = payload.get("secret", "")
        if incoming_secret != WEBHOOK_SECRET:
            print(f"🚨 [拒否] 認証キー不一致: 届いたキー='{incoming_secret}'")
            return jsonify({"status": "unauthorized", "message": "Invalid secret key"}), 401
            
        # 保存先フォルダが存在しない場合は自動作成
        os.makedirs(MT5_FILES_DIR, exist_ok=True)
        
        # MT5の MQL5/Files フォルダへ webhook_signal.txt として書き出し
        file_path = os.path.join(MT5_FILES_DIR, "webhook_signal.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            
        print(f"✅ [成功] MT5へシグナル書出完了: {file_path}")
        return jsonify({"status": "success", "message": "Signal written to MT5", "file": file_path}), 200

    except Exception as e:
        error_msg = f"❌ [エラー] 書き込み失敗: {type(e).__name__}: {e}"
        print(error_msg)
        return jsonify({"status": "error", "message": error_msg}), 500

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "MT5 Local Bridge Server v3"}), 200

if __name__ == "__main__":
    print(f"🚀 MT5 Bridge Server 起動中... (ポート: 5001)")
    print(f"📂 出力先フォルダ: {MT5_FILES_DIR}")
    if not os.path.exists(MT5_FILES_DIR):
        print(f"⚠️ [注意] 指定されたフォルダが存在しません。リクエスト受信時に自動作成を試みます。")
    app.run(host="0.0.0.0", port=5001)
