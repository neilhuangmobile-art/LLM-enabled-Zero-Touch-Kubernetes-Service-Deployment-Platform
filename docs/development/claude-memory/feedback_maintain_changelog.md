---
name: feedback-maintain-changelog
description: 每次有實質分量的改動要主動記進 CHANGELOG.md，不用等使用者交代
metadata:
  node_type: memory
  type: feedback
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-09-14T13:35:41.039Z
---

使用者 2026-09-14 要求建立 `CHANGELOG.md`（版本更新紀錄），並在下一個回合明確要求
把「每次更新要寫」這件事加入規則——代表這不是一次性的請求，是往後每次工作都要
遵守的標準流程。

**Why：** 使用者要拿這個專案去報告/口試，需要清楚的改動歷史記錄，而且明確表達過
不想每次都要重新交代同一件事（見 [[feedback_verify_before_claiming_done]] 這種
「不想重複講」的互動模式）。

**How to apply：**
- 這條規則已經寫進專案的 `AGENT_RULES.md`（每次開新對話會自動載入，不是只存在
  記憶裡），這份記憶是雙重備份，避免 `AGENT_RULES.md` 被覆寫或使用者換專案時
  漏掉同樣的習慣。
- 完成任何有實質分量的改動後（新功能、修好的 bug、架構調整——不是 typo/小文案
  調整），主動在 `CHANGELOG.md` 補一條，格式：日期時間 + 改了什麼 + 為什麼 +
  怎麼驗證（簡述），不用等使用者問「有沒有記錄」才想起來做。
- 用當下的實際日期時間，不要事後一次補一大批（那樣時間點會失真）。
- 這跟 `docs/security_review.md` 是互補關係：那份只收「評審會追問的風險類問題」，
  `CHANGELOG.md` 收全部有實質分量的改動，兩者常常同時記一件事但角度不同，不要
  搞混或只記一份。

相關記憶：[[feedback_verify_before_claiming_done]]。
