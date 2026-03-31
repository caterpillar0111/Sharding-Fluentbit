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
│  (shard.lua)     │           │  (shard.lua)     │
└──────┬───────────┘           └──────┬───────────┘
       │ shard.0 (forward)            │ shard.1 (forward)
       ▼                              ▼
┌──────────────────────┐    ┌──────────────────────┐
│  vector-0 (shard 0)  │    │  vector-1 (shard 1)  │
│  ─────────────────   │    │  ─────────────────   │
│  Vector container    │    │  Vector container    │
│  log-reader sidecar  │    │  log-reader sidecar  │
│  :8080 /logs API     │    │  :8080 /logs API     │
└──────────┬───────────┘    └──────────┬───────────┘
           ▼                           ▼
    ┌─────────────┐             ┌─────────────┐
    │   PVC-0     │             │   PVC-1     │
    │ /logs/      │             │ /logs/      │
    │  tool-001/  │             │  tool-002/  │
    │   {date}/   │             │   {date}/   │
    └─────────────┘             └─────────────┘
```

## Sharding 機制

Fluent Bit sidecar 執行 [fluentbit-config/shard.lua](fluentbit-config/shard.lua)，對 `toolid` 做 FNV-32a hash，結果 mod 256 得到 slot (0~255)，再依 log 的**日期**查對應版本的 SHARD_MAP 決定送往哪個 Vector。

```
toolid → fnv32a(toolid) % 256 = slot
log date → 查 SHARD_MAP_VERSIONS → 選對應版本的 SHARD_MAP
slot → SHARD_MAP → vector-0 or vector-1
```

### SHARD_MAP 是什麼

SHARD_MAP 是 slot 到 vector 的對應表。slot 總數固定 256，每台機台的 toolid 經過 hash 後會對應到其中一個 slot，slot 再對應到某個 vector。

### SHARD_MAP_VERSIONS（日期版本化）

路由規則採用**日期版本化**設計，讓 scale-up 可以在跨日時自動切換，不需要停機：

```lua
-- fluentbit-config/shard.lua
local SHARD_MAP_VERSIONS = {
    {
        effective = "2026-04-01",   -- 從這天起的 log 走新規則
        map = {{from=0,to=84,shard="0"},{from=85,to=170,shard="1"},{from=171,to=255,shard="2"}}
    },
    {
        effective = "2000-01-01",   -- 初始版本（永遠是 fallback）
        map = {{from=0,to=127,shard="0"},{from=128,to=255,shard="1"}}
    },
}
```

FLB 用 log 的 timestamp date 查表，找到第一個 `date >= effective` 的版本。**同一日期的資料永遠落在同一個 PVC**，S3 搬移不會有跨 PVC 碎片問題。

## 部署架構

```
namespace: ea-tapinfra（或其他 infra namespace）
├── StatefulSet: vector  (replicas = shards)
│   ├── container: vector         (log 接收與寫入 PVC)
│   └── container: log-reader     (sidecar，提供 HTTP 查詢 API :8080)
├── Service: vector-0, vector-1, ...  (per-shard，含 port 8080)
├── Service: vector-headless  (StatefulSet 必要)
├── PVC: log-storage-vector-0, log-storage-vector-1, ...  (自動建立)
└── NetworkPolicy: 限定有 vector-access=true label 的 namespace 才能連入

namespace: <各機台 namespace>
└── ConfigMap: fluentbit-config
        script /fluent-bit/etc/shard.lua  (SHARD_MAP_VERSIONS)
        Host vector-0.ea-tapinfra.svc.cluster.local
        Host vector-1.ea-tapinfra.svc.cluster.local
```

Vector 以 **StatefulSet** 部署，每個 shard 有獨立 PVC，pod 重啟或 rolling upgrade 時資料不遺失。

## 前置需求

- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [helm](https://helm.sh/docs/intro/install/)
- [Docker](https://www.docker.com/)

## 快速開始

```bash
# 1. 建立 Kind cluster
make cluster-up

# 2. 部署 Vector（Helm，部署至 ea-tapinfra namespace）
make deploy-vector

# 3. Build 測試 App image 並部署（含 Fluent Bit sidecar）
make deploy-apps

# 或一次完成所有步驟
make deploy-all
```

`deploy-vector` 會自動：
- 建立 `ea-tapinfra` namespace（已存在則略過）
- 將 `default` namespace 加上 `vector-access=true` label（允許 FLB 連入）
- 以 Helm 安裝/升級 Vector StatefulSet

## Helm 設定

Vector 透過 [helm/vector/](helm/vector/) chart 管理，主要參數在 [helm/vector/values.yaml](helm/vector/values.yaml)：

| 參數 | 預設值 | 說明 |
|---|---|---|
| `namespace` | `ea-tapinfra` | 部署目標 namespace |
| `shards` | `2` | Vector shard 數量（= StatefulSet replicas = PVC 數量）|
| `shardMap` | 見 values.yaml | log-reader 路由表，與 shard.lua 最新版本同步 |
| `storage.size` | `10Gi` | 每個 shard 的 PVC 大小 |
| `networkPolicy.enabled` | `true` | 是否啟用 NetworkPolicy |
| `logReader.enabled` | `true` | 是否啟用 log-reader sidecar |
| `logReader.port` | `8080` | log-reader HTTP port |

**開通某 namespace 的 FLB 連線：**

```bash
kubectl label namespace <namespace> vector-access=true
```

## Log Reader API

每個 Vector pod 內含 `log-reader` sidecar，提供 HTTP 查詢介面，使用者只需提供 `toolid` 即可取得 log。

### API 規格

```
GET /logs?toolid={toolid}&date={date}
  date 可選，預設今日（UTC）

Response 200:
{
  "toolid": "tool-001",
  "date": "2026-03-31",
  "shard": "0",
  "lines": ["line1", "line2", ...],
  "total_lines": 231
}

Response 400: { "error": "toolid is required" }
Response 404: { "error": "log not found" }
```

### 路由邏輯

1. 任一 vector pod 都可以接收查詢
2. log-reader 對 `toolid` 做 FNV-32a hash，計算應在哪個 shard
3. 若 log 在本機 PVC → 直接讀取返回
4. 若 log 在其他 shard → HTTP proxy 到對應的 `vector-N` pod
5. 若找不到（hash mismatch）→ fallback 掃描所有 shard

### 查詢指令

```bash
# 用 Makefile（自動 port-forward + curl + 關閉）
make query-log TOOL=tool-001
make query-log TOOL=tool-002

# 手動 port-forward 後用 Postman 或 curl
kubectl port-forward -n ea-tapinfra vector-0 8080:8080

curl "http://localhost:8080/logs?toolid=tool-001"
curl "http://localhost:8080/logs?toolid=tool-001&date=2026-03-31"
```

### 更新 log-reader image

```bash
# 修改 log-reader/main.go 後
make deploy-log-reader

# kind 環境需額外 rollout restart（imagePullPolicy: Never 不自動拉新 image）
kubectl rollout restart statefulset/vector -n ea-tapinfra
```

## Shard Scale-up 流程（零停機）

利用 SHARD_MAP_VERSIONS 的日期版本化機制，在跨日時自動切換，無需停機。

### 步驟（以 2 → 3 shards 為例）

**事前準備（任意時間，隔日生效）：**

```bash
# 1. 新增 vector-2 pod / PVC / Service
helm upgrade vector ./helm/vector --set shards=3

# 2. 在 shard.lua 最前面插入新版本（effective = 明天日期）
#    同步更新 values.yaml 的 shardMap
# 編輯 fluentbit-config/configmap.yaml 和 helm/vector/values.yaml

# 3. 套用 FLB ConfigMap
kubectl apply -f fluentbit-config/configmap.yaml

# 4. FLB 熱重載（不重啟 Pod，buffer 不遺失）
kubectl get pods -l app=tool -o name | xargs -I{} \
  kubectl exec {} -c fluent-bit -- kill -HUP 1

# 5. 更新 log-reader SHARD_MAP
helm upgrade vector ./helm/vector
kubectl rollout restart statefulset/vector -n ea-tapinfra
```

**跨日後（無需手動操作）：**
- 新日期的 log 自動走新 SHARD_MAP → 進 vector-2
- 舊日期的 log 依舊走舊 SHARD_MAP → 在原 shard
- log-reader fallback 確保歷史查詢正常

### 為什麼用日期而不是停機切換

| | 停機換 MAP | 日期版本化 |
|---|---|---|
| 維護視窗 | 需要 | 不需要 |
| 同日期資料跨 PVC | 可能 | 不會 |
| 遲到的 log 路由正確 | 不一定 | ✅ |
| S3 搬移資料完整 | 需確認 | ✅ |

## 常用指令

```bash
# 查看所有 Pod 狀態（含 ea-tapinfra）
make status

# 即時查看 Vector 日誌（SHARD=0 或 1）
make logs-vector SHARD=0

# 即時查看特定 tool 的 Fluent Bit 輸出
make logs-tool TOOL=tool-001

# 查詢 log（自動路由到正確 shard）
make query-log TOOL=tool-001
make query-log TOOL=tool-002

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
│   ├── 2026-03-30/         │   ├── 2026-03-30/
│   │   └── app.log         │   │   └── app.log
│   └── 2026-03-31/         │   └── 2026-03-31/
│       └── app.log         │       └── app.log
└── tool-003/               └── tool-004/
    └── ...                     └── ...
```

## 新增機台

複製 [test-app/deployment.yaml](test-app/deployment.yaml) 中的任一 Deployment 區塊，
修改 `metadata.name` 與所有 `toolid` label 值即可。

Fluent Bit 會自動根據 toolid 的 hash 決定送往哪個 Vector，**無需更動任何設定**。

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

```bash
kubectl port-forward pod/<tool-pod-name> 2020:2020
curl http://localhost:2020/api/v1/storage
```

重要欄位：
- `shard_router.chunks.busy` — 正在 retry 中的 chunk 數
- `shard_router.chunks.down` — 已 overflow 到磁碟的 chunk 數

## 未來擴充

- [ ] CronJob：每日將 PVC 內容上傳 S3 後清理本地
- [ ] TLS：Fluent Bit → Vector 加密傳輸
- [ ] 監控 PVC 用量：Prometheus alert 在快滿時通知
- [ ] 修正 Lua FNV-32a hash bug（`bit.tobit(h*16777619)` → `bit.lshift` 實作），使 FLB 與 log-reader 使用相同 hash，消除 fallback 需求
