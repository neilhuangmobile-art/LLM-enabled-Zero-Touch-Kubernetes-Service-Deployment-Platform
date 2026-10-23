# ZeroTouch K8s User Guide

ZeroTouch K8s is a web assistant for learning Kubernetes, deploying workloads, inspecting cluster state, and recovering unhealthy Pods. Users can operate it through the Chat page or through the dedicated sidebar tools.

## Chat

Use Chat for natural-language operations and questions. The assistant can answer how to use this system, explain Kubernetes concepts, list Pods and Deployments, scale workloads, update images, roll back apps, and dispatch deployments.

Useful chat examples:

- `list pods`
- `show deployments`
- `deploy 3 nginx:latest pods for web-frontend, port 80`
- `spin up 4 node:20-alpine pods for api-gateway, port 3000`
- `scale api-gateway to 5`
- `update api-gateway to node:22-alpine`
- `rollback api-gateway`
- `How do I debug CrashLoopBackOff?`
- `How do I use Healer?`

## Deploying from Chat

There is no separate "Deploy Console" in the sidebar anymore (removed 2026-09-13 for beginner-friendliness — one entry point, not two). Deploy directly from the Chat page: type a request that includes the image, replica count, app name, and optional port or memory limit. The system parses the request, shows a confirmation card, computes resource usage, runs Guardian validation and agent review, optionally runs kubectl dry-run, then creates the resources when you confirm and Kubernetes is connected.

Recommended format:

`deploy <replicas> <image> pods for <app-name>, port <port>`

Examples:

- `deploy 3 nginx:latest pods for web-frontend, port 80`
- `deploy 2 redis:latest pods for cache-service, port 6379`
- `spin up 4 node:20-alpine pods for api-gateway, port 3000`
- `幫我部署 5 個 redis pods 給 cache-service port 6379`

After deployment, use Pods or Deployments pages to inspect the result.

## Pods Page

The Pods page lists Pods in the default namespace. It shows Pod name, app label, phase, IP, node, restart count, age, and available actions. Use it to confirm whether a deployment actually created running Pods.

Chat shortcut: `list pods`.

## Deployments Page

The Deployments page lists Kubernetes Deployments. It shows app name, image, desired replicas, ready replicas, age, and actions such as delete. Use it to check whether a rollout is ready or stuck.

Chat shortcut: `show deployments`.

## Healer

Healer scans the default namespace for abnormal Pods such as CrashLoopBackOff, OOMKilled, ImagePullBackOff, ErrImagePull, and Error. It helps recover apps by deleting unhealthy Pods so the owning ReplicaSet or Deployment can recreate them.

How to use Healer:

1. Click `Healer` in the left sidebar.
2. Click `Scan Now` to detect abnormal Pods.
3. Review the issue list and status reason.
4. Click a single fix action for one Pod, or `Auto Fix All` to delete all detected unhealthy Pods.
5. Return to Pods or Deployments to confirm the new Pods become Running.

Important: Healer does not fix a bad image tag, bad environment variable, missing Secret, or broken application code by itself. It restarts/recreates Pods. If the root cause remains, the Pod may enter the same failure state again.

## GitOps Log

GitOps Log shows deployment history from git commits. Use it to audit what was deployed and identify versions for rollback. Click `Refresh` to reload recent deployment commits.

Chat shortcuts:

- `rollback <app-name>` rolls back an app when rollback support is available.
- `show deployments` helps identify the app name before rollback.

## Metrics

Metrics shows Prometheus observability status. It reports whether Prometheus is connected, the running Pod count, the endpoint URL, and quick PromQL references. Use it to check cluster health and workload counts.

If Prometheus is offline, start or expose Prometheus first. The UI suggests a port-forward command when it cannot connect.

## Dataset Manager and Knowledge Base (developer tools, not in the sidebar)

Dataset Manager (enriches training data) and Knowledge Base (manages the RAG index) are internal developer tools, not end-user features. They were removed from the sidebar on 2026-09-13 so regular users are not confused by ML/training-data jargon they never need. Their backend routes (`/api/dataset/*`, `/api/rag/*`) and pages still exist and can be reached directly by URL if a developer needs them.

## Safe Deployment Behavior

For deployment requests, the system prioritizes exact fields: replica count, image, app name, port, and memory. It should not deploy when a request is not a Kubernetes task. Guardian, agent review, and dry-run are used to reduce invalid manifests and unsafe operations.

If a user wants to learn the system, answer with page names and concrete steps. If a user wants to deploy Pods, ask for missing required fields only when needed: image, replica count, app name, and port.

## 分時擴縮與休眠（Scheduled Scaling and Idle Sleep）

ZeroTouch 可以照時段自動調整副本數，也可以讓太久沒人用的服務自動休眠，省下 CPU 和記憶體。連接客戶自己叢集的帳號也可以用分時擴縮：時段切換時平台把副本數寫進你的部署設定，你的叢集同步後（通常 1 分鐘內）生效；但休眠、自動喚醒和例外清單目前只支援平台自己的叢集，因為你的叢集回報的資料裡沒有網路流量，平台判斷不出有沒有人在用。

分時擴縮（scheduled scaling）：在對話輸入「shop-web 白天 3 個、晚上 1 個」「shop-web 9 點到 18 點 3 個，其他時間 1 個」「shop-web 平日 2 個，週末 1 個」。系統用固定規則整理成時段表（白天是 08:00 到 20:00，晚上是 20:00 到隔天 08:00），確認卡會顯示每月比整天開滿省多少（依公有雲牌價估算），按「套用」後生效。平台只在時段切換的那一刻調整副本數，中間手動改的數字會保留到下一個時段。取消：「取消 shop-web 的排程」。

休眠（idle sleep）：服務連續 5 天（可以改成 1 到 7 天，例如說「閒置 3 天就休眠」；監控只保留約 10 天的流量紀錄，所以最多 7 天）沒有人連線，平台會把副本數降到 0。判斷依據是 Prometheus 記錄的實際網路流量，健康檢查本身的流量不算；監控資料不夠久時不會休眠。

自動喚醒（auto wake）：休眠中的服務有人連進來時，平台會先回一頁「服務正在喚醒」，通常幾秒到 1 分鐘內恢復休眠前的副本數。也可以在對話說「喚醒 shop-web」，或在「休眠設定」模組按「喚醒」。不需要另外安裝 KEDA。

例外清單（exemption list）：「shop-web 不要休眠」把服務加入例外，它永遠不會因為沒人用而休眠；「shop-web 可以休眠」移出例外。

查看狀態：對話輸入「休眠設定」，會列出每個服務是否休眠中、排程、下一次什麼時候變、是否在例外清單，並可以直接按按鈕喚醒、加入或移出例外、取消排程。分析報告也會記錄每次休眠、喚醒、依排程擴縮。

## 專案（Projects）

專案用來把同一件事的對話、服務和上傳的檔案整理在一起，例如「網路商店」專案放 shop-web、shop-db。專案只有自己看得到，不能分享給別人。

建立專案：左側選單「專案」右邊的 + 號，輸入名稱（說明可以不填）。點專案名稱進入專案頁，有四個分頁：
總覽（這個專案的對話、說明、專案指示）、服務（專案裡的服務和狀態，可以把現有的服務移進來或移出去）、檔案（在專案裡上傳的 docker-compose 或 Kubernetes YAML 原始檔，可以下載）、設定（改名、刪除專案）。

在專案裡開對話：專案頁右上的「在『專案名稱』開始新對話」。在這則對話裡部署的服務會自動歸到這個專案。一個服務只能屬於一個專案。已經有的對話可以在專案頁「總覽」下面的「把現有的對話移進這個專案」移進來，也可以按「移出專案」變回一般對話；移動對話不會移動它部署過的服務。

專案指示（project instructions）：寫這個專案的偏好，例如「web 至少開 2 個副本」。AI 回答這個專案裡的問題時會參考，但部署時不會自動改設定，還是以部署單上確認的內容為準。

部署預設值（deploy defaults）：在專案頁「總覽」設定「至少幾個副本」「每個副本的記憶體」「每個副本的 CPU」。在這個專案的對話裡部署時，平台會先套用到部署單：句子沒講到的記憶體、CPU 才填，副本數比「至少」少就調高，並告訴你套用了什麼；你還可以在部署單上再改，之後照常審查。上傳 docker-compose 或 Kubernetes YAML 的服務有自己的設定，不套用預設值。

摘要分頁：近 7 天這個專案的服務部署了幾次、擴縮或換版本幾次、自動修復幾次（需要你處理幾次）、審查擋下幾次，以及最近的動態和分時擴縮排程；數字都是平台紀錄算出來的，只算這個專案裡的服務。排程分頁：為專案裡的服務設定、修改、取消分時擴縮，或按「建議時段」從流量取得建議。

刪除專案：設定分頁的「刪除專案」。可以勾選要一起刪掉的部分（整個專案、對話、服務、檔案、專案指示），沒勾的部分選擇「移出專案」（對話變成一般對話、服務照常運作）或「保留在專案中」。勾選服務會真的刪除正在運行的服務，要輸入專案名稱確認。部署紀錄一律保留。

對話紀錄存在平台的伺服器上，換一個瀏覽器或電腦登入同一個帳號也看得到。

不用打字也可以設定：到「我的服務」頁的「分時與休眠」，每個服務旁邊有「設定排程」（已有排程時是「修改排程」），輸入「白天 3 個、晚上 1 個」或點範例，按「預覽」確認時段表和省多少錢，再按「套用」。同一塊也可以喚醒、加入例外清單、取消排程，右上角可以改「閒置幾天就休眠」（1 到 7 天）。

部署時如果句子裡也講了時段（例如「shop-web 平日 2 個、週末 1 個，幫我部署」），平台會先部署，部署完成後提示你接著設定分時擴縮；分時擴縮只能設定在已經部署的服務上。

通知：手機開啟通知後，平台依排程調整副本數、因為叢集空間不夠沒有調成、服務休眠、有人連線自動喚醒時，都會推播到手機；這些也會出現在行事曆。設定排程時按的那一次「套用」就是你的確認，之後平台會照時段自動調整。手機監控版的服務列表會標出「⏱ 分時擴縮」「💤 休眠中」，點進服務可以看到時段表、下次什麼時候變、休眠狀態；手機只能看，不能修改。

建議時段（suggested schedule）：平台每小時會記錄每個服務收到的流量。累積滿 7 天後，可以在「我的服務」的分時與休眠按「建議時段」，或在對話說「建議 shop-web 的時段」，平台會整理出忙碌時段（例如平日 9 點到 18 點），建議忙碌時段開現在的副本數、其他時間 1 個，並換算每月省多少。這只是建議，確認時段表沒問題再按「套用」。資料還不夠時會告訴你還要幾天；流量一直都差不多時不會建議分時。連接客戶自己叢集的帳號沒有流量資料，不能用這個功能。

## 回收桶（Trash）

刪除的對話和專案會先放進左側選單的「回收桶」，保留 7 天，期間可以按「復原」拿回來，7 天後會自動永久刪除；也可以在回收桶按「永久刪除」馬上刪掉。
復原對話時，原本的專案還在就回到那個專案；復原專案時，說明、專案指示、部署預設值、上傳的檔案、跟專案一起刪掉的對話都會回來，名稱跟現有專案重複時會加上「（復原）」。
刪掉的服務不會進回收桶：服務是真的從叢集刪掉，需要的話要重新部署。沒有任何訊息的空白對話不會進回收桶。

## 每週自動分析報告（Weekly report）

平台每週一早上 9 點會自動產生你過去 7 天的分析報告（部署、擴縮、審查、自動修復的次數與最近的動態），手機有開通知的話會收到推播；打開網站時，對話頁上方會出現「上週的分析報告準備好了」，按「查看」就能看到完整報告。
過去 7 天沒有任何紀錄時不會產生空報告。不想要的話按「以後不要自動產生」，或在對話說「每週報告不要自動產生」；要恢復就說「每週報告要自動產生」。隨時也可以直接說「這週的分析報告」。

## 依你的習慣建議（Personalized suggestions）

部署時如果句子沒講記憶體、CPU，平台會依你過去的部署習慣預填建議值（部署 3 次以上，句子沒講副本數時也會建議副本數），並說明依據，例如「依你過去 6 次部署的習慣」。
剛開始還沒有足夠紀錄時，會參考公開專案統計出的常見設定。建議只是預填在部署單上，你可以修改，之後照常審查。
在專案裡部署時，專案的部署預設值優先；你在句子裡講的數字永遠最優先。不想要的話說「不要依我的習慣建議」，要恢復就說「依我的習慣建議」。

