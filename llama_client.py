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
    BASE_MODEL, DEPLOY_ADAPTER_PATH, DATASET_PATH,
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


def _deploy_fewshot(prompt_text: str, top_k: int = 3) -> list:
    """
    從部署範例索引（dataset/finetune_samples.jsonl 建的 rag/deploy_index.json）撈幾筆
    最相似的「輸入 → JSON」範例，當 few-shot 引導小模型的輸出格式。
    索引不存在或 rag 模組不可用時回傳空 list（模型仍可靠 system prompt 生成）。
    """
    try:
        from rag.retriever import retrieve_deploy_examples
        examples = retrieve_deploy_examples(prompt_text, top_k=top_k)
        if examples:
            print(f"[RAG] 注入 {len(examples)} 筆部署範例 few-shot")
        return examples
    except Exception:
        return []


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
def _try_server(prompt_text: str, examples: list = None) -> Optional[dict]:
    """嘗試呼叫常駐 Model Server。未啟動時回傳 None（觸發 fallback）。"""
    try:
        import urllib.request
        body = json.dumps({"prompt": prompt_text, "examples": examples or []}).encode()
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

    print(f"[Local] 正在載入部署模型（4-bit 量化）：{BASE_MODEL}")
    print("[Local] 提示：執行 'python core/model_server.py' 可避免每次重載模型")
    torch.cuda.empty_cache()

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )

    _tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=HF_TOKEN)
    if _tokenizer.pad_token is None:
        _tokenizer.pad_token = _tokenizer.eos_token

    _model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        token=HF_TOKEN,
        quantization_config=bnb_config,
        device_map={"": 0},
    )
    _model.config.use_cache = True

    if DEPLOY_ADAPTER_PATH and os.path.isdir(DEPLOY_ADAPTER_PATH):
        from peft import PeftModel
        print(f"[Local] 掛載部署 LoRA：{DEPLOY_ADAPTER_PATH}")
        _model = PeftModel.from_pretrained(_model, DEPLOY_ADAPTER_PATH)

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


# 2026-10-06 QA 掃描實測發現：count_patterns 只認 ASCII `\d+`，完全看不懂中文數字詞
# （一/二/兩/三…），「兩個副本」「三個副本」一個都抓不到，靜默退回預設值 1。只處理
# 十以內的中文數字（大於十的交給模型，口語很少直接講「十五個」這種大數的中文數字）。
_CN_DIGIT_MAP = {
    '一': 1, '二': 2, '兩': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}
_CN_COUNT_RE = re.compile(
    r'([一二兩两三四五六七八九十])\s*(?:個|个|副本|台|pods?|replicas?|instances?)',
)
# 已知服務關鍵字 + 複合服務詞——偵測「幫我部署兩個服務，一個nginx一個redis」這種
# 一次要求多個服務的請求，不要靜默只做其中一個（image 比對只會取第一個命中的關鍵字，
# 其餘服務跟它們各自的副本數會在沒有任何提示的情況下整個消失）。
_KNOWN_SERVICE_KEYWORDS = ('nginx', 'redis', 'postgres', 'mysql', 'node', 'python', 'golang')
# 2026-10-07 整理量化報告時發現的真實 bug：「起一個 nginx 跟一個 redis」沒有
# 「兩個/多個/幾個」這類明確的複數計數詞，完全沒命中，`_looks_like_multi_service_request()`
# 判定失敗，兩個不同服務被靜默合併成「image=nginx, pods=2」這種錯誤結果——使用者
# 要的是各一個 nginx 跟 redis，不是兩個 nginx。補上「<數量>個...跟/和/與...<數量>個」
# 這種用連接詞分隔兩個計數片語的結構，不要求一定要用「兩個」這種固定詞彙。
_MULTI_SERVICE_HINT_RE = re.compile(
    r'兩個|两个|多個|多个|幾個|几个|\d+\s*個\s*服務|\d+\s*個\s*服务|services?\b|'
    r'(?:一個|1\s*個|\d+\s*個).{0,15}(?:跟|和|與|与).{0,15}(?:一個|1\s*個|\d+\s*個)'
)


def _extract_pods_count(raw: str) -> Optional[int]:
    """依序嘗試 ASCII 數字（含負號）、中文數字詞，抓不到回傳 None（交給呼叫端決定
    要退回預設值 1，還是視為解析失敗）。"""
    count_patterns = [
        r'(?:deploy|run|start|launch|spin\s+up)\s+(-?\d+)\s*(?:pods?|replicas?|instances?)?',
        r'(-?\d+)\s*(?:pods?|replicas?|instances?|個|个|副本|台)',
        r'(?:pods?|replicas?|instances?|副本|數量|数量)\s*(?:=|:|為|是|to)?\s*(-?\d+)',
    ]
    for pat in count_patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return int(m.group(1))
    m = _CN_COUNT_RE.search(raw)
    if m:
        return _CN_DIGIT_MAP[m.group(1)]
    return None


def _looks_like_multi_service_request(raw: str, low: str) -> bool:
    distinct_keywords = {kw for kw in _KNOWN_SERVICE_KEYWORDS if kw in low}
    return len(distinct_keywords) >= 2 and bool(_MULTI_SERVICE_HINT_RE.search(raw))


def _deterministic_deploy_parse(prompt_text: str) -> Optional[dict]:
    """Fast parser for common deployment requests before asking the LLM.

    This protects the most important production fields: replica count, image,
    app name, and port. It only returns a result when the request clearly asks to
    deploy/run/start a Kubernetes workload.

    回傳值三種情況：None（不是部署請求，交給模型）、帶 `_parse_error` 的 dict
    （明確解析出問題所在，呼叫端應直接組清楚的錯誤訊息，不要再落入模型 pipeline）、
    或正常的部署 spec dict。
    """
    raw = (prompt_text or '').strip()
    low = raw.lower()
    deploy_words = (
        'deploy', 'deployment', 'run', 'start', 'launch', 'spin up',
        '部署', '佈署', '部屬', '啟動', '建立', '起 ', '幫我起', '幫我部署', '幫我部屬'
    )
    if not any(w in low for w in deploy_words):
        return None

    if _looks_like_multi_service_request(raw, low):
        return {'_parse_error': 'multi_service_unsupported', '_parser': 'deterministic'}

    explicit_pods = _extract_pods_count(raw)
    if explicit_pods is not None and not (1 <= explicit_pods <= 100):
        return {'_parse_error': 'replica_out_of_range', '_requested_pods': explicit_pods,
                '_parser': 'deterministic'}
    pods = explicit_pods if explicit_pods is not None else 1

    image = None
    image_patterns = [
        r'(?:image|映像|鏡像)\s*(?:=|:|為|是)?\s*([\w./:-]+)',
        # 2026-10-09 兩人同時使用 QA 抓到：Gemini 額度用完/過載（429/503）退回這個解析器時，
        # 中文輸入「部署 2 個 nginx:1.27，名稱 x」會丟掉版本號、「部署 2 個 redis:7.2 名稱 cache」
        # 甚至變成 nginx:latest——下面那條 lookahead 要求映像檔後面先有空白再接「，」，而中文
        # 輸入通常沒有空白，「名稱」也不在清單裡。補兩條更可靠的規則：
        # ① 帶 tag 或 registry 路徑的 token（開頭必須是英文字母，避開「10:30」這類時間）
        r'(?<![A-Za-z0-9_./-])([a-z][a-z0-9._-]*(?:/[a-z0-9._-]+)*:[a-z0-9][\w.-]*)(?![A-Za-z0-9_./:-])',
        # ② 常見映像檔名稱，前後只要不是英數字就算（中文字緊接在後也可以，例如「nginx服務」）
        r'(?<![A-Za-z0-9_./-])(nginx|redis|postgres|mysql|mongo|node|python|golang|httpd|busybox)(?![A-Za-z0-9_/.:-])',
        # 2026-10-07 量化評估腳本實測發現的真實 bug：「...node:20-alpine 作為
        # api-gateway」這種「image 後面接『作為/當作』再接 app 名稱」的句型，
        # image token 後面不是 pod/container/for/給/，這幾個詞，lookahead 完全
        # 沒命中，image 就被靜默退回預設值 nginx:latest（使用者明明打了 node
        # 卻部署出 nginx）。補上這幾個中文連接詞。2026-10-07 整理量化報告時又發現
        # 一個同一類的真實 bug：「部署九個 redis 副本」的「redis」後面接的是
        # 「副本」（計量單位，不在原本的 lookahead 清單裡），一樣靜默退回
        # nginx:latest（使用者明明打了 redis 卻部署出 nginx），補上「副本/台」。
        r'\b([a-z0-9][\w.-]*(?:/[\w.-]+)*(?::[\w][\w.-]*)?)\b(?=\s+(?:pods?|pod|container|容器|for|給|作為|當作|当作|当成|副本|台|，|,)|[.。!！?？]*$)',
    ]
    for pat in image_patterns:
        for m in re.finditer(pat, raw, re.IGNORECASE):
            cand = m.group(1).strip('.,，。')
            if cand.split(':')[0].lower() in {'port', 'image', 'replicas', 'pods', 'memory', 'cpu', 'name', 'app'}:
                continue  # 「port:80」這類是設定，不是映像檔
            if cand.lower() not in {'deploy', 'run', 'start', 'launch', 'pod', 'pods'} and (':' in cand or '/' in cand or cand.lower() in {'nginx', 'redis', 'postgres', 'mysql', 'mongo', 'node', 'python', 'golang', 'httpd', 'busybox'}):
                image = cand
                break
        if image:
            break
    if image and ':' not in image and '/' not in image:
        image = f'{image}:latest'
    if not image:
        image = 'nginx:latest'

    app_name = None
    # 候選名稱只抓 ASCII 字母/數字/連字號——Python 的 `\w` 預設是 Unicode-aware，
    # 會把中文字也當成「單字字元」比對進去。2026-10-03 實測發現的真實 bug：
    # 「...20GI記憶體的服務」這種沒給名稱、純中文敘述資源大小的句子，因為
    # 「服務」前面緊接著一串中文字也滿足 `[\w-]`，整段「20gi記憶體的」被當成
    # app_name 候選——正規化後就變成使用者從沒打過的「20gi」，被命名規則擋下。
    # 2026-10-03 又實測到第二組同類 bug：
    # ①「deploy app named 123abc」——pattern 1 原本把 "app"/"service" 也當成
    #   前綴觸發字，但這兩個字在「deploy an app named X」這種句型裡只是泛稱
    #   名詞、不是真的在引出名稱；"app" 後面緊接的下一個詞剛好是 "named"（也
    #   滿足候選字元集合），於是 "named" 自己被當成候選名稱。真正明確「這裡
    #   開始是名稱」的連接詞只有 for/named/name/叫做/名稱/給，拿掉 "app"/
    #   "service" 這兩個泛稱名詞，交給下面 pattern 2（後綴式）去處理
    #   「<name> app/service」這種名稱在前的寫法，兩種語序不衝突。
    # ②「我要部屬2個nginx服務」——pattern 2 抓到緊接在「服務」前面的
    #   "nginx"，但這就是剛剛已經抓到的 image 名稱，不是使用者另外取的名字，
    #   新增比對：候選字串等於 image 的基底名稱就跳過，不當作 app_name。
    # 2026-10-03 第三組同類 bug：`ask_llama()` 在 Gemini 翻譯層回傳的
    # "### DeploySpec" 結構化區塊驗證不完整時，會把這個區塊原文（不是使用者
    # 原始輸入）直接餵進這支函式當 fallback。區塊第一行是 markdown 標題
    # "### DeploySpec"，後面接著 "app_name: 123abc" 這種 key:value 格式的行
    # ——因為 `\s` 本身就包含換行字元，pattern 2 會跨行把標題裡的
    # "DeploySpec" 跟下一行 "app_name:" 開頭的 "app" 接在一起比對成功，抓出
    # 候選「DeploySpec」，正規化後變成使用者從沒打過的「deployspec」。這支
    # 函式設計上只處理單行的自然語言輸入，候選字跟關鍵字之間不應該跨行，
    # 這裡把 `\s` 限定成「同一行內的空白」（不含換行），從根本上排除這整類
    # 「結構化區塊被誤當白話文解析」的跨行誤判。
    app_patterns = [
        # 「作為/當作」跟剛才 image 那條同一個根因（2026-10-07 量化評估腳本
        # 實測發現）：「node:20-alpine 作為 api-gateway」這種句型，"作為" 後面
        # 接的才是使用者真正想取的名字，原本不在這份前綴清單裡，"api-gateway"
        # 整段被漏接，app_name 退回用 image 基底名稱（node）頂替。
        r'(?:for|named|name|給|叫做|名稱|作為|當作|当作|当成)[ \t]+([a-zA-Z0-9][A-Za-z0-9-]{1,50})',
        r'([a-zA-Z0-9][A-Za-z0-9-]{1,50})[ \t]*(?:service|app|deployment|服務)',
    ]
    _quantity_like = re.compile(r'^\d+(?:\.\d+)?(?:mi|gi|ki|m|g)?$', re.IGNORECASE)
    _image_base = (image.split('/')[-1].split(':')[0].lower() if image else '')
    for pat in app_patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            cand = m.group(1)
            # 純數字或「數字+資源單位」（20gi、500m、64mi…）一定是誤抓到容量/
            # 副本數之類的敘述；候選字串等於 image 基底名稱，也是誤抓到剛剛
            # 分析出來的 image 本身——兩種都不會是使用者真的想取的名字，直接
            # 跳過讓 app_name 落回下面的預設邏輯（用 image 名稱或 auto-app）。
            if (cand.lower() not in {'port', 'pods', 'pod', 'replicas', 'image'}
                    and not _quantity_like.match(cand)
                    and cand.lower() != _image_base):
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

    # 結尾的 `\b`（word boundary）在「數字+單位」後面緊接著中文字時會失效——
    # Python 的 `\w`／`\b` 預設是 Unicode-aware，中文字跟英數字母一樣算「單字
    # 字元」，兩個 \w 字元中間沒有邊界，`\b` 永遠對不上，導致整個 regex 配對
    # 失敗。2026-10-03 實測發現：「...20GI記憶體的服務」這種單位後面沒有空格、
    # 直接接中文的句子，記憶體/CPU 完全抓不到，使用者講的容量被整段默默丟掉。
    # 改成「後面不能緊接 ASCII 字母」的否定預查，中文字接在後面完全不受影響。
    # K8s 的資源單位字尾是大小寫敏感的（一定要 "Gi"，不是 "GI" 或 "gi"）,但使用者
    # 打字不會管這個——「20GI」「20gi」「20Gi」對使用者來說是同一件事。2026-10-03
    # 實測發現：抓到數值後如果原樣保留使用者打的大小寫，會在後面的格式驗證被擋下
    # （「memory must look like 128Mi or 1Gi」），對一個本來就該自動理解的新手輸入
    # 來說是不必要的卡關，這裡統一正規化成 K8s 要求的大小寫，不留給驗證層去擋。
    memory = None
    mm = re.search(r'(\d+)\s*(Mi|Gi|Ki|M|G)(?![a-zA-Z])', raw, re.IGNORECASE)
    if mm:
        unit = mm.group(2)
        canonical_unit = {'mi': 'Mi', 'gi': 'Gi', 'ki': 'Ki', 'm': 'M', 'g': 'G'}[unit.lower()]
        memory = f'{mm.group(1)}{canonical_unit}'

    cpu = None
    # 負向預查只套用在英文關鍵字後面（避免誤配到 "CPUs" 這種更長單字的中段），
    # 中文關鍵字（核／核心／顆）不需要、也不能套用同一個預查——"30核CPU" 這種
    # 中文單位緊接著英文單位的寫法，"核" 後面就是 "CPU" 的字母 C，如果不分開寫
    # 整段 `(?![a-zA-Z])` 會誤判成「後面接了字母、不是完整詞」而拒絕配對。
    cm = re.search(
        r'(\d+(?:\.\d+)?\s*m?)\s*(?:(?:CPU\s*cores?|CPU|cores?)(?![a-zA-Z])|核心|核|顆)',
        raw, re.IGNORECASE,
    )
    if cm:
        cpu = cm.group(1).replace(' ', '')

    result = {'pods': pods, 'image': image, 'app_name': _normalize_app_name(app_name), 'port': port}
    if memory:
        result['memory'] = memory
    if cpu:
        result['cpu'] = cpu
    result['_parser'] = 'deterministic'
    return result


# 翻譯層輸出的 ### DeploySpec 區塊 → dict。key 對照到下游用的欄位名。
_SPEC_KEY_MAP = {
    "replicas": "pods", "pods": "pods", "replica": "pods", "count": "pods",
    "image": "image", "container": "image",
    "app_name": "app_name", "name": "app_name", "app": "app_name", "service": "app_name",
    "port": "port", "memory": "memory", "mem": "memory", "cpu": "cpu",
}
_SPEC_PLACEHOLDER = {"", "none", "null", "n/a", "-", "<int>", "<name>", "<image:tag>",
                     "<e.g. 256mi>", "<e.g. 500m>"}


def parse_deploy_spec(text: str) -> Optional[dict]:
    """
    解析 Gemini 翻譯層輸出的規範化區塊：

        ### DeploySpec
        replicas: 3
        image: redis:7
        ...

    只認 `key: value` 行，沒出現的欄位就是使用者沒講、留白不補。
    解析不到任何有效欄位時回傳 None。
    """
    if not text:
        return None
    out: dict = {}
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if ":" not in line:
            continue
        raw_k, raw_v = line.split(":", 1)
        k = raw_k.strip().lower()
        v = raw_v.strip().strip('"').strip("'").rstrip(".,，。").strip()
        key = _SPEC_KEY_MAP.get(k)
        if not key or v.lower() in _SPEC_PLACEHOLDER:
            continue
        if key in ("pods", "port"):
            m = re.search(r"\d+", v)
            if not m:
                continue
            out[key] = int(m.group())
        elif key == "app_name":
            out[key] = _normalize_app_name(v)
        else:
            out[key] = v
    if not out:
        return None
    if out.get("image") and ":" not in out["image"] and "/" not in out["image"]:
        out["image"] = f"{out['image']}:latest"
    if "app_name" not in out and out.get("image"):
        out["app_name"] = _normalize_app_name(out["image"].split("/")[-1].split(":")[0])
    out["_parser"] = "deploy-spec"
    return out


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

    input_ids = tokenizer.apply_chat_template(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
        add_generation_prompt=True, return_tensors="pt",
    ).to(model.device)
    input_len = input_ids.shape[1]

    with torch.no_grad():
        outputs = model.generate(
            input_ids=input_ids,
            attention_mask=torch.ones_like(input_ids),
            max_new_tokens=200,
            temperature=0.3,
            do_sample=True,
            top_p=0.9,
            repetition_penalty=1.2,
            eos_token_id=tokenizer.eos_token_id,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
        )

    new_tokens = outputs[0][input_len:]
    generated  = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

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
def _predeploy_boundary_precheck(prompt_text: str) -> Optional[dict]:
    """2026-10-07 QA 掃描發現的另一層真實 bug：原本想在 Gemini 正規化前對原始輸入
    呼叫 `_deterministic_deploy_parse()` 做邊界值/多服務檢查，結果發現該函式本體的
    `deploy_words` 守門太窄（例如要求「起」後面要接空白、完全沒涵蓋「我要.../
    我想...」這種不含明確部署動詞的口語化講法）——「我要兩個 nginx 跟三個 mysql」
    這種明顯是多服務請求的句子，連 `deploy_words` 這關都過不了，整個函式直接回
    None，等於多服務/邊界值檢查從沒真的跑過。

    這裡獨立出一個更寬鬆、但範圍只聚焦在「有沒有多服務/邊界值訊號」本身的守門：
    `_looks_like_multi_service_request()` 本身就要求至少 2 個已知服務關鍵字
    （nginx/redis/postgres/mysql/node/python/golang）加上明確的數量提示詞，
    `_extract_pods_count()` 的正則也都綁定 pods/replicas/個/副本 這類數量詞彙，
    兩者都已經是語意上夠窄的訊號，不需要再疊加 `deploy_words` 這層守門——刻意
    不動 `_deterministic_deploy_parse()` 本體的守門（那是用來保護欄位抽取不要
    誤觸發在無關聊天內容上，兩個函式守門的風險考量不一樣）。
    """
    raw = (prompt_text or '').strip()
    low = raw.lower()
    if _looks_like_multi_service_request(raw, low):
        return {'_parse_error': 'multi_service_unsupported', '_parser': 'deterministic'}
    explicit_pods = _extract_pods_count(raw)
    if explicit_pods is not None and not (1 <= explicit_pods <= 100):
        return {'_parse_error': 'replica_out_of_range', '_requested_pods': explicit_pods,
                '_parser': 'deterministic'}
    return None


def _parse_error_response(deterministic: dict) -> Optional[dict]:
    """把 `_deterministic_deploy_parse()` 回傳的 `_parse_error` 標記轉成具體、雙語、
    可行動的錯誤訊息。抽成獨立函式是因為 2026-10-07 QA 掃描發現這段邏輯需要在
    Gemini 正規化「之前」跟「之後」各跑一次（見 `ask_llama()` 的說明），不想兩處
    各寫一份容易兜不起來的重複文案。"""
    if not deterministic or not deterministic.get('_parse_error'):
        return None
    kind = deterministic['_parse_error']
    if kind == 'replica_out_of_range':
        n = deterministic.get('_requested_pods')
        return {"error": f"副本數必須是 1~100 之間的正整數，你輸入的是 {n}，不合法 / "
                          f"Replica count must be a positive integer between 1 and 100; "
                          f"{n} is not valid."}
    if kind == 'multi_service_unsupported':
        return {"error": "目前一次只能部署一個服務，請把多個服務分開、一次講一個 / "
                          "Only one service can be deployed per request right now — "
                          "please describe each service separately."}
    return {"error": "解析失敗", "raw": str(deterministic)}


def ask_llama(prompt_text: str) -> dict:
    """
    呼叫 LLM 推論（優先順序）：
    1. Claude API — 毫秒級，無需 GPU（需設定 ANTHROPIC_API_KEY）
    2. RAG 增強 + Model Server HTTP（常駐本地 LLaMA）
    3. Fallback：本地直接載入
    """
    try:
        _warn_if_stale_normalize_env()

        # 2026-10-07 QA 掃描發現的真實 bug：副本數邊界值／多服務偵測這兩項
        # 2026-10-06 才修好的保護，原本只套用在 Gemini 正規化「之後」的文字
        # （`model_input`）——Gemini 把請求完整正規化成功時會在下面直接
        # `return spec`，根本不會跑到這兩項檢查；Gemini 正規化不完整時，檢查的
        # 又是 Gemini 改寫過的 `### DeploySpec` 文字，不是使用者原始措辭，而
        # `### DeploySpec` 格式本身是為單一服務設計的，多服務的訊號（「兩個...
        # 跟三個...」這種計數片語）常常在改寫過程中就已經遺失，導致同一類保護
        # 換個講法就隨機失效（實測：「部署兩個服務，一個nginx三個副本，一個redis
        # 一個副本」正確攔下，但「我要兩個 nginx 跟三個 mysql」只回無資訊的「解析
        # 失敗」）。修法：先對使用者原始輸入跑一次這兩項檢查，不管 Gemini 怎麼
        # 處理都不會被繞過；原始輸入沒有觸發任何保護時，才繼續走下面原本的流程。
        pre_check_error = _parse_error_response(_predeploy_boundary_precheck(prompt_text))
        if pre_check_error:
            return pre_check_error

        # ── 1. 翻譯層優先：一律先用 Gemini 把中/英、模糊/口語輸入正規化成 ### DeploySpec ──
        #    Gemini 只負責「聽懂」，沒講的欄位一律留白不猜。spec 齊全就直接回傳（零 GPU），
        #    不齊全就把 spec 當作模型輸入。Gemini 不可用時 fallback 回原始輸入。
        model_input = prompt_text
        if os.environ.get("USE_LLM_NORMALIZE") == "1":
            try:
                from core.gemini_client import gemini_normalize_deploy_request, is_available as gemini_available
                if gemini_available():
                    spec_block = gemini_normalize_deploy_request(prompt_text)
                    if spec_block:
                        print(f"[Gemini 翻譯層] {prompt_text!r} →\n{spec_block}")
                        spec = parse_deploy_spec(spec_block)
                        if spec and _validate(spec):
                            return spec
                        model_input = spec_block
            except Exception as e:
                print(f"[Gemini 翻譯層] 正規化失敗，改用原始輸入：{e}")

        # ── 2. 本地 deterministic 快路徑（乾淨輸入 / Gemini 掛掉時的後備）──
        deterministic = _deterministic_deploy_parse(model_input)
        deterministic_error = _parse_error_response(deterministic)
        if deterministic_error:
            # 2026-10-06 QA 掃描實測發現：0/負數/超過上限的副本數、或一次要求多個服務，
            # 原本會被這裡默默丟棄（_validate 判不過就無聲落入後面的 Gemini/小模型
            # pipeline），最後在最外層 except 被攔截成完全沒資訊的「解析失敗」。現在
            # deterministic parser 已經明確知道問題出在哪，直接組一句具體、雙語、
            # 可行動的訊息回傳，不要再讓它掉進後面的模糊 pipeline。
            return deterministic_error
        if deterministic and _validate(deterministic):
            print("[Parser] deterministic deployment parse")
            return deterministic

        # ── 3. Claude API（opt-in）──
        if os.environ.get("USE_CLAUDE_API") == "1":
            try:
                from core.claude_client import claude_parse_k8s, is_available as claude_available
                if claude_available():
                    result = claude_parse_k8s(model_input)
                    if result is not None:
                        print("[Claude API] 解析成功")
                        return result
            except Exception:
                pass

        # ── 4. 小模型 + RAG few-shot：撈相似的部署範例當引導 ──
        examples = _deploy_fewshot(model_input)

        _auto_start_server()

        server_result = _try_server(model_input, examples=examples)
        if server_result is not None:
            return server_result

        # ── 5. Fallback：本地直接載入 ──
        return _local_infer(model_input)

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

        if os.environ.get("USE_LLM_NORMALIZE") == "1" and message.strip():
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


def classify_intent(message: str) -> dict:
    """把一句聊天訊息分類成 {action, args, confidence, source}。

    給 web_demo 的 /api/intent 用：規則比對不到時才呼叫模型伺服器的 /classify
    （部署 3B，嚴格 JSON）。伺服器未啟動 / 失敗一律回 qa，讓呼叫端 fallback 到純問答。
    """
    fallback = {"action": "qa", "args": {}, "confidence": 0.0, "source": "llm_unavailable"}
    try:
        if not _is_server_alive() and not _auto_start_server():
            return fallback
        import urllib.request
        body = json.dumps({"message": message}).encode()
        req = urllib.request.Request(
            f"{MODEL_SERVER_URL}/classify",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            result = data.get("result") or {}
            if result.get("action"):
                result.setdefault("args", {})
                result.setdefault("confidence", 0.0)
                result["source"] = "llm"
                return result
    except Exception:
        pass
    return fallback


def diagnose_with_llm(context: dict) -> Optional[dict]:
    """呼叫監控小模型（Model Server 的 /diagnose 端點，Qwen2.5-1.5B 跑 CPU）做根因分析。

    回傳 {root_cause, severity, action, suggestion} 或 None（server 未啟動 / 監控模型未載入）。
    healer/diagnose.py 的規則層仍會先跑，這裡只補規則比不到的深度分析。
    """
    try:
        if not _is_server_alive() and not _auto_start_server():
            return None
        import urllib.request
        body = json.dumps({"context": context}).encode()
        req = urllib.request.Request(
            f"{MODEL_SERVER_URL}/diagnose",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data.get("diagnosis")
    except Exception:
        return None


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
