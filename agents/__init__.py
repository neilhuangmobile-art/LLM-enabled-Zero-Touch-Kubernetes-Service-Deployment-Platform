"""
agents/ — 多代理協作模組

各代理職責：
    security_agent  — 安全掃描（特權容器、hostNetwork、映像 tag 等）
    cost_agent      — 資源成本分析（over/under provisioning、月費估算）
    perf_agent      — 效能建議（HPA、副本數、健康探針、反親和性）
    orchestrator    — 協調器，整合三個代理並給出最終決策

快速使用：
    from agents.orchestrator import orchestrate, print_result
    result = orchestrate(my_manifest)
    print_result(result)
"""
