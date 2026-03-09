{{/*
vector.shardMap generates a JSON SHARD_MAP evenly distributing 256 slots across shards.
Example for shards=2: [{"from":0,"to":127,"shard":"0"},{"from":128,"to":255,"shard":"1"}]
*/}}
{{- define "vector.shardMap" -}}
{{- $shards := .Values.shards | int -}}
{{- $slotSize := div 256 $shards -}}
{{- $entries := list -}}
{{- range $i, $e := until $shards -}}
  {{- $from := mul $i $slotSize -}}
  {{- $to := sub (mul (add $i 1) $slotSize) 1 -}}
  {{- if eq $i (sub $shards 1) -}}
    {{- $to = 255 -}}
  {{- end -}}
  {{- $entry := printf "{\"from\":%d,\"to\":%d,\"shard\":\"%d\"}" $from $to $i -}}
  {{- $entries = append $entries $entry -}}
{{- end -}}
[{{ join "," $entries }}]
{{- end -}}
