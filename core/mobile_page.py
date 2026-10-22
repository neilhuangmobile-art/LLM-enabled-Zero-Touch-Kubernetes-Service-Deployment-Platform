"""
手機監控版（/m）的頁面（2026-10-22）。

手機端只看狀態：總覽、服務、修復紀錄、設定四個分頁，底部固定分頁列切換。
這一頁只呼叫唯讀 API，程式裡沒有任何部署、擴縮、刪除、修復或聊天的功能；
伺服器也會擋（手機登入的 session 是唯讀，見 web_demo.py 的 _readonly_guard），不是只靠畫面。

由 web_demo.py 用 render_template_string 渲染，變數：view（login / app）、username、error、
google_login_available、needs_setup。JS 裡不能出現雙大括號、大括號加百分比、大括號加井號（Jinja 語法）。
"""

MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ZeroTouch 監控</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0B1622">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/icons/apple-touch-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="ZeroTouch">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<script src="/static/push-client.js" defer></script>
<style>
:root{
  --void:#0B1622; --panel:#0F2030; --panel-2:#142A3C; --grid:#1D3A50; --grid-2:#28506B;
  --text:#DCE8F0; --text-2:#93AABB; --text-3:#62798A;
  --signal:#5CC8FF; --on-signal:#0B1622; --signal-soft:rgba(92,200,255,.12);
  --pass:#3DD68C; --pass-soft:rgba(61,214,140,.13);
  --caution:#F5B547; --caution-soft:rgba(245,181,71,.14);
  --stop:#FF6B6B; --stop-soft:rgba(255,107,107,.13);
  --radius:12px; --tab-h:64px;
  color-scheme:dark;
}
@media (prefers-color-scheme: light){
  :root{
    --void:#EEF3F7; --panel:#FFFFFF; --panel-2:#F4F8FB; --grid:#D5E0E8; --grid-2:#B9CAD6;
    --text:#13222E; --text-2:#4A6072; --text-3:#7A8E9C;
    --signal:#0A7FC0; --on-signal:#FFFFFF; --signal-soft:rgba(10,127,192,.10);
    --pass:#178A55; --pass-soft:rgba(23,138,85,.10);
    --caution:#A86A00; --caution-soft:rgba(168,106,0,.10);
    --stop:#C83A3A; --stop-soft:rgba(200,58,58,.10);
    color-scheme:light;
  }
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--void);color:var(--text);font:15px/1.6 "Noto Sans TC",system-ui,-apple-system,sans-serif;-webkit-text-size-adjust:100%}
button{font:inherit}
.top{position:sticky;top:0;z-index:5;background:var(--void);padding:calc(10px + env(safe-area-inset-top)) 16px 10px;border-bottom:1px solid var(--grid);display:flex;align-items:center;gap:10px}
.top h1{font-size:18px;margin:0;flex:1}
.top .upd{font-size:12px;color:var(--text-3)}
.back{border:0;background:none;color:var(--signal);font-size:15px;padding:4px 6px 4px 0}
main{padding:14px 16px calc(var(--tab-h) + 24px + env(safe-area-inset-bottom))}
.banner{border:1px solid var(--stop);background:var(--stop-soft);border-radius:var(--radius);padding:12px 14px;margin-bottom:12px}
.banner.caution{border-color:var(--caution);background:var(--caution-soft)}
.banner b{display:block;margin-bottom:2px}
.banner div{font-size:13.5px;color:var(--text-2)}
.card{background:var(--panel);border:1px solid var(--grid);border-radius:var(--radius);padding:14px;margin-bottom:12px}
.hero{display:flex;align-items:center;gap:14px}
.lamp{width:44px;height:44px;border-radius:50%;flex:none;background:var(--pass);box-shadow:0 0 18px var(--pass)}
.lamp.warn{background:var(--caution);box-shadow:0 0 18px var(--caution)}
.lamp.bad{background:var(--stop);box-shadow:0 0 18px var(--stop)}
.lamp.idle{background:var(--text-3);box-shadow:none}
.hero .t{font-size:18px;font-weight:700}
.hero .s{font-size:13px;color:var(--text-2)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.stat{background:var(--panel);border:1px solid var(--grid);border-radius:var(--radius);padding:10px;text-align:center}
.stat .n{font-size:22px;font-weight:700}
.stat .l{font-size:12px;color:var(--text-2)}
.sec{font-size:13px;color:var(--text-3);margin:16px 2px 8px}
.svc{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:var(--panel);border:1px solid var(--grid);border-radius:var(--radius);padding:12px 14px;margin-bottom:8px;color:var(--text)}
.svc .nm{flex:1;min-width:0}
.svc .nm b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svc .nm span{font-size:12.5px;color:var(--text-2)}
.chev{color:var(--text-3)}
.tag{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--pass);color:var(--pass);background:var(--pass-soft);white-space:nowrap}
.tag.warn{border-color:var(--caution);color:var(--caution);background:var(--caution-soft)}
.tag.bad{border-color:var(--stop);color:var(--stop);background:var(--stop-soft)}
.tag.idle{border-color:var(--grid-2);color:var(--text-2);background:none}
dl.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0;font-size:14px}
dl.kv dt{color:var(--text-3)} dl.kv dd{margin:0;word-break:break-word}
.ev{font-size:13px;color:var(--text-2);padding:6px 0;border-top:1px solid var(--grid)}
.ev:first-child{border-top:0}
.bar{display:flex;gap:2px;height:18px;margin-top:6px}
.bar i{flex:1;border-radius:2px;background:var(--pass)}
.bar i.bad{background:var(--stop)}
.tl{list-style:none;margin:0;padding:0}
.tl li{background:var(--panel);border:1px solid var(--grid);border-radius:var(--radius);padding:12px 14px;margin-bottom:8px}
.tl time{font-size:12px;color:var(--text-3)}
.tl .w{margin:2px 0 6px}
.empty{text-align:center;color:var(--text-2);padding:28px 10px}
.dim{color:var(--text-2)}
.t-warn{color:var(--caution)}
.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--grid-2);background:var(--panel-2);color:var(--text);border-radius:10px;padding:10px 16px;font-size:15px}
.btn.primary{background:var(--signal);border-color:var(--signal);color:var(--on-signal);font-weight:700}
.btn.block{width:100%}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.push-box{display:flex;flex-direction:column;gap:10px;color:var(--text-2);font-size:14px;line-height:1.7}
.push-box ol{margin:0;padding-left:20px}
.tabs{position:fixed;left:0;right:0;bottom:0;z-index:10;display:grid;grid-template-columns:repeat(4,1fr);background:var(--panel);border-top:1px solid var(--grid);padding-bottom:env(safe-area-inset-bottom)}
.tabs button{border:0;background:none;color:var(--text-3);height:var(--tab-h);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:12px;position:relative}
.tabs button svg{width:22px;height:22px}
.tabs button.on{color:var(--signal)}
.tabs .dot{position:absolute;top:10px;left:calc(50% + 8px);width:9px;height:9px;border-radius:50%;background:var(--stop);display:none}
.tabs .dot.show{display:block}
.view{display:none} .view.on{display:block}
/* 登入 */
.login{min-height:100vh;display:flex;flex-direction:column;justify-content:center;padding:24px 20px calc(24px + env(safe-area-inset-bottom))}
.login h1{font-size:24px;margin:0 0 4px}
.login p{color:var(--text-2);margin:0 0 20px}
.login label{display:block;font-size:13px;color:var(--text-2);margin:12px 0 4px}
.login input{width:100%;padding:12px;border-radius:10px;border:1px solid var(--grid-2);background:var(--panel);color:var(--text);font-size:16px}
.login .err{border:1px solid var(--stop);background:var(--stop-soft);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.login .or{text-align:center;color:var(--text-3);margin:16px 0;font-size:13px}
.login .g{display:flex;align-items:center;justify-content:center;gap:8px;text-decoration:none}
</style>
</head>
<body>
{% if view == 'login' %}
<div class="login">
  <img src="/icons/icon-192.png" alt="" width="56" height="56" style="border-radius:14px;margin-bottom:14px">
  <h1>ZeroTouch 監控</h1>
  <p>手機版只能查看服務狀態和自動修復紀錄。部署、擴縮等操作請用電腦開啟平台。</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="post" action="/auth/login">
    <input type="hidden" name="mode" value="monitor">
    <label for="u">帳號</label><input id="u" name="username" autocomplete="username" autocapitalize="none" required>
    <label for="p">密碼</label><input id="p" name="password" type="password" autocomplete="current-password" required>
    <div style="height:16px"></div>
    <button class="btn primary block" type="submit">登入</button>
  </form>
  {% if google_login_available %}
  <div class="or">或</div>
  <a class="btn block g" href="/auth/google/login?mode=monitor">使用 Google 帳號登入</a>
  {% endif %}
  <p style="margin-top:20px;font-size:13px">還沒有帳號？請先用電腦註冊，並完成叢集連線設定。</p>
</div>
{% else %}
<header class="top">
  <button class="back" id="back" type="button" hidden>‹ 服務</button>
  <h1 id="title">總覽</h1>
  <span class="upd" id="upd"></span>
</header>
<main>
  <div id="banners"></div>
  <section class="view" id="v-overview"></section>
  <section class="view" id="v-services"></section>
  <section class="view" id="v-history"></section>
  <section class="view" id="v-settings">
    <div class="card"><div style="font-weight:700;margin-bottom:8px">手機通知</div><div id="push-body"></div></div>
    <div class="card">
      <dl class="kv"><dt>帳號</dt><dd id="acct">{{ username }}</dd><dt>模式</dt><dd>手機監控版（只能查看）</dd></dl>
      <p class="dim" style="font-size:13.5px;margin:12px 0">部署、擴縮、刪除等操作請用電腦開啟平台。手機版登入的權限是唯讀的，伺服器不會接受這裡送出的任何變更。</p>
      <form method="post" action="/auth/logout"><button class="btn block" type="submit">登出</button></form>
    </div>
  </section>
</main>
<nav class="tabs" aria-label="分頁">
  <button type="button" data-tab="overview"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3"/></svg>總覽<span class="dot" id="dot-ov"></span></button>
  <button type="button" data-tab="services"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="4" width="16" height="6" rx="1.5"/><rect x="4" y="14" width="16" height="6" rx="1.5"/></svg>服務</button>
  <button type="button" data-tab="history"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>修復紀錄</button>
  <button type="button" data-tab="settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 8a6 6 0 1112 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 004 0"/></svg>設定</button>
</nav>
<script>
const NEEDS_SETUP = {{ 'true' if needs_setup else 'false' }};
const TITLES = {overview:'總覽', services:'服務', history:'修復紀錄', settings:'設定'};
const S = {status:null, deps:[], pods:[], healer:null, loadedAt:null, error:null};

function esc(v){ return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function getJSON(url){
  const r = await fetch(url, {cache:'no-store'});
  if(r.status === 401){ location.href = '/m'; throw new Error('登入已過期'); }
  let d = null;
  try { d = await r.json(); } catch(e){ throw new Error('伺服器回應格式不正確（HTTP ' + r.status + '）'); }
  if(!r.ok) throw new Error((d && d.error) || ('HTTP ' + r.status));
  return d;
}
function podOk(p){
  if(p.phase === 'Succeeded') return true;
  const ready = (p.conditions || []).some(c => c.type === 'Ready' && c.status === 'True');
  return p.phase === 'Running' && ready;
}
function svcState(d){
  const pods = S.pods.filter(p => p.app === d.name);
  if(!d.replicas) return {cls:'idle', label:'已停止', bad:false};
  if(pods.some(p => !podOk(p)) || d.ready < d.replicas) return {cls:'bad', label:'需要注意', bad:true};
  return {cls:'', label:'正常', bad:false};
}
// Kubernetes 的狀態代碼換成白話，原文放括號，看不懂代碼的人也知道發生什麼事
const REASON_ZH = {
  CrashLoopBackOff:'程式一啟動就結束，正在反覆重啟', OOMKilled:'記憶體不夠，被系統強制結束',
  ImagePullBackOff:'下載不到映像檔（名稱或版本可能打錯）', ErrImagePull:'下載不到映像檔（名稱或版本可能打錯）',
  Error:'程式執行時發生錯誤而結束', CreateContainerConfigError:'設定有問題，容器無法建立',
  RunContainerError:'容器無法啟動', ContainerCreating:'正在建立中', Pending:'等待安排到機器上執行',
  Completed:'已執行完畢'};
function reasonText(r){ return r ? (REASON_ZH[r] ? `${REASON_ZH[r]}（${r}）` : r) : ''; }
function fmtTime(iso, withDate){
  if(!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('zh-TW', withDate ? {hour12:false, month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'} : {hour12:false, hour:'2-digit', minute:'2-digit'});
}

async function refresh(){
  if(NEEDS_SETUP){ render(); return; }
  try{
    const [st, dp, pd, hl] = await Promise.all([
      getJSON('/api/status'), getJSON('/api/deployments'), getJSON('/api/pods'),
      getJSON('/api/healer/status').catch(() => null)]);
    S.status = st; S.deps = dp.deployments || []; S.pods = pd.pods || []; S.healer = hl;
    S.loadedAt = new Date(); S.error = null;
  }catch(e){
    // 連不上（主機關機）或伺服器錯誤：保留上一份資料但明確標示，不假裝是現況
    S.error = e.message || String(e);
  }
  render();
}

function renderBanners(){
  const b = [];
  if(S.error) b.push(['', '讀不到最新狀態', S.loadedAt ? `下面是 ${fmtTime(S.loadedAt)} 的資料，可能已經不是現況。原因：${S.error}` : `原因：${S.error}。平台的主機可能關機或網路中斷，請稍後再試。`]);
  const st = S.status;
  if(st && st.customer_cluster && !st.customer_cluster.ok) b.push(['', st.customer_cluster.title || '你的叢集目前離線', st.customer_cluster.howto || '']);
  else if(st && st.k8s === false) b.push(['', 'Kubernetes 沒有連線', '平台目前讀不到叢集狀態，Healer 也暫停自動修復。']);
  const h = S.healer;
  if(h && !h.running && h.message) b.push(['caution', 'Healer 自動監控沒有在執行', h.message]);
  document.getElementById('banners').innerHTML = b.map(x => `<div class="banner ${x[0]}"><b>${esc(x[1])}</b>${x[2] ? `<div>${esc(x[2])}</div>` : ''}</div>`).join('');
}

function renderOverview(){
  const el = document.getElementById('v-overview');
  if(NEEDS_SETUP){
    el.innerHTML = `<div class="card"><div class="hero"><div class="lamp idle"></div><div><div class="t">還沒連上叢集</div><div class="s">這個帳號還沒有完成叢集連線設定。請先用電腦登入平台，照著畫面完成同意書與連線步驟，之後手機就能看到服務狀態。</div></div></div></div>`;
    return;
  }
  if(!S.loadedAt){ el.innerHTML = '<div class="empty">讀取中…</div>'; return; }
  const states = S.deps.map(d => ({d, s:svcState(d)}));
  const bad = states.filter(x => x.s.bad);
  const badPods = S.pods.filter(p => !podOk(p)).length;
  const today = new Date().toDateString();
  const heals = ((S.healer && S.healer.recent_actions) || []).filter(a => a.time && new Date(a.time).toDateString() === today).length;
  let lamp, t, s;
  if(!S.deps.length){ lamp = 'idle'; t = '還沒有部署任何服務'; s = '用電腦在平台上部署第一個服務後，這裡就會顯示它的狀態。'; }
  else if(bad.length){ lamp = 'bad'; t = `${bad.length} 個服務需要注意`; s = 'Healer 會自動處理能修的問題，修不了的會通知你。'; }
  else { lamp = ''; t = `${S.deps.length} 個服務都正常`; s = '所有服務的 Pod 都在執行而且準備好了。'; }
  el.innerHTML = `<div class="card"><div class="hero"><div class="lamp ${lamp}"></div><div><div class="t">${esc(t)}</div><div class="s">${esc(s)}</div></div></div></div>
    <div class="stats"><div class="stat"><div class="n">${S.deps.length}</div><div class="l">服務</div></div>
      <div class="stat"><div class="n" style="color:${badPods ? 'var(--stop)' : 'inherit'}">${badPods}</div><div class="l">異常 Pod</div></div>
      <div class="stat"><div class="n">${heals}</div><div class="l">今天自動修復</div></div></div>
    ${bad.length ? `<div class="sec">需要注意</div>${bad.map(x => svcRow(x.d, x.s)).join('')}` : ''}`;
  document.getElementById('dot-ov').classList.toggle('show', bad.length > 0);
}

function svcRow(d, s){
  return `<button type="button" class="svc" data-svc="${esc(d.name)}"><span class="nm"><b>${esc(d.name)}</b><span>${esc(d.ready)}/${esc(d.replicas)} 個就緒 · ${esc(d.image || '')}</span></span><span class="tag ${s.cls}">${esc(s.label)}</span><span class="chev">›</span></button>`;
}

function renderServices(){
  const el = document.getElementById('v-services');
  if(NEEDS_SETUP){ el.innerHTML = '<div class="empty">還沒連上叢集。請先用電腦完成連線設定。</div>'; return; }
  if(!S.loadedAt){ el.innerHTML = '<div class="empty">讀取中…</div>'; return; }
  if(!S.deps.length){ el.innerHTML = '<div class="empty">還沒有部署任何服務。</div>'; return; }
  const rows = S.deps.map(d => ({d, s:svcState(d)})).sort((a, b) => (b.s.bad - a.s.bad));
  el.innerHTML = rows.map(x => svcRow(x.d, x.s)).join('');
}

let detailSeq = 0;
async function renderDetail(name){
  const el = document.getElementById('v-services');
  const seq = ++detailSeq;
  el.innerHTML = '<div class="empty">讀取中…</div>';
  try{
    if(!S.loadedAt) await refresh();   // 從通知直接開進詳情：服務摘要與 Healer 判斷要用到總表資料
    const d = await getJSON('/api/pods/' + encodeURIComponent(name));
    if(seq !== detailSeq) return;
    const pods = d.pods || [];
    if(!d.found || !pods.length){ el.innerHTML = '<div class="empty">找不到這個服務，它可能已經被刪除。</div>'; return; }
    const dep = S.deps.find(x => x.name === name);
    let html = dep ? `<div class="card"><dl class="kv"><dt>服務</dt><dd>${esc(dep.name)}</dd><dt>副本</dt><dd>${esc(dep.ready)}/${esc(dep.replicas)} 個就緒</dd><dt>映像檔</dt><dd>${esc(dep.image)}</dd><dt>最近更新</dt><dd>${esc(dep.updated || dep.age || '')}</dd></dl></div>` : '';
    // Healer 對這個服務最近一次的判斷（比 Kubernetes 的代碼好懂）
    const heal = ((S.healer && S.healer.recent_actions) || []).find(a => (a.pod || '').startsWith(name + '-') || a.pod === name);
    if(heal) html += `<div class="card"><div class="sec" style="margin-top:0">Healer 最近的判斷（${esc(fmtTime(heal.time, true))}）</div>
      <dl class="kv"><dt>原因</dt><dd>${esc(heal.root_cause || '沒有找到')}</dd><dt>處理</dt><dd>${esc(heal.action || '無')}</dd>
      <dt>結果</dt><dd>${heal.ok === false ? '沒修好' : heal.needs_user ? '需要你用電腦處理' : '已處理'}</dd></dl></div>`;
    for(const p of pods.slice(0, 5)){
      const c = (p.containers || [])[0] || {};
      const why = c.reason || c.last_reason || '';
      const warns = (p.events || []).filter(e => e.type === 'Warning').slice(0, 3);
      html += `<div class="card"><div style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><b style="flex:1;word-break:break-all;font-size:13.5px">${esc(p.name)}</b><span class="tag ${p.healthy ? '' : 'bad'}">${p.healthy ? '健康' : '有狀況'}</span></div>
        <dl class="kv"><dt>狀態</dt><dd>${p.healthy ? '正常執行中' : esc(reasonText(why) || p.phase)}</dd><dt>重啟</dt><dd>${esc(p.restarts)} 次</dd><dt>建立</dt><dd>${esc(p.age)}</dd></dl>
        ${warns.length ? `<div class="sec" style="margin-top:12px">警告事件</div>${warns.map(e => `<div class="ev"><b>${esc(e.reason)}</b>${e.count > 1 ? `（${esc(e.count)} 次）` : ''}　${esc(e.message)}</div>`).join('')}` : ''}
        <div class="sec" style="margin-top:12px">健康狀態（最近 1 小時，綠色正常、紅色有狀況）</div><div class="bar" id="bar-${esc(p.name)}"><i style="background:var(--grid)"></i></div></div>`;
    }
    el.innerHTML = html;
    for(const p of pods.slice(0, 5)){
      getJSON('/api/pods/' + encodeURIComponent(p.name) + '/history').then(h => {
        if(seq !== detailSeq) return;
        const bar = document.getElementById('bar-' + p.name);
        const hs = ((h && h.history) || []).slice(-60);
        if(bar) bar.innerHTML = hs.length ? hs.map(x => `<i class="${x.phase === 'Running' && x.total && x.ready === x.total ? '' : 'bad'}"></i>`).join('') : '<span class="dim" style="font-size:13px">還沒有累積資料</span>';
      }).catch(() => {});
    }
  }catch(e){ if(seq === detailSeq) el.innerHTML = `<div class="empty">讀取失敗：${esc(e.message || String(e))}</div>`; }
}

function renderHistory(){
  const el = document.getElementById('v-history');
  if(NEEDS_SETUP){ el.innerHTML = '<div class="empty">還沒連上叢集。</div>'; return; }
  const acts = (S.healer && S.healer.recent_actions) || [];
  if(!S.loadedAt){ el.innerHTML = '<div class="empty">讀取中…</div>'; return; }
  if(!acts.length){ el.innerHTML = '<div class="empty">還沒有自動修復過。<br>Pod 出狀況時，Healer 的處理過程會記在這裡。</div>'; return; }
  el.innerHTML = `<ul class="tl">${acts.map(a => `<li><time>${esc(fmtTime(a.time, true))}</time>
    <div class="w"><b>${esc(a.pod || '')}</b>　原因：${esc(a.root_cause || '沒有找到')}</div>
    <div style="display:flex;gap:8px;align-items:center"><span class="dim" style="flex:1;font-size:13.5px">處理：${esc(a.action || '無')}</span>
    ${a.ok === false ? '<span class="tag bad">沒修好</span>' : a.needs_user ? '<span class="tag warn">需要你處理</span>' : '<span class="tag">已處理</span>'}</div></li>`).join('')}</ul>`;
}

// ── 分頁：網址 #分頁 跟著變，手機的返回手勢就能回上一頁 ──
function route(){
  const h = decodeURIComponent(location.hash.slice(1) || 'overview');
  const [tab, sub] = h.split('/');
  const t = TITLES[tab] ? tab : 'overview';
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + t));
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  const back = document.getElementById('back');
  back.hidden = !(t === 'services' && sub);
  document.getElementById('title').textContent = (t === 'services' && sub) ? sub : TITLES[t];
  if(t === 'services' && sub) renderDetail(sub);
  else render();
  if(t === 'settings' && window.renderPushPanel) window.renderPushPanel();
  window.scrollTo(0, 0);
}
function render(){
  renderBanners();
  const h = location.hash.slice(1) || 'overview';
  if(h.startsWith('services/')) return;   // 詳情頁自己管，不要被 30 秒更新蓋掉
  renderOverview(); renderServices(); renderHistory();
  document.getElementById('upd').textContent = S.loadedAt ? `更新 ${fmtTime(S.loadedAt)}` : '';
}

document.addEventListener('click', e => {
  const tab = e.target.closest('[data-tab]');
  if(tab){ location.hash = tab.dataset.tab; return; }
  const svc = e.target.closest('[data-svc]');
  if(svc){ location.hash = 'services/' + encodeURIComponent(svc.dataset.svc); return; }
  if(e.target.closest('#back')) history.length > 1 ? history.back() : (location.hash = 'services');
});
window.addEventListener('hashchange', route);
document.addEventListener('visibilitychange', () => { if(!document.hidden) refresh(); });
setInterval(() => { if(!document.hidden) refresh(); }, 30000);

if('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});
// 從通知點進來：/m?pod=<服務名稱> 直接打開那個服務
(function(){
  const q = new URLSearchParams(location.search);
  const pod = q.get('pod');
  if(q.toString()) history.replaceState(null, '', '/m' + location.hash);
  if(pod && /^[a-z0-9][a-z0-9.\-]{0,252}$/.test(pod)) location.hash = 'services/' + pod;
  route();
  refresh();
})();
</script>
{% endif %}
</body>
</html>"""
