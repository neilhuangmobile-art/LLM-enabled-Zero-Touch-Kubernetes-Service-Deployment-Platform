# 多代理協作模組

## 架構說明

```
agents/
├── orchestrator.py    # 協調器（入口點）
├── security_agent.py  # 安全代理
├── cost_agent.py      # 成本代理
└── perf_agent.py      # 效能代理
```

## 快速使用

```bash
# 對 YAML 進行全代理評估
python agents/orchestrator.py my-deployment.yaml

# 儲存評估報告
python agents/orchestrator.py my-deployment.yaml --save-report

# 僅執行安全掃描
python agents/security_agent.py my-deployment.yaml

# 僅執行成本分析
python agents/cost_agent.py my-deployment.yaml

# 僅執行效能分析
python agents/perf_agent.py my-deployment.yaml
```

## 決策邏輯

| 決策 | 觸發條件 | 後續動作 |
|------|---------|---------|
| `approve` | 所有代理通過 | 允許部署 |
| `warn` | 有警告但無阻斷問題 | 顯示警告，允許部署 |
| `block` | 有 critical 安全問題或嚴重資源錯誤 | 拒絕部署，必須修復 |

## 代理說明

### security_agent
- 呼叫 `guardian/yaml_validator.py` 進行基礎掃描
- 額外檢查：privileged、hostNetwork、hostPID、runAsRoot、latest tag、resources.limits
- 產出安全分數（0-100）

### cost_agent
- 識別應用程式類型（web/java/llm/database/...）
- 比對推薦資源範圍，偵測 over/under provisioning
- 粗略估算月費（USD）

### perf_agent
- 檢查副本數（單副本警告）
- 建議 HPA 配置
- 檢查健康探針（readiness/liveness）
- 建議 Pod 反親和性

## 整合到主流程

```python
from agents.orchestrator import orchestrate

# 在 LLM 生成 manifest 後，送入代理評估
manifest = generate_manifest_from_llm(user_input)
result = orchestrate(manifest, save_report=True)

if result["decision"] == "block":
    print("部署被阻斷：", result["blockers"])
elif result["decision"] == "warn":
    print("警告：", result["warnings"])
    # 仍可繼續部署
    deploy(manifest)
else:
    deploy(manifest)
```

## 研究依據

> 「從「單一任務代理」向「多代理系統」演進是 2026 年的重要趨勢。
> 在這種架構下，專門的代理（如「安全代理」、「成本代理」、「效能代理」）
> 協作管理集群。例如，「補救代理」在偵測到故障後生成修復程式碼，
> 隨後將其交由「驗證代理」在沙盒環境中測試，最後才提交至 Git 倉庫」
>
> — 基於大型語言模型之零接觸 Kubernetes 部署平台架構與產業實務研究報告
