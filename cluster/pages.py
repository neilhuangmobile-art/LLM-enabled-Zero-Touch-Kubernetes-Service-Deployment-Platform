"""
cluster/pages.py
客戶自帶叢集的引導頁面：同意書 → 貼指令 → 連線檢查。2026-10-09 新增（第 4 階段）。

跟 web_demo.py 主頁面用同一套設計（2026-10-09 前端改版：深海藍主控台、四角刻度模組、
儀表訊號藍），深淺色跟主頁面共用 localStorage 的 zt_theme；獨立成一個樣板是因為主頁面是
單一大檔，這三頁跟主功能無關，分開比較好維護。
內容依 AGENT_RULES「對新手絕對友善」：中文、不假設看得懂 K8s、每個步驟講清楚怎麼做。
"""

ONBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>連線你的叢集 | ZeroTouch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Oxanium:wght@600;700&family=Noto+Sans+TC:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script>
(function(){try{var t=localStorage.getItem('zt_theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t;}catch(e){}})();
</script>
<style>
:root{
  --void:#0B1622;--panel:#0F2030;--panel-2:#142A3C;--grid:#1D3A50;--grid-2:#28506B;--grid-line:rgba(70,130,170,.10);
  --text:#DCE8F0;--text-2:#93AABB;--text-3:#62798A;
  --signal:#5CC8FF;--signal-soft:rgba(92,200,255,.12);--signal-line:rgba(92,200,255,.45);
  --pass:#3DD68C;--pass-soft:rgba(61,214,140,.13);--caution:#F5B547;--caution-soft:rgba(245,181,71,.14);
  --stop:#FF6B5B;--stop-soft:rgba(255,107,91,.14);--on-signal:#04111C;
  --f-display:"Oxanium","Noto Sans TC",system-ui,sans-serif;
  --f-body:"Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif;
  --f-id:"JetBrains Mono",Consolas,monospace;
  color-scheme:dark;
}
@media (prefers-color-scheme: light){ :root:not([data-theme="dark"]){
  --void:#E8EFF5;--panel:#FFFFFF;--panel-2:#F2F6FA;--grid:#CFDCE6;--grid-2:#B5C7D5;--grid-line:rgba(30,90,140,.07);
  --text:#0E1E2B;--text-2:#475D6D;--text-3:#7A8E9C;--signal:#0A6DB3;--signal-soft:rgba(10,109,179,.09);--signal-line:rgba(10,109,179,.4);
  --pass:#1C8A57;--pass-soft:rgba(28,138,87,.1);--caution:#A66A05;--caution-soft:rgba(166,106,5,.1);
  --stop:#C23A2B;--stop-soft:rgba(194,58,43,.09);--on-signal:#FFFFFF;color-scheme:light } }
:root[data-theme="light"]{
  --void:#E8EFF5;--panel:#FFFFFF;--panel-2:#F2F6FA;--grid:#CFDCE6;--grid-2:#B5C7D5;--grid-line:rgba(30,90,140,.07);
  --text:#0E1E2B;--text-2:#475D6D;--text-3:#7A8E9C;--signal:#0A6DB3;--signal-soft:rgba(10,109,179,.09);--signal-line:rgba(10,109,179,.4);
  --pass:#1C8A57;--pass-soft:rgba(28,138,87,.1);--caution:#A66A05;--caution-soft:rgba(166,106,5,.1);
  --stop:#C23A2B;--stop-soft:rgba(194,58,43,.09);--on-signal:#FFFFFF;color-scheme:light }
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:var(--f-body);font-size:15px;line-height:1.75;color:var(--text);background-color:var(--void);
  background-image:linear-gradient(var(--grid-line) 1px,transparent 1px),linear-gradient(90deg,var(--grid-line) 1px,transparent 1px);
  background-size:40px 40px;background-position:-1px -1px}
:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
a{color:var(--signal)}
code,.mono{font-family:var(--f-id);font-size:.9em}
.wrap{max-width:780px;margin:0 auto;padding:28px 16px 64px}
.top{display:flex;align-items:center;gap:10px;margin-bottom:22px;color:var(--signal)}
.top b{font-family:var(--f-display);font-weight:700;font-size:20px;letter-spacing:.04em;color:var(--text)}
.top form{margin-left:auto}
.steps{list-style:none;display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px;font-size:14px;color:var(--text-3)}
.steps li{display:flex;align-items:center;gap:8px}
.steps li + li::before{content:"";width:22px;height:1px;background:var(--grid-2)}
.steps .n{width:24px;height:24px;border-radius:50%;border:1px solid var(--grid-2);display:grid;place-items:center;font-family:var(--f-display);font-size:13px}
.steps li.on{color:var(--text);font-weight:500}
.steps li.on .n{border-color:var(--signal);background:var(--signal);color:var(--on-signal)}
.steps li.done .n{border-color:var(--signal);color:var(--signal)}
.card{position:relative;background:var(--panel);border:1px solid var(--grid);border-radius:4px;padding:28px}
.card::before,.card::after{content:"";position:absolute;width:10px;height:10px;border-color:var(--signal);border-style:solid;opacity:.75}
.card::before{left:-1px;top:-1px;border-width:2px 0 0 2px}
.card::after{right:-1px;bottom:-1px;border-width:0 2px 2px 0}
h1{font-size:23px;font-weight:700;margin-bottom:4px;text-wrap:balance}
.sub{color:var(--text-2);font-size:14px;margin-bottom:22px;display:flex;flex-wrap:wrap;gap:4px 16px}
h2{font-size:16px;font-weight:700;margin:24px 0 6px}
p,li{color:var(--text-2)}
strong{color:var(--text)}
ul,ol{padding-left:22px}
li{margin:4px 0}
li::marker{color:var(--signal)}
.can li::marker{color:var(--pass)} .cannot li::marker{color:var(--stop)}
.box{border:1px solid;border-radius:4px;padding:12px 14px;margin:12px 0;display:flex;gap:10px;align-items:flex-start}
.box.warn{background:var(--caution-soft);border-color:var(--caution)}
.box.err{background:var(--stop-soft);border-color:var(--stop)}
.box.ok{background:var(--pass-soft);border-color:var(--pass)}
.box.info{background:var(--signal-soft);border-color:var(--signal-line)}
.lamp{width:9px;height:9px;border-radius:50%;flex:none;margin-top:8px}
.box.ok .lamp{background:var(--pass);box-shadow:0 0 8px var(--pass)}
.box.warn .lamp{background:var(--caution);box-shadow:0 0 8px var(--caution);animation:blink 1.4s ease-in-out infinite}
.box.err .lamp{background:var(--stop);box-shadow:0 0 8px var(--stop)}
.box.info .lamp{background:var(--signal);animation:blink 1.4s ease-in-out infinite}
@keyframes blink{50%{opacity:.35}}
.cmd{display:flex;gap:8px;align-items:stretch;margin:12px 0}
.cmd pre{flex:1;min-width:0;background:var(--void);border:1px solid var(--grid-2);color:var(--text);padding:14px;border-radius:4px;overflow-x:auto;font-family:var(--f-id);font-size:13.5px;white-space:pre-wrap;word-break:break-all}
.actions{display:flex;gap:10px;justify-content:flex-end;margin-top:26px;flex-wrap:wrap;align-items:center}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:9px 20px;border-radius:6px;font-size:15px;font-weight:500;cursor:pointer;font-family:inherit;border:1px solid var(--grid-2);background:var(--panel-2);color:var(--text);text-decoration:none;transition:border-color .15s,filter .15s}
.btn:hover{border-color:var(--signal)}
.btn.primary{background:var(--signal);border-color:var(--signal);color:var(--on-signal)}
.btn.primary:hover{filter:brightness(1.08)}
.btn.small{padding:4px 12px;font-size:13.5px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.prog{list-style:none;padding:0;margin:8px 0;border-left:1px solid var(--grid);}
.prog li{position:relative;padding:4px 0 4px 18px;color:var(--text)}
.prog li::before{content:"";position:absolute;left:-5px;top:13px;width:9px;height:9px;border-radius:50%;background:var(--pass)}
.prog li.bad::before{background:var(--stop)}
.prog li.none::before{background:var(--grid-2)}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--grid-2);border-top-color:var(--signal);border-radius:50%;animation:spin .8s linear infinite;flex:none;margin-top:6px}
@keyframes spin{to{transform:rotate(360deg)}}
details{margin-top:14px;border-top:1px solid var(--grid);padding-top:10px}
summary{cursor:pointer;color:var(--signal)}
.foot{font-size:13.5px;color:var(--text-3);margin-top:14px}
#detail{display:none;background:var(--void);border:1px solid var(--grid);padding:10px 12px;border-radius:4px;white-space:pre-wrap;font-size:13px;color:var(--text-2)}
@media (max-width:560px){.card{padding:20px}.cmd{flex-direction:column}}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="11" fill="none" stroke="currentColor" stroke-width="2"/><g stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M13 3.5v19M3.5 13h19M6.3 6.3l13.4 13.4M19.7 6.3 6.3 19.7"/></g><circle cx="13" cy="13" r="3.4" fill="var(--panel)" stroke="currentColor" stroke-width="2"/></svg>
    <b>ZeroTouch</b>
    <form method="post" action="/auth/logout"><button class="btn small" type="submit">登出</button></form>
  </div>

  <ol class="steps" aria-label="連線步驟">
    <li class="{{ 'on' if step == 'consent' else 'done' }}"><span class="n">1</span>同意書</li>
    <li class="{{ 'on' if step == 'install' else ('done' if step == 'check' else '') }}"><span class="n">2</span>執行指令</li>
    <li class="{{ 'on' if step == 'check' else '' }}"><span class="n">3</span>連線檢查</li>
  </ol>

{% if step == 'consent' %}
  <div class="card">
    <h1>連線你的 Kubernetes 叢集：授權同意書</h1>
    <div class="sub"><span>帳號 {{ username }}</span><span>同意書版本 {{ consent_version }}</span></div>

    <h2>這是什麼？</h2>
    <p>ZeroTouch 會把你用自然語言描述的服務，部署到<strong>你自己電腦上的 Kubernetes</strong>，使用的是你電腦的 CPU 和記憶體。平台不會直接連進你的電腦：部署設定放在平台替你建立的私有 GitHub repo，由你電腦上的 Flux 自己去拉取、套用。</p>

    <h2>開始前請先準備好</h2>
    <ul>
      <li><strong>Windows 的 PowerShell</strong>，Windows 內建，不用另外安裝</li>
      <li><strong>Docker Desktop，並開啟 Kubernetes</strong>：打開 Docker Desktop，按右上角的齒輪進入 Settings，在 Kubernetes 頁籤勾選 Enable Kubernetes，按 Apply &amp; restart，等左下角變成綠燈</li>
      <li>電腦能連上網路，使用期間保持開機，Docker Desktop 保持開啟</li>
      <li>建議電腦至少有 16GB 記憶體。平台元件本身大約使用 300MB，你部署的服務另外計算</li>
    </ul>

    <h2>會在你的叢集裡安裝什麼</h2>
    <ul>
      <li><strong>Flux</strong>（CNCF 開源專案，安裝在 <code>flux-system</code>）：負責從 GitHub 拉取部署設定。如果你的叢集原本就有 Flux，會沿用，不會更動你現有的版本。</li>
      <li><strong>你的專屬 namespace</strong> <code>{{ namespace }}</code>：你部署的服務都在這裡，並套用 Kubernetes 內建的安全規則（Pod Security baseline），叢集會拒絕特權容器這類危險設定。</li>
      <li><strong>狀態回報程式</strong>（<code>zt-reporter</code>）：每 15 秒把上述 namespace 的狀態回報給平台。<strong>它只能讀取，不能建立、修改或刪除任何東西</strong>，原始碼是公開的。</li>
    </ul>

    <h2>平台能做什麼</h2>
    <ul class="can">
      <li>只能透過 Git，把部署設定放進 <code>{{ namespace }}</code>。部署、擴縮、更新、回滾、自動修復都是這樣做的</li>
    </ul>
    <h2>平台不能做什麼</h2>
    <ul class="cannot">
      <li>不能讀取或修改其他 namespace。唯一的例外是讀取節點的 CPU 和記憶體容量，用來檢查資源夠不夠</li>
      <li>不能在你的電腦上執行任何指令，也看不到你的檔案</li>
    </ul>

    <h2>會傳回平台的資料</h2>
    <p><code>{{ namespace }}</code> 裡的 Pod 和 Deployment 狀態、事件訊息、<strong>不健康 Pod 的最後 50 行 log</strong>（用來診斷問題），以及節點的 CPU 和記憶體容量。平台只在記憶體中保留最新一份狀態；同意紀錄與連線紀錄會保存在平台。</p>

    <h2>你在網站輸入的文字</h2>
    <p>你在聊天或部署欄位輸入的文字，會先送到 <strong>Google Gemini API</strong> 整理成固定格式，並檢查有沒有惡意操縱的意圖，之後才交給平台自己的模型處理。我們使用 Gemini 的付費方案，依 Google 的條款，這些內容不會被拿去改善 Google 的產品，但 Google 可能為了偵測濫用而短期保存。<strong>請不要輸入密碼、金鑰或個人資料。</strong>部署設定、叢集狀態與 log 不會送給 Gemini。</p>

    <h2>請注意的風險</h2>
    <ul>
      <li>你部署的服務會使用你電腦的資源。電腦變慢時，可以到網站刪除服務</li>
      <li>設定雖然經過三項審查與格式檢查，仍可能有誤，請在部署前看清楚確認畫面</li>
      <li><strong>不要把安裝指令分享給別人</strong>：指令 30 分鐘內有效，裡面含有讀取你部署設定的金鑰</li>
      <li>平台的伺服器關機時，已經部署的服務會照常運作，但沒辦法新增部署，也看不到狀態</li>
    </ul>

    <details>
      <summary>如何解除安裝</summary>
      <p style="margin-top:8px">在 PowerShell 執行：</p>
      <div class="cmd"><pre>{{ uninstall }}</pre></div>
      <p>如果 Flux 是這次才裝的、而且你沒有其他用途，也可以執行 <code>kubectl delete namespace flux-system</code> 移除它。</p>
    </details>

    <div class="actions">
      <form method="post" action="/onboard/decline"><button class="btn" type="submit">不同意並離開</button></form>
      <form method="post" action="/onboard/consent"><button class="btn primary" type="submit">同意並繼續</button></form>
    </div>
  </div>

{% elif step == 'install' %}
  <div class="card">
    <h1>在你的電腦執行一行指令</h1>
    <div class="sub"><span>大約需要 2 到 5 分鐘</span></div>
    {% if not github_ready %}
      <div class="box err"><span class="lamp"></span><div>平台這端還沒設定完成（管理員需要設定 GitHub），目前沒辦法產生安裝指令。請稍後再試，或聯絡管理員。</div></div>
    {% else %}
    <ol>
      <li>確認 <strong>Docker Desktop 已經開啟</strong>，左下角的 Kubernetes 是綠燈</li>
      <li>開啟 <strong>PowerShell</strong>：按鍵盤上的 Windows 鍵，輸入 <code>PowerShell</code>，按 Enter。不是「命令提示字元」</li>
      <li>按下方的「複製」，在 PowerShell 視窗按滑鼠右鍵貼上，再按 Enter</li>
      <li>等畫面出現綠色的「完成！」，回到這裡按「繼續」</li>
    </ol>
    <div class="cmd">
      <pre id="cmd">{{ command }}</pre>
      <button class="btn" type="button" onclick="copyCmd()" id="copy-btn">複製</button>
    </div>
    <p class="foot" id="copy-note">這個指令在 {{ expires_at }} 前有效，只能給你自己使用。想先看它會做什麼？<a href="{{ script_url }}" target="_blank" rel="noopener">檢視腳本內容</a>，只是檢視，不會執行。</p>
    {% endif %}
    <div class="actions">
      <form method="post" action="/onboard/back"><button class="btn" type="submit">返回</button></form>
      <a class="btn primary" href="/onboarding?step=check">繼續</a>
    </div>
  </div>

{% elif step == 'check' %}
  <div class="card">
    <h1>連線檢查</h1>
    <div class="sub"><span>namespace <code>{{ namespace }}</code></span></div>
    <div id="status-box" class="box info" role="status"><span class="spinner"></span><div>檢查中</div></div>
    <div id="howto"></div>
    <pre id="detail" class="mono"></pre>
    <h2>安裝進度</h2>
    <ul class="prog" id="prog"><li class="none">還沒收到安裝腳本的回報</li></ul>
    <p class="foot" id="auto-note">這個頁面每 5 秒會自動重新檢查一次。</p>
    <div class="actions">
      <a class="btn" href="/onboarding?step=install">返回</a>
      <button class="btn" type="button" onclick="check()">重新檢查</button>
      <a class="btn primary" id="enter-btn" href="/" style="display:none">進入 ZeroTouch</a>
    </div>
  </div>
{% endif %}
</div>

<script>
function copyCmd(){
  const pre = document.getElementById('cmd'), b = document.getElementById('copy-btn');
  const select = () => { const r = document.createRange(); r.selectNodeContents(pre); const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); };
  navigator.clipboard.writeText(pre.innerText)
    .then(() => { b.innerText = '已複製'; setTimeout(() => b.innerText = '複製', 2000); })
    .catch(() => { select(); b.innerText = '已選取，請按 Ctrl+C'; });
}
const STEP_NAMES = {start:'開始安裝', kubectl:'找到 kubectl', cluster:'連上叢集', flux:'安裝 Flux', crds:'Flux 啟動完成',
  bundle:'套用平台設定', reporter:'狀態回報程式啟動', done:'安裝腳本完成'};
function esc(s){ return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const WAITING = ['sync_pending', 'reporter_not_ready', 'installing', 'not_started'];
let timer = null;
async function check(){
  const box = document.getElementById('status-box');
  try{
    const r = await fetch('/api/onboard/status', {cache:'no-store'});
    if(r.status === 401){ location.href = '/'; return; }
    const d = await r.json();
    const cls = d.ok ? 'ok' : (WAITING.includes(d.code) ? 'warn' : 'err');
    box.className = 'box ' + cls;
    const busy = !d.ok && WAITING.includes(d.code) && d.code !== 'not_started';
    box.innerHTML = (busy ? '<span class="spinner"></span>' : '<span class="lamp"></span>') + '<div><strong>' + esc(d.title) + '</strong></div>';
    document.getElementById('howto').innerHTML = d.ok ? '' : '<p style="margin:8px 0 4px"><strong>怎麼辦：</strong>' + esc(d.howto) + '</p>';
    const det = document.getElementById('detail');
    if(d.detail && !d.ok){ det.style.display = 'block'; det.textContent = d.detail; } else { det.style.display = 'none'; }
    const prog = document.getElementById('prog');
    if(d.progress && d.progress.length){ prog.innerHTML = d.progress.map(p => '<li class="' + (p.ok ? '' : 'bad') + '">' + esc(STEP_NAMES[p.step] || p.step) + (p.ok ? '' : '：失敗') + '</li>').join(''); }
    document.getElementById('enter-btn').style.display = d.ok ? 'inline-flex' : 'none';
    if(d.ok){ clearInterval(timer); document.getElementById('auto-note').innerText = '連線成功，可以進入網站了。'; }
  }catch(e){
    box.className = 'box err';
    box.innerHTML = '<span class="lamp"></span><div>連不到 ZeroTouch 網站，請確認網路後按「重新檢查」。</div>';
  }
}
if(document.getElementById('status-box')){ check(); timer = setInterval(check, 5000); }
</script>
</body>
</html>
"""
