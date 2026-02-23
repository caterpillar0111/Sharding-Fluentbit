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

## 未來擴充

- [ ] CronJob：每日將 PVC 內容上傳 S3 後清理本地
- [ ] Log Reader API：sidecar 掛在 Vector pod 旁提供 HTTP 查詢介面
- [ ] TLS：Fluent Bit → Vector 加密傳輸
- [ ] 監控 PVC 用量：Prometheus alert 在快滿時通知
