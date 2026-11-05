"""Worker lifecycle regressions using Qt signals without a desktop window."""
import threading
import time
import unittest
from unittest.mock import patch

try:
    from PyQt5.QtCore import QCoreApplication
    import main
except ModuleNotFoundError as exc:
    if exc.name != "PyQt5" and not exc.name.startswith("PyQt5."):
        raise
    QCoreApplication = None
    main = None


@unittest.skipIf(main is None, "PyQt5 is not installed")
class ProcessThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Qt permits one application per process. Reuse any runner-owned instance.
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def observe(self, worker):
        events = []
        worker.stage.connect(lambda name, current, total: events.append(("stage", name)))
        worker.result.connect(lambda kind, message: events.append(("result", kind)))
        worker.finished.connect(lambda: events.append(("finished", None)))
        return events

    def await_finished(self, worker, events):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not worker.isRunning() and any(event[0] == "finished" for event in events):
                break
            time.sleep(0.005)
        self.assertFalse(worker.isRunning(), "worker failed to terminate")
        self.assertTrue(worker.wait(1000), "worker thread did not finish cleanup")
        self.app.processEvents()
        self.assertEqual(events[-1], ("finished", None))
        self.assertEqual(sum(event[0] == "result" for event in events), 1)
        self.assertEqual(sum(event[0] == "finished" for event in events), 1)

    def clean_worker(self, worker):
        worker.request_stop()
        self.assertTrue(worker.wait(3000), "test left a running QThread")
        self.app.processEvents()

    def test_pause_and_cancel_before_start_reach_pipeline_before_run(self):
        instances = []

        class FakePipeline:
            def __init__(self, cfg, log, progress, stage):
                self.cancelled = threading.Event()
                self.paused = False
                self.stage = stage
                instances.append(self)

            def set_paused(self, paused):
                self.paused = paused

            def cancel(self):
                self.cancelled.set()

            def run(self):
                # Both requests may precede creation of the backend in QThread.run.
                if not self.paused or not self.cancelled.is_set():
                    raise AssertionError("pre-start control request was lost")
                self.stage("cancelling", 1, 6)
                raise main.PipelineCancelled("stopped")

        worker = main.ProcessThread({"video": "example.mp4"})
        events = self.observe(worker)
        worker.request_pause(True)
        worker.request_stop()
        with patch.object(main, "Pipeline", FakePipeline):
            try:
                worker.start()
                self.await_finished(worker, events)
                self.assertEqual(len(instances), 1)
                self.assertTrue(instances[0].paused)
                self.assertTrue(instances[0].cancelled.is_set())
                self.assertEqual(events, [
                    ("stage", "cancelling"), ("result", "cancelled"), ("finished", None),
                ])
            finally:
                self.clean_worker(worker)

    def test_running_worker_forwards_pause_resume_and_stop_before_finished(self):
        entered = threading.Event()
        instances = []

        class FakePipeline:
            def __init__(self, cfg, log, progress, stage):
                self.cancelled = threading.Event()
                self.pause_requests = []
                self.stage = stage
                instances.append(self)

            def set_paused(self, paused):
                self.pause_requests.append(paused)

            def cancel(self):
                self.cancelled.set()

            def run(self):
                self.stage("running", 1, 6)
                entered.set()
                if not self.cancelled.wait(3):
                    raise AssertionError("stop was not forwarded to running backend")
                raise main.PipelineCancelled("stopped")

        worker = main.ProcessThread({})
        events = self.observe(worker)
        with patch.object(main, "Pipeline", FakePipeline):
            try:
                worker.start()
                self.assertTrue(entered.wait(2), "backend never entered run")
                worker.request_pause(True)
                worker.request_pause(False)
                self.assertTrue(worker.isRunning())
                self.assertEqual(instances[0].pause_requests, [False, True, False])
                worker.request_stop()
                self.await_finished(worker, events)
                self.assertEqual(events, [
                    ("stage", "running"), ("result", "cancelled"), ("finished", None),
                ])
            finally:
                self.clean_worker(worker)


if __name__ == "__main__":
    unittest.main()
