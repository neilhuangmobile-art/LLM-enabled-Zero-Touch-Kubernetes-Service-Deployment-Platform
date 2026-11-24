"""
tests/conftest.py
讓測試結果不受開發者本機 .env 影響。

core/config.py 匯入時會讀 .env，但只補「環境變數裡還沒有的」key。2026-10-09 發現：
.env 開了 CUSTOMER_CLUSTER_MODE=1 之後，test_web_demo_routes.py 一整批測試回 403——
測試帳號沒連過客戶叢集，被引導流程擋下。CI 沒有 .env 所以不會發生，換台電腦結果就不同。
這裡在任何專案模組被匯入前先固定成本機模式；需要客戶模式的測試自己用 monkeypatch 打開
（見 test_onboarding.py、test_remote_backend.py）。
"""
import os

os.environ["CUSTOMER_CLUSTER_MODE"] = "0"
# 2026-11-17：網站的 .env 開了多代理人部署解析，測試不能跟著去呼叫真的本地模型（8766）；
# 預設關閉，要測新流程的測試自己用 monkeypatch.setenv 打開（tests/test_pipeline_web.py）
os.environ["DEPLOY_PIPELINE"] = ""
# 2026-11-23：網站的 .env 開了反問改善（PIPELINE_CLARITY=on、PIPELINE_TRUST_SUGGEST=1），測試的假模型不認得
# 它多出來的 prompt（tests/test_pipeline.py 3 項失敗）。預設關閉，要測的測試自己 monkeypatch（tests/test_clarity.py）
os.environ["PIPELINE_CLARITY"] = "off"
os.environ["PIPELINE_TRUST_SUGGEST"] = "0"
os.environ["PIPELINE_CLARITY_COMBINE"] = ""
os.environ["PIPELINE_CLARITY_CHECK"] = ""
os.environ["PIPELINE_SIZE_TIER"] = ""
os.environ["PIPELINE_REASON_SIZING"] = ""
# 2026-11-24：網站的 .env 開了目標判斷（PIPELINE_TARGET_CHECK=on），會多出面向問題卡；要測的測試自己 monkeypatch（tests/test_target_check.py）
os.environ["PIPELINE_TARGET_CHECK"] = "off"
os.environ["PIPELINE_CHOOSE_STYLE"] = ""

# 事件紀錄（core/event_store.py）寫到暫存檔，不要混進開發者 data/events.db 的真實紀錄
import tempfile
os.environ["ZT_EVENTS_DB"] = os.path.join(tempfile.mkdtemp(prefix="zt-events-"), "events.db")
# 分時擴縮與休眠設定（core/autoscale_store.py）同理
os.environ["ZT_AUTOSCALE_DB"] = os.path.join(tempfile.mkdtemp(prefix="zt-autoscale-"), "autoscale.db")
# 對話與專案（core/workspace_store.py）同理
os.environ["ZT_WORKSPACE_DB"] = os.path.join(tempfile.mkdtemp(prefix="zt-workspace-"), "workspace.db")
# 推播訂閱與 VAPID 金鑰（core/push.py）同理，測試不能動到正式的訂閱名單與金鑰
_push_tmp = tempfile.mkdtemp(prefix="zt-push-")
os.environ["ZT_PUSH_DB"] = os.path.join(_push_tmp, "push.db")
os.environ["ZT_VAPID_KEY"] = os.path.join(_push_tmp, "vapid_private.pem")
os.environ["ZT_SCHEDULE_DB"] = os.path.join(_push_tmp, "schedule.db")
# 部署紀錄 repo（core/config.GITOPS_REPO_PATH，2026-11-11）同理：測試不能在開發者的 data/gitops 建 repo 或寫 commit
os.environ["GITOPS_REPO_PATH"] = os.path.join(tempfile.mkdtemp(prefix="zt-gitops-"), "gitops")
