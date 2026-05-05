{{/*
Common helpers — name, fullname, labels.
Mirrors the convention from `helm create` so anyone reading the chart finds
the expected shape, but trimmed of unused bits.
*/}}

{{- define "brightdata-exporter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "brightdata-exporter.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "brightdata-exporter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "brightdata-exporter.labels" -}}
helm.sh/chart: {{ include "brightdata-exporter.chart" . }}
{{ include "brightdata-exporter.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "brightdata-exporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "brightdata-exporter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "brightdata-exporter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "brightdata-exporter.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Resolve the Secret + key carrying the API token.

If auth.existingSecret is set, refer to that. Otherwise the chart's own
Secret (rendered when auth.apiToken is non-empty) is used.

Renders helpfully as: secretName / secretKey for use in envFrom.secretRef.
*/}}
{{- define "brightdata-exporter.tokenSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}
{{- include "brightdata-exporter.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "brightdata-exporter.tokenSecretKey" -}}
{{- if .Values.auth.existingSecret -}}
{{- default "BRIGHTDATA_API_TOKEN" .Values.auth.existingSecretKey -}}
{{- else -}}
BRIGHTDATA_API_TOKEN
{{- end -}}
{{- end -}}

{{/*
Resolve the Secret + key carrying the optional /api/* bearer auth token.
Resolution order:
  1. auth.existingAuthSecret  → use that Secret + auth.existingAuthSecretKey
  2. auth.apiAuthToken inline → use the chart's own Secret + canonical key
  3. otherwise                → render nothing (caller must guard)
Returns "" when no auth token source is configured at all.
*/}}
{{- define "brightdata-exporter.authTokenSecretName" -}}
{{- if .Values.auth.existingAuthSecret -}}
{{- .Values.auth.existingAuthSecret -}}
{{- else if and .Values.auth.apiAuthToken (not .Values.auth.existingSecret) -}}
{{- include "brightdata-exporter.fullname" . -}}
{{- else if and .Values.auth.apiAuthToken .Values.auth.existingSecret -}}
{{- /* auth token expected to be in the same existingSecret as the API token */ -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}{{- /* no source configured */ -}}{{- end -}}
{{- end -}}

{{- define "brightdata-exporter.authTokenSecretKey" -}}
{{- if .Values.auth.existingAuthSecret -}}
{{- default "BRIGHTDATA_API_AUTH_TOKEN" .Values.auth.existingAuthSecretKey -}}
{{- else -}}
BRIGHTDATA_API_AUTH_TOKEN
{{- end -}}
{{- end -}}
