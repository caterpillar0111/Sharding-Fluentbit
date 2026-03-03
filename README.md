# Sharding-Fluentbit POC

多機台 Pod → Fluent Bit sidecar → Vector aggregator (分片) → PVC 的 Log 收集 POC。

## 架構

```
Pod (tool-001)                 Pod (tool-002)
┌──────────────────┐           ┌──────────────────┐
│ .NET App (NLog)  │           │ .NET App (NLog)  │
│  /var/log/app/   │           │  /var/log/app/   │
│       ↓          │           │       ↓          │
│  Fluent Bit      │           │  Fluent Bit      │
│  (Lua sharding)  │           │  (Lua sharding)  │
└──────┬───────────┘           └──────┬───────────┘
       │ shard.0 (forward)            │ shard.1 (forward)
       ▼                              ▼
┌─────────────┐              ┌─────────────┐
│  vector-0   │              │  vector-1   │
│ (shard 0)   │              │ (shard 1)   │
└──────┬──────┘              └──────┬──────┘
       ▼                            ▼
┌─────────────┐              ┌─────────────┐
│   PVC-0     │              │   PVC-1     │
│ /logs/      │              │ /logs/      │
│  tool-001/  │              │  tool-002/  │
│   {date}/   │              │   {date}/   │
└─────────────┘              └─────────────┘
```

## Sharding 機制

Fluent Bit sidecar 內嵌 Lua script，對 `toolid` 做 FNV-32a hash，結果 mod 256 得到 slot (0~255)，再查 SHARD_MAP 決定送往哪個 Vector。

```
toolid → fnv32a(toolid) % 256 = slot → SHARD_MAP → vector-0 or vector-1
```

SHARD_MAP 定義在 [fluentbit-config/configmap.yaml](fluentbit-config/configmap.yaml)：

```
slot 0~127   → vector-0
slot 128~255 → vector-1
```

### SHARD_MAP 是什麼

SHARD_MAP 是 slot 到 vector 的對應表。slot 總數固定 256，每台機台的 toolid 經過 hash 後會對應到其中一個 slot，slot 再對應到某個 vector。

這樣設計的好處：直接用 `hash % vector數量` 的話，一旦增加 vector，幾乎所有機台都會重新分配，歷史 log 和新 log 會分散在不同 PVC。透過固定的 slot 層，增加 vector 時只需把部分 slot 範圍指向新 vector，其餘的完全不動。

### 擴充 Vector 時

只需修改 SHARD_MAP，將現有 shard 尾端的 slot 範圍切給新 vector。**已存在的 slot 範圍絕對不能改**，否則在線機台會被重新分配到不同 PVC，造成同一台機台的 log 散落在兩個地方。

```
# 目前（2 個 vector）
slot 0~127   → vector-0
slot 128~255 → vector-1

# 加入 vector-2 後
slot 0~127   → vector-0  (不變)
slot 128~191 → vector-1  (縮小，但原有機台不受影響)
slot 192~255 → vector-2  (新增，只有 slot 落在此範圍的新機台才會進來)
```

## 前置需求

- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Docker](https://www.docker.com/)

## 快速開始

```bash
# 1. 建立 Kind cluster
make cluster-up

# 2. 部署 Vector aggregator (vector-0, vector-1)
make deploy-vector

# 3. Build 測試 App image 並部署（含 Fluent Bit sidecar）
make deploy-apps

# 或一次完成所有步驟
make deploy-all
```

## 常用指令

```bash
# 查看所有 Pod 狀態
make status

# 即時查看 Vector 日誌（SHARD=0 或 1）
make logs-vector SHARD=0

# 即時查看特定 tool 的 Fluent Bit 輸出
make logs-tool TOOL=tool-001

# 查看特定 shard 的 PVC 內已寫入的檔案
make browse-pvc SHARD=0

# 清除所有 K8s 資源（保留 cluster）
make clean

# 刪除 cluster
make cluster-down
```

## PVC 目錄結構

各 shard 的 PVC 各自獨立：

```
PVC-0 (/logs/)              PVC-1 (/logs/)
├── tool-001/               ├── tool-002/
│   ├── 2026-02-23/         │   ├── 2026-02-23/
│   │   └── app.log         │   │   └── app.log
│   └── 2026-02-24/         │   └── 2026-02-24/
│       └── app.log         │       └── app.log
└── tool-003/               └── tool-004/
    └── ...                     └── ...
```

## 新增機台

複製 [test-app/deployment.yaml](test-app/deployment.yaml) 中的任一 Deployment 區塊，
修改 `metadata.name` 與所有 `toolid` label 值即可。

Fluent Bit 會自動根據 toolid 的 hash 決定送往哪個 Vector，**無需更動任何設定**。

## 調整 Sharding 規則（不重啟機台）

1. 修改 [fluentbit-config/configmap.yaml](fluentbit-config/configmap.yaml) 的 `SHARD_MAP`
2. Apply ConfigMap：
   ```bash
   kubectl apply -f fluentbit-config/configmap.yaml
   ```
3. 對各機台的 Fluent Bit 發送 SIGHUP（熱重載，不重啟 Pod）：
   ```bash
   kubectl exec <pod-name> -c fluent-bit -- kill -HUP 1
   ```

## Fluent Bit Buffer（Vector 斷線保護）

Fluent Bit 啟用 filesystem buffer，Vector 短暫打不通時 log 不會遺失。

### 機制說明

| 情況 | 結果 |
|---|---|
| Vector 短暫打不通 | chunk 在 memory 持續 retry（指數退避） |
| Vector 長時間打不通 | chunk overflow 到 `/tmp/flb-storage/` 磁碟 |
| Vector 恢復後 | 積壓的 chunk 全部補送，不遺失 |
| Fluent Bit container 重啟 | tail position DB 保留，重啟後從上次位置繼續讀 |
| Pod 被刪除重建 | buffer 隨 pod 消失（需掛 PVC 才能保留） |

### 監控積壓狀況（HTTP API）

Fluent Bit 內建 HTTP monitoring server，透過 port-forward 查詢：

```bash
# port-forward 到本機
kubectl port-forward pod/<tool-pod-name> 2020:2020

# 查詢 storage 狀態
curl http://localhost:2020/api/v1/storage
```

重要欄位：
- `shard_router.chunks.busy` — 正在 retry 中的 chunk 數
- `shard_router.chunks.total` — 積壓中的 chunk 總數
- `shard_router.chunks.down` — 已 overflow 到磁碟的 chunk 數

Vector 正常時 `busy=0`；Vector 打不通時 `busy` 數量會持續增加。

### 手動測試斷線補送

```bash
# 1. 確認所有 pod 正常
kubectl get pods

# 2. 模擬 vector-0 下線
kubectl scale deployment/vector-0 --replicas=0

# 3. 觀察 Fluent Bit 開始 retry（另開 terminal）
kubectl logs -f <tool-001-pod> -c fluent-bit | grep -E "retry|chunk"

# 4. 查詢積壓狀況（另開 terminal）
kubectl port-forward pod/<tool-001-pod> 2020:2020
curl http://localhost:2020/api/v1/storage

# 5. 恢復 vector-0
kubectl scale deployment/vector-0 --replicas=1

# 6. 確認補送完成（busy 歸零）
curl http://localhost:2020/api/v1/storage

# 7. 確認 PVC 資料完整
kubectl exec <vector-0-pod> -c vector -- wc -l /logs/tool-001/$(date +%Y-%m-%d)/app.log
```

## 未來擴充

- [ ] CronJob：每日將 PVC 內容上傳 S3 後清理本地
- [ ] Log Reader API：sidecar 掛在 Vector pod 旁提供 HTTP 查詢介面
- [ ] TLS：Fluent Bit → Vector 加密傳輸
- [ ] 監控 PVC 用量：Prometheus alert 在快滿時通知
