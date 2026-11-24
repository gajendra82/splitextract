import os
import sys
import time
import subprocess
import json

tests_dir = os.path.abspath(r"splitextract/tests")
test_files = sorted([f for f in os.listdir(tests_dir) if f.startswith("test_") and f.endswith(".py")])

progress_log = os.path.abspath(r"splitextract/scratch/test_progress.log")
with open(progress_log, "w", encoding="utf-8") as f:
    f.write(f"Test audit started for {len(test_files)} files\n")

print(f"Discovered {len(test_files)} test files.", flush=True)

results = []
total_passed = 0
total_failed = 0
total_skipped = 0
total_errors = 0

start_all = time.time()

for idx, tf in enumerate(test_files, 1):
    rel_path = os.path.join("splitextract", "tests", tf)
    t0 = time.time()
    
    cmd = [
        sys.executable, "-m", "pytest", rel_path,
        "-q", "--tb=short", "--no-header"
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath("splitextract") + os.pathsep + env.get("PYTHONPATH", "")
    env["OCR_ENGINE"] = "paddle"
    
    # 20 second timeout per test file
    timeout_sec = 25
    
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env
        )
        dur = time.time() - t0
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        last_line = lines[-1] if lines else ""
        
        passed = 0
        failed = 0
        skipped = 0
        errors = 0
        
        for part in last_line.split(","):
            part = part.strip()
            if "passed" in part:
                try: passed = int(part.split()[0])
                except: pass
            elif "failed" in part:
                try: failed = int(part.split()[0])
                except: pass
            elif "skipped" in part:
                try: skipped = int(part.split()[0])
                except: pass
            elif "error" in part:
                try: errors = int(part.split()[0])
                except: pass
                
        if proc.returncode != 0 and failed == 0 and errors == 0:
            errors = 1

        total_passed += passed
        total_failed += failed
        total_skipped += skipped
        total_errors += errors
        
        status = "PASS" if (proc.returncode == 0 and failed == 0 and errors == 0) else "FAIL"
        msg = f"[{idx:3d}/{len(test_files)}] {tf:<45} | {status:<4} | {dur:5.2f}s | P:{passed} F:{failed} S:{skipped} E:{errors}"
        print(msg, flush=True)
        with open(progress_log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        
        if status == "FAIL":
            fail_snippet = "\n".join(lines[-15:])
            print("--- FAILURE DETAILS ---\n" + fail_snippet + "\n-----------------------", flush=True)
            with open(progress_log, "a", encoding="utf-8") as f:
                f.write("--- FAILURE DETAILS ---\n" + fail_snippet + "\n-----------------------\n")
            
        results.append({
            "file": tf,
            "status": status,
            "duration": dur,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "output": output if status == "FAIL" else ""
        })
        
    except subprocess.TimeoutExpired:
        dur = time.time() - t0
        msg = f"[{idx:3d}/{len(test_files)}] {tf:<45} | TIMEOUT | {dur:5.2f}s"
        print(msg, flush=True)
        with open(progress_log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        total_errors += 1
        results.append({
            "file": tf,
            "status": "TIMEOUT",
            "duration": dur,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 1,
            "output": f"Execution timed out after {timeout_sec}s"
        })
    except Exception as exc:
        dur = time.time() - t0
        msg = f"[{idx:3d}/{len(test_files)}] {tf:<45} | ERROR | {dur:5.2f}s | {exc}"
        print(msg, flush=True)
        with open(progress_log, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        total_errors += 1
        results.append({
            "file": tf,
            "status": "ERROR",
            "duration": dur,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 1,
            "output": str(exc)
        })

total_dur = time.time() - start_all
summary = {
    "total_files": len(test_files),
    "total_duration": total_dur,
    "total_passed": total_passed,
    "total_failed": total_failed,
    "total_skipped": total_skipped,
    "total_errors": total_errors,
    "results": results
}

with open(r"splitextract/scratch/test_audit_summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)

print("\n" + "="*70, flush=True)
print(f"FULL TEST AUDIT COMPLETE in {total_dur:.2f}s", flush=True)
print(f"Passed: {total_passed} | Failed: {total_failed} | Skipped: {total_skipped} | Errors: {total_errors}", flush=True)
print("="*70, flush=True)