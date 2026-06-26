import logging
import requests
from flask import Flask, jsonify
from flask_cors import CORS
import yfinance as yf
import feedparser
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

CHART_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d&includePrePost=false'
HISTORY_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range_}&includePrePost=false'


def create_headers_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    return session


custom_session = create_headers_session()


def resolve_currency(symbol, yahoo_currency=None):
    """Prefer Yahoo's currency; fall back to exchange suffix rules."""
    if yahoo_currency:
        return yahoo_currency
    upper = symbol.upper()
    if upper.endswith(('.NS', '.BO')):
        return 'INR'
    if upper.endswith('.L'):
        return 'GBP'
    if upper.endswith('=X'):
        return 'USD'
    return 'USD'


def _fast_value(fast, key):
    """Read a field from yfinance FastInfo via attribute or mapping access."""
    if fast is None:
        return None
    val = getattr(fast, key, None)
    if val is not None:
        return val
    try:
        return fast[key]
    except (KeyError, TypeError):
        pass
    try:
        return fast.get(key)
    except AttributeError:
        return None


def fetch_yfinance_quote(symbol):
    ticker = yf.Ticker(symbol, session=custom_session)
    fast = ticker.fast_info
    price = _fast_value(fast, 'last_price')
    if price is None or float(price) <= 0:
        raise ValueError('fast_info returned no last_price')

    prev_close = _fast_value(fast, 'previous_close') or price
    currency = _fast_value(fast, 'currency')
    name = _fast_value(fast, 'short_name') or symbol.split('.')[0]

    return {
        'price': float(price),
        'prevClose': float(prev_close),
        'currency': currency,
        'name': name,
        'source': 'yfinance',
    }


def fetch_chart_quote(symbol):
    url = CHART_URL.format(symbol=requests.utils.quote(symbol, safe=''))
    resp = custom_session.get(url, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    result = (payload.get('chart') or {}).get('result')
    if not result:
        err = (payload.get('chart') or {}).get('error')
        raise ValueError(f'chart API empty result: {err}')

    block = result[0]
    meta = block.get('meta') or {}
    price = meta.get('regularMarketPrice')
    if price is None or float(price) <= 0:
        raise ValueError('chart API missing regularMarketPrice')

    closes = (block.get('indicators') or {}).get('quote', [{}])[0].get('close') or []
    spark = [float(c) for c in closes if c is not None]

    return {
        'price': float(price),
        'prevClose': float(meta.get('previousClose') or meta.get('chartPreviousClose') or price),
        'open': meta.get('regularMarketOpen'),
        'high': meta.get('regularMarketDayHigh'),
        'low': meta.get('regularMarketDayLow'),
        'vol': meta.get('regularMarketVolume'),
        'w52h': meta.get('fiftyTwoWeekHigh'),
        'w52l': meta.get('fiftyTwoWeekLow'),
        'currency': meta.get('currency'),
        'name': meta.get('longName') or meta.get('shortName') or symbol.split('.')[0],
        'prices': spark,
        'source': 'yahoo_chart',
    }


def fetch_live_quote(symbol):
    """Try yfinance first, then Yahoo chart API. No simulated fallback."""
    errors = []
    for label, fetcher in (('yfinance', fetch_yfinance_quote), ('yahoo_chart', fetch_chart_quote)):
        try:
            data = fetcher(symbol)
            logger.info('%s: live price %.4f via %s', symbol, data['price'], label)
            return data
        except Exception as exc:
            errors.append(f'{label}: {exc}')
            logger.warning('%s: %s failed — %s', symbol, label, exc)

    logger.error('%s: all quote sources failed — %s', symbol, '; '.join(errors))
    return None


def fetch_yfinance_history(symbol, period='2y', interval='1d'):
    ticker = yf.Ticker(symbol, session=custom_session)
    hist = ticker.history(period=period, interval=interval, auto_adjust=True)
    if hist is None or hist.empty:
        raise ValueError('yfinance history empty')
    closes = hist['Close'].dropna().tolist()
    if len(closes) < 20:
        raise ValueError(f'only {len(closes)} points')
    return [float(c) for c in closes]


def fetch_chart_history(symbol, interval='1d', range_='2y'):
    url = HISTORY_URL.format(
        symbol=requests.utils.quote(symbol, safe=''),
        interval=interval,
        range_=range_,
    )
    resp = custom_session.get(url, timeout=20)
    resp.raise_for_status()
    payload = resp.json()

    result = (payload.get('chart') or {}).get('result')
    if not result:
        err = (payload.get('chart') or {}).get('error')
        raise ValueError(f'chart history empty result: {err}')

    closes = (result[0].get('indicators') or {}).get('quote', [{}])[0].get('close') or []
    prices = [float(c) for c in closes if c is not None]
    if len(prices) < 20:
        raise ValueError(f'only {len(prices)} points')
    return prices


def fetch_backtest_history(symbol):
    """2 years of daily closes for SMA backtesting."""
    errors = []
    for label, fetcher in (
        ('yfinance_history', fetch_yfinance_history),
        ('yahoo_chart_history', fetch_chart_history),
    ):
        try:
            prices = fetcher(symbol)
            logger.info('%s: backtest history %d points via %s', symbol, len(prices), label)
            return prices, label
        except Exception as exc:
            errors.append(f'{label}: {exc}')
            logger.warning('%s: backtest %s failed — %s', symbol, label, exc)

    logger.error('%s: all backtest history sources failed — %s', symbol, '; '.join(errors))
    return None, errors


def build_quote_response(symbol, data):
    price = data['price']
    prev_close = data['prevClose']
    currency = resolve_currency(symbol, data.get('currency'))

    open_px = data.get('open')
    high = data.get('high')
    low = data.get('low')
    if open_px is None:
        open_px = prev_close
    if high is None:
        high = max(price, prev_close)
    if low is None:
        low = min(price, prev_close)

    spark = data.get('prices')
    if not spark:
        spark = [prev_close, price]

    return {
        'price': round(price, 2),
        'prevClose': round(prev_close, 2),
        'open': round(float(open_px), 2),
        'high': round(float(high), 2),
        'low': round(float(low), 2),
        'vol': data.get('vol'),
        'cap': None,
        'currency': currency,
        'name': data.get('name', symbol.split('.')[0]),
        'pe': None,
        'eps': None,
        'divYield': None,
        'beta': None,
        'w52h': round(float(data['w52h']), 2) if data.get('w52h') else None,
        'w52l': round(float(data['w52l']), 2) if data.get('w52l') else None,
        'avgVol': None,
        'rev': None,
        'sector': None,
        'prices': spark,
        'source': data.get('source'),
        'simulated': False,
    }


# ── Quote Route ─────────────────────────────────────────────────────────────
@app.route('/quote/<symbol>')
def get_quote(symbol):
    try:
        symbol_upper = symbol.upper()
        live = fetch_live_quote(symbol_upper)
        if not live:
            return jsonify({
                'error': 'Live quote unavailable',
                'symbol': symbol_upper,
            }), 503

        return jsonify(build_quote_response(symbol_upper, live))

    except Exception as exc:
        logger.exception('%s: unexpected quote error', symbol)
        return jsonify({'error': str(exc)}), 500


def _info_float(info, key):
    val = info.get(key)
    if val is None or val == 'None':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def fetch_yfinance_fundamentals(symbol):
    ticker = yf.Ticker(symbol, session=custom_session)
    info = ticker.info
    if not info or len(info) < 3:
        raise ValueError('yfinance info empty')

    fcf = _info_float(info, 'freeCashflow')
    shares = _info_float(info, 'sharesOutstanding')
    fcf_ps = (fcf / shares) if fcf and shares and shares > 0 else None

    eps = _info_float(info, 'trailingEps') or _info_float(info, 'epsTrailingTwelveMonths')
    if fcf_ps is None and eps:
        fcf_ps = eps * 0.75

    rev_g = _info_float(info, 'revenueGrowth')
    earn_g = _info_float(info, 'earningsQuarterlyGrowth') or _info_float(info, 'earningsGrowth')

    return {
        'fcfPerShare': round(fcf_ps, 4) if fcf_ps is not None else None,
        'freeCashflow': fcf,
        'sharesOutstanding': shares,
        'revenueGrowth': rev_g,
        'earningsGrowth': earn_g,
        'eps': eps,
        'pe': _info_float(info, 'trailingPE'),
        'beta': _info_float(info, 'beta'),
        'sector': info.get('sector') or info.get('industry'),
        'source': 'yfinance_info',
    }


# ── Fundamentals Route (DCF) ───────────────────────────────────────────────────
@app.route('/fundamentals/<symbol>')
def get_fundamentals(symbol):
    try:
        symbol_upper = symbol.upper()
        data = fetch_yfinance_fundamentals(symbol_upper)
        logger.info('%s: fundamentals via %s (fcf/sh=%s)', symbol_upper, data['source'], data.get('fcfPerShare'))
        return jsonify(data)
    except Exception as exc:
        logger.warning('%s: fundamentals failed — %s', symbol, exc)
        return jsonify({'error': str(exc), 'symbol': symbol.upper()}), 503


# ── Backtest History Route ───────────────────────────────────────────────────
@app.route('/backtest/<symbol>')
def get_backtest(symbol):
    try:
        symbol_upper = symbol.upper()
        prices, meta = fetch_backtest_history(symbol_upper)
        if prices is None:
            return jsonify({
                'error': 'Historical data unavailable',
                'symbol': symbol_upper,
                'details': meta,
            }), 503

        source = meta
        return jsonify({
            'prices': [round(p, 4) for p in prices],
            'count': len(prices),
            'source': source,
            'symbol': symbol_upper,
        })

    except Exception as exc:
        logger.exception('%s: unexpected backtest error', symbol)
        return jsonify({'error': str(exc)}), 500


# ── News Feed Route ─────────────────────────────────────────────────────────
@app.route('/news/<symbol>')
def get_news(symbol):
    try:
        items = []
        clean = symbol.upper().replace('.NS', '').replace('.BO', '').replace('.L', '').replace('-USD', '')
        rss = f'https://news.google.com/rss/search?q={clean}+stock+market+finance&hl=en-IN&gl=IN&ceid=IN:en'
        feed = feedparser.parse(rss)

        for entry in feed.entries[:8]:
            items.append({
                'title': entry.get('title', ''),
                'publisher': entry.get('source', {}).get('text', 'Market News'),
                'link': entry.get('link', ''),
                'time': 0,
            })
        return jsonify({'news': items})
    except Exception as exc:
        logger.warning('news/%s failed: %s', symbol, exc)
        return jsonify({'news': [], 'error': str(exc)}), 500


# ── Search Route ─────────────────────────────────────────────────────────────
@app.route('/search/<query>')
def search_ticker(query):
    try:
        q_upper = query.upper()
        market = 'IN' if q_upper.endswith(('.NS', '.BO')) else 'US'
        currency = resolve_currency(q_upper)
        return jsonify({
            'sym': q_upper,
            'name': q_upper.split('.')[0],
            'market': market,
            'currency': currency,
            'valid': True,
        })
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/')
def health():
    return jsonify({'status': 'QuantTerminal Core Feed Online', 'version': '5.1'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)