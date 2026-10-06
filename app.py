"""Single-user eBay sourcing workbench. Python 3.11+, standard library only."""
import csv
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from monitor import inspect_url, source_for

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get('DATA_DIR', str(ROOT / 'data'))) / 'workbench.sqlite3'
PASSWORD = os.environ.get('APP_PASSWORD', '')
AUTO_ENABLED = os.environ.get('ENABLE_MONITOR', '0') == '1'
INTERVAL = max(900, int(os.environ.get('MONITOR_INTERVAL_SECONDS', '1800')))
SESSIONS = {}
ATTEMPTS = {}
AUTH_LOCK = threading.Lock()
CHECK_LOCK = threading.Lock()

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, product_id TEXT, data TEXT NOT NULL)')
    return c

def all_products():
    with db() as c:
        return [json.loads(r[0]) for r in c.execute('SELECT data FROM products ORDER BY rowid DESC')]

def get_product(pid):
    with db() as c:
        row = c.execute('SELECT data FROM products WHERE id=?', (pid,)).fetchone()
    if not row:
        raise ValueError('商品が見つかりません')
    return json.loads(row[0])

def save(p):
    with db() as c:
        c.execute('INSERT OR REPLACE INTO products VALUES (?,?)', (p['id'], json.dumps(p, ensure_ascii=False)))

def numeric(v, default=0):
    n = float(default if v in ('', None) else v)
    if not math.isfinite(n) or n < 0 or n > 100000000:
        raise ValueError('金額は0以上の有限な数値を入力してください')
    return n

MONEY = ['cost','domestic','international','other','reserve','price_usd','shipping_usd','rate','fee_pct','fx_pct','fixed_usd','min_profit']
DEFAULTS = dict(cost=0, domestic=0, international=0, other=0, reserve=0, price_usd=0, shipping_usd=0, rate=150, fee_pct=15, fx_pct=3, fixed_usd=0.4, min_profit=2000)

def validate(raw, old=None):
    p = dict(old or {})
    p.update({k: str(raw.get(k, p.get(k, ''))).strip()[:5000] for k in ['name','english_title','description','url','sku','notes']})
    if not p['name']:
        raise ValueError('商品名を入力してください')
    p['source'] = source_for(p['url'])
    for k in MONEY:
        p[k] = numeric(raw.get(k, p.get(k, DEFAULTS[k])))
    if p['rate'] <= 0 or p['fee_pct'] >= 100 or p['fx_pct'] >= 100:
        raise ValueError('為替は0より大きく、手数料率は100%未満にしてください')
    p['stock'] = raw.get('stock', p.get('stock','unknown'))
    p['fulfillment'] = raw.get('fulfillment', p.get('fulfillment','unsecured'))
    if p['stock'] not in ['unknown','in_stock','out_of_stock'] or p['fulfillment'] not in ['unsecured','owned','wholesale']:
        raise ValueError('状態の選択が不正です')
    p['monitoring'] = bool(raw.get('monitoring', p.get('monitoring',False)))
    p['id'] = (old or {}).get('id',secrets.token_hex(8))
    p['created_at'] = (old or {}).get('created_at',now())
    p['updated_at'] = now()
    if old and p['url'] != old['url']:
        p.pop('snapshot',None)
        p.pop('manual_checked_at',None)
        p['stock'] = 'unknown'
    if raw.get('confirm_stock') is True:
        p['manual_checked_at'] = now()
    elif old and p['stock'] != old['stock']:
        p.pop('manual_checked_at',None)
    return p

def profit(p):
    d = lambda key: Decimal(str(p[key]))
    proceeds = ((d('price_usd')+d('shipping_usd'))*(1-d('fee_pct')/100)-d('fixed_usd'))*d('rate')*(1-d('fx_pct')/100)
    cost = sum(d(k) for k in ['cost','domestic','international','other','reserve'])
    value = proceeds-cost
    minimum = ((cost+d('min_profit'))/(d('rate')*(1-d('fx_pct')/100))+d('fixed_usd'))/(1-d('fee_pct')/100)-d('shipping_usd')
    return {'profit':round(float(value),2),'required_cash':round(float(cost),2),'minimum_usd':float(max(Decimal(0),minimum).quantize(Decimal('.01'),rounding=ROUND_CEILING))}

def view(p):
    p = dict(p)
    p['calculation'] = profit(p)
    reasons = []
    if p['fulfillment']=='unsecured': reasons.append('仕入れ未確保')
    if p['calculation']['profit']<p['min_profit']: reasons.append('最低利益未達')
    stamp = p.get('manual_checked_at')
    stock = p['stock']
    if p['monitoring']:
        if not AUTO_ENABLED: reasons.append('自動監視は無効')
        snap = p.get('snapshot',{})
        stamp = snap.get('checked_at')
        stock = snap.get('stock','unknown')
        if snap.get('price') is not None and snap['price']>p['cost']: reasons.append('仕入れ値上がり：再計算が必要')
    if stock!='in_stock': reasons.append('売切れ' if stock=='out_of_stock' else '在庫要確認')
    try:
        stale = not stamp or (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()>INTERVAL*2
    except (ValueError,TypeError): stale=True
    if stale: reasons.append('在庫確認が古い／未確認')
    p['review_reasons'] = reasons
    return p

def draft(p):
    title = p['english_title'].strip()
    if not title: title = '[Enter English title] ' + p['name']
    return {'title':title[:80], 'description':p['description'] or 'Please enter an accurate English description, condition, defects, and included accessories.', 'note':'英語は入力内容を使用。自動翻訳・eBayへの出品は未接続です。'}

def check(pid):
    if not AUTO_ENABLED:
        raise ValueError('自動取得は無効です。READMEの設定を確認してください')
    if not CHECK_LOCK.acquire(blocking=False):
        raise ValueError('別の商品を確認中です。少し待ってください')
    try:
        p = get_product(pid)
        snap = inspect_url(p['url'],p.get('sku',''))
        with db() as c:
            # Do not overwrite edits made while a network request was running.
            row = c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
            if not row: return
            current = json.loads(row[0])
            if current['url'] != p['url'] or current.get('sku')!=p.get('sku'): return
            current['snapshot']=snap
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(current,ensure_ascii=False),pid))
            c.execute('INSERT INTO events(product_id,data) VALUES (?,?)',(pid,json.dumps(snap,ensure_ascii=False)))
            c.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)')
    finally: CHECK_LOCK.release()

def worker():
    while True:
        if AUTO_ENABLED:
            for p in all_products():
                if not p['monitoring']: continue
                stamp=p.get('snapshot',{}).get('checked_at')
                due = not stamp or (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()>=INTERVAL
                if due:
                    try: check(p['id'])
                    except Exception: pass
                    time.sleep(3)
        time.sleep(30)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # Never log credentials or request bodies.
    def send(self, code, obj, content_type='application/json; charset=utf-8', cookie=None, download=None):
        data = json.dumps(obj,ensure_ascii=False).encode() if content_type.startswith('application/json') else obj
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        if cookie: self.send_header('Set-Cookie',cookie)
        if download: self.send_header('Content-Disposition',f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>2_000_000: raise ValueError('データが大きすぎます')
        return json.loads(self.rfile.read(n) or b'{}')

    def authed(self):
        c=SimpleCookie()
        try: c.load(self.headers.get('Cookie',''))
        except Exception: return False
        token=c.get('workbench')
        with AUTH_LOCK:
            return bool(token and SESSIONS.get(token.value,0)>time.time())

    def do_GET(self):
        path=urlsplit(self.path).path
        assets={'/':'index.html','/style.css':'style.css','/ui.js':'ui.js','/manifest.json':'manifest.json','/icon.svg':'icon.svg'}
        if path in assets:
            types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/manifest+json','.svg':'image/svg+xml'}
            file=ROOT/'static'/assets[path]
            return self.send(200,file.read_bytes(),types[file.suffix])
        if path=='/health': return self.send(200,{'status':'ok'})
        if not self.authed(): return self.send(401,{'error':'ログインしてください'})
        if path=='/api/products': return self.send(200,{'products':[view(p) for p in all_products()], 'auto_enabled':AUTO_ENABLED,'interval_seconds':INTERVAL,'ebay_connected':False})
        if path=='/api/backup': return self.send(200,{'version':1,'products':all_products()},download='ebay-workbench-backup.json')
        if path=='/api/export':
            s=io.StringIO(); w=csv.writer(s)
            w.writerow(['商品名','仕入先URL','英語タイトル','説明','販売USD','送料USD','概算利益JPY','確認事項'])
            for p in all_products():
                v=view(p); d=draft(p)
                def cell(x):
                    x=str(x)
                    return "'"+x if x.lstrip().startswith(('=','+','-','@','\t','\r')) else x
                w.writerow([cell(x) for x in [p['name'],p['url'],d['title'],d['description'],p['price_usd'],p['shipping_usd'],v['calculation']['profit'],' / '.join(v['review_reasons'])]])
            return self.send(200,('\ufeff'+s.getvalue()).encode(),'text/csv; charset=utf-8',download='listing-drafts.csv')
        return self.send(404,{'error':'見つかりません'})

    def do_POST(self):
        if self.headers.get('X-App-Request')!='workbench': return self.send(403,{'error':'リクエストを確認できません'})
        try:
            path=urlsplit(self.path).path
            data=self.body()
            if not isinstance(data,dict): raise ValueError('JSONオブジェクトが必要です')
            if path=='/api/login':
                ip=self.client_address[0]
                with AUTH_LOCK:
                    hits=[t for t in ATTEMPTS.get(ip,[]) if t>time.time()-300]
                    if len(hits)>=10: return self.send(429,{'error':'5分後に再試行してください'})
                    if not hmac.compare_digest(str(data.get('password','')).encode(),PASSWORD.encode()):
                        ATTEMPTS[ip]=hits+[time.time()]
                        return self.send(401,{'error':'パスワードが違います'})
                    ATTEMPTS.pop(ip,None)
                    token=secrets.token_urlsafe(32)
                    for key in list(SESSIONS):
                        if SESSIONS[key]<time.time(): del SESSIONS[key]
                    SESSIONS[token]=time.time()+86400
                secure='; Secure' if os.environ.get('COOKIE_SECURE','0')=='1' else ''
                return self.send(200,{'ok':True},cookie=f'workbench={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400{secure}')
            if not self.authed(): return self.send(401,{'error':'ログインしてください'})
            if path=='/api/logout':
                c=SimpleCookie();c.load(self.headers.get('Cookie',''))
                with AUTH_LOCK: SESSIONS.pop(c['workbench'].value,None)
                return self.send(200,{'ok':True},cookie='workbench=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
            if path=='/api/save':
                old=get_product(data['id']) if data.get('id') else None
                p=validate(data,old)
                for other in all_products():
                    if other['id']!=p['id'] and other['url']==p['url']: raise ValueError('同じURLの商品が登録済みです')
                save(p);return self.send(200,{'product':view(p)})
            if path=='/api/delete':
                with db() as c:
                    c.execute('DELETE FROM products WHERE id=?',(str(data.get('id','')),))
                    c.execute('DELETE FROM events WHERE product_id=?',(str(data.get('id','')),))
                return self.send(200,{'ok':True})
            if path=='/api/check': check(str(data.get('id','')));return self.send(200,{'ok':True})
            if path=='/api/draft': return self.send(200,draft(get_product(str(data.get('id','')))))
            if path=='/api/history':
                with db() as c:
                    rows=c.execute('SELECT data FROM events WHERE product_id=? ORDER BY id DESC LIMIT 20',(str(data.get('id','')),)).fetchall()
                return self.send(200,{'events':[json.loads(r[0]) for r in rows]})
            if path=='/api/restore':
                raw=data.get('backup',{})
                if not isinstance(raw,dict) or raw.get('version')!=1 or not isinstance(raw.get('products'),list): raise ValueError('バックアップ形式が違います')
                if len(raw['products'])>1000: raise ValueError('一度に1000件までです')
                items=[validate(x) for x in raw['products']]
                if len({p['url'] for p in items})!=len(items): raise ValueError('重複URLがあります')
                with db() as c:
                    existing={p['url'] for p in all_products()}
                    count=0
                    for p in items:
                        if p['url'] in existing: continue
                        p['monitoring']=False
                        c.execute('INSERT INTO products VALUES (?,?)',(p['id'],json.dumps(p,ensure_ascii=False)))
                        count+=1
                return self.send(200,{'count':count})
            return self.send(404,{'error':'見つかりません'})
        except (ValueError,TypeError,KeyError) as e: return self.send(400,{'error':str(e)})
        except Exception: return self.send(500,{'error':'処理できませんでした。設定と保存先を確認してください'})

if __name__=='__main__':
    if len(PASSWORD)<12:
        raise SystemExit('APP_PASSWORDに12文字以上のパスワードを設定してください。')
    db().close()
    threading.Thread(target=worker,daemon=True).start()
    ThreadingHTTPServer((os.environ.get('HOST','127.0.0.1'),int(os.environ.get('PORT','8000'))),Handler).serve_forever()
