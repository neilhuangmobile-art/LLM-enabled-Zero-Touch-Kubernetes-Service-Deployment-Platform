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

# 事件紀錄（core/event_store.py）寫到暫存檔，不要混進開發者 data/events.db 的真實紀錄
import tempfile
os.environ["ZT_EVENTS_DB"] = os.path.join(tempfile.mkdtemp(prefix="zt-events-"), "events.db")
