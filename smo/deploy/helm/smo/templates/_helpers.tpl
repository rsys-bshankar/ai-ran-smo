{{- /*
  Named templates shared by the chart's other templates: image and tag, the merged settings of a module (smo.module), labels,
  secret names, the database URL and environment, the database roles, and the mutual-TLS mode, secret and issuer. Nothing here
  renders by itself. Each template's comment gives the dict it expects. smo.podLabels must stay free of anything that changes
  between chart releases (it is part of every pod template).
*/ -}}
{{/* The tag of every SMO image. */}}
{{- define "smo.tag" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}

{{/* The image of a module: smo.image (dict "root" . "name" "sme"). */}}
{{- define "smo.image" -}}
{{- printf "%s/%s%s:%s" .root.Values.image.registry .root.Values.image.prefix .name (include "smo.tag" .root) -}}
{{- end -}}

{{/* The module's settings: the defaults with what the module names laid over them. smo.module (dict "root" . "name" "sme"). */}}
{{- define "smo.module" -}}
{{- $m := deepCopy .root.Values.moduleDefaults -}}
{{- $m = mustMergeOverwrite $m (get .root.Values.modules .name) -}}
{{- toYaml $m -}}
{{- end -}}

{{- /* The labels of every object the chart makes, with the chart name and version: smo.labels (dict "root" .). Not for pod templates: use smo.podLabels. */}}
{{- define "smo.labels" -}}
app.kubernetes.io/part-of: smo
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" }}
{{- end -}}

{{/* The labels of a pod template: the same without the chart version. A pod template that names the chart version is a changed pod template on every chart release, and a changed
     pod template restarts the pod; the chart version belongs on the objects (smo.labels), not on the pods they make. */}}
{{- define "smo.podLabels" -}}
app.kubernetes.io/part-of: smo
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{/* The Secret holding db-password and enrollment-secret. */}}
{{- define "smo.secretName" -}}
{{- default "smo-secrets" .Values.secrets.existingSecret -}}
{{- end -}}

{{/* The Secret holding the per-module role passwords (db-password-<role>); a hook resource, see role-secrets.yaml. */}}
{{- define "smo.roleSecretName" -}}
{{- default "smo-role-secrets" .Values.databaseRoles.existingSecret -}}
{{- end -}}

{{/* The role a module connects as, or "" for the owner: smo.dbRole (dict "root" . "module" $m). */}}
{{- define "smo.dbRole" -}}
{{- if and .root.Values.databaseRoles.enabled .module.databaseRole -}}{{ .module.databaseRole }}{{- end -}}
{{- end -}}

{{/* PR-SEC-2.4: "true" when the bundled Postgres serves TLS and the modules verify it (postgres.enabled and postgres.tls.enabled), else "false". smo.pgTls (the root context). */}}
{{- define "smo.pgTls" -}}
{{- if and .Values.postgres.enabled .Values.postgres.tls.enabled -}}true{{- else -}}false{{- end -}}
{{- end -}}

{{/* Where Postgres is, as the URL every module takes (the password is read from the file, never put in the URL). */}}
{{- define "smo.databaseUrl" -}}
{{- $user := ternary (printf "smo_%s" (replace "-" "_" (default "" .role))) "" (ne (default "" .role) "") -}}
{{- if .root.Values.postgres.enabled -}}
{{- if eq (include "smo.pgTls" .root) "true" -}}
{{- /* PR-SEC-2.4: the bundled Postgres serves TLS; the module checks its certificate against the CA mounted at /run/pg-tls and against the name `postgres` */ -}}
{{- printf "postgresql+psycopg://%s@postgres:5432/smo?sslmode=verify-full&sslrootcert=/run/pg-tls/ca.crt" (default "smo" $user) -}}
{{- else -}}
{{- printf "postgresql+psycopg://%s@postgres:5432/smo" (default "smo" $user) -}}
{{- end -}}
{{- else -}}
{{- $e := .root.Values.postgres.external -}}
{{- if not $e.host }}{{ fail "postgres.enabled=false needs postgres.external.host" }}{{ end -}}
{{- $tsa := ternary (printf "&target_session_attrs=%s" $e.targetSessionAttrs) "" (ne (default "" $e.targetSessionAttrs) "") -}}
{{- if contains "," (toString $e.host) -}}
{{- /* several hosts: libpq tries them in order and, with targetSessionAttrs, takes the one that can write (the port is one for all, or one each; the driver wants one per host, so a single port is repeated) */ -}}
{{- $hosts := splitList "," (toString $e.host) -}}
{{- $ports := list -}}
{{- if contains "," (toString $e.port) -}}{{- $ports = splitList "," (toString $e.port) -}}{{- else -}}{{- range $hosts -}}{{- $ports = append $ports (toString $e.port) -}}{{- end -}}{{- end -}}
{{- printf "postgresql+psycopg://%s@/%s?host=%s&port=%s&sslmode=%s%s" (default $e.user $user) $e.database (join "," $hosts) (join "," $ports) $e.sslmode $tsa -}}
{{- else -}}
{{- printf "postgresql+psycopg://%s@%s:%v/%s?sslmode=%s%s" (default $e.user $user) $e.host $e.port $e.database $e.sslmode $tsa -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The pull secrets of a pod spec. */}}
{{- define "smo.pullSecrets" -}}
{{- with .Values.image.pullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The environment of a container that talks to the database: the URL and the password file of its role (the owner's when it has none). */}}
{{- define "smo.dbEnv" -}}
- name: SMO_DATABASE_URL
  value: {{ include "smo.databaseUrl" (dict "root" .root "role" .role) | quote }}
- name: SMO_DATABASE_PASSWORD_FILE
  value: {{ if .role }}/run/secrets/db_password_{{ .role }}{{ else }}/run/secrets/db_password{{ end }}
{{- end -}}

{{/* The roles in use: the `databaseRole` of every enabled module, once each, as a JSON list ("[]" when roles are off). Read it with fromJsonArray. */}}
{{- define "smo.dbRoles" -}}
{{- $roles := list -}}
{{- if .Values.databaseRoles.enabled -}}
{{- range $name, $_ := .Values.modules -}}
{{- $m := include "smo.module" (dict "root" $ "name" $name) | fromYaml -}}
{{- if and $m.enabled $m.databaseRole -}}{{- $roles = append $roles $m.databaseRole -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson (uniq $roles) -}}
{{- end -}}

{{/* PR-SEC-2: how a module takes part in mutual TLS: "server", "client" or "off". smo.mtlsMode (dict "root" . "name" "sme" "module" $m). A worker has no port, so it is a client. */}}
{{- define "smo.mtlsMode" -}}
{{- if not .root.Values.mtls.enabled -}}off
{{- else if eq .module.kind "worker" -}}{{ ternary "off" "client" (eq (toString .module.mtls) "off") }}
{{- else -}}{{ .module.mtls }}
{{- end -}}
{{- end -}}

{{/* The Secret a module's certificate is in. */}}
{{- define "smo.mtlsSecretName" -}}
{{- printf "%s%s" .name .root.Values.mtls.secretSuffix -}}
{{- end -}}

{{/* The issuer of the module certificates: smo.mtlsIssuerRef (dict "root" .) -> a YAML map. */}}
{{- define "smo.mtlsIssuerRef" -}}
{{- if .root.Values.mtls.certManager.createCA -}}
name: smo-mtls-ca
kind: Issuer
group: cert-manager.io
{{- else -}}
{{- if not .root.Values.mtls.certManager.issuerRef.name }}{{ fail "mtls.certManager.createCA=false needs mtls.certManager.issuerRef.name" }}{{ end -}}
{{ toYaml .root.Values.mtls.certManager.issuerRef }}
{{- end -}}
{{- end -}}
