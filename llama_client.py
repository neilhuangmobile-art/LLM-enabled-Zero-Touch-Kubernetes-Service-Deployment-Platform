"""
llama_client.py
Llama 3.1 推理模組 + 高品質標註資料收集

使用優先順序：
1. 優先連線 core/model_server.py（常駐 HTTP server，不需重載模型）
   - 若 server 尚未啟動，自動在背景啟動它（首次需等待 1~2 分鐘）
   - 之後每次呼叫都是毫秒級，不需重載模型
2. Server 啟動失敗時，fallback 到本地直接載入
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import ensure_utf8_output
ensure_utf8_output()

import torch
import json
import re
import time
import subprocess
from typing import Optional
from datetime import datetime, timezone

# 從 config 引用，不再硬編碼路徑
from core.config import (
    BASE_MODEL, ADAPTER_PATH, DATASET_PATH,
    SYSTEM_PROMPT, MODEL_SERVER_URL, HF_TOKEN,
)

_model, _tokenizer = None, None
_server_launch_attempted = False  # 避免重複嘗試啟動


# ══════════════════════════════════════════════════════════════════
# RAG 知識增強（選用，索引不存在時自動跳過）
# ══════════════════════════════════════════════════════════════════
_DEPLOY_INTENT_RE = re.compile(
    r'(deploy|spin up|部署|建立)\D{0,10}\d+\D{0,10}(pod|個)', re.IGNORECASE
)


def _try_augment_with_rag(prompt_text: str) -> str:
    """
    嘗試以 K8s 知識庫增強 prompt，防止 LLM 幻覺。
    若索引未建立或 rag 模組不可用，靜默回傳原始 prompt。
    部署類指令（例如「deploy 3 pods」）不套用 RAG：範例文件裡的部署句型
    會誤導 LoRA 模型接龍生成一堆不相關的假部署指令，而部署本身也不需要參考知識。
    """
    if _DEPLOY_INTENT_RE.search(prompt_text):
        return prompt_text
    try:
        from rag.retriever import augment_prompt
        augmented = augment_prompt(prompt_text, top_k=2, max_context_chars=500)
        if augmented != prompt_text:
            print("[RAG] 知識增強已注入")
        return augmented
    except Exception:
        return prompt_text  # 靜默降級，不影響主流程


def _try_augment_with_rag_ex(prompt_text: str, history: list = None) -> tuple:
    """
    與 _try_augment_with_rag 邏輯相同，但同時回傳引用來源（source/score/text），
    供 chat_llama 回傳給前端顯示「查看引用來源」。索引未建立或查無結果時回傳空list。
    """
    if _DEPLOY_INTENT_RE.search(prompt_text):
        return prompt_text, []
    try:
        from rag.retriever import augment_prompt_ex, CHAT_MIN_SCORE
        augmented, docs = augment_prompt_ex(prompt_text, history=history, top_k=2, max_context_chars=500,
                                             min_score=CHAT_MIN_SCORE)
        if augmented != prompt_text:
            print("[RAG] 知識增強已注入")
        return augmented, docs
    except Exception:
        return prompt_text, []


# ══════════════════════════════════════════════════════════════════
# 自動啟動 Model Server（核心改善：不再需要手動啟動）
# ══════════════════════════════════════════════════════════════════
def _is_server_alive() -> bool:
    """檢查 Model Server 是否已在運行。"""
    try:
        import urllib.request
        urllib.request.urlopen(f"{MODEL_SERVER_URL}/health", timeout=2)
        return True
    except Exception:
        return False


def _auto_start_server() -> bool:
    """Check the Model Server, and only auto-start it when explicitly enabled.

    AUTO_START_MODEL_SERVER is off by default so Ctrl+C on the web terminal does not
    leave a hidden model process running in the background.
    """
    global _server_launch_attempted

    if _is_server_alive():
        return True

    if os.environ.get("AUTO_START_MODEL_SERVER") != "1":
        return False

    if _server_launch_attempted:
        return False

    _server_launch_attempted = True

    server_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core", "model_server.py")
    if not os.path.exists(server_py):
        print("[Model Server] 找不到 core/model_server.py，改用本地載入")
        return False

    print("=" * 55)
    print("[Model Server] 未啟動，正在自動後台啟動...")
    print("   若你想手動 Ctrl+C 管理，請另外執行 python3 core/model_server.py")
    print("=" * 55)

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_server.log")
    with open(log_path, "w", encoding="utf-8") as log_f:
        subprocess.Popen(
            [sys.executable, server_py],
            stdout=log_f,
            stderr=log_f,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

    print("   等待模型載入", end="", flush=True)
    for i in range(180):
        time.sleep(1)
        if _is_server_alive():
            print(f"\n[Model Server] 就緒！（{i + 1} 秒）")
            return True
        if i % 15 == 14:
            print(".", end="", flush=True)

    print("\n[Model Server] 啟動超時，改用本地載入（較慢）")
    return False


# ══════════════════════════════════════════════════════════════════
# HTTP Client（優先使用 Model Server）
# ══════════════════════════════════════════════════════════════════
def _try_server(prompt_text: str) -> Optional[dict]:
    """嘗試呼叫常駐 Model Server。未啟動時回傳 None（觸發 fallback）。"""
    try:
        import urllib.request
        body = json.dumps({"prompt": prompt_text}).encode()
        req  = urllib.request.Request(
            f"{MODEL_SERVER_URL}/infer",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            return data.get("result")
    except Exception:
        return None  # server 未啟動，靜默 fallback


# ══════════════════════════════════════════════════════════════════
# 本地直接載入（Fallback）
# ══════════════════════════════════════════════════════════════════
def _load_model_once():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer

    from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
    from peft import PeftModel

    print("[Local] 正在清理顯存並載入 Llama 3.1 GPU 專家模型 (4-bit 量化)...")
    print("[Local] 提示：執行 'python core/model_server.py' 可避免每次重載模型")
    torch.cuda.empty_cache()

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )

    _tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=HF_TOKEN)
    _tokenizer.pad_token = _tokenizer.eos_token

    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        token=HF_TOKEN,
        quantization_config=bnb_config,
        device_map={"": 0},
    )
    base.config.use_cache = True

    if os.path.exists(ADAPTER_PATH):
        print(f"[Local] 偵測到微調權重，正在合併：{ADAPTER_PATH}")
        _model = PeftModel.from_pretrained(base, ADAPTER_PATH)
    else:
        print("[Local] 未找到 LoRA 權重，使用原始基礎模型")
        _model = base

    _model.eval()
    return _model, _tokenizer


# ══════════════════════════════════════════════════════════════════
# 解析輔助
# ══════════════════════════════════════════════════════════════════
def _extract_first_json(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _fix_nulls(text: str) -> str:
    return re.sub(r'(?<=:)\s*null\b', ' "NULL"', text)


def _normalize_app_name(name: str) -> str:
    name = re.sub(r'[^a-zA-Z0-9-]+', '-', (name or '').strip().lower()).strip('-')
    name = re.sub(r'-+', '-', name)
    if not name:
        return 'auto-app'
    if not re.match(r'^[a-z0-9]', name):
        name = 'app-' + name
    return name[:50].rstrip('-') or 'auto-app'


def _deterministic_deploy_parse(prompt_text: str) -> Optional[dict]:
    """Fast parser for common deployment requests before asking the LLM.

    This protects the most important production fields: replica count, image,
    app name, and port. It only returns a result when the request clearly asks to
    deploy/run/start a Kubernetes workload.
    """
    raw = (prompt_text or '').strip()
    low = raw.lower()
    deploy_words = (
        'deploy', 'deployment', 'run', 'start', 'launch', 'spin up',
        '部署', '佈署', '部屬', '啟動', '建立', '起 ', '幫我起', '幫我部署', '幫我部屬'
    )
    if not any(w in low for w in deploy_words):
        return None

    count_patterns = [
        r'(?:deploy|run|start|launch|spin\s+up)\s+(\d+)\s*(?:pods?|replicas?|instances?)?',
        r'(\d+)\s*(?:pods?|replicas?|instances?|個|副本|台)',
        r'(?:pods?|replicas?|instances?|副本)\s*(?:=|:|為|是|to)?\s*(\d+)',
    ]
    pods = None
    for pat in count_patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            pods = int(m.group(1))
            break
    if pods is None:
        pods = 1
    if not (1 <= pods <= 100):
        return None

    image = None
    image_patterns = [
        r'(?:image|映像|鏡像)\s*(?:=|:|為|是)?\s*([\w./:-]+)',
        r'\b([a-z0-9][\w.-]*(?:/[\w.-]+)*(?::[\w][\w.-]*)?)\b(?=\s+(?:pods?|pod|container|容器|for|給|，|,)|[.。!！?？]*$)',
    ]
    for pat in image_patterns:
        for m in re.finditer(pat, raw, re.IGNORECASE):
            cand = m.group(1).strip('.,，。')
            if cand.lower() not in {'deploy', 'run', 'start', 'launch', 'pod', 'pods'} and (':' in cand or '/' in cand or cand in {'nginx', 'redis', 'postgres', 'mysql', 'node', 'python', 'golang'}):
                image = cand
                break
        if image:
            break
    if image and ':' not in image and '/' not in image:
        image = f'{image}:latest'
    if not image:
        image = 'nginx:latest'

    app_name = None
    app_patterns = [
        r'(?:for|named|name|app|service|給|叫做|名稱|服務)\s+([a-zA-Z0-9][\w-]{1,50})',
        r'([a-zA-Z0-9][\w-]{1,50})\s*(?:service|app|deployment|服務)',
    ]
    for pat in app_patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            cand = m.group(1)
            if cand.lower() not in {'port', 'pods', 'pod', 'replicas', 'image'}:
                app_name = cand
                break
    if not app_name:
        if image and image != 'nginx:latest':
            app_name = image.split('/')[-1].split(':')[0]
        else:
            app_name = 'auto-app'

    port = 80
    pm = re.search(r'(?:port|端口|連接埠)\s*(?:=|:|為|是)?\s*(\d{1,5})', raw, re.IGNORECASE)
    if pm:
        port = int(pm.group(1))
        if not (1 <= port <= 65535):
            port = 80

    memory = None
    mm = re.search(r'(\d+\s*(?:Mi|Gi|Ki|M|G))\b', raw, re.IGNORECASE)
    if mm:
        memory = mm.group(1).replace(' ', '')

    cpu = None
    cm = re.search(r'(\d+(?:\.\d+)?\s*m?)\s*(?:CPU\s*cores?|CPU|cores?|核心|顆)\b', raw, re.IGNORECASE)
    if cm:
        cpu = cm.group(1).replace(' ', '')

    result = {'pods': pods, 'image': image, 'app_name': _normalize_app_name(app_name), 'port': port}
    if memory:
        result['memory'] = memory
    if cpu:
        result['cpu'] = cpu
    result['_parser'] = 'deterministic'
    return result


def _parse(text: str) -> Optional[dict]:
    snippet = _extract_first_json(text)
    if snippet:
        fixed = _fix_nulls(snippet)
        try:
            result = json.loads(fixed)
            if isinstance(result, dict):
                return {k: v for k, v in result.items() if v != "NULL"}
        except Exception:
            pass

    try:
        from json_repair import repair_json
        target = snippet or text
        result = json.loads(repair_json(_fix_nulls(target)))
        if isinstance(result, dict):
            print("[DEBUG] json_repair 修復成功")
            return {k: v for k, v in result.items() if v != "NULL"}
    except Exception:
        pass

    src = snippet or text
    pods_m  = re.search(r'"(?:pods|replicas)"\s*:\s*(\d+)', src)
    image_m = re.search(r'"image"\s*:\s*"([^"]+)"',         src)
    app_m   = re.search(r'"app_name"\s*:\s*"([^"]+)"',      src)
    port_m  = re.search(r'"port"\s*:\s*(\d+)',              src)
    mem_m   = re.search(r'"memory"\s*:\s*"([^"]+)"',        src)
    cpu_m   = re.search(r'"cpu"\s*:\s*"?(\d+(?:\.\d+)?m?)"?', src)
    node_m  = re.search(r'"node_count"\s*:\s*(\d+)',        src)

    if pods_m:
        print("[DEBUG] 使用 Regex 萃取")
        result = {
            "pods"    : int(pods_m.group(1)),
            "image"   : image_m.group(1) if image_m else "nginx:latest",
            "app_name": app_m.group(1)   if app_m   else "auto-app",
        }
        if port_m:
            result["port"] = int(port_m.group(1))
        if mem_m and mem_m.group(1) != "NULL":
            result["memory"] = mem_m.group(1)
        if cpu_m and cpu_m.group(1) != "NULL":
            result["cpu"] = cpu_m.group(1)
        if node_m:
            result["node_count"] = int(node_m.group(1))
        return result

    return None


def _validate(result: dict) -> bool:
    if not result or not isinstance(result, dict):
        return False
    if "error" in result:
        return False
    try:
        return 1 <= int(result.get("pods", 0)) <= 100
    except (ValueError, TypeError):
        return False


def _local_infer(prompt_text: str) -> dict:
    """本地直接推論（fallback 路徑）。"""
    model, tokenizer = _load_model_once()

    full_prompt = (
        f"### System\n{SYSTEM_PROMPT}\n\n"
        f"### User\n{prompt_text}\n"
        "### Assistant\n{"
    )

    inputs    = tokenizer(full_prompt, return_tensors="pt").to("cuda")
    input_len = inputs["input_ids"].shape[1]

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=200,
            temperature=0.3,
            do_sample=True,
            top_p=0.9,
            repetition_penalty=1.2,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.eos_token_id,
        )

    new_tokens = outputs[0][input_len:]
    generated  = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    if not generated.startswith("{"):
        generated = "{" + generated

    print(f"[DEBUG] 模型原始輸出：{generated}")

    snippet = _extract_first_json(generated)
    if not snippet:
        raise ValueError(f"找不到完整 JSON，原始輸出：{generated}")

    snippet = re.sub(r':\s*null\b', ': "NULL"', snippet)
    result  = _parse(snippet)

    if result and _validate(result):
        result.setdefault("image",    "nginx:latest")
        result.setdefault("app_name", "auto-app")
        result["pods"] = int(result["pods"])
        if "port" in result:
            try:
                p = int(result["port"])
                if 1 <= p <= 65535:
                    result["port"] = p
                else:
                    del result["port"]
            except (ValueError, TypeError):
                del result["port"]
        if "memory" in result:
            if not re.match(r"^\d+(Mi|Gi|Ki|M|G)$", str(result["memory"])):
                del result["memory"]
        if "cpu" in result:
            if not re.match(r"^\d+(\.\d+)?m?$", str(result["cpu"])):
                del result["cpu"]
        if "node_count" in result:
            try:
                node_count = int(result["node_count"])
                if node_count < 1:
                    del result["node_count"]
                else:
                    result["node_count"] = node_count
            except (ValueError, TypeError):
                del result["node_count"]
        return result

    raise ValueError(f"驗證失敗，解析結果：{result}")


_stale_normalize_env_warned = False


def _warn_if_stale_normalize_env():
    """翻譯層開關從 USE_CLAUDE_NORMALIZE 改名為 USE_LLM_NORMALIZE（底層換成 Gemini）。
    如果 .env／環境變數裡還留著舊名稱，舊設定會安靜地失效，這裡印一次警告避免誤判。
    """
    global _stale_normalize_env_warned
    if _stale_normalize_env_warned:
        return
    if os.environ.get("USE_CLAUDE_NORMALIZE") == "1" and os.environ.get("USE_LLM_NORMALIZE") != "1":
        print("[翻譯層] 偵測到舊的 USE_CLAUDE_NORMALIZE 設定，這個變數名稱已改為 "
              "USE_LLM_NORMALIZE，舊設定不會生效。")
    _stale_normalize_env_warned = True


# 聊天訊息如果乾淨、簡短，本地模型自己就看得懂，不需要多打一次翻譯層 API（省額度、省時間）。
# 只有「明顯複雜/偏長」或「看起來像格式雜訊/注入」的訊息才值得送去正規化。
_CHAT_NORMALIZE_LENGTH_THRESHOLD = 60  # 字元數，超過視為「較長/操作性問題」
_CHAT_SUSPICIOUS_PATTERNS = (
    "###", "System:", "system:", "Human:", "human:", "Assistant:", "assistant:",
    "＃＃＃", "```",
)


def _chat_needs_normalize(message: str) -> bool:
    """判斷這則聊天訊息值不值得送去翻譯層。乾淨、簡短、沒有可疑格式的訊息直接跳過。"""
    if len(message) > _CHAT_NORMALIZE_LENGTH_THRESHOLD:
        return True
    if any(p in message for p in _CHAT_SUSPICIOUS_PATTERNS):
        return True
    return False


# ══════════════════════════════════════════════════════════════════
# 公開 API
# ══════════════════════════════════════════════════════════════════
def ask_llama(prompt_text: str) -> dict:
    """
    呼叫 LLM 推論（優先順序）：
    1. Claude API — 毫秒級，無需 GPU（需設定 ANTHROPIC_API_KEY）
    2. RAG 增強 + Model Server HTTP（常駐本地 LLaMA）
    3. Fallback：本地直接載入
    """
    try:
        deterministic = _deterministic_deploy_parse(prompt_text)
        if deterministic and _validate(deterministic):
            print("[Parser] deterministic deployment parse")
            return deterministic

        _warn_if_stale_normalize_env()

        # 翻譯層（opt-in）：deterministic parser 解不出來時，先用 Gemini 把模糊/口語化的
        # 輸入正規化成清楚句子，再交回本地 parser／模型繼續處理。Gemini 只負責「聽懂」，
        # 缺漏欄位一律留白不猜，決定權仍在本地 parser／模型手上。
        if os.environ.get("USE_LLM_NORMALIZE") == "1":
            try:
                from core.gemini_client import gemini_normalize_deploy_request, is_available as gemini_available
                if gemini_available():
                    normalized = gemini_normalize_deploy_request(prompt_text)
                    if normalized:
                        print(f"[Gemini 翻譯層] {prompt_text!r} → {normalized!r}")
                        retry = _deterministic_deploy_parse(normalized)
                        if retry and _validate(retry):
                            retry["_parser"] = "deterministic-after-gemini-normalize"
                            return retry
                        prompt_text = normalized
            except Exception as e:
                print(f"[Gemini 翻譯層] 正規化失敗，改用原始輸入：{e}")

        # Claude API is opt-in. Default path uses the local Model Server.
        if os.environ.get("USE_CLAUDE_API") == "1":
            try:
                from core.claude_client import claude_parse_k8s, is_available as claude_available
                if claude_available():
                    result = claude_parse_k8s(prompt_text)
                    if result is not None:
                        print("[Claude API] 解析成功")
                        return result
            except Exception:
                pass

        # RAG 知識增強（索引不存在時自動跳過）
        enhanced = _try_augment_with_rag(prompt_text)

        # 2. 自動啟動 Model Server（已在跑則直接跳過）
        _auto_start_server()

        # 嘗試 HTTP server
        server_result = _try_server(enhanced)
        if server_result is not None:
            return server_result

        # 3. Fallback：本地直接載入
        return _local_infer(enhanced)

    except Exception as e:
        return {"error": "解析失敗", "raw": str(e)}


def chat_llama(message: str, history: list = None) -> tuple:
    """Use the local Model Server for natural-language chat, without Claude API.

    Returns (reply, sources): sources is the list of RAG documents (source/score/text)
    used to augment the prompt, or [] when RAG wasn't used / found nothing relevant.
    """
    history = history or []
    try:
        _warn_if_stale_normalize_env()

        if os.environ.get("USE_LLM_NORMALIZE") == "1" and _chat_needs_normalize(message):
            try:
                from core.gemini_client import gemini_normalize_chat_message, is_available as gemini_available
                if gemini_available():
                    normalized = gemini_normalize_chat_message(message, history)
                    if normalized:
                        print(f"[Gemini 翻譯層] chat {message!r} → {normalized!r}")
                        message = normalized
            except Exception as e:
                print(f"[Gemini 翻譯層] chat 正規化失敗，改用原始輸入：{e}")

        if not _auto_start_server():
            return "[Local Model unavailable] model server is not running", []
        import urllib.request
        enhanced_message, sources = _try_augment_with_rag_ex(message, history)
        body = json.dumps({"message": enhanced_message, "history": history}).encode()
        req = urllib.request.Request(
            f"{MODEL_SERVER_URL}/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            reply = data.get("reply")
            if reply:
                return reply, sources
    except Exception as e:
        return f"[Local Model unavailable] {e}", []
    return "[Local Model unavailable] empty response", []


def save_gold_sample(user_input: str, corrected_json: dict):
    if "error" in corrected_json:
        print("[Dataset] 有 error，跳過存檔")
        return

    try:
        pods = int(corrected_json.get("pods", 0))
        assert 1 <= pods <= 100
    except Exception:
        print("[Dataset] pods 異常，跳過存檔")
        return

    user_input = user_input.strip()
    if user_input.isdigit():
        user_input = f"deploy {user_input} pods"

    sample = {
        "input" : user_input,
        "output": {
            "pods"    : pods,
            "image"   : corrected_json.get("image",    "nginx:latest"),
            "app_name": corrected_json.get("app_name", "auto-app"),
            **( {"port":   corrected_json["port"]}   if "port"   in corrected_json else {} ),
            **( {"memory": corrected_json["memory"]} if "memory" in corrected_json else {} ),
            **( {"cpu":    corrected_json["cpu"]}    if "cpu"    in corrected_json else {} ),
            **( {"node_count": corrected_json["node_count"]} if "node_count" in corrected_json else {} ),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    with open(DATASET_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    print(f"[Dataset] 已存入：{sample['input']} → {sample['output']}")
