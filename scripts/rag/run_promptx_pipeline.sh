#!/usr/bin/env bash
# PROMPTx-RAG 剩餘步驟一次跑完（2026-11-15，使用者出門兩天，電腦保持開機）
# 1. 等目前的 Claude 出題、評估表建立兩個程序結束（避免同時寫同一個檔案）
# 2. Claude 產生擾動版本（錯字、極短、囉嗦、中英夾雜）
# 3. 評估表補上新題目與擾動版本（可接續）
# 4. 策略學習比較（5 折 × 3 種子、跨出題者、修正 t 檢定）
# 每一步都能接續；某一步失敗會記錄後繼續下一步，最後寫 DONE。
cd /d/LLM-enabled-Zero-Touch-Kubernetes-Service-Deployment-Platform || exit 1
LOG=docs/rag_promptx/pipeline.log
mkdir -p docs/rag_promptx
export PYTHONIOENCODING=utf-8 TQDM_DISABLE=1
FILTER='Warn|warn|attn|symlink|Fetching|tokenizer|pre tokenize|Inference'
say() { echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

running() {
  powershell -NoProfile -Command "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*$1*' }).Count" 2>/dev/null | tr -d '\r'
}

say "開始：等待出題與評估表程序結束"
while [ "$(running gen_claude_set)" != "0" ] || [ "$(running strategy_eval)" != "0" ]; do sleep 60; done
say "出題題數：$(grep -c . docs/bench_blind/dev_claude.jsonl 2>/dev/null)"

# 出題若被額度上限打斷，補跑一次（已完成的批次會跳過）
if [ "$(grep -c . docs/bench_blind/dev_claude.jsonl 2>/dev/null)" -lt 280 ]; then
  say "出題不足 280 題，補跑"
  python -u scripts/rag/gen_claude_set.py questions 2>&1 | grep -vE "$FILTER" >> "$LOG"
fi

say "步驟 2：產生擾動版本"
for i in 1 2 3; do
  python -u scripts/rag/gen_claude_set.py perturb 2>&1 | grep -vE "$FILTER" >> "$LOG"
  n=$(cut -d'"' -f4 docs/bench_blind/dev_perturb.jsonl 2>/dev/null | sort -u | wc -l)
  say "擾動版本涵蓋題數：$n（第 $i 次）"
  [ "$n" -ge 400 ] && break
  sleep 1800   # 可能遇到訂閱用量上限，等 30 分鐘再試
done

say "步驟 2.5：防汙染測試"
python -m pytest tests/test_field_retriever.py -q -p no:warnings -o addopts="" 2>&1 | tail -3 >> "$LOG"

say "步驟 3：評估表補上新題目與擾動版本"
python -u rag/strategy_eval.py build 2>&1 | grep -vE "$FILTER" >> "$LOG"
say "評估表筆數：$(grep -c . docs/rag_promptx/strategy_table.jsonl)"

say "步驟 4：策略學習比較"
python -u rag/policy.py evaluate 2>&1 | grep -vE "$FILTER" >> "$LOG"

say "DONE"
