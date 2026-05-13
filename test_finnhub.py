import os, sys, datetime
import finnhub

env_file = "/opt/stock-sentinel/.env"
api_key = None
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            if line.startswith("FINNHUB_API_KEY="):
                api_key = line.strip().split("=", 1)[1].strip('"').strip("'")
                break

if not api_key:
    print("FINNHUB_API_KEY not found in .env")
    sys.exit(1)

client = finnhub.Client(api_key=api_key)

end = int(datetime.datetime.now().timestamp())
start = int((datetime.datetime.now() - datetime.timedelta(days=252)).timestamp())

print("Testing Finnhub stock_candles for AAPL...")
try:
    res = client.stock_candles("AAPL", "D", start, end)
    if res and res.get("s") == "ok":
        c_len = len(res.get('c', []))
        print(f"Success! Got {c_len} days of data.")
    else:
        print("Failed or no data:", res)
except Exception as e:
    print("Error:", e)
