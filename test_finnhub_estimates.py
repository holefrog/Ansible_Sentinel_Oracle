import os, sys
import finnhub

env_file = "/opt/stock-sentinel/.env"
api_key = None
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            if line.startswith("FINNHUB_API_KEY="):
                api_key = line.strip().split("=", 1)[1].strip('"').strip("'")
                break

client = finnhub.Client(api_key=api_key)

print("Testing Finnhub price_target for AAPL...")
try:
    res = client.price_target("AAPL")
    print(res)
except Exception as e:
    print(f"Error: {e}")

print("\nTesting Finnhub recommendation_trends for AAPL...")
try:
    res = client.recommendation_trends("AAPL")
    print(res)
except Exception as e:
    print(f"Error: {e}")
