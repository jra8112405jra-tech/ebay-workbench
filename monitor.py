"""Experimental public JSON-LD reader; no browser automation or bot-wall bypass."""
import ipaddress
import json
import math
import socket
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

HOSTS={
 'jp.mercari.com':'メルカリ', 'paypayfleamarket.yahoo.co.jp':'Yahooフリマ',
 'www.amazon.co.jp':'Amazon', 'amazon.co.jp':'Amazon',
 'item.rakuten.co.jp':'楽天', 'store.shopping.yahoo.co.jp':'Yahooショッピング'
}

def source_for(url):
    try:
        u=urlsplit(url)
        if u.scheme!='https' or u.hostname not in HOSTS or u.username or u.password or u.port not in (None,443) or not u.path or u.path=='/':
            raise ValueError()
    except ValueError: raise ValueError('対応5サイトのHTTPS商品URLを入力してください（短縮URLは非対応）')
    return HOSTS[u.hostname]

def safe(url):
    source_for(url)
    host=urlsplit(url).hostname
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('外部の公開商品ページのみ取得できます')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

class LD(HTMLParser):
    def __init__(self):
        super().__init__();self.active=False;self.parts=[];self.blocks=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script' and dict(attrs).get('type','').lower()=='application/ld+json': self.active=True;self.parts=[]
    def handle_data(self,data):
        if self.active:self.parts.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.active:
            self.blocks.append(''.join(self.parts));self.active=False

def walk(x):
    if isinstance(x,list):
        for n in x: yield from walk(n)
    elif isinstance(x,dict):
        yield x
        for key in ('@graph','mainEntity'):
            if key in x: yield from walk(x[key])

def parse_html(html,sku=''):
    parser=LD();parser.feed(html); products=[]
    for block in parser.blocks:
        try: obj=json.loads(block)
        except (ValueError,RecursionError):continue
        for p in walk(obj):
            types=p.get('@type',[])
            if isinstance(types,str):types=[types]
            if 'Product' in types: products.append(p)
    if sku: products=[p for p in products if str(p.get('sku',''))==sku]
    if len(products)!=1: return {'stock':'unknown','price':None,'reason':'単一商品の構造化データを特定できません。型番・SKUと実ページを確認してください'}
    p=products[0];offers=p.get('offers',{})
    if isinstance(offers,list):
        if len(offers)!=1:return {'stock':'unknown','price':None,'reason':'複数オファー／バリエーションのため要確認'}
        offers=offers[0]
    if not isinstance(offers,dict) or offers.get('@type')=='AggregateOffer':return {'stock':'unknown','price':None,'reason':'価格・在庫を特定できません'}
    status=str(offers.get('availability','')).rsplit('/',1)[-1]
    stock={'InStock':'in_stock','OutOfStock':'out_of_stock','SoldOut':'out_of_stock'}.get(status,'unknown')
    price=None
    if offers.get('priceCurrency')=='JPY':
        try:
            candidate=float(offers.get('price'))
            if math.isfinite(candidate) and candidate>=0:price=candidate
        except (TypeError,ValueError):pass
    return {'stock':stock,'price':price,'reason':'公開JSON-LDを取得（実ページとの一致を要確認）' if stock!='unknown' else '在庫状態を確定できません'}

def inspect_url(url,sku=''):
    result={'stock':'unknown','price':None,'checked_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}
    try:
        safe(url)
        req=urllib.request.Request(url,headers={'User-Agent':'SourcingWorkbench/0.1 (public product metadata)','Accept':'text/html'})
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=12) as r:
            if 'text/html' not in r.headers.get('Content-Type',''):raise ValueError('HTMLページではありません')
            data=r.read(2_000_001)
            if len(data)>2_000_000:raise ValueError('ページが大きすぎます')
            html=data.decode(r.headers.get_content_charset() or 'utf-8',errors='replace')
        result.update(parse_html(html,sku))
    except Exception:
        result['reason']='取得失敗・アクセス制限・非対応形式。実ページで確認してください'
    return result
