"""The UI's single background worker and polled completion delivery."""

import queue
import threading


class SingleWorker:
    """One daemon worker; completion callbacks run exclusively from poll()."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._results = queue.Queue()
        self._busy = False
        threading.Thread(target=self._run, name="file-assistant-worker", daemon=True).start()

    def _run(self):
        while True:
            work, done = self._jobs.get()
            try:
                value = work()
            except Exception as exc:
                self._results.put((done, None, exc))
            else:
                self._results.put((done, value, None))

    def submit(self, work, done):
        if self._busy:
            raise RuntimeError("The sole worker is still occupied.")
        self._busy = True
        self._jobs.put((work, done))

    def poll(self):
        try:
            done, value, error = self._results.get_nowait()
        except queue.Empty:
            return
        self._busy = False
        done(value, error)
