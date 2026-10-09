"""
tests/test_deterministic_parse.py
測 llama_client.py 的 _deterministic_deploy_parse()——2026-10-03 使用者實測時
發現的真實 bug：純中文、沒指定 app_name 的資源需求句子（例如「幫我部屬一個
30核CPU，20GI記憶體的服務」）被誤判成「使用者指定了 app_name」，而且記憶體/CPU
數值整段默默消失。

根因是這段程式碼大量使用 Python 預設 Unicode-aware 的 `\\w`／`\\b`：
- app_name 候選正則 `[\\w-]` 把中文字也當成「單字字元」比對進去，「...記憶體的
  服務」整段被誤判成「某個東西 + 服務」的命名樣式，抓出「20gi記憶體的」當候選，
  正規化後變成使用者從沒打過的「20gi」，被 K8s 命名規則擋下。
- 記憶體/CPU 數值正則結尾的 `\\b`（word boundary），在「數字+單位」後面緊接著
  中文字、中間沒有空格或標點時，因為中文字跟英數字母一樣算 `\\w`，兩個 \\w
  字元之間沒有邊界，`\\b` 永遠對不上，整個 regex 配對失敗，數值被默默丟掉
  （不是解析錯誤、是完全沒抓到，連警告都沒有）。
"""
import pytest

llama_client = pytest.importorskip("llama_client")

_deterministic_deploy_parse = llama_client._deterministic_deploy_parse


def test_pure_chinese_resource_sentence_does_not_misparse_app_name():
    """使用者實測的原句：沒給名字，只講要多少 CPU/記憶體——app_name 必須落回
    auto-app，不能把「20gi記憶體的」這種容量敘述誤判成使用者指定的名字。"""
    result = _deterministic_deploy_parse("幫我部屬一個30核CPU，20GI記憶體的服務")

    assert result is not None
    assert result["app_name"] == "auto-app"


def test_pure_chinese_resource_sentence_extracts_memory_and_cpu():
    """同一句話，記憶體跟 CPU 數值要真的被抓出來，不能因為後面緊接中文字
    （沒有空格分隔）就讓 `\\b` 失效、整段默默消失。"""
    result = _deterministic_deploy_parse("幫我部屬一個30核CPU，20GI記憶體的服務")

    assert result is not None
    assert result.get("memory") == "20Gi"
    assert result.get("cpu") == "30"


def test_memory_unit_casing_normalized_to_k8s_canonical_form():
    """2026-10-03 使用者實測連帶發現的第二個 bug：使用者打的「20GI」（全大寫）
    被原樣保留，後面 web_demo.py 的格式驗證（`^\\d+(Mi|Gi|Ki|M|G)$`，大小寫
    敏感）會把它擋下，回報「memory must look like 128Mi or 1Gi」——對一個本該
    自動理解的新手輸入來說是不必要的卡關。現在無論使用者打 GI／gi／Gi，都要
    統一正規化成 K8s 要求的 "Gi"。"""
    for raw_unit, expected in [("GI", "Gi"), ("gi", "Gi"), ("Gi", "Gi"),
                                ("MI", "Mi"), ("mi", "Mi"), ("Mi", "Mi")]:
        result = _deterministic_deploy_parse(f"幫我部署一個 64{raw_unit} 記憶體的服務")
        assert result is not None
        assert result.get("memory") == f"64{expected}", f"輸入單位 {raw_unit!r} 正規化失敗"


def test_quantity_like_candidate_never_becomes_app_name():
    """防呆：就算候選字串剛好是純 ASCII（沒有中文字混進來），「數字+資源單位」
    這種字串本身也不該被當成 app_name——例如純英文的「a 20Gi memory service」。"""
    result = _deterministic_deploy_parse("deploy a 20Gi memory service")

    assert result is not None
    assert result["app_name"] != "20gi"
    assert result.get("memory", "").lower() == "20gi"


def test_image_name_before_service_keyword_never_becomes_app_name():
    """2026-10-03 使用者錄 demo 時又實測到的真實 bug：「我要部屬2個nginx服務」
    沒給自訂名字，只是把 image 名稱（nginx）接著「服務」講——pattern 2
    （「<候選> service/app/deployment/服務」）原本看到 "nginx" 緊接著
    "服務" 就直接當成使用者取的名字，但 "nginx" 其實就是前面已經抓到的
    image，不是另外取的名字，app_name 必須落回 auto-app。"""
    result = _deterministic_deploy_parse("我要部屬2個nginx服務")

    assert result is not None
    assert result["image"] == "nginx:latest"
    assert result["app_name"] == "auto-app"


def test_named_keyword_not_swallowed_by_generic_app_service_prefix():
    """2026-10-03 使用者錄 demo 時又實測到的真實 bug：「deploy app named
    123abc」裡，pattern 1 原本把 "app"／"service" 也當成「這裡開始是名稱」
    的前綴觸發字，但這兩個字在「deploy an app named X」這種句型裡只是
    泛稱名詞，後面緊接的下一個詞剛好是真正的連接詞 "named"，導致 "named"
    自己被誤判成候選名稱，使用者真正想取的 "123abc" 完全沒被抓到。"""
    result = _deterministic_deploy_parse("deploy app named 123abc")

    assert result is not None
    assert result["app_name"] == "123abc"


def test_service_named_x_still_extracts_x_not_named():
    """對照組（這次修法順便修好的既有已知限制）：「a service named my-app」
    現在也要正確抓到 my-app，不能再被 "service" 這個泛稱詞搶先攔截。"""
    result = _deterministic_deploy_parse("deploy a service named my-app using nginx:latest")

    assert result is not None
    assert result["app_name"] == "my-app"


def test_structured_deployspec_block_header_not_misread_as_app_name():
    """2026-10-03 使用者錄 demo 時第三次實測到的真實 bug：`ask_llama()` 在
    Gemini 翻譯層回傳的 "### DeploySpec" 結構化區塊驗證不完整時，會把這個
    區塊原文直接丟進這支函式當 fallback 輸入——區塊第一行是 markdown 標題
    "### DeploySpec"，緊接著下一行是 "app_name: ..." 這種 key:value 格式。
    因為 `\\s` 本身包含換行字元，pattern 2 原本會跨行把標題裡的 "DeploySpec"
    跟下一行 "app_name:" 開頭的 "app" 接在一起比對成功，抓出候選
    「DeploySpec」，正規化後變成使用者從沒打過的「deployspec」。候選字跟
    關鍵字之間現在限定「同一行」，不能再跨行誤判。"""
    text = "### DeploySpec\napp_name: 123abc\nimage: nginx:latest\nreplicas: 1"
    result = _deterministic_deploy_parse(text)

    assert result is not None
    assert result["app_name"] != "deployspec"


def test_cpu_with_chinese_unit_immediately_before_english_cpu_keyword():
    """「30核CPU」這種中文單位字緊接著英文關鍵字 CPU 的寫法，不能因為中文
    單位後面「湊巧」接著一個 ASCII 字母（C）就被新加的防呆機制誤判成
    「不是完整詞」而拒絕配對。"""
    result = _deterministic_deploy_parse("幫我部署一個要 8 核CPU 的服務")

    assert result is not None
    assert result.get("cpu") == "8"


def test_existing_named_app_still_parsed_correctly():
    """對照組：既有「for <name>」「叫做 <name>」這種明確指定名稱的句子，
    修法前後行為必須不變，不能連帶改壞。"""
    result = _deterministic_deploy_parse(
        "deploy 2 nginx pods for web-frontend, port 8080"
    )
    assert result["app_name"] == "web-frontend"

    result_zh = _deterministic_deploy_parse("幫我部署一個叫做 payment-api 的服務")
    assert result_zh["app_name"] == "payment-api"


def test_zuo_wei_connector_extracts_image_and_app_name():
    """2026-10-07 research_benchmark.py 實測發現的真實 bug：「image 作為/當作
    app_name」這種句型，「作為」沒被列在 image/app_name 擷取規則的連接詞清單裡，
    image 被靜默退回預設值 nginx:latest（使用者明明打了 node 卻部署出 nginx），
    app_name 也沒接到 "作為" 後面真正的名字、退回用 image 基底名稱頂替。"""
    result = _deterministic_deploy_parse("請部署 4 個 node:20-alpine 作為 api-gateway")

    assert result is not None
    assert result["image"] == "node:20-alpine"
    assert result["app_name"] == "api-gateway"


def test_dang_zuo_connector_extracts_image_and_app_name():
    result = _deterministic_deploy_parse("幫我把 redis:7 當作 cache-service 部署起來")

    assert result is not None
    assert result["image"] == "redis:7"
    assert result["app_name"] == "cache-service"


class TestPredeployBoundaryPrecheckSurvivesGemini:
    """2026-10-07 大規模掃描（206 題，走正確端點 /api/deploy/parse）實測發現的
    真實 P0：2026-10-06 修好的副本數邊界值／多服務偵測，原本只套用在 Gemini
    正規化「之後」的文字——Gemini 把請求完整正規化成功時直接回傳，根本不會跑到
    這兩項檢查；Gemini 正規化不完整時，檢查的又是 Gemini 改寫過的文字，不是
    使用者原始措辭，而多服務的訊號常常在改寫過程中就已經遺失。實測對照：
    「部署兩個服務，一個nginx三個副本，一個redis一個副本」正確攔下，但結構幾乎
    一樣的「我要兩個 nginx 跟三個 mysql」只回無資訊的「解析失敗」。

    修法：在 ask_llama() 最前面，先對使用者原始輸入（Gemini 碰過之前）跑一次
    _predeploy_boundary_precheck()，不管 Gemini 怎麼處理都不會被繞過。這裡
    統一用 USE_LLM_NORMALIZE=0 測，確認保護本身（不靠 Gemini）就是正確的——
    管線最前面已經保證 Gemini 不會比這個檢查先跑到。"""

    @staticmethod
    def _ask(monkeypatch, text):
        monkeypatch.setenv("USE_LLM_NORMALIZE", "0")
        return llama_client.ask_llama(text)

    def test_multi_service_without_explicit_deploy_verb_is_caught(self, monkeypatch):
        """2026-10-06 的 deploy_words 守門要求訊息要有「部署/起 /deploy」這類明確
        動詞，「我要兩個 nginx 跟三個 mysql」沒有，連 _deterministic_deploy_parse()
        本體都直接放行（回 None，完全沒檢查）——_predeploy_boundary_precheck()
        不要求這個守門，一樣要抓到。"""
        result = self._ask(monkeypatch, "我要兩個 nginx 跟三個 mysql")
        assert "error" in result
        assert "一次只能部署一個服務" in result["error"]

    def test_multi_service_with_colon_structure_is_caught(self, monkeypatch):
        result = self._ask(monkeypatch, "起兩個服務：api 用 node，db 用 postgres")
        assert "error" in result
        assert "一次只能部署一個服務" in result["error"]

    def test_negative_replica_without_pods_keyword_is_caught(self, monkeypatch):
        """「replica 數量是 -1」沒有「部署/起 /deploy」這類動詞，一樣要抓到。"""
        result = self._ask(monkeypatch, "replica 數量是 -1，image 用 nginx")
        assert "error" in result
        assert "-1" in result["error"]

    def test_negative_replica_with_deploy_verb_is_caught(self, monkeypatch):
        result = self._ask(monkeypatch, "deploy -1 pods of nginx")
        assert "error" in result
        assert "-1" in result["error"]

    def test_clean_single_service_request_still_works(self, monkeypatch):
        """對照組：乾淨的單一服務請求不能被連帶改壞。"""
        result = self._ask(monkeypatch, "deploy 3 pods of nginx:latest")
        assert "error" not in result
        assert result["pods"] == 3
        assert result["image"] == "nginx:latest"

    def test_unrelated_qa_question_not_falsely_flagged(self, monkeypatch):
        """對照組：不相關的一般問答不能被誤判成多服務部署請求——只是一句提到
        nginx 的問句，不該被 _looks_like_multi_service_request() 誤抓。"""
        result = self._ask(monkeypatch, "什麼是 Kubernetes 的 Pod？")
        # 不應該是「一次只能部署一個服務」這種誤判；是不是部署請求本身交給後面
        # 的 pipeline 判斷，這裡只確認沒有被誤判成多服務。
        if "error" in result:
            assert "一次只能部署一個服務" not in result["error"]

    def test_yi_ge_gen_yi_ge_multi_service_phrasing_is_caught(self, monkeypatch):
        """2026-10-07 整理量化報告時發現的真實 bug：「起一個 nginx 跟一個 redis」
        沒有「兩個/多個/幾個」這類固定複數計數詞，_looks_like_multi_service_request()
        原本判定失敗，兩個不同服務被靜默合併成 image=nginx 的 2 個副本——使用者
        要的是各一個，不是兩個 nginx。"""
        result = self._ask(monkeypatch, "起一個 nginx 跟一個 redis")
        assert "error" in result
        assert "一次只能部署一個服務" in result["error"]


class TestImageExtractionConnectorWords:
    """2026-10-07 整理量化報告時發現的真實 bug：「部署九個 redis 副本」的 image
    擷取正則 lookahead 清單沒有「副本」這個計量單位，擷取失敗後靜默退回預設值
    nginx:latest（使用者明明打了 redis 卻部署出 nginx）。"""

    def test_fu_ben_connector_extracts_correct_image(self):
        result = _deterministic_deploy_parse("部署九個 redis 副本")
        assert result is not None
        assert result["image"] == "redis:latest"
        assert result["pods"] == 9

    def test_tai_connector_extracts_correct_image(self):
        result = _deterministic_deploy_parse("部署三台 mysql")
        assert result is not None
        assert result["image"] == "mysql:latest"


# 2026-10-09 兩人同時使用 QA 抓到：Gemini 429/503 退回這個解析器時，中文輸入會丟掉版本號，
# 甚至把 redis 換成 nginx（使用者要 redis 卻部署 nginx）。單獨送時 Gemini 正常所以測不到。
@pytest.mark.parametrize("text,image,app", [
    ("部署 2 個 nginx:1.27，名稱 qa-b-web，port 80", "nginx:1.27", "qa-b-web"),
    ("部署 2 個 redis:7.2 名稱 cache", "redis:7.2", "cache"),
    ("部署 3 個 redis 名稱 cache", "redis:latest", "cache"),
    ("部署 1 個 postgres:16，記憶體 512Mi", "postgres:16", None),
    ("run myrepo/app:v1.2 named api port:8080", "myrepo/app:v1.2", "api"),
])
def test_chinese_input_keeps_image_and_tag_2026_10_09(text, image, app):
    r = llama_client._deterministic_deploy_parse(text)
    assert r["image"] == image
    if app:
        assert r["app_name"] == app


def test_port_and_time_are_not_mistaken_for_images_2026_10_09():
    assert llama_client._deterministic_deploy_parse("run myrepo/app:v1.2 named api port:8080")["port"] == 8080
    assert llama_client._deterministic_deploy_parse("10:30 部署 2 個 nginx")["image"] == "nginx:latest"
