import sys
import time
import threading

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False
    class _Dummy:
        def __getattr__(self, _):
            return ""
    Fore = _Dummy()
    Style = _Dummy()


class UIConsole:
    REFRESH_INTERVAL = 0.5

    def __init__(self):
        self._sent = 0
        self._failed = 0
        self._total = 0
        self._start_time: float = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread = None

    def start(self, total: int) -> None:
        with self._lock:
            self._total = total
            self._sent = 0
            self._failed = 0
            self._start_time = time.monotonic()
            self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()

    def record_sent(self) -> None:
        with self._lock:
            self._sent += 1

    def record_failed(self) -> None:
        with self._lock:
            self._failed += 1

    def _loop(self) -> None:
        while self._running:
            self._render()
            time.sleep(self.REFRESH_INTERVAL)

    def _render(self) -> None:
        with self._lock:
            sent = self._sent
            failed = self._failed
            total = self._total
            elapsed = time.monotonic() - self._start_time

        processed = sent + failed
        speed = processed / elapsed if elapsed > 0 else 0.0
        remaining = total - processed
        eta_sec = remaining / speed if speed > 0 else 0
        eta_str = self._format_eta(eta_sec)
        pct = (processed / total * 100) if total > 0 else 0

        cpu_str = "N/A"
        ram_str = "N/A"
        if HAS_PSUTIL:
            try:
                cpu_str = f"{psutil.cpu_percent(interval=0):.0f}%"
                ram_str = f"{psutil.virtual_memory().percent:.0f}%"
            except Exception:
                pass

        bar_width = 30
        filled = int(bar_width * processed / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_width - filled)

        line = (
            f"\r{Fore.CYAN}[{bar}]{Style.RESET_ALL} "
            f"{pct:5.1f}%  "
            f"{Fore.GREEN}Sent:{sent}{Style.RESET_ALL} | "
            f"{Fore.RED}Fail:{failed}{Style.RESET_ALL} | "
            f"Total:{total}  "
            f"{Fore.YELLOW}{speed:.1f} m/s{Style.RESET_ALL}  "
            f"ETA:{eta_str}  "
            f"CPU:{cpu_str} RAM:{ram_str}  "
        )

        sys.stdout.write(line)
        sys.stdout.flush()

    @staticmethod
    def _format_eta(seconds: float) -> str:
        if seconds <= 0 or seconds > 360000:
            return "--:--:--"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def print_summary(self) -> None:
        with self._lock:
            sent = self._sent
            failed = self._failed
            elapsed = time.monotonic() - self._start_time
        speed = (sent + failed) / elapsed if elapsed > 0 else 0
        print(f"\n{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
        print(f"  {Fore.GREEN}Sent:   {sent}{Style.RESET_ALL}")
        print(f"  {Fore.RED}Failed: {failed}{Style.RESET_ALL}")
        print(f"  Speed:  {speed:.2f} mails/sec")
        print(f"  Time:   {elapsed:.1f}s")
        print(f"{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
