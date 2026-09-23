# Python Data Extraction Service VAPT deliverables

Project: Sales_POD_Invoice_Intelligence_Platform
Component: Python Data Extraction Service
Assessment date: 2026-09-22

The sample file `VAPT_Sales_POD_Invoice_Intelligence_Platform_Report.pdf` was used only as the format and presentation reference. Its findings were not copied. That file was not modified.

## Report

- `report/VAPT_Sales_POD_Python_Data_Extraction_Service_Report.pdf`
- `report/VAPT_Sales_POD_Python_Data_Extraction_Service_Report.docx`
- `python_vapt_findings.json`

## Evidence

- `evidence/` — sanitized curl transcripts, CORS preflight, SSRF listener log, health body, rewritten journal excerpt
- `raw_scans/` — reserved; nmap was not installed and was not run
- `source_review/` — route, blob, concurrency, privilege, and container notes
- `dependencies/` — virtualenv freeze and pip-audit JSON
- `tls/openssl_protocols.txt` — certificate and protocol checks
- `network/` — listeners, iptables, ufw

Customer invoice text and secret values are not stored in these files.

## Tools actually used

curl, openssl, ss, iptables, ufw, journalctl, manual source review, pip-audit 2.10.1

Not used: Burp Suite, OWASP ZAP, Nikto, nmap, Nuclei, Semgrep, Bandit, Trivy, testssl.sh
