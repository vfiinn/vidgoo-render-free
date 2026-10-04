import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.dash import DashSegmentsFD

import bot


class FragmentDownloadTests(unittest.TestCase):
    def download_fragments(self, workers, *, fail_fragment=None, fail_count=0,
                           expect_failure=False):
        payloads = [f"fragment-{index}\n".encode() for index in range(8)]
        lock = threading.Lock()
        first_wave_started = threading.Event()
        later_fragments_finished = threading.Event()
        all_finished = threading.Event()
        active = peak_active = 0
        started = set()
        first_wave = set(range(workers))
        later_in_first_wave = first_wave - {0}
        finished = []
        requests = [0] * len(payloads)
        server_errors = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal active, peak_active
                index = int(self.path.lstrip("/"))
                counted = True
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                    started.add(index)
                    requests[index] += 1
                    fail_request = index == fail_fragment and (
                        fail_count is None or requests[index] <= fail_count
                    )
                    if first_wave.issubset(started):
                        first_wave_started.set()
                try:
                    if fail_request:
                        body = b"Fragment unavailable"
                        self.send_response(404)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        with lock:
                            self.wfile.write(body)
                            self.wfile.flush()
                            active -= 1
                            counted = False
                        return
                    # Hold the first wave until all worker requests overlap,
                    # then finish the first fragment after the other workers.
                    # Events enforce this ordering without timing-based assertions.
                    if workers > 1 and fail_fragment is None and index < workers:
                        if not first_wave_started.wait(5):
                            raise TimeoutError("Fragment worker requests did not overlap")
                        if index == 0 and not later_fragments_finished.wait(5):
                            raise TimeoutError("Later fragments did not finish first")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payloads[index])))
                    self.end_headers()
                    with lock:
                        # Keep completion bookkeeping atomic with writing this
                        # short response, before a downloader worker can reuse it.
                        self.wfile.write(payloads[index])
                        self.wfile.flush()
                        finished.append(index)
                        active -= 1
                        counted = False
                        if later_in_first_wave.issubset(finished):
                            later_fragments_finished.set()
                        if len(finished) == len(payloads):
                            all_finished.set()
                except Exception as error:
                    with lock:
                        server_errors.append(error)
                    self.close_connection = True
                finally:
                    if counted:
                        with lock:
                            active -= 1

            def log_message(self, *args):
                pass

        class FragmentHTTPServer(ThreadingHTTPServer):
            request_queue_size = 8

        server = FragmentHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "fragments.mp4"
                options = bot.build_ydl_options(Path(folder))
                if workers == 1:
                    options["concurrent_fragment_downloads"] = 1
                else:
                    self.assertEqual(options.get("concurrent_fragment_downloads"), 8)
                options.update({
                    "cachedir": False,
                    "proxy": "",
                    # Isolate the native fragment retry loop from HTTP retries.
                    "retries": 0,
                    "socket_timeout": 10,
                    "noprogress": True,
                })
                base_url = f"http://127.0.0.1:{server.server_port}"
                info = {
                    "id": "fragment-fixture",
                    "title": "Fragment fixture",
                    "format_id": "fixture",
                    "url": f"{base_url}/0",
                    "ext": "mp4",
                    "protocol": "http_dash_segments",
                    "fragments": [
                        {"url": f"{base_url}/{index}"} for index in range(len(payloads))
                    ],
                }
                with bot.yt_dlp.YoutubeDL(options) as ydl:
                    downloader_type = get_suitable_downloader(info, ydl.params)
                    self.assertIs(downloader_type, DashSegmentsFD)
                    downloader = downloader_type(ydl, ydl.params)
                    if expect_failure:
                        # A parallel worker can close the shared destination while
                        # another fragment is appended, before DownloadError reaches
                        # this thread. Both outcomes must abort without publishing.
                        with self.assertRaises((bot.yt_dlp.utils.DownloadError, ValueError)) as failure:
                            downloader.download(str(output), info)
                        if isinstance(failure.exception, ValueError):
                            self.assertEqual(str(failure.exception), "write to closed file")
                        self.assertFalse(output.exists(), "Incomplete final file was published")
                    else:
                        success, _ = downloader.download(str(output), info)
                        self.assertTrue(success)
                        self.assertEqual(output.read_bytes(), b"".join(payloads))
                        self.assertTrue(all_finished.wait(5), "Fragment handlers did not finish")
            self.assertEqual(server_errors, [])
            if not expect_failure:
                self.assertEqual(sorted(finished), list(range(len(payloads))))
            self.assertLessEqual(peak_active, workers)
            return peak_active, finished, requests
        finally:
            # Release waiting handlers even if an assertion or download fails.
            first_wave_started.set()
            later_fragments_finished.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_parallel_fragments_preserve_serial_output(self):
        serial_peak, serial_order, _ = self.download_fragments(workers=1)
        self.assertEqual(serial_peak, 1)
        self.assertEqual(serial_order, list(range(8)))

        parallel_peak, parallel_order, _ = self.download_fragments(workers=8)
        self.assertEqual(parallel_peak, 8)
        self.assertTrue(all(parallel_order.index(index) < parallel_order.index(0)
                            for index in range(1, 8)))

    def test_transient_nonfirst_fragment_retries_without_data_loss(self):
        _, _, requests = self.download_fragments(
            workers=8, fail_fragment=1, fail_count=1
        )
        self.assertEqual(requests, [1, 2, 1, 1, 1, 1, 1, 1])

    def test_permanent_nonfirst_fragment_failure_rejects_incomplete_file(self):
        _, finished, requests = self.download_fragments(
            workers=8, fail_fragment=1, fail_count=None, expect_failure=True
        )
        self.assertEqual(requests[1], 4)
        self.assertNotIn(1, finished)


if __name__ == "__main__":
    unittest.main()

