"""Persistensi SQLite dan cek kepemilikan pembatalan (langsung ke servicer)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from proto import bioskop_pb2 as pb
from server import LayananBioskop


def _pesan(svc, req_id, kursi, nama):
    return svc.PesanKursi(
        pb.PesanKursiRequest(request_id=req_id, kursi_id=kursi, nama_pemesan=nama), None
    )


def test_restart_tidak_menghapus_pesanan_dan_idempotensi(tmp_path):
    db = str(tmp_path / "bioskop.db")
    svc = LayananBioskop(db_path=db)
    hasil = _pesan(svc, "r1", "A1", "Pahri")
    assert hasil.sukses and hasil.booking_id

    # "Restart": instance baru dengan file DB yang sama.
    svc2 = LayananBioskop(db_path=db)
    kursi = {k.id: k for k in svc2.LihatKursi(pb.LihatKursiRequest(), None).kursi}
    assert kursi["A1"].tersedia is False
    assert svc2.pesanan[hasil.booking_id]["nama_pemesan"] == "Pahri"
    # Retry request_id lama tetap mengembalikan booking yang sama.
    ulang = _pesan(svc2, "r1", "A1", "Pahri")
    assert ulang.booking_id == hasil.booking_id


def test_batal_milik_orang_lain_ditolak_dan_pemilik_bisa_batal(tmp_path):
    db = str(tmp_path / "bioskop.db")
    svc = LayananBioskop(db_path=db)
    hasil = _pesan(svc, "r1", "B2", "Pahri")

    salah = svc.BatalkanPesanan(
        pb.BatalkanPesananRequest(request_id="b-salah", booking_id=hasil.booking_id, nama_pemesan="Orang Lain"),
        None,
    )
    assert salah.sukses is False
    assert "milik pemesan lain" in salah.pesan
    assert svc.kursi["B2"]["tersedia"] is False

    benar = svc.BatalkanPesanan(
        pb.BatalkanPesananRequest(request_id="b-benar", booking_id=hasil.booking_id, nama_pemesan="Pahri"),
        None,
    )
    assert benar.sukses is True
    assert svc.kursi["B2"]["tersedia"] is True


def test_mode_tanpa_db_tetap_in_memory():
    svc = LayananBioskop()
    hasil = _pesan(svc, "r1", "C3", "Pahri")
    assert hasil.sukses
    assert svc.db_path is None
