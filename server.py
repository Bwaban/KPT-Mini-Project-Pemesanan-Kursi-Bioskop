"""gRPC server for the single-screen cinema seat reservation demo.

State can be persisted to SQLite so a restart does not wipe bookings:
pass ``db_path`` (or set ``BIOSKOP_DB_PATH``) to enable it. With
``db_path=None`` (the default, used by the test suite) everything stays
in memory exactly like before.
"""

from concurrent import futures
import json
import os
import secrets
import sqlite3
import threading

import grpc

from proto import bioskop_pb2 as pb
from proto import bioskop_pb2_grpc as rpc


def buat_kursi():
    return {
        f"{baris}{nomor}": {
            "id": f"{baris}{nomor}",
            "baris": baris,
            "nomor": nomor,
            "tersedia": True,
        }
        for baris in "ABCDE"
        for nomor in range(1, 11)
    }


class LayananBioskop(rpc.LayananBioskopServicer):
    def __init__(self, db_path=None):
        self.kursi = buat_kursi()
        self.pesanan = {}  # booking_id -> {kursi_id, nama_pemesan}
        self.hasil_permintaan = {}  # (operasi, request_id) -> response
        self.versi = 1
        self.kondisi = threading.Condition()
        self.db_path = db_path
        self._db = None
        if db_path:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._db_init()
            self._db_load()

    # -- SQLite persistence -------------------------------------------------
    def _db_init(self):
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER);
            CREATE TABLE IF NOT EXISTS kursi (id TEXT PRIMARY KEY, baris TEXT, nomor INTEGER, tersedia INTEGER);
            CREATE TABLE IF NOT EXISTS pesanan (booking_id TEXT PRIMARY KEY, kursi_id TEXT, nama_pemesan TEXT);
            CREATE TABLE IF NOT EXISTS hasil (operasi TEXT, request_id TEXT, kind TEXT, sukses INTEGER, pesan TEXT, booking_id TEXT, kursi_id TEXT, pesanan_json TEXT, PRIMARY KEY (operasi, request_id));
            """
        )
        self._db.commit()

    def _db_load(self):
        rows = self._db.execute("SELECT id, baris, nomor, tersedia FROM kursi").fetchall()
        if rows:
            self.kursi = {
                r[0]: {"id": r[0], "baris": r[1], "nomor": r[2], "tersedia": bool(r[3])}
                for r in rows
            }
        else:
            for data in self.kursi.values():
                self._db.execute(
                    "INSERT INTO kursi VALUES (?, ?, ?, ?)",
                    (data["id"], data["baris"], data["nomor"], 1),
                )
        self.pesanan = {
            r[0]: {"kursi_id": r[1], "nama_pemesan": r[2]}
            for r in self._db.execute("SELECT booking_id, kursi_id, nama_pemesan FROM pesanan").fetchall()
        }
        for r in self._db.execute(
            "SELECT operasi, request_id, kind, sukses, pesan, booking_id, kursi_id, pesanan_json FROM hasil"
        ).fetchall():
            operasi, request_id, kind, sukses, pesan, booking_id, kursi_id, pesanan_json = r
            if kind == "banyak":
                hasil = pb.HasilPesanBanyakResponse(
                    sukses=bool(sukses),
                    pesan=pesan,
                    pesanan=[pb.TiketPesanan(**t) for t in json.loads(pesanan_json or "[]")],
                )
            else:
                hasil = pb.HasilPesanResponse(
                    sukses=bool(sukses), pesan=pesan, booking_id=booking_id or "", kursi_id=kursi_id or ""
                )
            self.hasil_permintaan[(operasi, request_id)] = hasil
        row = self._db.execute("SELECT v FROM meta WHERE k='versi'").fetchone()
        if row:
            self.versi = row[0]
        self._db.commit()

    def _db_save(self):
        """Write the whole state. Always called while self.kondisi is held."""
        if not self._db:
            return
        self._db.execute("INSERT OR REPLACE INTO meta VALUES ('versi', ?)", (self.versi,))
        for data in self.kursi.values():
            self._db.execute(
                "INSERT OR REPLACE INTO kursi VALUES (?, ?, ?, ?)",
                (data["id"], data["baris"], data["nomor"], 1 if data["tersedia"] else 0),
            )
        self._db.execute("DELETE FROM pesanan")
        for booking_id, p in self.pesanan.items():
            self._db.execute(
                "INSERT INTO pesanan VALUES (?, ?, ?)", (booking_id, p["kursi_id"], p["nama_pemesan"])
            )
        self._db.execute("DELETE FROM hasil")
        for (operasi, request_id), hasil in self.hasil_permintaan.items():
            if isinstance(hasil, pb.HasilPesanBanyakResponse):
                kind, booking_id, kursi_id = "banyak", "", ""
                pesanan_json = json.dumps(
                    [{"booking_id": t.booking_id, "kursi_id": t.kursi_id} for t in hasil.pesanan]
                )
            else:
                kind, booking_id, kursi_id = "tunggal", hasil.booking_id, hasil.kursi_id
                pesanan_json = "[]"
            self._db.execute(
                "INSERT INTO hasil VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (operasi, request_id, kind, 1 if hasil.sukses else 0, hasil.pesan, booking_id, kursi_id, pesanan_json),
            )
        self._db.commit()

    # -- helpers ------------------------------------------------------------
    def _kursi_pb(self):
        return [pb.Kursi(**data) for data in self.kursi.values()]

    def _snapshot(self):
        return pb.PembaruanKursi(kursi=self._kursi_pb(), versi=self.versi)

    def LihatKursi(self, request, context):
        with self.kondisi:
            return pb.DaftarKursiResponse(kursi=self._kursi_pb(), versi=self.versi)

    def PesanKursi(self, request, context):
        if not request.request_id.strip() or not request.kursi_id.strip():
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "request_id dan kursi_id wajib diisi")
        key = ("pesan", request.request_id)
        with self.kondisi:
            if key in self.hasil_permintaan:
                return self.hasil_permintaan[key]
            kursi = self.kursi.get(request.kursi_id)
            if kursi is None:
                hasil = pb.HasilPesanResponse(sukses=False, pesan="Kursi tidak ditemukan", kursi_id=request.kursi_id)
            elif not kursi["tersedia"]:
                hasil = pb.HasilPesanResponse(sukses=False, pesan="Kursi sudah dipesan", kursi_id=request.kursi_id)
            else:
                booking_id = secrets.token_urlsafe(18)
                kursi["tersedia"] = False
                self.pesanan[booking_id] = {"kursi_id": request.kursi_id, "nama_pemesan": request.nama_pemesan.strip()}
                hasil = pb.HasilPesanResponse(
                    sukses=True,
                    pesan=f"Kursi {request.kursi_id} berhasil dipesan",
                    booking_id=booking_id,
                    kursi_id=request.kursi_id,
                )
                self.versi += 1
                self.kondisi.notify_all()
            self.hasil_permintaan[key] = hasil
            self._db_save()
            return hasil

    def PesanBeberapaKursi(self, request, context):
        kursi_ids = list(request.kursi_id)
        if not request.request_id.strip() or not kursi_ids:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "request_id dan minimal satu kursi wajib diisi")
        key = ("pesan_banyak", request.request_id)
        with self.kondisi:
            if key in self.hasil_permintaan:
                return self.hasil_permintaan[key]

            if len(set(kursi_ids)) != len(kursi_ids):
                hasil = pb.HasilPesanBanyakResponse(sukses=False, pesan="Daftar kursi berisi pilihan yang sama")
            else:
                kursi_tidak_ada = [kursi_id for kursi_id in kursi_ids if kursi_id not in self.kursi]
                kursi_sudah_dipesan = [kursi_id for kursi_id in kursi_ids if kursi_id in self.kursi and not self.kursi[kursi_id]["tersedia"]]
                if kursi_tidak_ada:
                    hasil = pb.HasilPesanBanyakResponse(
                        sukses=False,
                        pesan=f"Kursi tidak ditemukan: {', '.join(kursi_tidak_ada)}",
                    )
                elif kursi_sudah_dipesan:
                    hasil = pb.HasilPesanBanyakResponse(
                        sukses=False,
                        pesan=f"Kursi sudah dipesan: {', '.join(kursi_sudah_dipesan)}",
                    )
                else:
                    daftar_pesanan = []
                    for kursi_id in kursi_ids:
                        booking_id = secrets.token_urlsafe(18)
                        self.kursi[kursi_id]["tersedia"] = False
                        self.pesanan[booking_id] = {
                            "kursi_id": kursi_id,
                            "nama_pemesan": request.nama_pemesan.strip(),
                        }
                        daftar_pesanan.append(pb.TiketPesanan(booking_id=booking_id, kursi_id=kursi_id))
                    hasil = pb.HasilPesanBanyakResponse(
                        sukses=True,
                        pesan=f"{len(daftar_pesanan)} kursi berhasil dipesan",
                        pesanan=daftar_pesanan,
                    )
                    self.versi += 1
                    self.kondisi.notify_all()

            self.hasil_permintaan[key] = hasil
            self._db_save()
            return hasil

    def BatalkanPesanan(self, request, context):
        if not request.request_id.strip() or not request.booking_id.strip():
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "request_id dan booking_id wajib diisi")
        key = ("batal", request.request_id)
        with self.kondisi:
            if key in self.hasil_permintaan:
                return self.hasil_permintaan[key]
            pesanan = self.pesanan.get(request.booking_id)
            if pesanan is None:
                hasil = pb.HasilPesanResponse(sukses=False, pesan="Pesanan tidak ditemukan atau sudah dibatalkan")
            elif (
                request.nama_pemesan.strip()
                and pesanan["nama_pemesan"]
                and request.nama_pemesan.strip() != pesanan["nama_pemesan"]
            ):
                # Kepemilikan: nama yang dikirim harus cocok dengan nama pemesan.
                # Client lama yang tidak mengirim nama (string kosong) tetap
                # diizinkan, supaya API lama tidak rusak.
                hasil = pb.HasilPesanResponse(sukses=False, pesan="Pesanan ini milik pemesan lain")
            else:
                self.pesanan.pop(request.booking_id)
                kursi_id = pesanan["kursi_id"]
                self.kursi[kursi_id]["tersedia"] = True
                hasil = pb.HasilPesanResponse(sukses=True, pesan=f"Pesanan kursi {kursi_id} dibatalkan", kursi_id=kursi_id)
                self.versi += 1
                self.kondisi.notify_all()
            self.hasil_permintaan[key] = hasil
            self._db_save()
            return hasil

    def PantauKursi(self, request, context):
        with self.kondisi:
            versi_terkirim = self.versi
            yield self._snapshot()
        while context.is_active():
            with self.kondisi:
                self.kondisi.wait_for(
                    lambda: self.versi != versi_terkirim or not context.is_active(), timeout=1.0
                )
                if not context.is_active():
                    return
                if self.versi == versi_terkirim:
                    continue
                versi_terkirim = self.versi
                pembaruan = self._snapshot()
            yield pembaruan


def main():
    db_path = os.getenv("BIOSKOP_DB_PATH", "data/bioskop.db")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    rpc.add_LayananBioskopServicer_to_server(LayananBioskop(db_path=db_path), server)
    port = "0.0.0.0:50051"
    server.add_insecure_port(port)
    server.start()
    print(f"Server gRPC berjalan di {port} (persistensi: {db_path})", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
