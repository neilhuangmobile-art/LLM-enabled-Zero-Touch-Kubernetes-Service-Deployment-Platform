---
name: 2026-10-09-gemini-keys-tos-issue
description: .env 的 6 把 Gemini key 各來自不同人的 Google 帳號、程式輪流使用湊額度，違反 Google APIs 條款 §2(d)；修正方案已提出、待使用者決定
metadata:
  node_type: memory
  type: project
  originSessionId: 110ca64c-0ce9-476a-aa64-bad49bb5b63d
  modified: 2026-10-09T09:25:52.024Z
---

2026-10-09 使用者確認：`.env` 的 `GEMINI_API_KEYS` 共 6 把，**每一把都是不同人的 Google 帳號**，
`core/gemini_client.py` 主動 round-robin 輪流用（第 9 行註解寫「跟隊友借 key 湊額度」）。

判斷：違反 Google APIs 服務條款 §2(d)「will not attempt to circumvent such limitations」，
也牽涉 §4(b)(1) 憑證保密（https://developers.google.com/terms）。額度是依「專案」計算，同一專案多把 key 不會增加額度。
另一個問題：免費方案的輸入會被 Google 用來改善產品，而同意書（cluster/pages.py）沒提到使用者輸入會送到 Gemini。

提出的修正（待使用者同意）：
- 使用者：自己帳號開一把 key、開啟付費並設花費上限、換掉 .env、請隊友刪除借出的 key
- 我：同意書補 Gemini 說明、改掉程式註解、寫進 docs/security_review.md（第 34 節）

**Why**：使用者要口試，評審問到翻譯層怎麼撐住多使用者時，這是很難辯護的點。
**How to apply**：不要建議「多借幾把 key」這類做法；提高額度的合規方式只有付費。Gemini 503 過載跟 key 數量無關，
靠本機備援解析器（llama_client._deterministic_deploy_parse）撐住。
