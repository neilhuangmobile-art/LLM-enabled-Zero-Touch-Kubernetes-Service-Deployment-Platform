"""
test_llama.py - 獨立測試腳本
不需要 K8s，只測試 LLaMA 解析是否正確
執行：python test_llama.py
"""
import json
import sys
import os

# ==============================
# 先測試 null 解析（不需要 GPU）
# ==============================
def test_null_parsing():
    import re

    print("=" * 50)
    print("  Step 1：測試 null 解析邏輯（不需要 GPU）")
    print("=" * 50)

    test_strings = [
        '{"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": null, "memory": null}',
        '{"pods": 5, "image": "redis:7-alpine", "app_name": "cache-server", "port": null, "memory": null}',
        '{"pods": 2, "image": "postgres:15", "app_name": "db-primary", "port": null, "memory": null }',
    ]

    all_pass = True
    for s in test_strings:
        fixed = re.sub(r':\s*null\b', ': "NULL"', s)
        result = json.loads(fixed)
        result = {k: v for k, v in result.items() if v != "NULL"}
        pods_ok = isinstance(result.get("pods"), int) and result["pods"] > 0
        print(f"  {'✅' if pods_ok else '❌'} {result}")
        if not pods_ok:
            all_pass = False

    print(f"\n  結果：{'全部通過 ✅' if all_pass else '有失敗 ❌'}")
    return all_pass


# ==============================
# 測試 llama_client 的 _parse 函式
# ==============================
def test_parse_function():
    print("\n" + "=" * 50)
    print("  Step 2：測試 _parse 函式")
    print("=" * 50)

    try:
        from llama_client import _parse, _validate
    except ImportError as e:
        print(f"  ❌ 無法載入 llama_client：{e}")
        return False

    test_cases = [
        '{"pods": 3, "image": "nginx:latest", "app_name": "web-frontend", "port": null, "memory": null}',
        '{"pods": 5, "image": "redis:7-alpine", "app_name": "cache-server", "port": null, "memory": null}',
        '{"pods": 2, "image": "postgres:15", "app_name": "db-primary", "port": 5432, "memory": null}',
        '{"pods": 1, "image": "node:20-alpine", "app_name": "api-gateway", "port": 3000, "memory": "256Mi"}',
    ]

    all_pass = True
    for s in test_cases:
        result = _parse(s)
        valid  = _validate(result) if result else False
        print(f"  {'✅' if valid else '❌'} 解析：{result}")
        if not valid:
            all_pass = False

    print(f"\n  結果：{'全部通過 ✅' if all_pass else '有失敗 ❌'}")
    return all_pass


# ==============================
# 測試完整 ask_llama（需要 GPU）
# ==============================
def test_ask_llama():
    print("\n" + "=" * 50)
    print("  Step 3：測試完整 ask_llama（需要 GPU）")
    print("=" * 50)

    try:
        from llama_client import ask_llama
    except ImportError as e:
        print(f"  ❌ 無法載入 llama_client：{e}")
        return False

    test_inputs = [
        "deploy 3 pods of nginx:latest",
        "部署 2 個 redis:7-alpine，命名為 cache-server",
        "launch 1 postgres:15 pod for db-primary, port 5432",
    ]

    passed = 0
    for inp in test_inputs:
        print(f"\n  輸入：{inp}")
        result = ask_llama(inp)
        if "error" in result:
            print(f"  ❌ 失敗：{result['raw'][:100]}")
        else:
            print(f"  ✅ 成功：{result}")
            passed += 1

    print(f"\n  結果：{passed}/{len(test_inputs)} 通過")
    return passed == len(test_inputs)


# ==============================
# 主程式
# ==============================
if __name__ == "__main__":
    step1 = test_null_parsing()
    if not step1:
        print("\n❌ Step 1 失敗，請先修好 null 解析問題")
        sys.exit(1)

    step2 = test_parse_function()
    if not step2:
        print("\n❌ Step 2 失敗，llama_client._parse 有問題")
        sys.exit(1)

    print("\n✅ Step 1 & 2 全部通過，代表解析邏輯正確")
    print("   現在進行 Step 3（需要 GPU，會載入模型）...")
    ans = input("   繼續跑 Step 3？(y/n): ").strip().lower()
    if ans == "y":
        test_ask_llama()
    else:
        print("   跳過 Step 3")
