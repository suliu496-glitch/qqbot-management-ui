"""Small shared runtime state; no imports with bot side effects."""
import threading
import time

lock = threading.RLock()
task_lock = threading.Lock()
started = time.time()
ws_connected = False
paused = False
last_event = None
test_mode = False
