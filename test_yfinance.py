import yfinance as yf
import requests

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
})

print("Testing yfinance for AAPL...")
try:
    ticker = yf.Ticker("AAPL", session=session)
    hist = ticker.history(period="1mo")
    if not hist.empty:
        print(f"Success! Got {len(hist)} days of data via Ticker.")
    else:
        print("Failed to get data via Ticker, dataframe is empty.")
except Exception as e:
    print(f"Ticker Error: {e}")

print("Testing yf.download for AAPL...")
try:
    df = yf.download("AAPL", period="1mo", progress=False, session=session)
    if not df.empty:
        print(f"Success! Got {len(df)} days of data via download.")
    else:
        print("Failed to get data via download, dataframe is empty.")
except Exception as e:
    print(f"Download Error: {e}")
