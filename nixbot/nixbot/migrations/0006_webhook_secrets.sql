-- Webhook secrets are per-project for every forge; see hook_secrets.py.

ALTER TABLE gitea_webhook_secrets RENAME TO webhook_secrets;
