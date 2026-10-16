{{- define "newapi.fullname" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "newapi.secretName" -}}
{{- include "newapi.fullname" . }}-secrets
{{- end -}}

{{- define "newapi.labels" -}}
app.kubernetes.io/name: newapi
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* selectorLabels <ctx> <component> */}}
{{- define "newapi.selector" -}}
app.kubernetes.io/name: newapi
app.kubernetes.io/instance: {{ (index . 0).Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{/* image ref from {repository, tag, digest} */}}
{{- define "newapi.image" -}}
{{- if .digest -}}
{{ .repository }}:{{ .tag }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end -}}

{{- define "newapi.affinity" -}}
{{- if .Values.avoidNodes }}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: NotIn
              values:
                {{- toYaml .Values.avoidNodes | nindent 16 }}
{{- end }}
{{- end -}}

{{- define "newapi.podSecurity" -}}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "newapi.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* env shared by master and slave */}}
{{- define "newapi.appEnv" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "newapi.secretName" . }}
      key: POSTGRES_PASSWORD
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "newapi.secretName" . }}
      key: REDIS_PASSWORD
- name: SQL_DSN
  value: "postgresql://newapi:$(POSTGRES_PASSWORD)@{{ include "newapi.fullname" . }}-postgres:5432/newapi?sslmode=disable"
- name: REDIS_CONN_STRING
  value: "redis://:$(REDIS_PASSWORD)@{{ include "newapi.fullname" . }}-valkey:6379/0"
- name: SESSION_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "newapi.secretName" . }}
      key: SESSION_SECRET
- name: CRYPTO_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "newapi.secretName" . }}
      key: CRYPTO_SECRET
- name: SESSION_COOKIE_SECURE
  value: "true"
- name: SESSION_COOKIE_TRUSTED_URL
  value: {{ .Values.publicUrl | quote }}
- name: TRUSTED_PROXIES
  value: {{ .Values.newapi.trustedProxies | quote }}
- name: NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
{{- range $k, $v := .Values.newapi.env }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- range $k, $v := .Values.newapi.extraEnv }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}
