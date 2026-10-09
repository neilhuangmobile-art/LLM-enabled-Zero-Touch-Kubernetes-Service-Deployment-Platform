"""
tests/test_chat_normalize_privacy.py
測 llama_client.chat_llama() 送給 Gemini 翻譯層的內容。

2026-10-09 發現：web_demo 的接地問答會把「[現況] 叢集狀態摘要 + [問題] 使用者問題」
整段當成 message 傳進 chat_llama()，翻譯層再把整段送給 Gemini 改寫——叢集狀態
（服務名稱、映像檔、Pod 事件）因此離開平台，而且可能被改寫成跟事實不符的內容。
修正後只送 [問題] 那段，現況摘要原封不動留在本機。
"""
import pytest

llama_client = pytest.importorskip("llama_client")
gemini_client = pytest.importorskip("core.gemini_client")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_normalize(message, history):
        calls.append(message)
        return "NORMALIZED"

    monkeypatch.setenv("USE_LLM_NORMALIZE", "1")
    monkeypatch.setattr(gemini_client, "gemini_normalize_chat_message", fake_normalize)
    monkeypatch.setattr(gemini_client, "is_available", lambda: True)
    # 停在 model server 這一步：翻譯層已經跑完，不需要真的推論
    monkeypatch.setattr(llama_client, "_auto_start_server", lambda: False)
    return calls


def test_cluster_snapshot_is_not_sent_to_gemini(sent):
    grounded = "[現況]\nDeployments（your namespace）：\n  - web: 1/1 ready, image=nginx:1.27 [OK]\n\n[問題]\nweb 正常嗎"
    llama_client.chat_llama(grounded, [])
    assert sent == ["web 正常嗎"]


def test_plain_question_is_sent_unchanged(sent):
    llama_client.chat_llama("什麼是 Deployment", [])
    assert sent == ["什麼是 Deployment"]


def test_snapshot_is_kept_for_local_model(sent, monkeypatch):
    """整理後的問題要接回原本的現況摘要，本地模型才能接地回答。"""
    seen = []
    monkeypatch.setattr(llama_client, "_auto_start_server", lambda: True)

    def capture(message, history):
        seen.append(message)
        raise RuntimeError("stop here")

    monkeypatch.setattr(llama_client, "_try_augment_with_rag_ex", capture)
    grounded = "[現況]\n所有 Pod 目前健康。\n\n[問題]\nweb 正常嗎"
    llama_client.chat_llama(grounded, [])
    assert seen == ["[現況]\n所有 Pod 目前健康。\n\n[問題]\nNORMALIZED"]
