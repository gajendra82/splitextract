# Migration Phases 1–6 Status
Date: 2026-11-09

## Completed
- Credentials installed at /etc/google/split-extract/invoice-ai-production.json (mode 600, root:root)
- Old credential preserved at /etc/google/split-extract.json
- .env backed up and updated
- Production model references updated to gemini-3.5-flash-lite
- Auth / text / image validation PASS against new project
- Service NOT restarted

## Backups
/var/www/html/split-extract/migration-backups/vertex-migrate-20261109/
