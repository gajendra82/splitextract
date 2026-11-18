# Python API VAPT summary

Classification: CONFIDENTIAL. Assessment date: 22 October 2026. Generated: 2026-10-22T10:21:27Z.

The service is a FastAPI application on port 8001. Thirteen unique findings are open. None are Critical or High. Six are Medium. No remediation was verified.

## Counts

| Severity | Unique findings |
| --- | --- |
| Critical | 0 |
| High | 0 |
| Medium | 6 |
| Low | 4 |
| Informational | 3 |

Total unique: 13. Open: 13. Remediated: 0.

## Findings

| ID | Severity | Status | Title |
| --- | --- | --- | --- |
| CAN-PY-001 | Medium | Open | Extraction API has no authentication |
| CAN-PY-002 | Medium | Open | split_raw_url is fetched without a destination allow list |
| CAN-PY-003 | Medium | Open | Caller-supplied blob container and path are used with the service credential |
| CAN-PY-004 | Medium | Open | Environment file holding cloud and model credentials is world-readable |
| CAN-PY-005 | Medium | Open | API listens in cleartext on all interfaces |
| CAN-PY-009 | Medium | Open | Unauthenticated health output includes the in-flight document identity |
| CAN-PY-006 | Low | Open | Handlers return exception text to the caller |
| CAN-PY-007 | Low | Open | CORS reflects any origin and allows credentials |
| CAN-PY-008 | Low | Open | Security headers are absent and server versions are disclosed |
| CAN-PY-010 | Low | Open | Upload size and concurrency limits do not cover every extraction route |
| CAN-PY-011 | Informational | Open | The extraction process runs as root |
| CAN-PY-012 | Informational | Open | Container build file does not match the running service |
| CAN-PY-013 | Informational | Open | Dependency advisories are present; exploitability was not demonstrated |

## Remediation order

- 1. Authenticate every extraction route and restrict nginx /extract/ to the Laravel servers. Disable public API documentation.
- 2. Remove arbitrary URL fetch, or allow only named HTTPS storage hosts, and stop returning internal exception text.
- 3. Ignore client container names and client blob prefixes. Stop returning an account-key SAS to anonymous callers.
- 4. Change /var/www/html/.env to mode 600 and rotate the credentials that were world-readable.
- 5. Bind the API to 127.0.0.1 so TLS and the nginx body limit cannot be bypassed.
- 6. Remove in-flight file names and batch identifiers from unauthenticated health output.
- 7. Replace reflected CORS with an explicit origin list. Add HSTS and hide server versions.
- 8. Enforce one upload-size cap and one queue on every extraction route. Remove or protect /test-extract.
- 9. Run the service as an unprivileged user. Align the Dockerfile with runner.py.
- 10. Patch pillow, python-multipart, starlette, and the PDF libraries in a test environment, then retest extraction.

## Limitations

- The target is the live extraction process. Destructive, high-volume, and data-plane tests were not run.
- No application account exists, so role and cross-account tests could not be performed. Object-level storage access was not executed.
- Server-side URL fetch was not exercised. Cloud-metadata and internal-network destinations were not requested.
- No synthetic or customer file was submitted, so MIME confusion, parser crashes, and processing timeouts were not measured at runtime.
- Rate-limit effectiveness and off-box Internet exposure of port 8001 were not measured. The port answered on this host's own primary address, and the host firewall does not filter it.
- Port 8000 is a separate listening process and was not assessed. nginx also maps ports 8002 and 8003; those ports were not listening.
- The Laravel application was out of scope. A prior Python assessment dated 22 September 2026 was not treated as retest evidence.
- Dependency advisories were not exploited and were not each converted into a vulnerability.
