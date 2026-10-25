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
.bar{display:flex;gap:1px;height:18px;margin-top:6px;border-radius:4px;overflow:hidden}
.bar i{flex:1;min-width:1px;background:var(--pass)}
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
/* 圖表 */
.chart{width:100%;height:auto;display:block;touch-action:pan-y}
.chart .ax{stroke:var(--grid);stroke-width:1}
.chart text{fill:var(--text-3);font-size:10px}
.chart-cap{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.chart-cap b{font-size:15px}
.chart-cap .now{font-size:20px;font-weight:700}
.readout{font-size:12.5px;color:var(--text-2);min-height:18px;margin-top:4px}
.donuts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.donut{text-align:center}
.donut .t{font-size:13px;color:var(--text-2);margin-top:4px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:var(--text-2);margin-top:10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.hbar{margin:8px 0}
.hbar .row{display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px}
.hbar .track{height:10px;border-radius:5px;background:var(--panel-2);overflow:hidden}
.hbar .fill{height:100%;border-radius:5px;background:var(--signal)}
.note{font-size:12.5px;color:var(--text-3);margin-top:8px}
.tabs{position:fixed;left:0;right:0;bottom:0;z-index:10;display:grid;grid-template-columns:repeat(5,1fr);background:var(--panel);border-top:1px solid var(--grid);padding-bottom:env(safe-area-inset-bottom)}
.tabs button{border:0;background:none;color:var(--text-3);height:var(--tab-h);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:12px;position:relative}
.tabs button svg{width:22px;height:22px}
.tabs button.on{color:var(--signal)}
.tabs .dot{position:absolute;top:10px;left:calc(50% + 8px);width:9px;height:9px;border-radius:50%;background:var(--stop);display:none}
.tabs .dot.show{display:block}
.view{display:none} .view.on{display:block}
.gear{border:0;background:none;color:var(--text-2);padding:4px;margin-left:4px;display:flex}
.gear.on{color:var(--signal)}
/* 行事曆（只能看） */
.mcal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.mcal-head b{font-size:16px}
.mcal-head button{border:1px solid var(--grid-2);background:var(--panel);color:var(--text);border-radius:8px;width:36px;height:32px;font-size:18px}
.mcal{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.mcal .wd{text-align:center;font-size:11px;color:var(--text-3)}
.mcal button{aspect-ratio:1/1;border:1px solid var(--grid);background:var(--panel);color:var(--text);border-radius:8px;font-size:13px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:0}
.mcal button.today{border-color:var(--signal);color:var(--signal);font-weight:700}
.mcal button.sel{background:var(--signal);border-color:var(--signal);color:var(--on-signal)}
.mcal .mk{display:flex;gap:2px;height:6px}
.mcal .mk i{width:5px;height:5px;border-radius:50%}
.mk-task{background:var(--signal)}.mk-due{background:var(--caution)}.mk-dep{background:var(--pass)}.mk-bad{background:var(--stop)}.mk-note{background:var(--text-2)}
.mcal button.sel .mk i{outline:1px solid var(--on-signal)}
.mday h3{font-size:13px;color:var(--text-3);margin:14px 2px 6px;font-weight:600}
.mrow{background:var(--panel);border:1px solid var(--grid);border-radius:var(--radius);padding:10px 12px;margin-bottom:8px;font-size:14px}
.mrow .t{font-size:12px;color:var(--text-3)}
.mrow.del{border-color:var(--stop)}
.mnote{white-space:pre-wrap;background:var(--panel);border:1px solid var(--grid);border-radius:var(--radius);padding:10px 12px;font-size:14px}
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
  <button class="gear" type="button" data-tab="settings" aria-label="設定"><svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg></button>
</header>
<main>
  <div id="banners"></div>
  <section class="view" id="v-overview"></section>
  <section class="view" id="v-services"></section>
  <section class="view" id="v-charts"></section>
  <section class="view" id="v-calendar"></section>
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
  <button type="button" data-tab="charts"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>圖表</button>
  <button type="button" data-tab="calendar"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>行事曆<span class="dot" id="dot-cal"></span></button>
  <button type="button" data-tab="history"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>修復紀錄</button>
</nav>
<script>
const NEEDS_SETUP = {{ 'true' if needs_setup else 'false' }};
const TITLES = {overview:'總覽', services:'服務', charts:'圖表', calendar:'行事曆', history:'修復紀錄', settings:'設定'};
const S = {status:null, deps:[], pods:[], healer:null, dash:null, dashError:null, loadedAt:null, error:null, autoscale:null};

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
    loadMCal();
    // 2026-10-23：分時擴縮與休眠的設定（唯讀；抓不到不影響其他畫面）
    getJSON('/api/autoscale').then(a => { S.autoscale = a && a.ok !== false ? a : null; if(!location.hash.startsWith('#services/')) renderServices(); })
      .catch(() => { S.autoscale = null; });
    // 圖表資料另外抓：Prometheus 慢或連不上時，不要拖住上面的狀態
    getJSON('/api/dashboard').then(d => { S.dash = d; S.dashError = null; renderCharts(); })
      .catch(e => { S.dashError = e.message || String(e); renderCharts(); });
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

function asOf(name){ return ((S.autoscale && S.autoscale.items) || []).find(x => x.name === name) || null; }
function svcRow(d, s){
  const a = asOf(d.name);
  const extra = a ? (a.sleeping ? ' · 💤 休眠中' : '') + (a.schedule ? ' · ⏱ 分時擴縮' : '') : '';
  return `<button type="button" class="svc" data-svc="${esc(d.name)}"><span class="nm"><b>${esc(d.name)}</b><span>${esc(d.ready)}/${esc(d.replicas)} 個就緒 · ${esc(d.image || '')}${esc(extra)}</span></span><span class="tag ${s.cls}">${esc(s.label)}</span><span class="chev">›</span></button>`;
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
    const dep = S.deps.find(x => x.name === name);
    // 2026-10-24：休眠中（副本數 0）的服務沒有 Pod，原本會被當成「找不到」；服務還在部署清單裡就照常顯示
    if(!dep && (!d.found || !pods.length)){ el.innerHTML = '<div class="empty">找不到這個服務，它可能已經被刪除。</div>'; return; }
    let html = dep ? `<div class="card"><dl class="kv"><dt>服務</dt><dd>${esc(dep.name)}</dd><dt>副本</dt><dd>${esc(dep.ready)}/${esc(dep.replicas)} 個就緒</dd><dt>映像檔</dt><dd>${esc(dep.image)}</dd><dt>最近更新</dt><dd>${esc(dep.updated || dep.age || '')}</dd></dl></div>` : '';
    if(!pods.length) html += '<div class="card dim">目前沒有執行中的 Pod（服務休眠中，或副本數是 0）。</div>';
    // 2026-10-23：分時擴縮與休眠（只能看，修改要用電腦）
    const asx = asOf(name);
    if(asx){
      const rows = asx.schedule ? asx.schedule.rows.map(r => `<dt>${esc(r.when)}</dt><dd>${esc(r.replicas)} 個</dd>`).join('') : '<dt>排程</dt><dd>沒有設定</dd>';
      const nxt = asx.schedule && asx.schedule.next ? `<dt>下次變化</dt><dd>${esc(asx.schedule.next.at)} 變成 ${esc(asx.schedule.next.replicas)} 個</dd>` : '';
      const sleepLine = (S.autoscale && S.autoscale.remote) ? '' : `<dt>休眠</dt><dd>${asx.sleeping ? `休眠中，醒來開 ${esc(asx.sleep_replicas)} 個` : asx.exempt ? '在例外清單，不會休眠' : '太久沒人用會自動休眠'}</dd>`;
      html += `<div class="card"><div class="sec" style="margin-top:0">分時擴縮</div><dl class="kv">${rows}${nxt}${sleepLine}</dl>
        <div class="dim" style="font-size:12.5px;margin-top:8px">手機只能查看；要設定或修改排程請用電腦開啟平台。${(S.autoscale && S.autoscale.remote_note) ? '<br>' + esc(S.autoscale.remote_note) : ''}</div></div>`;
    }
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
        <div class="sec" style="margin-top:12px">健康狀態（綠色正常、紅色有狀況）</div><div id="hist-${esc(p.name)}"><div class="dim" style="font-size:13px">讀取中…</div></div></div>`;
    }
    el.innerHTML = html;
    pods.slice(0, 5).forEach((p, n) => {
      getJSON('/api/pods/' + encodeURIComponent(p.name) + '/history').then(h => {
        if(seq !== detailSeq) return;
        const box = document.getElementById('hist-' + p.name);
        const hs = ((h && h.history) || []).slice(-120);
        if(!box) return;
        if(!hs.length){ box.innerHTML = '<span class="dim" style="font-size:13px">還沒有累積資料。Healer 每 30 秒記錄一次，過幾分鐘再來看。</span>'; return; }
        const ok = x => x.phase === 'Running' && x.total && x.ready === x.total;
        box.innerHTML = `<div class="bar">${hs.map(x => `<i class="${ok(x) ? '' : 'bad'}"></i>`).join('')}</div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-3);margin-top:2px"><span>${esc(hhmm(hs[0].t))}</span><span>${esc(hhmm(hs[hs.length - 1].t))}</span></div>
          <div class="sec" style="margin-top:12px">重啟次數趨勢</div>
          ${hs.length > 1 ? lineChart('rs' + n, hs.map(x => ({t:x.t, v:x.restarts || 0})), '次', 'var(--stop)', 0) : '<span class="dim" style="font-size:13px">資料還不夠畫趨勢。</span>'}`;
      }).catch(() => {});
    });
  }catch(e){ if(seq === detailSeq) el.innerHTML = `<div class="empty">讀取失敗：${esc(e.message || String(e))}</div>`; }
}

// ── 圖表（手寫 SVG，不載入外部套件） ──
const CHARTS = {};
function hhmm(t){ return new Date(t).toLocaleTimeString('zh-TW', {hour12:false, hour:'2-digit', minute:'2-digit'}); }
function fmtNum(v, digits){ return Number(v).toFixed(digits).replace(/\.0+$/, ''); }
// 折線圖：點圖上任一處會顯示最接近那個時間點的數值
function lineChart(id, pts, unit, color, digits){
  CHARTS[id] = {pts, unit, digits};
  const W = 340, H = 150, L = 38, R = 8, T = 10, B = 22;
  const max = Math.max(...pts.map(p => p.v), 0);
  const top = max > 0 ? max * 1.15 : 1;
  const x = i => L + (pts.length > 1 ? i * (W - L - R) / (pts.length - 1) : (W - L - R) / 2);
  const y = v => T + (H - T - B) * (1 - v / top);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  const area = `${line}L${x(pts.length - 1).toFixed(1)},${y(0)}L${x(0).toFixed(1)},${y(0)}Z`;
  const last = pts[pts.length - 1];
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" data-chart="${id}" role="img" aria-label="趨勢圖">
    <line class="ax" x1="${L}" y1="${y(0)}" x2="${W - R}" y2="${y(0)}"/><line class="ax" x1="${L}" y1="${y(top / 1.15)}" x2="${W - R}" y2="${y(top / 1.15)}" stroke-dasharray="3 3"/>
    <text x="${L - 4}" y="${y(0) + 3}" text-anchor="end">0</text><text x="${L - 4}" y="${y(top / 1.15) + 3}" text-anchor="end">${esc(fmtNum(top / 1.15, digits))}</text>
    <text x="${L}" y="${H - 6}">${esc(hhmm(pts[0].t))}</text><text x="${W - R}" y="${H - 6}" text-anchor="end">${esc(hhmm(last.t))}</text>
    <path d="${area}" style="fill:${color};opacity:.14"/><path d="${line}" style="fill:none;stroke:${color};stroke-width:2;stroke-linejoin:round"/>
    <circle class="mark" cx="${x(pts.length - 1)}" cy="${y(last.v)}" r="4" style="fill:${color}"/></svg>
    <div class="readout" id="ro-${id}">最新 ${esc(hhmm(last.t))}：${esc(fmtNum(last.v, digits))} ${esc(unit)}（點圖表看其他時間）</div>`;
}
function onChartTap(svg, clientX){
  const c = CHARTS[svg.dataset.chart];
  if(!c || !c.pts.length) return;
  const box = svg.getBoundingClientRect(), W = 340, L = 38, R = 8;
  const vx = (clientX - box.left) * W / box.width;
  const i = Math.max(0, Math.min(c.pts.length - 1, Math.round((vx - L) / ((W - L - R) / Math.max(1, c.pts.length - 1)))));
  const p = c.pts[i];
  const mark = svg.querySelector('.mark'), line = svg.querySelector('path:last-of-type');
  if(mark && line){
    const seg = line.getAttribute('d').split(/[ML]/).filter(Boolean)[i];
    if(seg){ const [cx, cy] = seg.split(','); mark.setAttribute('cx', cx); mark.setAttribute('cy', cy); }
  }
  const ro = document.getElementById('ro-' + svg.dataset.chart);
  if(ro) ro.textContent = `${hhmm(p.t)}：${fmtNum(p.v, c.digits)} ${c.unit}`;
}
// 環狀圖：parts = [{v, color, label}]
function donut(parts, center, sub){
  const total = parts.reduce((s, p) => s + Math.max(0, p.v), 0) || 1;
  const r = 42, C = 2 * Math.PI * r;
  let off = 0;
  const arcs = parts.map(p => {
    const len = Math.max(0, p.v) / total * C;
    const s = `<circle cx="60" cy="60" r="${r}" style="fill:none;stroke:${p.color};stroke-width:16" stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}" transform="rotate(-90 60 60)"/>`;
    off += len; return s;
  }).join('');
  return `<svg class="chart" viewBox="0 0 120 120" style="max-width:150px;margin:0 auto"><circle cx="60" cy="60" r="${r}" style="fill:none;stroke:var(--panel-2);stroke-width:16"/>${arcs}
    <text x="60" y="58" text-anchor="middle" style="fill:var(--text);font-size:15px;font-weight:700">${esc(center)}</text>
    <text x="60" y="74" text-anchor="middle">${esc(sub)}</text></svg>`;
}
function chartEmpty(msg){ return `<div class="empty" style="padding:18px 6px">${esc(msg)}</div>`; }

function renderCharts(){
  const el = document.getElementById('v-charts');
  if(!el) return;
  if(NEEDS_SETUP){ el.innerHTML = '<div class="empty">還沒連上叢集。請先用電腦完成連線設定。</div>'; return; }
  const d = S.dash;
  if(!d){ el.innerHTML = S.dashError ? chartEmpty('讀不到圖表資料：' + S.dashError) : '<div class="empty">讀取中…</div>'; return; }
  let trendMsg = '';
  if(!d.k8s_enabled) trendMsg = 'Kubernetes 沒有連線，讀不到用量。';
  else if(d.customer_cluster) trendMsg = '你的叢集沒有安裝指標收集元件（metrics-server），所以沒有用量趨勢。下面的容量與用量是依各服務設定的資源需求計算。';
  else if(!d.prometheus_up) trendMsg = '監控元件（Prometheus）目前連不上，暫時沒有用量趨勢。';
  const cpu = d.cpu_trend || [], mem = d.mem_trend || [];
  const trendCard = (title, pts, unit, color, digits, id) => `<div class="card"><div class="chart-cap"><b>${title}</b><span class="dim" style="font-size:12px">過去 30 分鐘</span></div>
    ${trendMsg ? chartEmpty(trendMsg) : pts.length < 2 ? chartEmpty('資料還不夠畫趨勢，服務剛啟動時要等幾分鐘。') : lineChart(id, pts, unit, color, digits)}</div>`;
  let html = trendCard('CPU 用量', cpu, '核', 'var(--pass)', 3, 'cpu') + trendCard('記憶體用量', mem, 'MiB', 'var(--signal)', 1, 'mem');

  const cap = d.cluster_capacity, used = d.cluster_used, rem = d.cluster_remaining;
  if(cap && used && rem){
    const mineC = d.my_total_cpu_cores || 0, mineM = d.my_total_mem_gib || 0;
    const parts = (mine, u, r) => [{v:mine, color:'var(--signal)'}, {v:Math.max(0, u - mine), color:'var(--caution)'}, {v:r, color:'var(--panel-2)'}];
    const pct = (u, c) => c ? Math.round(u / c * 100) + '%' : '-';
    html += `<div class="card"><div class="chart-cap"><b>叢集容量</b><span class="dim" style="font-size:12px">整個叢集共用</span></div>
      <div class="donuts"><div class="donut">${donut(parts(mineC, used.cpu_cores, rem.cpu_cores), pct(used.cpu_cores, cap.cpu_cores), '已使用')}<div class="t">CPU：剩 ${esc(fmtNum(rem.cpu_cores, 2))} / ${esc(fmtNum(cap.cpu_cores, 2))} 核</div></div>
      <div class="donut">${donut(parts(mineM, used.mem_gib, rem.mem_gib), pct(used.mem_gib, cap.mem_gib), '已使用')}<div class="t">記憶體：剩 ${esc(fmtNum(rem.mem_gib, 1))} / ${esc(fmtNum(cap.mem_gib, 1))} GiB</div></div></div>
      <div class="legend"><span><i style="background:var(--signal)"></i>我的服務</span><span><i style="background:var(--caution)"></i>其他使用者與系統</span><span><i style="background:var(--panel-2);border:1px solid var(--grid-2)"></i>剩餘</span></div>
      <div class="note">${(d.cluster_used_source || {}).cpu === 'requests' ? 'CPU 以各服務申請的量計算' : 'CPU 以實際用量計算'}；${(d.cluster_used_source || {}).mem === 'usage' ? '記憶體以實際用量計算' : '記憶體以各服務申請的量計算'}。同一時間可能有其他使用者在部署，數字會變動。</div></div>`;
  } else {
    html += `<div class="card"><b>叢集容量</b>${chartEmpty('讀不到叢集容量。')}</div>`;
  }

  const deps = d.my_deployments || [];
  if(deps.length){
    const maxM = Math.max(...deps.map(x => x.mem_gib || 0), 0.001), maxC = Math.max(...deps.map(x => x.cpu_cores || 0), 0.001);
    const rows = [...deps].sort((a, b) => (b.mem_gib || 0) - (a.mem_gib || 0));
    html += `<div class="card"><div class="chart-cap"><b>各服務佔用的資源</b><span class="dim" style="font-size:12px">依申請量</span></div>
      ${rows.map(x => `<div class="hbar"><div class="row"><span>${esc(x.name)}<span class="dim">　×${esc(x.replicas)}</span></span><span class="dim">${esc(fmtNum((x.mem_gib || 0) * 1024, 0))} MiB · ${esc(fmtNum(x.cpu_cores || 0, 2))} 核</span></div>
        <div class="track"><div class="fill" style="width:${Math.max(2, (x.mem_gib || 0) / maxM * 100).toFixed(1)}%"></div></div>
        <div class="track" style="margin-top:3px;height:6px"><div class="fill" style="width:${Math.max(2, (x.cpu_cores || 0) / maxC * 100).toFixed(1)}%;background:var(--pass)"></div></div></div>`).join('')}
      <div class="legend"><span><i style="background:var(--signal)"></i>記憶體</span><span><i style="background:var(--pass)"></i>CPU</span></div></div>`;
  }
  html += `<div class="card"><div class="chart-cap"><b>每月預估費用</b><span class="now">US$ ${esc(fmtNum(d.my_monthly_usd || 0, 2))}</span></div>
    <div class="dim" style="font-size:13px">依你所有服務申請的 CPU ${esc(fmtNum(d.my_total_cpu_cores || 0, 2))} 核、記憶體 ${esc(fmtNum(d.my_total_mem_gib || 0, 2))} GiB，以一般雲端價格估算。</div></div>`;
  el.innerHTML = html;
}

// ── 行事曆（只能看：預約、平台做過的事、備註；要預約、取消、確認執行都用電腦） ──
const WDZ = '日一二三四五六';
const SST = {scheduled:'已預約', due:'等你確認', running:'執行中', done:'已完成', failed:'失敗', blocked:'被擋下', cancelled:'已取消', expired:'已過期'};
const z2 = n => String(n).padStart(2, '0');
function twp(ts){ const d = new Date(ts * 1000 + 8 * 3600 * 1000); return {y:d.getUTCFullYear(), mo:d.getUTCMonth() + 1, d:d.getUTCDate(), h:d.getUTCHours(), mi:d.getUTCMinutes(), wd:d.getUTCDay()}; }
function twDay(ts){ const p = twp(ts); return `${p.y}-${z2(p.mo)}-${z2(p.d)}`; }
function twHM(ts){ const p = twp(ts); return `${z2(p.h)}:${z2(p.mi)}`; }
const MC = {month:null, sel:null, data:null, err:null};
async function loadMCal(){
  if(NEEDS_SETUP){ renderMCal(); return; }
  const m = MC.month || twDay(Date.now() / 1000).slice(0, 7);
  try{
    MC.data = await getJSON('/api/calendar?month=' + m); MC.month = m; MC.err = null;
    if(!MC.sel || MC.sel.slice(0, 7) !== m) MC.sel = MC.data.today.slice(0, 7) === m ? MC.data.today : m + '-01';
  }catch(e){ MC.err = e.message || String(e); }
  renderMCal();
}
function mcalShift(n){
  const [y, m] = (MC.month || twDay(Date.now() / 1000).slice(0, 7)).split('-').map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  MC.month = `${d.getUTCFullYear()}-${z2(d.getUTCMonth() + 1)}`; MC.sel = null;
  loadMCal();
}
function renderMCal(){
  const el = document.getElementById('v-calendar');
  if(!el) return;
  if(NEEDS_SETUP){ el.innerHTML = '<div class="empty">還沒連上叢集。請先用電腦完成連線設定。</div>'; return; }
  if(!MC.data){ el.innerHTML = MC.err ? `<div class="empty">行事曆讀取失敗：${esc(MC.err)}</div>` : '<div class="empty">讀取中…</div>'; return; }
  const [y, m] = MC.month.split('-').map(Number);
  const first = new Date(Date.UTC(y, m - 1, 1)).getUTCDay(), ndays = new Date(Date.UTC(y, m, 0)).getUTCDate();
  let cells = WDZ.split('').map(w => `<div class="wd">${w}</div>`).join('') + '<div></div>'.repeat(first);
  let dueAll = 0;
  for(let d = 1; d <= ndays; d++){
    const day = `${MC.month}-${z2(d)}`, info = MC.data.days[day] || {};
    const tasks = info.tasks || [], evs = info.events || [];
    const due = tasks.some(t => t.status === 'due'); dueAll += tasks.filter(t => t.status === 'due').length;
    const live = tasks.some(t => t.status === 'scheduled');
    const bad = evs.some(e => e.kind === 'heal' || e.kind === 'review' || e.outcome === 'fail');
    const dep = evs.some(e => ['deploy', 'scale', 'update', 'delete', 'rollback'].includes(e.kind));
    cells += `<button type="button" data-mcal-day="${day}" class="${day === MC.data.today ? 'today' : ''} ${day === MC.sel ? 'sel' : ''}" aria-label="${m} 月 ${d} 日">${d}
      <span class="mk">${live ? '<i class="mk-task"></i>' : ''}${due ? '<i class="mk-due"></i>' : ''}${dep ? '<i class="mk-dep"></i>' : ''}${bad ? '<i class="mk-bad"></i>' : ''}${info.note ? '<i class="mk-note"></i>' : ''}</span></button>`;
  }
  const info = MC.data.days[MC.sel] || {};
  const [sy, sm, sd] = MC.sel.split('-').map(Number);
  const tasks = (info.tasks || []).slice().sort((a, b) => a.run_at - b.run_at), evs = (info.events || []).slice().sort((a, b) => a.ts - b.ts);
  const pill = s => `<span class="tag ${s === 'due' ? 'warn' : ['failed', 'blocked'].includes(s) ? 'bad' : ['cancelled', 'expired'].includes(s) ? 'idle' : ''}">${esc(SST[s] || s)}</span>`;
  el.innerHTML = `<div class="card">
      <div class="mcal-head"><button type="button" data-mcal-shift="-1" aria-label="上個月">‹</button><b>${y} 年 ${m} 月</b><button type="button" data-mcal-shift="1" aria-label="下個月">›</button></div>
      <div class="mcal">${cells}</div>
      <div class="legend"><span><i style="background:var(--signal)"></i>預約</span><span><i style="background:var(--caution)"></i>等你確認</span><span><i style="background:var(--pass)"></i>部署／變更</span><span><i style="background:var(--stop)"></i>修復／擋下</span><span><i style="background:var(--text-2)"></i>備註</span></div></div>
    <div class="mday"><h3>${sm} 月 ${sd} 日（週${WDZ[new Date(Date.UTC(sy, sm - 1, sd)).getUTCDay()]}）的預約</h3>
      ${tasks.length ? tasks.map(t => `<div class="mrow ${t.kind === 'delete' ? 'del' : ''}"><div style="display:flex;gap:8px;align-items:center"><span class="t">${esc(twHM(t.run_at))}</span><b style="flex:1">${esc(t.summary)}</b>${pill(t.status)}</div>${t.result ? `<div class="t" style="margin-top:4px">${esc(t.result)}</div>` : ''}</div>`).join('') : '<div class="dim" style="font-size:13.5px">這天沒有預約。</div>'}
      <h3>這天發生的事</h3>
      ${evs.length ? evs.map(e => `<div class="mrow"><span class="t">${esc(twHM(e.ts))}</span>　<b style="color:${e.kind === 'heal' || e.kind === 'review' || e.outcome === 'fail' ? 'var(--stop)' : 'inherit'}">${esc(e.label)}</b> ${esc(e.app || '')}${e.detail ? `<div class="t" style="margin-top:2px">${esc(e.detail)}</div>` : ''}</div>`).join('') : '<div class="dim" style="font-size:13.5px">平台這天沒有紀錄。</div>'}
      <h3>備註</h3>${info.note ? `<div class="mnote">${esc(info.note)}</div>` : '<div class="dim" style="font-size:13.5px">沒有備註。</div>'}
      <p class="note">要預約、取消、確認執行或寫備註，請用電腦打開平台。手機版只能查看。</p></div>`;
  const dc = document.getElementById('dot-cal'); if(dc) dc.classList.toggle('show', dueAll > 0);
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
  document.querySelectorAll('.tabs button, .gear').forEach(b => b.classList.toggle('on', b.dataset.tab === t));
  if(t === 'calendar') loadMCal();
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
  renderOverview(); renderServices(); renderCharts(); renderHistory();
  document.getElementById('upd').textContent = S.loadedAt ? `更新 ${fmtTime(S.loadedAt)}` : '';
}

document.addEventListener('click', e => {
  const ch = e.target.closest('[data-chart]');
  if(ch){ onChartTap(ch, e.clientX); return; }
  const md = e.target.closest('[data-mcal-day]');
  if(md){ MC.sel = md.dataset.mcalDay; renderMCal(); return; }
  const ms = e.target.closest('[data-mcal-shift]');
  if(ms){ mcalShift(parseInt(ms.dataset.mcalShift, 10)); return; }
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
