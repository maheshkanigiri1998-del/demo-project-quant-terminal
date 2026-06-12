from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import feedparser
import json
import requests

app = Flask(__name__)
CORS(app)

# ── Browser Disguise Configuration ──────────────────────────────────────────
# This session setup prevents Yahoo Finance from blocking cloud-hosted servers
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'})

# ── Quote ──────────────────────────────────────────────────────────────────────
@app.route('/quote/<symbol>')
def get_quote(symbol):
    try:
        ticker = yf.Ticker(symbol, session=session)
        info   = ticker.info
        hist   = ticker.history(period="5d", interval="1d")
        prices = hist['Close'].dropna().tolist()

        price = (info.get("currentPrice") or info.get("regularMarketPrice")
                 or info.get("navPrice") or (prices[-1] if prices else None))
        prev  = (info.get("previousClose") or info.get("regularMarketPreviousClose")
                 or (prices[-2] if len(prices) >= 2 else None))

        return jsonify({
            "price":     price,
            "prevClose": prev,
            "open":      info.get("open")     or info.get("regularMarketOpen"),
            "high":      info.get("dayHigh")  or info.get("regularMarketDayHigh"),
            "low":       info.get("dayLow")   or info.get("regularMarketDayLow"),
            "vol":       info.get("volume")   or info.get("regularMarketVolume"),
            "cap":       info.get("marketCap"),
            "currency":  info.get("currency", "USD"),
            "name":      info.get("shortName") or info.get("longName") or symbol,
            "pe":        info.get("trailingPE"),
            "eps":       info.get("trailingEps"),
            "divYield":  info.get("dividendYield"),
            "beta":      info.get("beta"),
            "w52h":      info.get("fiftyTwoWeekHigh"),
            "w52l":      info.get("fiftyTwoWeekLow"),
            "avgVol":    info.get("averageVolume") or info.get("averageDailyVolume10Day"),
            "rev":       info.get("totalRevenue"),
            "sector":    info.get("sector",""),
            "prices":    prices
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── History ────────────────────────────────────────────────────────────────────
@app.route('/history/<symbol>')
def get_history(symbol):
    try:
        period   = request.args.get('period', '1mo')
        imap     = {'1d':'5m','5d':'15m','1mo':'1d','6mo':'1wk'}
        interval = imap.get(period, '1d')
        ticker   = yf.Ticker(symbol, session=session)
        hist     = ticker.history(period=period, interval=interval)
        closes   = hist['Close'].dropna().tolist()
        # RSI calculation server-side
        rsi = calc_rsi(closes)
        return jsonify({"prices": closes, "rsi": rsi, "count": len(closes)})
    except Exception as e:
        return jsonify({"error": str(e), "prices": [], "rsi": []}), 500

# ── Backtest data (2 years) ────────────────────────────────────────────────────
@app.route('/backtest/<symbol>')
def get_backtest(symbol):
    try:
        ticker = yf.Ticker(symbol, session=session)
        hist   = ticker.history(period="2y", interval="1d")
        prices = hist['Close'].dropna().tolist()
        return jsonify({"prices": prices, "count": len(prices)})
    except Exception as e:
        return jsonify({"error": str(e), "prices": []}), 500

# ── News feed ─────────────────────────────────────────────────────────────────
@app.route('/news/<symbol>')
def get_news(symbol):
    try:
        ticker = yf.Ticker(symbol, session=session)
        news   = ticker.news or []
        items  = []
        for n in news[:8]:
            items.append({
                "title":     n.get("title",""),
                "publisher": n.get("publisher",""),
                "link":      n.get("link",""),
                "time":      n.get("providerPublishTime", 0)
            })
        return jsonify({"news": items})
    except Exception as e:
        return jsonify({"news": [], "error": str(e)}), 500

# ── Search ─────────────────────────────────────────────────────────────────────
@app.route('/search/<query>')
def search_ticker(query):
    try:
        ticker    = yf.Ticker(query, session=session)
        info      = ticker.info
        name      = info.get("shortName") or info.get("longName") or query
        currency  = info.get("currency","USD")
        qtype     = info.get("quoteType","")
        exchange  = info.get("exchange","")

        if query.upper().endswith((".NS",".BO")):     market = "IN"
        elif query.upper().endswith(".L"):           market = "UK"
        elif qtype == "CRYPTOCURRENCY":              market = "CRYPTO"
        elif qtype == "CURRENCY" or "=X" in query:  market = "FX"
        elif "=F" in query:                          market = "CMDTY"
        else:                                        market = "US"

        return jsonify({"sym": query.upper(), "name": name,
                        "market": market, "currency": currency,
                        "valid": bool(info.get("regularMarketPrice")
                                      or info.get("currentPrice"))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── RSI helper ─────────────────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return []
    rsi_vals = []
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    for i in range(period, len(prices)):
        avg_g = sum(gains[i-period:i]) / period
        avg_l = sum(losses[i-period:i]) / period
        if avg_l == 0:
            rsi_vals.append(100)
        else:
            rs = avg_g / avg_l
            rsi_vals.append(round(100 - (100 / (1 + rs)), 2))
    return rsi_vals

# ── Health check (Railway needs this) ─────────────────────────────────────────
@app.route('/')
def health():
    return jsonify({"status": "QuantTerminal API running", "version": "3.1"})

if __name__ == '__main__':
    print("=" * 55)
    print("  QuantTerminal Backend  →  http://localhost:5000")
    print("  Routes: /quote  /history  /backtest  /news  /search")
    print("=" * 55)
    app.run(host='0.0.0.0', port=5000, debug=False)