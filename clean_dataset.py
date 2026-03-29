"""
clean_dataset.py
清理訓練資料集，移除錯誤樣本
執行：python clean_dataset.py
"""
import json
import os
import re
import shutil
from datetime import datetime

DATASET_PATH = r"D:\k8s_new\dataset\finetune_samples.jsonl"


def _validate_sample(data: dict) -> tuple[bool, list[str]]:
    """
    回傳 (是否合法, 錯誤原因列表)
    檢查項目：
    - 必要欄位：input, output
    - output 不能有 error 欄位
    - pods 必須是 1~100 整數
    - input 不能是純數字或空白
    - port 若存在，必須是合法 port number
    - memory 若存在，格式必須是 NNNMi / NNNGi
    """
    reasons = []

    inp    = data.get("input", "")
    output = data.get("output", {})

    # 必要欄位
    if not inp:
        reasons.append("input 為空")
    if inp.strip().isdigit():
        reasons.append("input 是純數字")
    if not isinstance(output, dict):
        reasons.append("output 不是 dict")
        return False, reasons

    # error 欄位
    if "error" in output:
        reasons.append("output 有 error 欄位")

    # pods 驗證
    pods_val = output.get("pods")
    try:
        pods_int = int(pods_val)
        if not (1 <= pods_int <= 100):
            reasons.append(f"pods 超出範圍（{pods_int}）")
    except (ValueError, TypeError):
        reasons.append(f"pods 非整數（{pods_val}）")

    # port 驗證（選填）
    port_val = output.get("port")
    if port_val is not None:
        try:
            port_int = int(port_val)
            if not (1 <= port_int <= 65535):
                reasons.append(f"port 超出範圍（{port_int}）")
        except (ValueError, TypeError):
            reasons.append(f"port 非整數（{port_val}）")

    # memory 驗證（選填）
    mem_val = output.get("memory")
    if mem_val is not None:
        if not re.match(r"^\d+(Mi|Gi|Ki|M|G)$", str(mem_val)):
            reasons.append(f"memory 格式錯誤（{mem_val}）")

    return len(reasons) == 0, reasons


def clean():
    if not os.path.exists(DATASET_PATH):
        print(f"❌ 找不到檔案：{DATASET_PATH}")
        return

    # 備份
    backup_path = DATASET_PATH + f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(DATASET_PATH, backup_path)
    print(f"📦 已備份原始檔案：{backup_path}")

    total   = 0
    kept    = []
    dropped = []

    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                data  = json.loads(line)
                valid, reasons = _validate_sample(data)
                if valid:
                    kept.append(line)
                else:
                    inp = data.get("input", "")[:40]
                    dropped.append((inp, ", ".join(reasons)))
            except json.JSONDecodeError as e:
                dropped.append(("(JSON解析失敗)", str(e)))

    # 寫回乾淨資料
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        for line in kept:
            f.write(line + "\n")

    print(f"\n✅ 清理完成！")
    print(f"   原始筆數：{total}")
    print(f"   保留筆數：{len(kept)}")
    print(f"   刪除筆數：{len(dropped)}")

    if dropped:
        print(f"\n🗑️  被刪除的資料（最多顯示20筆）：")
        for inp, reason in dropped[:20]:
            print(f"   input='{inp}' → {reason}")


if __name__ == "__main__":
    clean()
