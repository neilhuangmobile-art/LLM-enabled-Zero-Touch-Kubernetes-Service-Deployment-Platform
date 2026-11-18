# 知識庫資料來源與授權

本資料夾的內容依來源分為以下幾類。程式碼的授權不受下列資料授權影響。

| 檔案 | 來源 | 授權 | 說明 |
|---|---|---|---|
| `catalog_seed.yaml` | 本專案人工撰寫 | 同本專案 | 情境描述、能否單一容器運作、建議資源、注意事項 |
| `catalog_ext.yaml` | [awesome-selfhosted-data](https://github.com/awesome-selfhosted/awesome-selfhosted-data)（軟體名稱、英文簡介、分類、原始碼網址）；[docker-library/official-images](https://github.com/docker-library/official-images)（Apache 2.0）、[docker-library/docs](https://github.com/docker-library/docs)（MIT） | **CC BY-SA 3.0**（改編作品，沿用原授權） | 由 `rag/expand_catalog.py` 篩選並改寫：中文情境描述與類別由 Gemini 依英文簡介產生，標記 `generated: true`，尚未逐筆人工確認 |
| `catalog.json` | 上面兩份合併，加上 Docker Hub 映像檔設定（連接埠、資料目錄）與 docker-library/docs 的環境變數名稱 | 含 `catalog_ext.yaml` 的部分為 CC BY-SA 3.0 | 由 `rag/build_catalog.py` 產生，每筆的 `sources` 欄位列出來源 |
| `k8s_official/*.md` | [kubernetes/website](https://github.com/kubernetes/website) | CC BY 4.0 | 原文節錄，已移除網站排版語法；每個檔案開頭標示原始網址。由 `scripts/rag/fetch_k8s_docs.py` 下載 |
| `troubleshooting.yaml` | 本專案撰寫，依 Kubernetes 官方文件與 Healer 修復紀錄 | 同本專案 | T14 之後的條目在 `source` 欄位列出對照的官方文件 |
| `requirements.yaml`、`lessons.yaml` | 本專案撰寫 | 同本專案 | 失敗經驗來自本專案的測試紀錄，不含測試題原句 |
| `examples.jsonl` | 由 Gemini 依軟體目錄改寫的新手說法 | 同本專案 | 已用防汙染測試確認不與開發集、盲測集重疊 |

## CC BY-SA 3.0 標示

`catalog_ext.yaml` 以及 `catalog.json` 中來自它的條目，改編自 awesome-selfhosted-data
（作者名單見其 AUTHORS 檔），依 [Creative Commons Attribution-ShareAlike 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/) 釋出。
修改內容：篩選仍在維護、有 Docker 版、星數 2,000 以上的軟體；對應 Docker Hub 映像檔；加上中文情境描述、類別與建議資源。
