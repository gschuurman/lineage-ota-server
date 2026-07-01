{{- define "lineage-ota-server.fullname" -}}
{{- .Release.Name -}}-lineage-ota-server
{{- end -}}

{{- define "lineage-ota-server.labels" -}}
app.kubernetes.io/name: lineage-ota-server
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "lineage-ota-server.secretName" -}}
{{- if .Values.existingSecret -}}
{{ .Values.existingSecret }}
{{- else -}}
{{ include "lineage-ota-server.fullname" . }}
{{- end -}}
{{- end -}}
