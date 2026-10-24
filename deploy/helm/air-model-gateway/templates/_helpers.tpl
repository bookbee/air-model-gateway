{{- define "amg.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "amg.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (include "amg.name" .) .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "amg.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "amg.selectorLabels" -}}
app.kubernetes.io/name: {{ include "amg.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "amg.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "amg.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/component: gateway
app.kubernetes.io/part-of: air-platform
app.kubernetes.io/managed-by: {{ .Release.Service }}
air-platform/environment: {{ .Values.environment }}
{{- end }}

{{- define "amg.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "amg.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "amg.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) }}
{{- end }}
{{- end }}

{{/* True when at least one DevOps config file is delivered via the ConfigMap. */}}
{{- define "amg.hasConfig" -}}
{{- if or .Values.config.routing .Values.config.pricing .Values.config.policy }}true{{ end }}
{{- end }}

{{- define "amg.validate" -}}
{{- if or (contains "<" .Values.image.repository) (contains "<" (toString .Values.secrets.gcp.project)) }}
{{- fail "image.repository or secrets.gcp.project still holds a <placeholder>: fill deploy/gke/values-<env>.yaml or pass IMAGE_REPOSITORY / GCP_PROJECT" }}
{{- end }}
{{- if not (has .Values.environment (list "local" "staging" "prod")) }}
{{- fail (printf "environment must be local, staging or prod, got %q" .Values.environment) }}
{{- end }}
{{- if not (has .Values.secrets.mode (list "gcpSecretManager" "kubernetesSecret" "none")) }}
{{- fail (printf "secrets.mode must be gcpSecretManager, kubernetesSecret or none, got %q" .Values.secrets.mode) }}
{{- end }}
{{- if and (eq .Values.secrets.mode "gcpSecretManager") (not .Values.secrets.gcp.project) }}
{{- fail "secrets.gcp.project is required when secrets.mode=gcpSecretManager" }}
{{- end }}
{{- if and (eq .Values.secrets.mode "kubernetesSecret") (not .Values.secrets.kubernetesSecret.name) }}
{{- fail "secrets.kubernetesSecret.name is required when secrets.mode=kubernetesSecret" }}
{{- end }}
{{- if and .Values.redis.tls.enabled (eq .Values.secrets.mode "gcpSecretManager") (not .Values.redis.tls.gcpSecret) }}
{{- fail "redis.tls.gcpSecret is required when redis.tls.enabled and secrets.mode=gcpSecretManager" }}
{{- end }}
{{- if and .Values.redis.tls.enabled (eq .Values.secrets.mode "kubernetesSecret") (not .Values.redis.tls.kubernetesSecret) }}
{{- fail "redis.tls.kubernetesSecret is required when redis.tls.enabled and secrets.mode=kubernetesSecret" }}
{{- end }}
{{- if and (ne .Values.environment "local") (not .Values.config.policy) (not .Values.config.imagePolicyPath) }}
{{- fail "outside local, set config.policy (--set-file) or config.imagePolicyPath" }}
{{- end }}
{{- if le (int .Values.terminationGracePeriodSeconds) (add .Values.preStopSleepSeconds .Values.gracefulShutdownSeconds | int) }}
{{- fail "terminationGracePeriodSeconds must exceed preStopSleepSeconds + gracefulShutdownSeconds" }}
{{- end }}
{{- end }}
