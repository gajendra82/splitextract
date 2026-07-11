# Rollback Procedure — OCR Platform

---

## Quick rollback (feature flags only)

If a reliability flag causes issues:

```bash
# Edit /var/www/html/.env — set offending flag(s) to false
sudo systemctl restart split-extract
bash /var/www/html/split-extract/scripts/check-health.sh
```

No code rollback required.

---

## Full rollback to previous code

### 1. Stop systemd services

```bash
sudo systemctl stop split-extract split-pdf
sudo systemctl disable split-extract split-pdf   # optional
```

### 2. Restore application code

```bash
# Example: git checkout previous tag/commit
cd /var/www/html/split-extract && git checkout <previous-ref>
cd /var/www/html/split-pdf && git checkout <previous-ref>   # if versioned
```

### 3. Restore configuration

```bash
# Restore backed-up /var/www/html/.env if changed
sudo cp /backup/.env /var/www/html/.env
```

### 4. Return to tmux (optional legacy mode)

```bash
cd /var/www/html/split-extract
bash start-tmux.sh
# split-pdf: start manually or restore previous process manager
```

### 5. Verify

```bash
curl -sf http://127.0.0.1:8000/health
curl -sf http://127.0.0.1:8001/live
# Process a known-good test invoice through Laravel
```

---

## Rollback from systemd to tmux only

```bash
sudo systemctl stop split-extract split-pdf
sudo systemctl disable split-extract split-pdf

cd /var/www/html/split-extract
bash start-tmux.sh

# split-pdf: restore previous uvicorn/tmux session if applicable
```

---

## Disable watchdog only

```bash
# In /var/www/html/.env
OCR_WATCHDOG_ENABLED=false

sudo systemctl restart split-extract
```

---

## Verification checklist

- [ ] `GET /health` on port 8000 returns 200
- [ ] `GET /live` and `GET /ready` on port 8001 return 200 (when idle)
- [ ] Laravel pipeline completes end-to-end
- [ ] No unexpected restarts: `systemctl show split-extract -p NRestarts`

---

## Emergency contacts / data to collect

```bash
bash /var/www/html/split-extract/scripts/status-services.sh > /tmp/ocr-status.txt
curl -s http://127.0.0.1:8001/ready | jq . > /tmp/ready.json
sudo journalctl -u split-pdf -u split-extract --since "1 hour ago" > /tmp/ocr-journal.log
```
