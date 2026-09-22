{{/*
Names. The StatefulSet name is capped at 52 characters: its pods carry a
controller-revision-hash label "<name>-<10-char hash>" that must fit in 63.
*/}}
{{- define "curator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 52 | trimSuffix "-" }}
{{- end }}

{{- define "curator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 52 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 52 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "curator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Selector labels: never change them, a StatefulSet selector is immutable. */}}
{{- define "curator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "curator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "curator.labels" -}}
helm.sh/chart: {{ include "curator.chart" . }}
{{ include "curator.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | toString | trunc 63 | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "curator.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion | toString }}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}
{{- end }}

{{/*
The mount prefix as the Daemon normalizes it (daemon.settings.normalize_base_path):
"" or "/x[/y]" without a trailing slash; "curation" and "/curation/" both give "/curation".
Only URL path characters, the same rule as the frontend (frontend/README.md).
*/}}
{{- define "curator.basePath" -}}
{{- $raw := .Values.server.basePath | default "" | toString | trim | trimAll "/" }}
{{- if $raw }}
{{- if not (regexMatch "^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*$" $raw) }}
{{- fail (printf "server.basePath 写法不对：%q（形如 /curation，只能用字母、数字和 . _ ~ -）" .Values.server.basePath) }}
{{- end }}
{{- range (splitList "/" $raw) }}
{{- if or (eq . ".") (eq . "..") }}
{{- fail (printf "server.basePath 不能含 . 或 .. 段：%q" $.Values.server.basePath) }}
{{- end }}
{{- end }}
{{- printf "/%s" $raw }}
{{- end }}
{{- end }}

{{- define "curator.masterKeySecret" -}}
{{- .Values.masterKey.existingSecret | default (printf "%s-master-key" (include "curator.fullname" .)) }}
{{- end }}

{{- define "curator.siteConfigDir" -}}/etc/curator/site{{- end }}
{{- define "curator.authDir" -}}/etc/curator/auth{{- end }}
{{- define "curator.htpasswdFile" -}}{{ include "curator.authDir" . }}/htpasswd{{- end }}

{{/*
Seconds the Daemon gives running CLI children to finish their episode after SIGTERM before
it kills them and records the task as paused(system) (design doc 09 §2.3), plus the margin
it needs to persist that and exit. The pod's grace period must cover preStop + this.
*/}}
{{- define "curator.stopBudgetSeconds" -}}100{{- end }}

{{/* Refuse configurations the Daemon cannot run, before anything reaches the cluster. */}}
{{- define "curator.validate" -}}
{{- if ne (int .Values.replicaCount) 1 }}
{{- fail (printf "replicaCount 只能是 1（现在是 %v）：SQLite 只能有一个进程写，多个副本就是多份互不相干的库（09 篇 §2、D2）" .Values.replicaCount) }}
{{- end }}
{{- if ne (int .Values.server.maxRunningTasks) 1 }}
{{- fail "server.maxRunningTasks 目前只能是 1：Daemon 还不读这一项，要等任务编排（W5）接上（04 篇 §2.3、P1）" }}
{{- end }}
{{- if not (has .Values.auth.mode (list "htpasswd" "basic")) }}
{{- fail (printf "auth.mode 只能是 htpasswd 或 basic，现在是 %q" .Values.auth.mode) }}
{{- end }}
{{- if and (eq .Values.auth.mode "htpasswd") (not .Values.auth.htpasswdSecret) }}
{{- fail "auth.mode=htpasswd 需要 auth.htpasswdSecret：存放账号表的 Secret 名" }}
{{- end }}
{{- if and (eq .Values.auth.mode "basic") (not (and .Values.auth.username .Values.auth.existingPasswordSecret)) }}
{{- fail "auth.mode=basic 需要 auth.username 和 auth.existingPasswordSecret 两个都配（半配的鉴权最危险，08 篇 §5.1）" }}
{{- end }}
{{- if not .Values.masterKey.key }}
{{- fail "masterKey.key 不能为空：主密钥在 Secret 里的键名" }}
{{- end }}
{{- if not (has .Values.persistence.scratch.type (list "emptyDir" "pvc")) }}
{{- fail (printf "persistence.scratch.type 只能是 emptyDir 或 pvc，现在是 %q" .Values.persistence.scratch.type) }}
{{- end }}
{{- if and (eq .Values.persistence.scratch.type "pvc") (not .Values.persistence.scratch.size) }}
{{- fail "persistence.scratch.type=pvc 需要 persistence.scratch.size" }}
{{- end }}
{{- if not (has .Values.logging.format (list "json" "text")) }}
{{- fail (printf "logging.format 只能是 json 或 text，现在是 %q" .Values.logging.format) }}
{{- end }}
{{- if and .Values.server.publicBaseUrl (not (regexMatch "^https?://" (toString .Values.server.publicBaseUrl))) }}
{{- fail (printf "server.publicBaseUrl 必须以 http:// 或 https:// 开头：%q" .Values.server.publicBaseUrl) }}
{{- end }}
{{- $grace := int .Values.terminationGracePeriodSeconds }}
{{- $preStop := int .Values.preStopSleepSeconds }}
{{- $need := add $preStop (int (include "curator.stopBudgetSeconds" .)) }}
{{- if lt $grace $need }}
{{- fail (printf "terminationGracePeriodSeconds=%d 太短：至少要 preStop %d 秒 + 子进程收尾 90 秒 + 10 秒余量 = %d 秒，否则升级时任务来不及被系统暂停（09 篇 §2.3）" $grace $preStop $need) }}
{{- end }}
{{- $_ := include "curator.basePath" . }}
{{- end }}

{{/*
The Daemon's environment. Every name here is a setting the Daemon (backend/daemon) or the
CLI it starts (backend/curation) reads; backend/tests/deploy checks that. Secrets only
ever come through secretKeyRef.
*/}}
{{- define "curator.env" -}}
- name: CURATOR_PORT
  value: {{ .Values.server.port | quote }}
- name: CURATOR_DATA_DIR
  value: {{ .Values.persistence.data.mountPath | quote }}
- name: CURATOR_SCRATCH_DIR
  value: {{ .Values.persistence.scratch.mountPath | quote }}
- name: CURATION_EXPORT_SCRATCH
  value: {{ .Values.persistence.scratch.mountPath | quote }}
{{- with (include "curator.basePath" .) }}
- name: CURATOR_BASE_PATH
  value: {{ . | quote }}
{{- end }}
{{- with .Values.server.publicBaseUrl }}
- name: CURATOR_PUBLIC_BASE_URL
  value: {{ . | quote }}
{{- end }}
- name: CURATOR_TZ_OFFSET
  value: {{ .Values.server.tzOffset | quote }}
- name: CURATOR_LOG_FORMAT
  value: {{ .Values.logging.format | quote }}
- name: CURATOR_LOG_LEVEL
  value: {{ .Values.logging.level | quote }}
{{- with .Values.server.sseHeartbeatSeconds }}
- name: CURATOR_SSE_HEARTBEAT_S
  value: {{ . | quote }}
{{- end }}
{{- with .Values.localDataRoot }}
- name: CURATOR_LOCAL_DATA_ROOT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.server.tosEndpoint }}
- name: TOS_ENDPOINT
  value: {{ . | quote }}
{{- end }}
- name: CURATION_CONFIG
  value: {{ printf "%s/site.yaml" (include "curator.siteConfigDir" .) | quote }}
{{- if .Values.reasoningEffortTable }}
- name: CURATOR_REASONING_EFFORT_TABLE
  value: {{ printf "%s/reasoning-effort.json" (include "curator.siteConfigDir" .) | quote }}
{{- end }}
- name: CURATOR_AUTH_MODE
  value: {{ .Values.auth.mode | quote }}
{{- if eq .Values.auth.mode "htpasswd" }}
- name: CURATOR_HTPASSWD_FILE
  value: {{ include "curator.htpasswdFile" . | quote }}
{{- else }}
- name: CURATOR_AUTH_USER
  value: {{ .Values.auth.username | quote }}
- name: CURATOR_AUTH_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.auth.existingPasswordSecret | quote }}
      key: {{ .Values.auth.passwordKey | quote }}
{{- end }}
- name: CURATOR_MASTER_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "curator.masterKeySecret" . | quote }}
      key: {{ .Values.masterKey.key | quote }}
{{- with .Values.masterKey.nextKey }}
- name: CURATOR_MASTER_KEY_NEXT
  valueFrom:
    secretKeyRef:
      name: {{ include "curator.masterKeySecret" $ | quote }}
      key: {{ . | quote }}
      optional: true
{{- end }}
{{- with .Values.masterKey.versionKey }}
- name: CURATOR_MASTER_KEY_VERSION
  valueFrom:
    secretKeyRef:
      name: {{ include "curator.masterKeySecret" $ | quote }}
      key: {{ . | quote }}
      optional: true
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}
