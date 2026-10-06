'use strict';
const $=s=>document.querySelector(s);
let products=[];let autoEnabled=false;
const defaults={cost:0,domestic:0,international:0,other:0,reserve:0,price_usd:0,shipping_usd:0,rate:150,fee_pct:15,fx_pct:3,fixed_usd:0.4,min_profit:2000,stock:'unknown',fulfillment:'unsecured'};
const moneyKeys=Object.keys(defaults).filter(k=>typeof defaults[k]==='number');
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,data){const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-App-Request':'workbench'},body:JSON.stringify(data)});const obj=await r.json();if(!r.ok){if(r.status===401){$('#workspace').hidden=true;$('#login').hidden=false;}throw Error(obj.error||'処理できませんでした');}return obj;}
const yen=n=>'¥'+Math.round(n).toLocaleString('ja-JP');
function element(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function button(text,fn){const b=element('button',text,'subtle');b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){toast(e.message);}finally{b.disabled=false;}};return b;}
function date(text){return text?new Date(text).toLocaleString('ja-JP'):'未確認';}
async function load(){const data=await api('/api/products');products=data.products;autoEnabled=data.auto_enabled;$('#login').hidden=true;$('#workspace').hidden=false;$('#connection').textContent=`eBay直接出品：未接続 ｜ 自動監視：${autoEnabled?'実験機能・有効':'無効'} ｜ 手動確認は${data.interval_seconds/30}分で再確認の対象。確認事項はこの画面内の判定です。eBayへの反映は行いません。`;render();}
function render(){
 $('#count').textContent=products.length;$('#attention').textContent=products.filter(p=>p.review_reasons.length).length;$('#profitable').textContent=products.filter(p=>p.calculation.profit>=p.min_profit).length;
 const q=$('#search').value.toLowerCase(),filter=$('#filter').value;
 const list=products.filter(p=>(p.name+' '+p.sku+' '+p.source).toLowerCase().includes(q)&&(filter==='all'||filter==='attention'&&p.review_reasons.length||filter==='profitable'&&p.calculation.profit>=p.min_profit));
 const root=$('#products');root.replaceChildren();
 if(!list.length){root.append(element('div',products.length?'条件に合う商品がありません。':'まずは商品URLを1件登録して、利益を計算してみましょう。','empty'));return;}
 for(const p of list){
  const card=element('article',undefined,'card'),top=element('div',undefined,'card-top'),title=element('div');title.append(element('div',p.source+' / '+(p.sku||'型番未入力'),'source'),element('h2',p.name));top.append(title,element('span',p.review_reasons.length?'要確認':'確認済み','badge'+(p.review_reasons.length?'':' good')));card.append(top);
  const row=element('div',undefined,'money-row');for(const [label,value,loss]of [['概算利益',yen(p.calculation.profit),p.calculation.profit<0],['必要現金（予備費込）',yen(p.calculation.required_cash)],['最低利益の販売価格','$'+p.calculation.minimum_usd.toFixed(2)]]){const cell=element('div');cell.append(element('span',label),element('strong',value,loss?'loss':''));row.append(cell);}card.append(row);
  if(p.review_reasons.length)card.append(element('p',p.review_reasons.join(' / '),'reasons'));
  const snap=p.snapshot;
  card.append(element('div',p.monitoring?`自動確認：${date(snap?.checked_at)} ｜ ${snap?.reason||'まだ取得していません'}${snap?.price!=null?' ｜ 取得価格 '+yen(snap.price):''}`:`手動在庫確認：${date(p.manual_checked_at)}`,'snapshot'));
  const actions=element('div',undefined,'actions');actions.append(button('編集',()=>edit(p)),button('下書き',()=>showDraft(p)));
  const link=element('a','仕入先を開く');link.href=p.url;link.target='_blank';link.rel='noopener noreferrer';actions.append(link);
  actions.append(button('今すぐ自動確認',async()=>{await api('/api/check',{id:p.id});await load();toast('確認しました。取得内容と実ページを照合してください');}));
  actions.append(button('確認履歴',async()=>{const data=await api('/api/history',{id:p.id});$('#draft-text').value=data.events.length?data.events.map(x=>`${date(x.checked_at)}\n${x.reason}\n在庫：${x.stock} / 価格：${x.price==null?'不明':yen(x.price)}`).join('\n\n'):'自動確認の履歴はありません';$('#draft-dialog h2').textContent='在庫確認履歴';$('#draft-dialog').showModal();}));
  actions.append(button('削除',async()=>{if(confirm('この商品を削除しますか？')){await api('/api/delete',{id:p.id});await load();}}));card.append(actions);root.append(card);
 }
}
function edit(p){const f=$('#product-form');f.reset();for(const [k,v]of Object.entries({...defaults,...p})){const input=f.elements.namedItem(k);if(!input)continue;if(input.type==='checkbox')input.checked=!!v;else input.value=v??'';}f.elements.confirm_stock.checked=false;$('#edit-title').textContent=p?'商品を編集':'商品を登録';estimate();$('#editor').showModal();}
function readForm(){const f=$('#product-form'),obj=Object.fromEntries(new FormData(f));for(const k of moneyKeys)obj[k]=Number(obj[k]||0);for(const k of ['monitoring','confirm_stock'])obj[k]=f.elements[k].checked;return obj;}
function estimate(){const p=readForm();const net=((p.price_usd+p.shipping_usd)*(1-p.fee_pct/100)-p.fixed_usd)*p.rate*(1-p.fx_pct/100);const costs=p.cost+p.domestic+p.international+p.other+p.reserve;$('#estimate').textContent=`概算利益 ${yen(net-costs)} ／ 必要現金 ${yen(costs)}`;}
async function showDraft(p){const d=await api('/api/draft',{id:p.id});$('#draft-dialog h2').textContent='出品下書き';$('#draft-text').value=d.title+'\n\n'+d.description;$('#draft-dialog').showModal();}
$('#login-form').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,b=f.querySelector('button');b.disabled=true;try{await api('/api/login',{password:f.elements.password.value});f.reset();await load();}catch(e){toast(e.message);}finally{b.disabled=false;}};
$('#product-form').onsubmit=async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button[type=submit]');b.disabled=true;try{await api('/api/save',readForm());$('#editor').close();await load();toast('保存しました');}catch(e){toast(e.message);}finally{b.disabled=false;}};
$('#product-form').oninput=estimate;$('#add').onclick=()=>edit(null);$('#close').onclick=()=>$('#editor').close();$('#draft-close').onclick=()=>$('#draft-dialog').close();$('#search').oninput=render;$('#filter').onchange=render;
$('#copy').onclick=async()=>{try{await navigator.clipboard.writeText($('#draft-text').value);toast('コピーしました');}catch(e){$('#draft-text').select();toast('テキストを選択しました。手動でコピーしてください');}};
$('#logout').onclick=async()=>{try{await api('/api/logout',{});products=[];$('#workspace').hidden=true;$('#login').hidden=false;}catch(e){toast(e.message);}};
$('#restore').onclick=()=>$('#backup-file').click();$('#backup-file').onchange=async e=>{const file=e.target.files[0];if(!file)return;try{if(file.size>1900000)throw Error('ファイルが大きすぎます');const backup=JSON.parse(await file.text());if(!confirm('バックアップの商品を追加します。同じURLの商品はスキップします。続けますか？'))return;const data=await api('/api/restore',{backup});await load();toast(`${data.count}件を追加しました。自動監視と在庫確認は再設定してください。`);}catch(e){toast(e.message);}finally{e.target.value='';}};
load().catch(e=>{if(e.message!=='ログインしてください')toast(e.message);});
setInterval(()=>{if(!$('#workspace').hidden&&!$('#editor').open)load().catch(e=>toast(e.message));},60000);
