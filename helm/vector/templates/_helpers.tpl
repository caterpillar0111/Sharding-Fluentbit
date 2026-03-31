{{/*
vector.shardMap serializes .Values.shardMap into a JSON array for the log-reader SHARD_MAP env var.
The shardMap in values.yaml is manually maintained and must match the latest version in
fluentbit-config/shard.lua's SHARD_MAP_VERSIONS table.

Example values.yaml entry:
  shardMap:
    - {from: 0,   to: 127, shard: "0"}
    - {from: 128, to: 255, shard: "1"}

Produces: [{"from":0,"to":127,"shard":"0"},{"from":128,"to":255,"shard":"1"}]
*/}}
{{- define "vector.shardMap" -}}
{{- $entries := list -}}
{{- range .Values.shardMap -}}
  {{- $entry := printf "{\"from\":%d,\"to\":%d,\"shard\":\"%s\"}" (int .from) (int .to) (.shard | toString) -}}
  {{- $entries = append $entries $entry -}}
{{- end -}}
[{{ join "," $entries }}]
{{- end -}}
