from threading import Event, Lock


class Interrupted(RuntimeError):
    pass


class Stopped(Interrupted):
    pass


class Control:
    def __init__(self, status=lambda _: None):
        self.status = status
        self.lock = Lock()
        self.ready = Event()
        self.ready.set()
        self.stopped = Event()
        self.awaiting_user = Event()
        self.generation = 0
        self.reason = ""

    def pause(self, reason="Paused"):
        with self.lock:
            if self.ready.is_set() and not self.stopped.is_set():
                self.generation += 1
                self.reason = reason
                self.ready.clear()
                self.status(reason)

    def resume(self):
        with self.lock:
            if not self.stopped.is_set():
                self.ready.set()
                self.reason = ""
                self.status("Running")

    def stop(self):
        with self.lock:
            self.generation += 1
            self.stopped.set()
            self.ready.set()
            self.status("Stopping")

    def wait_ready(self):
        while not self.ready.wait(0.1):
            pass
        if self.stopped.is_set():
            raise Stopped()
        return self.generation

    def check(self, generation):
        if self.stopped.is_set():
            raise Stopped()
        if self.generation != generation or not self.ready.is_set():
            raise Interrupted()
