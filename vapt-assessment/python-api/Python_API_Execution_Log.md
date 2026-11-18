# Python API VAPT execution log

Generated: 2026-10-22T10:21:27Z

## Inventory

- Repository: /var/www/html/split-extract
- Branch: main
- HEAD: d51af4e13f7ea8c7960b0553b98e912a66f3c0b5 (2026-10-07T10:06:27Z) pushing last modification on live.
- Working tree: app.py and several test files differ from HEAD. app.py mtime 2026-10-17T15:57:37Z UTC.
- Diff stat versus HEAD for app.py was recorded as 4410 insertions and 184 deletions. The diff body was not copied into the report.
- Running process: PID 106735, uid 0, /var/www/html/venv/bin/python3 /var/www/html/split-extract/runner.py, started 2026-10-21T06:49:41Z UTC, after the app.py mtime.
- Framework confirmed from source and the live OpenAPI title: FastAPI. Not Flask or Django.
- Routes from live OpenAPI: GET /, /health, /live, /ready; POST /split-and-extract, /test-extract, /extract-sales-statement, /extract-pod-grn. Documentation routes /docs, /redoc, /openapi.json also returned HTTP 200.
- Two Python functions are decorated as POST /extract-sales-statement. The live OpenAPI description matches the later function.
- Dependencies: requirements.txt uses minimum versions. The running virtualenv freeze is scans/venv_freeze.txt.
- No SQL, ORM, os.system, shell=True, eval, exec, or pickle use was found in the application Python.
- Outbound HTTP: requests.get of caller-supplied split_raw_url, and requests.post from services/internal_invoice_ai.py to a configured model URL with an optional bearer token.
- Live unit: /etc/systemd/system/split-extract.service, User=root, EnvironmentFile=/var/www/html/.env, ExecStart runner.py. The copy under deploy/systemd/ is not the loaded unit.
- nginx location /extract/ proxies to 127.0.0.1:8001 with client_max_body_size 500M. Port 8000 is a different listener. Ports 8002 and 8003 were not listening.

## Commands and tools

- Bandit 1.9.4. JSON: scans/bandit.json (medium and higher) and scans/bandit_all.json.
- pip-audit 2.10.1. Failed attempt: requirement reinstall of venv_freeze.txt stopped on cffi==2.0.0 and installed nothing. Successful attempt: --path site-packages. Result: 109 records, 70 unique identifiers, 12 packages. Exit status 1 because advisories were found.
- curl to 127.0.0.1:8001 and to nginx through --resolve zydus-py.mediola.in:443:127.0.0.1. One health GET to 148.230.66.182:8001.
- openssl s_client and x509. ss, ps, stat, ufw, iptables -S INPUT.
- Python ast parsing was used as a syntax check and does not write bytecode.
- Not installed or not run: OWASP ZAP, Semgrep, nmap, Burp Suite.

## Tests not performed

- The target is the live extraction process. Destructive, high-volume, and data-plane tests were not run.
- No application account exists, so role and cross-account tests could not be performed. Object-level storage access was not executed.
- Server-side URL fetch was not exercised. Cloud-metadata and internal-network destinations were not requested.
- No synthetic or customer file was submitted, so MIME confusion, parser crashes, and processing timeouts were not measured at runtime.
- Rate-limit effectiveness and off-box Internet exposure of port 8001 were not measured. The port answered on this host's own primary address, and the host firewall does not filter it.
- Port 8000 is a separate listening process and was not assessed. nginx also maps ports 8002 and 8003; those ports were not listening.
- The Laravel application was out of scope. A prior Python assessment dated 22 September 2026 was not treated as retest evidence.
- Dependency advisories were not exploited and were not each converted into a vulnerability.

## Application code

This assessment wrote files only under vapt-assessment/python-api/. It did not change application behavior and did not restart the service.
