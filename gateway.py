"""Web gateway: HTTP/SSE for the browser, gRPC calls to the cinema server."""

import json
import os
import threading
import time
import uuid

import grpc
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from benchmarks.web_benchmark import run_web_benchmark
from proto import bioskop_pb2 as pb
from proto import bioskop_pb2_grpc as rpc


GRPC_TARGET = os.getenv("GRPC_TARGET", "127.0.0.1:50051")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
channel = grpc.insecure_channel(GRPC_TARGET)
stub = rpc.LayananBioskopStub(channel)
app = FastAPI(title="Bioskop Kita")


class PesanBody(BaseModel):
    request_id: str
    kursi_id: str
    nama_pemesan: str


class PesanBanyakBody(BaseModel):
    request_id: str
    kursi_ids: list[str]
    nama_pemesan: str


class BatalBody(BaseModel):
    request_id: str
    booking_id: str
    nama_pemesan: str = ""


def panggil_dengan_retry(method, request, timeout=2.0, attempts=3):
    """Retry transient gRPC errors with the same request (and request_id)."""
    for attempt in range(attempts):
        try:
            return method(request, timeout=timeout)
        except grpc.RpcError as error:
            retryable = error.code() in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE)
            if not retryable or attempt == attempts - 1:
                raise
            time.sleep(0.25 * (2**attempt))


def error_http(error):
    code = error.code()
    status = 503 if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED) else 502
    raise HTTPException(status_code=status, detail=error.details() or code.name)


def serialisasi_kursi(kursi):
    return [
        {"id": k.id, "baris": k.baris, "nomor": k.nomor, "tersedia": k.tersedia}
        for k in kursi
    ]


@app.get("/api/health")
def health():
    try:
        response = stub.LihatKursi(pb.LihatKursiRequest(), timeout=2.0)
        return {"status": "ok", "kursi": len(response.kursi), "versi": response.versi}
    except grpc.RpcError as error:
        raise HTTPException(status_code=503, detail=f"server gRPC tidak terjangkau: {error.code().name}")


_benchmark_lock = threading.Lock()


@app.post("/api/benchmark")
def jalankan_benchmark():
    """Benchmark demo: server terisolasi, kursi live tidak berubah."""
    if not _benchmark_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Benchmark sedang berjalan, coba lagi sebentar")
    try:
        scenarios = run_web_benchmark(total_requests=100)
    except Exception as error:  # jangan bocorkan traceback ke client
        raise HTTPException(status_code=500, detail=f"Benchmark gagal: {error}")
    finally:
        _benchmark_lock.release()
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "isolated": True,
        "note": "Dijalankan pada server gRPC terisolasi (bukan server live); kursi demo tidak berubah.",
        "scenarios": scenarios,
    }


@app.get("/")
def halaman_utama():
    return FileResponse(os.path.join(BASE_DIR, "frontend", "index.html"))


@app.get("/api/seats")
def lihat_kursi():
    try:
        response = panggil_dengan_retry(stub.LihatKursi, pb.LihatKursiRequest())
        return {"kursi": serialisasi_kursi(response.kursi), "versi": response.versi}
    except grpc.RpcError as error:
        error_http(error)


@app.post("/api/book")
def pesan_kursi(body: PesanBody):
    request = pb.PesanKursiRequest(
        request_id=body.request_id,
        kursi_id=body.kursi_id,
        nama_pemesan=body.nama_pemesan,
    )
    try:
        result = panggil_dengan_retry(stub.PesanKursi, request)
        return {"sukses": result.sukses, "pesan": result.pesan, "booking_id": result.booking_id, "kursi_id": result.kursi_id}
    except grpc.RpcError as error:
        error_http(error)


@app.post("/api/book-batch")
def pesan_beberapa_kursi(body: PesanBanyakBody):
    request = pb.PesanBeberapaKursiRequest(
        request_id=body.request_id,
        kursi_id=body.kursi_ids,
        nama_pemesan=body.nama_pemesan,
    )
    try:
        result = panggil_dengan_retry(stub.PesanBeberapaKursi, request)
        return {
            "sukses": result.sukses,
            "pesan": result.pesan,
            "pesanan": [
                {"booking_id": item.booking_id, "kursi_id": item.kursi_id}
                for item in result.pesanan
            ],
        }
    except grpc.RpcError as error:
        error_http(error)


@app.post("/api/cancel")
def batalkan_pesanan(body: BatalBody):
    request = pb.BatalkanPesananRequest(
        request_id=body.request_id, booking_id=body.booking_id, nama_pemesan=body.nama_pemesan
    )
    try:
        result = panggil_dengan_retry(stub.BatalkanPesanan, request)
        return {"sukses": result.sukses, "pesan": result.pesan, "booking_id": result.booking_id, "kursi_id": result.kursi_id}
    except grpc.RpcError as error:
        error_http(error)


@app.get("/api/stream")
def pantau_kursi():
    def kirim_event():
        try:
            for pembaruan in stub.PantauKursi(pb.PantauKursiRequest()):
                payload = {"kursi": serialisasi_kursi(pembaruan.kursi), "versi": pembaruan.versi}
                yield f"id: {pembaruan.versi}\nevent: seats\ndata: {json.dumps(payload)}\n\n"
        except grpc.RpcError:
            # EventSource will reconnect and open a fresh gRPC stream.
            return

    return StreamingResponse(kirim_event(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/{asset_path:path}")
def aset_frontend(asset_path: str):
    path = os.path.abspath(os.path.join(BASE_DIR, "frontend", asset_path))
    frontend_dir = os.path.abspath(os.path.join(BASE_DIR, "frontend"))
    if not path.startswith(frontend_dir + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File tidak ditemukan")
    return FileResponse(path)
