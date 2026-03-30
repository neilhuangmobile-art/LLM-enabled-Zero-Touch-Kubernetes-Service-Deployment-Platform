# guardian/__init__.py
# Phase 6 驗證防護層 - 對外介面

from guardian.dry_run import validate_manifest, validate_from_llm_result as dry_run_llm
from guardian.yaml_validator import validate_yaml, validate_from_llm_result as yaml_validate_llm


def validate_all(llm_result: dict, dry_run_mode: str = "client") -> dict:
    """
    一次執行全部驗證：YAML 安全掃描 + kubectl dry-run。
    這是給 0_touch_generate_pods.py 和 web_demo.py 呼叫的統一入口。

    Args:
        llm_result    : ask_llama() 回傳的 dict
        dry_run_mode  : "client"（語法）或 "server"（完整）

    Returns:
        {
            "ok"         : bool,     # 全部通過才是 True
            "yaml_errors": list,
            "yaml_warnings": list,
            "dry_run_ok" : bool|None,
            "dry_run_errors": list,
            "dry_run_warnings": list,
        }
    """
    yaml_result = yaml_validate_llm(llm_result)
    dry_result  = dry_run_llm(llm_result, mode=dry_run_mode)

    overall_ok = (
        yaml_result["ok"] and
        (dry_result["ok"] is True or dry_result["ok"] is None)
        # ok=None 表示 kubectl 不可用，視為通過（降級）
    )

    return {
        "ok"               : overall_ok,
        "yaml_errors"      : yaml_result["errors"],
        "yaml_warnings"    : yaml_result["warnings"],
        "dry_run_ok"       : dry_result["ok"],
        "dry_run_errors"   : dry_result["errors"],
        "dry_run_warnings" : dry_result["warnings"],
    }
