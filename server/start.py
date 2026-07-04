import subprocess, sys, os, signal

pid_file = "/tmp/mailtrack.pid"

old_pid = None
try:
    with open(pid_file) as f:
        old_pid = int(f.read().strip())
    os.kill(old_pid, signal.SIGTERM)
except (FileNotFoundError, ProcessLookupError, ValueError):
    pass

import time
time.sleep(1)

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
    stdout=open("/tmp/mailtrack.log", "w"),
    stderr=subprocess.STDOUT,
)

with open(pid_file, "w") as f:
    f.write(str(proc.pid))

print(f"Server PID: {proc.pid}")
proc.wait()
