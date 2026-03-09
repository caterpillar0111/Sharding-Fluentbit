package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type shardEntry struct {
	From  int    `json:"from"`
	To    int    `json:"to"`
	Shard string `json:"shard"`
}

type logResponse struct {
	ToolID     string   `json:"toolid"`
	Date       string   `json:"date"`
	Shard      string   `json:"shard"`
	Lines      []string `json:"lines"`
	TotalLines int      `json:"total_lines"`
}

type errorResponse struct {
	Error string `json:"error"`
}

var (
	myShard   string
	namespace string
	logDir    string
	port      string
	shardMap  []shardEntry
)

func main() {
	// Parse shard identity from pod name (e.g. "vector-0" → "0")
	podName := os.Getenv("MY_POD_NAME")
	parts := strings.Split(podName, "-")
	myShard = parts[len(parts)-1]

	namespace = getEnv("NAMESPACE", "ea-tapinfra")
	logDir = getEnv("LOG_DIR", "/logs")
	port = getEnv("PORT", "8080")

	// Parse SHARD_MAP from env (JSON array)
	shardMapJSON := os.Getenv("SHARD_MAP")
	if err := json.Unmarshal([]byte(shardMapJSON), &shardMap); err != nil {
		log.Fatalf("failed to parse SHARD_MAP: %v", err)
	}

	http.HandleFunc("/logs", handleLogs)
	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	log.Printf("log-reader starting: shard=%s namespace=%s port=%s", myShard, namespace, port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatal(err)
	}
}

func handleLogs(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	toolid := r.URL.Query().Get("toolid")
	if toolid == "" {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(errorResponse{Error: "toolid is required"})
		return
	}

	date := r.URL.Query().Get("date")
	if date == "" {
		date = time.Now().UTC().Format("2006-01-02")
	}

	// direct=true: skip routing, read local PVC only (used by fallback proxy calls)
	direct := r.URL.Query().Get("direct") == "true"

	if !direct {
		targetShard := computeShard(toolid)
		if targetShard != myShard {
			proxyToShard(w, r, targetShard, toolid, date)
			return
		}
	}

	// Try local read
	lines, err := readLocalFile(toolid, date)
	if err != nil && os.IsNotExist(err) && !direct {
		// Hash mismatch fallback: FLB Lua uses a different hash than Go's standard FNV-32a.
		// Ask each other shard to read from its own local PVC directly.
		for _, e := range shardMap {
			if e.Shard == myShard {
				continue
			}
			if proxyFallback(w, r, e.Shard, toolid, date) {
				return
			}
		}
		w.WriteHeader(http.StatusNotFound)
		json.NewEncoder(w).Encode(errorResponse{Error: "log not found"})
		return
	}
	if err != nil {
		if os.IsNotExist(err) {
			w.WriteHeader(http.StatusNotFound)
			json.NewEncoder(w).Encode(errorResponse{Error: "log not found"})
		} else {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(errorResponse{Error: err.Error()})
		}
		return
	}

	json.NewEncoder(w).Encode(logResponse{
		ToolID:     toolid,
		Date:       date,
		Shard:      myShard,
		Lines:      lines,
		TotalLines: len(lines),
	})
}

func computeShard(toolid string) string {
	h := fnv.New32a()
	h.Write([]byte(toolid))
	slot := int(h.Sum32() % 256)

	for _, e := range shardMap {
		if slot >= e.From && slot <= e.To {
			return e.Shard
		}
	}
	return "0"
}

func readLocalFile(toolid, date string) ([]string, error) {
	path := filepath.Join(logDir, toolid, date, "app.log")
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var lines []string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		lines = append(lines, scanner.Text())
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return lines, nil
}

func proxyToShard(w http.ResponseWriter, r *http.Request, shard, toolid, date string) {
	target := fmt.Sprintf("http://vector-%s.%s.svc.cluster.local:%s/logs?toolid=%s&date=%s",
		shard, namespace, port, toolid, date)

	resp, err := http.Get(target)
	if err != nil {
		w.WriteHeader(http.StatusBadGateway)
		json.NewEncoder(w).Encode(errorResponse{Error: fmt.Sprintf("proxy error: %v", err)})
		return
	}
	defer resp.Body.Close()

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}

// proxyFallback tries a shard and writes the response if found (HTTP 200).
// Returns true if the file was found and response written.
func proxyFallback(w http.ResponseWriter, r *http.Request, shard, toolid, date string) bool {
	// direct=true tells the target shard to read its local PVC without routing
	target := fmt.Sprintf("http://vector-%s.%s.svc.cluster.local:%s/logs?toolid=%s&date=%s&direct=true",
		shard, namespace, port, toolid, date)

	resp, err := http.Get(target)
	if err != nil || resp.StatusCode != http.StatusOK {
		if resp != nil {
			resp.Body.Close()
		}
		return false
	}
	defer resp.Body.Close()

	w.WriteHeader(http.StatusOK)
	io.Copy(w, resp.Body)
	return true
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
