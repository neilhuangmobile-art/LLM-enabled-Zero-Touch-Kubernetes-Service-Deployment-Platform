---
name: 2026-10-07-new-machine-python-version-risk
description: 新電腦組裝後安裝環境的關鍵風險——requirements.txt釘死舊版套件(torch==2.4.1等)在最新版Python上可能沒有對應wheel；使用者已決定直接裝最新Python、接受"卡住再修"策略，不是裝3.9.13；下次卡住時要升級套件版本不是回頭裝舊Python
metadata:
  type: project
  originSessionId: 7df9e735-1dd3-4dd3-9f9f-4efb99c934ca
  modified: 2026-10-07T11:20:50.408Z
---

使用者準備組新電腦，2026-10-07 討論到「新電腦的 Python 應該會裝最新版，舊版現在裝不了」
這個疑慮，查證後確認是真的風險：這台機器實測跑 **Python 3.9.13**，`requirements.txt`
裡的套件版本是釘死的舊版（例如 `torch==2.4.1`，當下 PyPI 最新已經到 2.8.0）。已知
PyTorch 在 Windows 上對 Python 3.13 目前還沒有官方 wheel，`bitsandbytes` 這類有編譯
後端的套件也有同樣風險——新電腦如果用系統預設安裝程式裝「最新版 Python」，`pip install
-r requirements.txt` 很可能卡在 torch 那一步直接裝不起來。

**已經做的**：`docs/setup.md` 新增「安裝 Python」這一節（排在裝套件之前，新的步驟 1），
說明最穩妥做法是裝跟這台機器一樣的 3.9.x（pyenv-win 或官網固定版本網址），系統需求
表格也從模糊的「3.9+」改成明確的「3.9.x」並加警告框。

**2026-10-07 使用者明確決定：不照建議裝 3.9.x，新機器直接裝最新版 Python**，接受
「先裝裝看，卡住再修」的策略——明確說「你只要幫我提醒下一個就行，之後如果有甚麼不
一樣再來修」。已經把 `docs/setup.md` 的警告框跟步驟 1 都改成反映這個實際決定：不再
強制要求裝 3.9.x，改成告訴使用者卡住時的判斷方式跟修法方向。

**Why**：`requirements.txt` 的版本號是刻意釘死的（2026-09-14 的決定，見 CLAUDE.md），
目的是避免套件升級時默默壞掉，但這個決定的前提是「Python 版本也跟著固定」。使用者
知道這個取捨，選擇接受風險換取用最新 Python 的方便。

**How to apply**：下次（換新電腦）接續工作時，**第一件事是直接跑
`pip install -r requirements.txt` 看看有沒有裝不起來的套件**，不用先假設要裝 3.9——
如果有錯誤（通常會是 `ERROR: Could not find a version that satisfies the
requirement`），九成是 torch/bitsandbytes 這類有編譯後端的套件在新 Python 版本上沒
對應 wheel，這時要做的是：把 `docs/setup.md` 警告框裡講的「升級卡住的套件版本，然後
重新確認 transformers/peft/bitsandbytes 彼此版本相容」走一遍，更新
`requirements.txt` 裡對應的版本號，不是回頭要求裝 3.9。
