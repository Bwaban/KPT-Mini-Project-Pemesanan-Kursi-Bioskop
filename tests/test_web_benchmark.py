"""Benchmark web: 3 skenario, server terisolasi, tanpa numpy/matplotlib."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from benchmarks.web_benchmark import run_web_benchmark


def test_web_benchmark_tiga_skenario():
    scenarios = run_web_benchmark(total_requests=60)
    assert [s["concurrency"] for s in scenarios] == [1, 5, 20]
    for s in scenarios:
        assert s["total_requests"] == 60
        assert s["throughput_req_per_sec"] > 0
        assert s["latency_avg_ms"] >= 0
        assert s["latency_p95_ms"] >= s["latency_p50_ms"]
        assert s["error_count"] == 0
