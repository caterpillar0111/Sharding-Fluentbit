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

### 擴充 Vector 時

只需修改 SHARD_MAP，將部分 slot 範圍指向新的 vector，**已存在的 slot 範圍不動**，在線機台不需要重啟。

```
# 加入 vector-2 後的 SHARD_MAP 範例
slot 0~127   → vector-0  (不變)
slot 128~191 → vector-1  (縮小)
slot 192~255 → vector-2  (新增)
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
