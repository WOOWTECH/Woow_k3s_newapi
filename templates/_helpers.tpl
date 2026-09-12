{{/*
Helper templates for the newapi chart.
Resource names and selectors are fixed (not derived from the release name),
matching the original Kustomize manifests: changing a selector or pod-template
label would restart every pod.
*/}}

{{/* `annotations:` block with the keep policy, or nothing. */}}
{{- define "newapi.keepAnnotations" -}}
{{- if .Values.keepOnUninstall -}}
annotations:
  helm.sh/resource-policy: keep
{{- end -}}
{{- end -}}
