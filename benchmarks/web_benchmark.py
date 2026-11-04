"""Benchmark kinerja untuk demo web.

Berbeda dengan benchmarks/benchmark.py (CLI, butuh numpy/matplotlib),
modul ini murni stdlib + gRPC sehingga bisa dipakai gateway tanpa
dependensi tambahan.

PENTING untuk demo: benchmark berjalan pada server gRPC TERISOLASI
yang dibuat khusus per skenario, BUKAN pada server live. Jadi
menjalankan benchmark dari web tidak memesan kursi demo, tidak
mengubah database, dan tidak mengganggu pengguna lain.
"""

import random
import threading
import time
from concurrent import futures

import grpc

from proto import bioskop_pb2 as pb
from proto import bioskop_pb2_grpc as rpc
from server import LayananBioskop


def _start_isolated_server():
    """Server in-memory di port dinamis. Coba IPv4 dulu, fallback IPv6."""
    for bind, target_fmt in (("127.0.0.1:0", "127.0.0.1:{}"), ("[::1]:0", "[::1]:{}")):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=40))
        rpc.add_LayananBioskopServicer_to_server(LayananBioskop(), server)
        port = server.add_insecure_port(bind)
        server.start()
        target = target_fmt.format(port)
        channel = grpc.insecure_channel(target)
        try:
            rpc.LayananBioskopStub(channel).LihatKursi(pb.LihatKursiRequest(), timeout=3.0)
            return server, channel, target
        except grpc.RpcError:
            channel.close()
            server.stop(grace=None)
    raise RuntimeError("server benchmark terisolasi tidak bisa dijangkau")


def _percentile(sorted_values, pct):
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round((pct / 100.0) * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def _run_scenario(concurrency, total_requests):
    server, channel, target = _start_isolated_server()
    stub = rpc.LayananBioskopStub(channel)
    try:
        per_client = total_requests // concurrency
        actual = per_client * concurrency
        barrier = threading.Barrier(concurrency)
        latencies, errors = [], 0
        lock = threading.Lock()
        pool = [f"{b}{n}" for b in "ABCDE" for n in range(1, 11)]

        def worker(client_id):
            nonlocal errors
            local_lat, local_err = [], 0
            barrier.wait()
            for idx in range(per_client):
                t0 = time.perf_counter()
                try:
                    if idx % 10 < 7:
                        stub.LihatKursi(pb.LihatKursiRequest(), timeout=5.0)
                    else:
                        stub.PesanKursi(
                            pb.PesanKursiRequest(
                                request_id=f"webbench-c{concurrency}-u{client_id}-r{idx}-{random.randint(1000, 9999)}",
                                kursi_id=random.choice(pool),
                                nama_pemesan=f"BenchClient-{client_id}",
                            ),
                            timeout=5.0,
                        )
                except grpc.RpcError:
                    local_err += 1
                local_lat.append((time.perf_counter() - t0) * 1000.0)
            with lock:
                latencies.extend(local_lat)
                errors += local_err

        start = time.perf_counter()
        with futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(worker, range(concurrency)))
        duration = time.perf_counter() - start
        lat = sorted(latencies)
        return {
            "concurrency": concurrency,
            "total_requests": actual,
            "duration_seconds": round(duration, 4),
            "throughput_req_per_sec": round(actual / duration, 2) if duration > 0 else 0.0,
            "latency_avg_ms": round(sum(lat) / len(lat), 3) if lat else 0.0,
            "latency_min_ms": round(lat[0], 3) if lat else 0.0,
            "latency_max_ms": round(lat[-1], 3) if lat else 0.0,
            "latency_p50_ms": round(_percentile(lat, 50), 3),
            "latency_p95_ms": round(_percentile(lat, 95), 3),
            "latency_p99_ms": round(_percentile(lat, 99), 3),
            "error_count": errors,
            "error_rate_pct": round(errors / actual * 100.0, 2) if actual else 0.0,
        }
    finally:
        channel.close()
        server.stop(grace=None)


def run_web_benchmark(total_requests=100):
    """Jalankan skenario 1, 5, 20 client. Total per skenario ~total_requests."""
    return [_run_scenario(c, total_requests) for c in (1, 5, 20)]
