"""
cluster/pages.py
客戶自帶叢集的引導頁面：同意書 → 貼指令 → 連線檢查。2026-10-09 新增（第 4 階段）。

跟 web_demo.py 主頁面共用同一組 CSS 變數與字型，看起來是同一個網站；獨立成一個樣板是因為
主頁面是 6500 行的單一檔案，這三頁跟主功能無關，分開比較好維護。
內容依 AGENT_RULES「對新手絕對友善」：中文在前、不假設看得懂 K8s、每個步驟講清楚怎麼做。
"""

ONBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>連線你的叢集 · ZeroTouch</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#FAFAFA;--surface:#FFFFFF;--border:#E5E7EB;--border2:#D1D5DB;
  --text:#111827;--text2:#4B5563;--text3:#9CA3AF;
  --green:#16A34A;--green-dark:#15803D;--green-light:#DCFCE7;
  --red:#DC2626;--red-light:#FEE2E2;--blue:#2563EB;--blue-light:#DBEAFE;
  --yellow:#B45309;--yellow-light:#FEF3C7;
  --radius:10px;--radius-sm:6px;--shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
}
body{font-family:'DM Sans','Microsoft JhengHei',sans-serif;background:var(--bg);color:var(--text);line-height:1.65}
.wrap{max-width:760px;margin:0 auto;padding:32px 16px 64px}
.steps{display:flex;gap:8px;margin-bottom:24px;font-size:13px;color:var(--text3);flex-wrap:wrap}
.steps span{padding:4px 12px;border-radius:20px;border:1px solid var(--border)}
.steps span.on{background:var(--green-light);border-color:var(--green);color:var(--green-dark);font-weight:600}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:28px}
h1{font-size:22px;margin-bottom:6px}
.sub{color:var(--text2);font-size:14px;margin-bottom:20px}
h2{font-size:15px;margin:20px 0 6px}
p,li{font-size:14px;color:var(--text2)}
ul,ol{padding-left:22px}
li{margin:3px 0}
code,.mono{font-family:'DM Mono',monospace;font-size:13px}
.box{border-radius:var(--radius-sm);padding:12px 14px;font-size:14px;margin:12px 0}
.box.warn{background:var(--yellow-light);color:var(--yellow)}
.box.err{background:var(--red-light);color:var(--red)}
.box.ok{background:var(--green-light);color:var(--green-dark)}
.box.info{background:var(--blue-light);color:#1E40AF}
.cmd{display:flex;gap:8px;align-items:stretch;margin:10px 0}
.cmd pre{flex:1;background:#111827;color:#F9FAFB;padding:14px;border-radius:var(--radius-sm);overflow-x:auto;font-family:'DM Mono',monospace;font-size:13px;white-space:pre-wrap;word-break:break-all}
.actions{display:flex;gap:12px;justify-content:flex-end;margin-top:24px;flex-wrap:wrap}
.btn{padding:11px 22px;border-radius:var(--radius-sm);font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;border:1px solid var(--border2);background:var(--surface);color:var(--text);text-decoration:none;display:inline-block}
.btn:hover{background:var(--bg)}
.btn.primary{background:var(--green);border-color:var(--green);color:#fff}
.btn.primary:hover{background:var(--green-dark)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.copy{white-space:nowrap}
.prog{list-style:none;padding:0;margin:8px 0}
.prog li{padding:4px 0;font-size:14px}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--border2);border-top-color:var(--green);border-radius:50%;animation:spin 1s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
details{margin-top:10px}summary{cursor:pointer;font-size:14px;color:var(--blue)}
.foot{font-size:12px;color:var(--text3);margin-top:16px}
</style>
</head>
<body>
<div class="wrap">
  <div class="steps">
    <span class="{{ 'on' if step == 'consent' else '' }}">① 同意書 Consent</span>
    <span class="{{ 'on' if step == 'install' else '' }}">② 執行指令 Run command</span>
    <span class="{{ 'on' if step == 'check' else '' }}">③ 連線檢查 Check</span>
  </div>

{% if step == 'consent' %}
  <div class="card">
    <h1>連線你的 Kubernetes 叢集：授權同意書</h1>
    <div class="sub">Connect your Kubernetes cluster — consent &amp; checklist ｜ 帳號 {{ username }} ｜ 版本 {{ consent_version }}</div>

    <h2>這是什麼？</h2>
    <p>ZeroTouch 會把你用自然語言描述的服務，部署到<strong>你自己電腦上的 Kubernetes</strong>，使用的是你電腦的 CPU 與記憶體。平台不會直接連進你的電腦：部署設定放在平台替你建立的私有 GitHub repo，由你電腦上的 Flux 自己去拉取、套用。</p>

    <h2>開始前請先準備好</h2>
    <ul>
      <li><strong>Windows 的 PowerShell</strong>（內建，不用安裝）</li>
      <li><strong>Docker Desktop，並開啟 Kubernetes</strong>：Docker Desktop → 右上角齒輪 Settings → Kubernetes → 勾選 Enable Kubernetes → Apply &amp; restart，等左下角變綠燈</li>
      <li>電腦能連上網路，並且在使用期間保持開機、Docker Desktop 保持開啟</li>
      <li>建議電腦至少 16GB 記憶體。平台元件本身大約使用 300MB；你部署的服務另外計算</li>
    </ul>

    <h2>會在你的叢集裡安裝什麼</h2>
    <ul>
      <li><strong>Flux</strong>（CNCF 開源專案，安裝在 <code>flux-system</code>）：負責從 GitHub 拉取部署設定。如果你的叢集原本就有 Flux，會沿用，不會更動你現有的版本。</li>
      <li><strong>你的專屬 namespace</strong> <code>{{ namespace }}</code>：你部署的服務都在這裡，並套用 Kubernetes 內建的安全規則（Pod Security baseline），叢集會拒絕特權容器等危險設定。</li>
      <li><strong>狀態回報程式</strong>（<code>zt-reporter</code>）：每 15 秒把上述 namespace 的狀態回報給平台。<strong>它只能讀取，不能建立、修改或刪除任何東西</strong>，原始碼是公開的。</li>
    </ul>

    <h2>平台能做什麼、不能做什麼</h2>
    <ul>
      <li>✅ 只能透過 Git，把部署設定放進 <code>{{ namespace }}</code>（部署、擴縮、更新、回滾、自動修復都是這樣做）</li>
      <li>❌ 不能讀取或修改其他 namespace（唯一的例外是讀取節點的 CPU / 記憶體容量，用來檢查資源夠不夠）</li>
      <li>❌ 不能在你的電腦上執行任何指令，也看不到你的檔案</li>
    </ul>

    <h2>會傳回平台的資料</h2>
    <p><code>{{ namespace }}</code> 裡的 Pod / Deployment 狀態、事件訊息、<strong>不健康 Pod 的最後 50 行 log</strong>（用來診斷問題），以及節點的 CPU / 記憶體容量。平台只在記憶體中保留最新一份狀態；同意紀錄與連線紀錄會保存在平台。</p>

    <h2>請注意的風險</h2>
    <ul>
      <li>你部署的服務會使用你電腦的資源。電腦變慢時，可以到網站刪除服務</li>
      <li>設定雖然經過三方審查與格式檢查，仍可能有誤，請在部署前看清楚確認畫面</li>
      <li><strong>不要把安裝指令分享給別人</strong>：指令 30 分鐘內有效，裡面含有讀取你部署設定的金鑰</li>
      <li>平台（我們的伺服器）關機時：已經部署的服務會照常運作，但無法新增部署、也看不到狀態</li>
    </ul>

    <details>
      <summary>如何解除安裝 / How to uninstall</summary>
      <p style="margin-top:8px">在 PowerShell 執行：</p>
      <div class="cmd"><pre>{{ uninstall }}</pre></div>
      <p>如果 Flux 是這次才裝的、而且你沒有其他用途，也可以執行 <code>kubectl delete namespace flux-system</code> 移除它。</p>
    </details>

    <div class="actions">
      <form method="post" action="/onboard/decline"><button class="btn" type="submit">不同意並離開 / Decline &amp; leave</button></form>
      <form method="post" action="/onboard/consent"><button class="btn primary" type="submit">同意並繼續 / Agree &amp; continue</button></form>
    </div>
  </div>

{% elif step == 'install' %}
  <div class="card">
    <h1>在你的電腦執行一行指令</h1>
    <div class="sub">Run one command on your computer</div>
    {% if not github_ready %}
      <div class="box err">平台這端還沒設定完成（管理員需要設定 GitHub），目前無法產生安裝指令。請稍後再試，或聯絡管理員。<br>
      The platform isn't fully configured yet (GitHub settings missing). Please try again later.</div>
    {% else %}
    <ol>
      <li>確認 <strong>Docker Desktop 已開啟</strong>，左下角 Kubernetes 是綠燈</li>
      <li>開啟 <strong>PowerShell</strong>：按鍵盤上的 Windows 鍵，輸入 <code>PowerShell</code>，按 Enter（不是「命令提示字元」）</li>
      <li>按下方「複製」，在 PowerShell 視窗按滑鼠右鍵貼上，再按 Enter</li>
      <li>等畫面出現綠色的「完成！」（第一次大約 2~5 分鐘），回到這裡按「繼續」</li>
    </ol>
    <div class="cmd">
      <pre id="cmd">{{ command }}</pre>
      <button class="btn copy" type="button" onclick="copyCmd()" id="copy-btn">複製 Copy</button>
    </div>
    <p class="foot">這個指令在 {{ expires_at }} 前有效，只能給你自己使用。想先看它會做什麼？<a href="{{ script_url }}" target="_blank" rel="noopener">檢視腳本內容</a>（只是檢視，不會執行）。</p>
    {% endif %}
    <div class="actions">
      <form method="post" action="/onboard/back"><button class="btn" type="submit">返回 / Back</button></form>
      <a class="btn primary" href="/onboarding?step=check">繼續 / Continue</a>
    </div>
  </div>

{% elif step == 'check' %}
  <div class="card">
    <h1>連線檢查</h1>
    <div class="sub">Checking the connection to your cluster ｜ namespace <code>{{ namespace }}</code></div>
    <div id="status-box" class="box info"><span class="spinner"></span>檢查中… / Checking…</div>
    <div id="howto"></div>
    <pre id="detail" class="mono" style="display:none;background:var(--bg);padding:10px;border-radius:6px;white-space:pre-wrap;font-size:12px;color:var(--text2)"></pre>
    <h2>安裝進度 / Install progress</h2>
    <ul class="prog" id="prog"><li>（尚未收到 / nothing yet）</li></ul>
    <p class="foot" id="auto-note">這個頁面每 5 秒會自動重新檢查一次。 / This page re-checks every 5 seconds.</p>
    <div class="actions">
      <a class="btn" href="/onboarding?step=install">返回 / Back</a>
      <button class="btn" type="button" onclick="check()">重新檢查 / Check again</button>
      <a class="btn primary" id="enter-btn" href="/" style="display:none">進入 ZeroTouch / Enter</a>
    </div>
  </div>
{% endif %}
  <p class="foot" style="text-align:center">
    <form method="post" action="/auth/logout" style="display:inline"><button class="btn" style="padding:4px 10px;font-size:12px" type="submit">登出 / Log out</button></form>
  </p>
</div>

<script>
function copyCmd(){
  const t=document.getElementById('cmd').innerText;
  navigator.clipboard.writeText(t).then(()=>{const b=document.getElementById('copy-btn');b.innerText='已複製 ✓';setTimeout(()=>b.innerText='複製 Copy',2000)})
   .catch(()=>{const r=document.createRange();r.selectNodeContents(document.getElementById('cmd'));const s=window.getSelection();s.removeAllRanges();s.addRange(r);alert('請按 Ctrl+C 複製 / Press Ctrl+C to copy');});
}
const STEP_NAMES={start:'開始 / start',kubectl:'找到 kubectl',cluster:'連上叢集 / cluster reachable',flux:'安裝 Flux',crds:'Flux 啟動完成',bundle:'套用平台設定',reporter:'回報程式啟動',done:'安裝腳本完成 / done'};
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
let timer=null;
async function check(){
  const box=document.getElementById('status-box');
  try{
    const r=await fetch('/api/onboard/status',{cache:'no-store'});
    if(r.status===401){location.href='/';return}
    const d=await r.json();
    box.className='box '+(d.ok?'ok':(d.code==='sync_pending'||d.code==='reporter_not_ready'||d.code==='installing'||d.code==='not_started'?'warn':'err'));
    box.innerHTML=(d.ok?'✅ ':(d.code==='sync_pending'||d.code==='reporter_not_ready'||d.code==='installing'?'<span class="spinner"></span>':'⚠️ '))+'<strong>'+esc(d.title)+'</strong>';
    document.getElementById('howto').innerHTML=d.ok?'':'<p style="margin:8px 0 4px"><strong>怎麼辦 / What to do：</strong>'+esc(d.howto)+'</p>';
    const det=document.getElementById('detail');
    if(d.detail&&!d.ok){det.style.display='block';det.textContent=d.detail}else{det.style.display='none'}
    const prog=document.getElementById('prog');
    if(d.progress&&d.progress.length){prog.innerHTML=d.progress.map(p=>'<li>'+(p.ok?'✅':'❌')+' '+esc(STEP_NAMES[p.step]||p.step)+'</li>').join('')}
    document.getElementById('enter-btn').style.display=d.ok?'inline-block':'none';
    if(d.ok){clearInterval(timer);document.getElementById('auto-note').innerText='連線成功，可以進入網站了。 / Connected — you can enter the site.'}
  }catch(e){box.className='box err';box.innerText='無法連到 ZeroTouch 網站，請確認網路後按「重新檢查」。 / Cannot reach ZeroTouch.'}
}
if(document.getElementById('status-box')){check();timer=setInterval(check,5000)}
</script>
</body>
</html>
"""
