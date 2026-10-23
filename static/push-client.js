// 手機通知（Web Push）設定區塊：完整版 Healer 頁與手機監控版共用（2026-10-22）
// 用法：頁面上放 <div id="push-body"></div>，需要時呼叫 renderPushPanel()。
// 每種「開不了」的狀況都直接講原因和下一步，不能只有一顆按不下去的按鈕。
(function () {
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  async function getJSON(url) {
    const r = await fetch(url);
    if (r.status === 401) throw new Error('登入已經過期，請重新整理頁面後再登入。');
    let d = null;
    try { d = await r.json(); } catch (e) { throw new Error('伺服器回應格式不正確（HTTP ' + r.status + '），請稍後再試。'); }
    if (!r.ok) throw new Error((d && d.error) || ('伺服器回應 HTTP ' + r.status));
    return d;
  }
  async function postPush(path, body) {
    const r = await fetch('/api/push/' + path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok || d.ok === false) throw new Error(d.error || ('伺服器回應 HTTP ' + r.status));
    return d;
  }
  function isIOS() { return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1); }
  function isStandalone() { return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true; }
  function b64urlToBytes(s) {
    const pad = '='.repeat((4 - s.length % 4) % 4);
    const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, function (c) { return c.charCodeAt(0); });
  }
  async function currentSub() {
    const reg = await navigator.serviceWorker.ready;
    return reg.pushManager.getSubscription();
  }

  let synced = false;
  async function renderPushPanel(msg) {
    const box = document.getElementById('push-body');
    if (!box) return;
    const show = function (html) { box.innerHTML = '<div class="push-box">' + html + (msg ? '<div>' + msg + '</div>' : '') + '</div>'; };
    if (!window.isSecureContext) {
      show('<div class="t-warn">通知只能在 https 網址下開啟。請改用平台的對外網址（https:// 開頭）打開這個頁面。</div>'); return;
    }
    if (isIOS() && !isStandalone()) {
      show('<div>iPhone／iPad 要先把平台加到主畫面，才能收到通知（需要 iOS 16.4 以上）：</div>' +
        '<ol><li>用 <b>Safari</b> 打開這個網址</li><li>點下方的「分享」按鈕（方框加向上箭頭）</li><li>選「加入主畫面」</li><li>從主畫面的 ZeroTouch 圖示打開，再回到這裡按「開啟通知」</li></ol>'); return;
    }
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      show('<div class="t-warn">這個瀏覽器不支援推播通知。請改用 Chrome、Edge、Firefox，或 iPhone 上加到主畫面的 Safari。</div>'); return;
    }
    if (Notification.permission === 'denied') {
      show('<div class="t-warn">你之前拒絕了這個網站的通知，瀏覽器不會再問一次。</div>' +
        '<div>請到瀏覽器的網站設定（網址列左邊的圖示 → 網站設定 → 通知）改成「允許」，再重新整理這一頁。手機上的 App 版本請到手機的「設定 → 通知 → ZeroTouch」打開。</div>'); return;
    }
    let st;
    try { st = await getJSON('/api/push/status'); } catch (e) { show('<div class="t-warn">讀不到通知設定：' + esc(e.message || String(e)) + '</div>'); return; }
    if (!st.available) { show('<div class="t-warn">平台這端還沒有設定好推播功能（伺服器缺少推播套件），請通知管理員。</div>'); return; }
    let sub = null;
    try { sub = await currentSub(); } catch (e) {}
    if (sub && !synced) {
      // 伺服器那份可能被清掉過（例如推播服務回報失效），每次進頁面先同步一次
      synced = true;
      try { await postPush('subscribe', {subscription: sub.toJSON()}); } catch (e) { sub = null; }
    }
    if (sub) {
      show('<div>✅ 這台裝置<b>已開啟</b>通知。Healer 發現問題或修好時，會通知你服務名稱和原因；同一個問題 6 小時內只通知一次。</div>' +
        '<div class="dim">平台的主機關機時不會送出通知，所以沒收到通知不代表一切正常。這個帳號目前有 ' + esc(st.devices) + ' 台裝置開啟通知（最多 ' + esc(st.max_devices) + ' 台）。</div>' +
        '<div class="actions"><button type="button" class="btn" data-act="push-test">送測試通知</button><button type="button" class="btn" data-act="push-off">關閉這台裝置的通知</button></div>');
    } else {
      show('<div>開啟後，Healer 發現問題或修好時，這台裝置會跳出通知；點通知會直接打開那個服務的詳情。通知只寫服務名稱和原因，不會顯示日誌內容或密碼。</div>' +
        '<div class="actions"><button type="button" class="btn primary" data-act="push-on">開啟通知</button></div>');
    }
  }
  async function turnOn() {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { renderPushPanel(perm === 'denied' ? '' : '<span class="t-warn">你沒有允許通知，所以還沒開啟。想開的時候再按一次即可。</span>'); return; }
      const st = await getJSON('/api/push/status');
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64urlToBytes(st.public_key)});
      await postPush('subscribe', {subscription: sub.toJSON()});
      synced = true;
      await postPush('test', {endpoint: sub.endpoint});
      renderPushPanel('已送出一則測試通知。如果幾秒內沒有看到，請確認手機或電腦沒有開「勿擾模式」。');
    } catch (e) { renderPushPanel('<span class="t-warn">開啟失敗：' + esc(e.message || String(e)) + '</span>'); }
  }
  async function turnOff() {
    try {
      const sub = await currentSub();
      if (sub) { await postPush('unsubscribe', {endpoint: sub.endpoint}); await sub.unsubscribe(); }
      renderPushPanel('已關閉這台裝置的通知。');
    } catch (e) { renderPushPanel('<span class="t-warn">關閉失敗：' + esc(e.message || String(e)) + '</span>'); }
  }
  async function test() {
    try {
      const sub = await currentSub();
      if (!sub) throw new Error('這台裝置還沒開啟通知。');
      await postPush('test', {endpoint: sub.endpoint});
      renderPushPanel('已送出測試通知。如果幾秒內沒有看到，請確認沒有開「勿擾模式」。');
    } catch (e) { renderPushPanel('<span class="t-warn">' + esc(e.message || String(e)) + '</span>'); }
  }
  document.addEventListener('click', function (e) {
    const b = e.target.closest('[data-act^="push-"]');
    if (!b) return;
    b.disabled = true;
    ({'push-on': turnOn, 'push-off': turnOff, 'push-test': test}[b.dataset.act] || function () {})();
  });
  window.renderPushPanel = renderPushPanel;
})();
