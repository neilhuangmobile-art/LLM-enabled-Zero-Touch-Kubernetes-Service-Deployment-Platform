# Kubernetes 新手小百科（給完全沒學過 K8s 的人看）

這份文件用最簡單的話解釋部署會用到的名詞，不用懂 kubectl、不用懂 YAML。
在這個系統裡，你只要用 Chat 打自然語言就能完成一切，這份文件是給你理解「系統剛剛幫我做了什麼」用的。

## 什麼是 Pod

Pod 是 Kubernetes 裡最小的部署單位，你可以把它想成「一個正在跑的小盒子，裡面裝著你的程式」。
你在 Chat 打「deploy 3 nginx pods」，意思就是「幫我開 3 個一樣的小盒子」。
Pod 裡面實際跑程式的東西叫 Container（容器），大部分情況一個 Pod 裡只有一個 Container。

## 什麼是 Deployment

Deployment 是一份「我想要的狀態」的宣告，例如「我要用 nginx:latest 這個版本、一直維持 3 個 Pod 在跑」。
你不會直接管 Pod，而是管 Deployment，Kubernetes 會自動照著 Deployment 的描述去建立、修復、維持 Pod 數量。
如果有 Pod 壞掉，Deployment 會自動生一個新的補上，這就是「自我修復」的基礎。

## 什麼是 ReplicaSet

ReplicaSet 是 Deployment 底下自動生出來、實際負責「數 Pod 數量夠不夠、不夠就補一個」的東西。
一般使用者不需要直接碰它，Deployment 已經幫你管好了，知道這個名字存在就好。

## 什麼是 Replicas / 副本數

副本數就是「要開幾個一模一樣的 Pod」。開多個的原因：
- 其中一個壞掉，其他還在撐著，服務不會整個掛掉
- 流量大的時候可以分攤給多個 Pod 處理

## 什麼是 Service

Pod 壞掉重建之後，內部的網路位址（IP）會換一個新的，直接記 Pod 的 IP 是不可靠的。
Service 是一個固定不變的「門牌號碼」，會自動幫你把流量轉發到背後那一群 Pod，不管它們的 IP 怎麼變。
部署的時候系統會自動幫你連 Deployment 跟 Service 一起建好，不用另外設定。

## 什麼是 Namespace（命名空間）

Namespace 是叢集裡用來分隔資源的「資料夾」，這個系統預設把所有東西放在叫 `default` 的 namespace 裡。
不同 namespace 的東西彼此看不到，通常用來區隔不同專案或不同環境（開發 / 測試 / 正式上線）。

## 什麼是 Image（映像檔）

Image 是打包好的「程式 + 執行環境」，例如 `nginx:latest`、`redis:7`。
冒號後面的部分叫 tag（版本標籤），代表這個 image 的版本；沒有寫的話系統預設抓 `latest`（最新版）。

## 什麼是 Port（連接埠）

Port 是這個服務對外接受連線用的門號，例如網頁伺服器常用 80，Redis 常用 6379。
部署時如果沒特別講，系統會用該程式常見的預設值。

## 什麼是 Node（節點）跟 Cluster（叢集）

Cluster（叢集）是整套 Kubernetes 系統，由一台或多台機器組成。
Node（節點）就是叢集裡的一台機器，Pod 實際上是被排進某一個 Node 裡執行的。
這個系統用 Docker Desktop 內建的 Kubernetes，所以「叢集」其實是 Docker 在你電腦裡模擬出來的節點群。

## 什麼是記憶體 / CPU 限制（Resource Requests / Limits）

部署時可以額外指定這個 Pod 最多能用多少記憶體或 CPU，例如 `256Mi`（記憶體）、`500m`（CPU）。
沒有設定的話 Kubernetes 不會限制它，一個 Pod 有可能把整台機器的資源吃光，影響到其他 Pod。

## Pod 常見的狀態是什麼意思

| 狀態 | 白話意思 |
|------|---------|
| Running | 正常執行中，沒事 |
| Pending | 還在排隊，可能還在等資源或還在下載 image |
| CrashLoopBackOff | 容器一直啟動失敗、一直重開、一直失敗，通常是程式本身有問題或設定錯了 |
| ImagePullBackOff / ErrImagePull | 抓不到指定的 image，通常是名字打錯、版本不存在，或是私有倉庫沒有權限 |
| OOMKilled | 用超過設定的記憶體上限，被系統強制關掉 |
| Restarts（重啟次數） | 這個 Pod 被重新啟動過幾次；次數高代表不太穩定，即使現在看起來正常也值得留意 |

## 什麼是 YAML / manifest

Kubernetes 內部其實是用 YAML 這種格式的設定檔描述「我要什麼樣的 Deployment、Service」。
這個系統的設計就是讓使用者完全不用碰 YAML：你講白話，模型把它轉成結構化的資料，
程式再用固定樣板把它轉成 YAML，這就是「零接觸」的意思——你從頭到尾都不用寫任何 YAML 或程式碼。

## 什麼是 kubectl

kubectl 是 Kubernetes 官方的命令列工具，工程師平常要操作叢集會打 `kubectl get pods` 這類指令。
這個系統的核心價值就是讓你完全不需要學 kubectl，用 Chat 打白話就能做到一樣的事。

## 什麼是 GitOps

GitOps 是「把每次部署的設定都記錄成 git 版本」的做法，這樣可以清楚看到部署歷史、也方便回滾到舊版本。
這個系統每次部署都會自動幫你做這件事，你可以在 GitOps Log 頁面看到部署紀錄。

## 什麼是 Rollback（回滾）

如果新版本出問題，回滾就是「退回到上一個還正常的版本」。
在這個系統裡，直接在 Chat 打「rollback <應用程式名稱>」就會幫你處理，不需要手動操作。
