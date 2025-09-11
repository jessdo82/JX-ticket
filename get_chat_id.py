import os, sys, requests
token = os.environ.get("TELEGRAM_BOT_TOKEN") or (sys.argv[1] if len(sys.argv)>1 else None)
if not token:
    print("用法：先設 TELEGRAM_BOT_TOKEN 或 python get_chat_id.py <BotToken>")
    sys.exit(1)
url = f"https://api.telegram.org/bot{token}/getUpdates"
print("提示：先對你的 Bot 說話，或在要收通知的群組@它說話")
r = requests.get(url, timeout=20)
print(r.text)
