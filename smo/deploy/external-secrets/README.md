# External Secrets example (PR-SEC-4.7)

How to supply every Secret the Helm chart reads from a secret manager instead of creating it by hand or letting the chart generate it. The example uses the External Secrets Operator (ESO) with HashiCorp Vault as the provider; any ESO provider works with a different `ClusterSecretStore`. The inventory of what each secret is, who owns it and how it is rotated is `docs/SECRETS.md`.

**Status: this example has NOT been applied to a cluster.** There is no cluster in the environment where it was written. What is checked is that the YAML parses and that every Secret name and key it creates is one the chart reads (`tests_integration/test_external_secrets_example.py`). That ESO accepts the manifests, that the Vault paths and role below work, and that the SMO starts on the Secrets ESO creates are still to be confirmed on a lab cluster by the owner or a CI job that has one (`OPEN_ITEMS.md`, SEC-4.7).

## Files

| File | What it is |
|---|---|
| `secret-store-vault.yaml` | A `ClusterSecretStore` named `smo-vault`: Vault KV v2 at mount `secret`, Kubernetes authentication with the role `smo-external-secrets`. Address, mount and role are placeholders. |
| `external-secrets.yaml` | One `ExternalSecret` per Secret the chart reads, in the namespace `smo`. |
| `values.yaml` | The chart values that name those Secrets (`secrets.existingSecret`, `databaseRoles.existingSecret`, `gui.oidcClientSecretRef`, `gui.totpKeySecretRef`, `identityKeySecretRef`). Names only. |

## The Secrets, and who reads them

| Secret (target) | Keys | Read by | Vault path (property) |
|---|---|---|---|
| `smo-secrets` | `db-password`, `enrollment-secret` | Postgres, the migrate Job, every module with a database or an enrollment (`secrets.existingSecret`) | `smo/database` (`owner-password`), `smo/enrollment` (`secret`) |
| `smo-role-secrets` | `db-password-<role>`, one per `databaseRole` of `values.yaml` (19 today) | the migrate Job and each module with a role (`databaseRoles.existingSecret`) | `smo/database-roles` (`<role>`) |
| `smo-gui-oidc` | `client-secret` | the GUI backend, as `GUI_OIDC_CLIENT_SECRET` (`gui.oidcClientSecretRef`), only with OIDC login | `smo/gui-oidc` (`client-secret`) |
| `smo-gui-totp` | `totp-key` | the GUI backend, as `GUI_TOTP_KEY` (`gui.totpKeySecretRef`), only to let local accounts enrol a one-time code | `smo/gui-totp` (`key`) |
| `smo-identity-key` | `identity-key` | every module with a database, as `SMO_IDENTITY_KEY` (`identityKeySecretRef`): encrypts the SME identities (invoker secrets) in `module_identity` (SEC-15.18) | `smo/identity-key` (`key`) |
| `<module>-mtls` (optional) | `tls.crt`, `tls.key`, `ca.crt` | each module, with `mtls.enabled` and `mtls.certManager.enabled=false` | `smo/mtls/<module>` (`tls.crt`, `tls.key`, `ca.crt`) |

The file holds the mTLS Secret of `r1-termination` as the pattern; repeat it for the other modules. Not in the example: the ingress TLS Secrets (`ingress.*.tlsSecretName`, normally cert-manager's), `image.pullSecrets` (a registry login of your own), the Secrets rApp Management writes for each rApp instance (`rapp-<instanceId>-credentials`, made at run time by the platform, not by you), and any Postgres of your own that the database is in.

The roles and enrollment secret have the same meaning as with the chart's own Secrets (`deploy/helm/smo/README.md`, "Secrets"): `enrollment-secret` is what makes a module an SMO module at SME, so keep it out of reach of rApps.

## Use

1. Install the External Secrets Operator and enable Vault's Kubernetes auth method; make a policy that can read `secret/data/smo/*` and the role `smo-external-secrets` bound to the operator's service account (the names are in `secret-store-vault.yaml`).
2. Write the values into Vault. For example (generate your own; nothing here is a usable value):

       vault kv put secret/smo/database owner-password="$(openssl rand -hex 24)"
       vault kv put secret/smo/enrollment secret="$(openssl rand -hex 24)"
       vault kv put secret/smo/database-roles onboarding="$(openssl rand -hex 24)" sme="$(openssl rand -hex 24)" ...   # one property per role
       vault kv put secret/smo/gui-totp key="$(openssl rand -base64 32)"
       vault kv put secret/smo/identity-key key="$(openssl rand -base64 32)"

3. `kubectl apply -f secret-store-vault.yaml -f external-secrets.yaml`, then wait for `kubectl -n smo get externalsecret` to show `SecretSynced`.
4. `helm install smo deploy/helm/smo -n smo -f deploy/external-secrets/values.yaml` (with the rest of your values). The chart then never creates `smo-secrets` or `smo-role-secrets`.

Two things to know. The database password in `smo-secrets` is the password the Postgres role was created with: on a database that already exists, changing it in Vault does not change the role; follow the rotation runbook in `docs/SECRETS.md` (`ALTER ROLE`, then the new value in Vault, then restart). And the processes read their password once at start, so a refreshed Secret needs a `kubectl rollout restart` of the modules; `refreshInterval: 1h` only keeps the Kubernetes Secret current.

## Alternative: Vault Agent injector or the Secrets Store CSI driver

If you would rather not have a Kubernetes Secret at all, the modules already read their secrets as files under `/run/secrets` (the `*_FILE` convention of `docs/SECRETS.md`), so a sidecar-injected file or a CSI volume can supply them. The chart does not template this: it mounts the Kubernetes Secrets named above, so using Vault Agent (annotations on the pod, files rendered to `/run/secrets/db_password`, `db_password_<role>`, `enrollment_secret`) or the Secrets Store CSI driver (a `SecretProviderClass` with `secretObjects` to sync a Secret, which is then the same as the ExternalSecret route) means adding pod annotations or volumes through your own overlay. Neither has been tried here. The ExternalSecret route is the one that needs no change to the chart.

## Not done

Not applied to a cluster. Not run against a real Vault. Only Vault KV v2 with Kubernetes authentication is shown; AWS Secrets Manager, Azure Key Vault and GCP Secret Manager need only a different `ClusterSecretStore` (the ExternalSecrets are provider-neutral). No `PushSecret`, so nothing generated by the chart goes back to Vault.
