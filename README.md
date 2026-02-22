# Sharding-Fluentbit POC

多機台 Pod → Fluent Bit sidecar → Vector aggregator → PVC 的 Log 收集 POC。

## 架構

```
Pod (tool-001)                 Pod (tool-002)
┌──────────────────┐           ┌──────────────────┐
│ .NET App (NLog)  │           │ .NET App (NLog)  │
│  /var/log/app/   │           │  /var/log/app/   │
│       ↓          │           │       ↓          │
│  Fluent Bit      │           │  Fluent Bit      │
└──────┬───────────┘           └──────┬───────────┘
       │  forward (24224)             │
       └──────────────┬──────────────┘
                      ▼
              ┌───────────────┐
              │    Vector     │
              │ (Aggregator)  │
              └───────┬───────┘
                      ▼
              ┌───────────────┐
              │      PVC      │
              │ /logs/{toolid}│
              │  /{date}/     │
              └───────────────┘
```

## 前置需求

- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Docker](https://www.docker.com/)

## 快速開始

```bash
# 1. 建立 Kind cluster
make cluster-up

# 2. 部署 Vector aggregator
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

# 即時查看 Vector 日誌
make logs-vector

# 即時查看特定 tool 的 Fluent Bit 輸出
make logs-tool TOOL=tool-001

# 查看 PVC 內已寫入的檔案
make browse-pvc

# 清除所有 K8s 資源（保留 cluster）
make clean

# 刪除 cluster
make cluster-down
```

## PVC 目錄結構

```
/logs/
├── tool-001/
│   ├── 2024-01-15/
│   │   └── app.log
│   └── 2024-01-16/
│       └── app.log
└── tool-002/
    └── 2024-01-15/
        └── app.log
```

## 新增機台

複製 [test-app/deployment.yaml](test-app/deployment.yaml) 中的任一 Deployment 區塊，
修改 `metadata.name` 與所有 `toolid` label 值即可，無需更動 Fluent Bit 或 Vector 設定。

## 未來擴充

- [ ] CronJob：每日將 PVC 內容上傳 S3
- [ ] TLS：Fluent Bit → Vector 加密傳輸
- [ ] Vector HA：多 replica + 共享 RWX PVC
