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
           ▲                           ▲
           └──────────┬────────────────┘
              ┌───────┴────────┐
              │   Aggregator   │
              │  :8090 /logs   │
              │  /logs/all     │
              └────────────────┘
```

## Sharding 機制

Fluent Bit sidecar 執行 [fluentbit-config/shard.lua](fluentbit-config/shard.lua)，對 `toolid` 做 FNV-32a hash，結果 mod 256 得到 slot (0~255)，再依 SHARD_MAP 決定送往哪個 Vector。

```
toolid → fnv32a(toolid) % 256 = slot → SHARD_MAP → vector-0 or vector-1
```

### SHARD_MAP 是什麼

SHARD_MAP 是 slot 到 vector 的對應表。slot 總數固定 256，每台機台的 toolid 經過 hash 後會對應到其中一個 slot，slot 再對應到某個 vector。

```lua
-- fluentbit-config/shard.lua
local SHARD_MAP = {
    {from=0,   to=127, shard="0"},
    {from=128, to=255, shard="1"},
}
```

scale-up 時直接修改 SHARD_MAP 並 apply configmap。同一 toolid 在切換前後可能分散在不同 shard，查詢時由 aggregator 自動 merge，不影響資料完整性。

## 部署架構

```
namespace: ea-tapinfra（或其他 infra namespace）
├── StatefulSet: vector  (replicas = shards)
│   ├── container: vector         (log 接收與寫入 PVC)
│   └── container: log-reader     (sidecar，Flask，提供 HTTP 查詢 API :8080)
├── Deployment: aggregator        (Flask，統一查詢入口 :8090)
├── Service: vector-0, vector-1, ...  (per-shard，含 port 8080)
├── Service: vector-headless  (StatefulSet 必要)
├── Service: aggregator  (port 8090)
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
- Python 3.12+（僅本機開發用，container 內已打包）

## 快速開始

```bash
# 1. 建立 Kind cluster
make cluster-up

# 2. 部署 Vector（Helm，部署至 ea-tapinfra namespace）
make deploy-vector

# 3. Build 測試 App image 並部署（含 Fluent Bit sidecar）
make deploy-apps

# 4. Build + 部署 log-reader（Python/Flask sidecar）
make deploy-log-reader

# 5. Build + 部署 Aggregator
make deploy-aggregator

# 或一次完成 Vector + Apps
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
| `storage.size` | `10Gi` | 每個 shard 的 PVC 大小 |
| `networkPolicy.enabled` | `true` | 是否啟用 NetworkPolicy |
| `logReader.enabled` | `true` | 是否啟用 log-reader sidecar |
| `logReader.port` | `8080` | log-reader HTTP port |

**開通某 namespace 的 FLB 連線：**

```bash
kubectl label namespace <namespace> vector-access=true
```

## Log Reader API

每個 Vector pod 內含 `log-reader` sidecar（Python/Flask），提供 HTTP 查詢介面。

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

GET /logs/shard/index?date={date}
  回傳本 shard 在指定日期有 log 的 toolid 清單（僅 directory listing，不讀檔案內容）

Response 200:
{
  "shard": "0",
  "date": "2026-04-09",
  "toolids": ["tool-001", "tool-003"]
}

Response 400: { "error": "toolid is required" }
Response 404: { "error": "log not found" }
```

### 路由邏輯

1. 任一 vector pod 都可以接收查詢
2. log-reader 對 `toolid` 做 FNV-32a hash，計算應在哪個 shard
3. 若 log 在本機 PVC → 直接讀取返回
4. 若 log 在其他 shard → HTTP proxy 到對應的 `vector-N` pod
5. 若找不到（hash mismatch）→ fallback 掃描所有 shard 的本機 PVC

> **Note on hash mismatch:** FLB Lua 的 FNV-32a 有 float64 精度 bug (`bit.tobit(h*16777619)`)，
> 導致部分 toolid 被送到與 log-reader 計算結果不同的 shard。
> log-reader 以 fallback 機制（`direct=true`）自動掃描其他 shard 補救，查詢結果仍正確。

### 更新 log-reader image

```bash
# 修改 log-reader/main.py 後
make deploy-log-reader

# deploy-log-reader 已包含 rollout restart，kind 環境不需額外操作
```

## Aggregator API

Aggregator 是一個獨立的 Flask service，提供**跨 shard 統一查詢**入口，適合下游需要批次拉取所有 toolid log 的場景。

### API 規格

```
GET /logs?toolid={toolid}&date={date}
  date 可選，預設今日（UTC）
  平行掃所有 shard index → 找到 toolid 所在 shard → 拉取 content → merge

Response 200:
{
  "toolid": "tool-001",
  "date": "2026-04-09",
  "shards": [0],
  "lines": ["line1", "line2", ...],
  "total_lines": 231
}

GET /logs/all?date={date}
  回傳指定日期所有 toolid 清單（不含內容），供下游規劃 bulk 查詢

Response 200:
{
  "date": "2026-04-09",
  "total_tools": 42,
  "toolids": ["tool-001", "tool-002", ...]
}
```

### Cache 策略

| 日期 | Cache 行為 |
|---|---|
| 過去日期 | 永久 cache（資料不會再變動） |
| 今天 | 每次重新掃所有 shard index（避免 stale） |

### 查詢指令

```bash
# 透過 Aggregator 查詢（自動 port-forward）
make query-log TOOL=tool-001
make query-log TOOL=tool-002

# 手動 port-forward
kubectl port-forward -n ea-tapinfra svc/aggregator 8090:8090

curl "http://localhost:8090/logs?toolid=tool-001"
curl "http://localhost:8090/logs/all"
curl "http://localhost:8090/logs/all?date=2026-04-08"

# 直接對 log-reader 查詢（單 shard）
kubectl port-forward -n ea-tapinfra vector-0 8080:8080
curl "http://localhost:8080/logs?toolid=tool-001"
curl "http://localhost:8080/logs/shard/index"
```

## Shard Scale-up 流程

### 步驟（以 2 → 3 shards 為例）

```bash
# 1. 新增 vector-2 pod / PVC / Service
helm upgrade vector ./helm/vector --set shards=3

# 2. 修改 fluentbit-config/shard.lua 的 SHARD_MAP（重新均分或追加 shard）
#    同步修改 helm/vector/values.yaml 的 shardMap

# 3. 套用 FLB ConfigMap
kubectl apply -f fluentbit-config/configmap.yaml

# 4. 更新 log-reader SHARD_MAP + aggregator SHARDS 數量
make deploy-log-reader
kubectl set env deployment/aggregator SHARDS=3 -n ea-tapinfra
```

**注意：** FLB pod 使用 subPath mount，ConfigMap 更新不會自動生效，需等 pod 自然重啟（crash、維護）才會吃到新路由。重啟前後同一 toolid 的 log 可能落在不同 shard，aggregator 查詢時會自動 merge，資料不遺失。

## 常用指令

```bash
# 查看所有 Pod 狀態（含 ea-tapinfra）
make status

# 即時查看 Vector 日誌（SHARD=0 或 1）
make logs-vector SHARD=0

# 即時查看特定 tool 的 Fluent Bit 輸出
make logs-tool TOOL=tool-001

# 查詢 log（透過 Aggregator，自動路由 + merge）
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
- [ ] Aggregator：支援 scale-up 時同一 toolid 跨 shard merge 後再送 S3
