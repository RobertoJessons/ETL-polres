import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import json
import os
import re
import sys
import subprocess
import threading
import queue
import time
import contextlib
import importlib.util
from datetime import datetime
import pandas as pd

# Dependensi opsional untuk fitur "Upload ke Google Sheets". Kalau belum
# terinstall, fitur ini otomatis dinonaktifkan (tombol tetap ada tapi akan
# menampilkan pesan cara install saat diklik) -- tidak bikin GUI gagal jalan.
try:
    import gspread
    from google.oauth2.service_account import Credentials as GCredentials
    GSPREAD_TERSEDIA = True
except ImportError:
    GSPREAD_TERSEDIA = False

# Dependensi opsional untuk fitur "Uji Spesifikasi Minimum" (logging
# CPU/RAM/Disk I/O per-proses selama ETL berjalan). Kalau belum terinstall,
# fitur ini otomatis dinonaktifkan -- ETL tetap jalan normal tanpa logging.
try:
    import psutil
    PSUTIL_TERSEDIA = True
except ImportError:
    PSUTIL_TERSEDIA = False


# =====================================================================
# MEMUAT MODUL ETL NYATA (Cleaning Data TA.py / Cleaning_Data_TA.py)
# ---------------------------------------------------------------------
# Modul itu sekarang sudah dibungkus jadi fungsi jalankan_etl(path_input),
# jadi aman diimport tanpa memicu proses ETL. File dicari di folder yang
# sama dengan main_ctk.py, dengan atau tanpa spasi di nama filenya.
# =====================================================================

def muat_modul_etl():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    kandidat = ["Cleaning Data.py", "Cleaning_Data.py"]
    for nama in kandidat:
        path_modul = os.path.join(base_dir, nama)
        if os.path.exists(path_modul):
            spec = importlib.util.spec_from_file_location("cleaning_data", path_modul)
            modul = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(modul)
            return modul, path_modul
    return None, None


# =====================================================================
# MONITOR SPESIFIKASI MINIMUM (CPU / RAM / DISK I/O PER-PROSES)
# ---------------------------------------------------------------------
# Dipakai untuk bab evaluasi skripsi: mengukur beban resource riil selama
# jalankan_etl() berjalan, supaya bisa disimpulkan spesifikasi minimum
# perangkat (ambil nilai puncak/max + margin, bukan rata-rata).
# =====================================================================

class MonitorSpesifikasi:
    """Sampling CPU%/RAM/Disk I/O proses aplikasi sendiri di background
    thread, interval tetap (default 0.5 detik) supaya cpu_percent() akurat
    dan tidak mengganggu (blocking) proses ETL utama."""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.aktif = False
        self.sampel = []  # list of dict: waktu, cpu_percent, ram_mb, read_mb, write_mb
        self._thread = None
        self._proc = psutil.Process(os.getpid()) if PSUTIL_TERSEDIA else None

    def mulai(self):
        if not PSUTIL_TERSEDIA or self._proc is None:
            return
        self.sampel = []
        self.aktif = True
        # panggilan pertama cpu_percent() selalu 0.0 (baseline internal psutil),
        # jadi dipanggil sekali di awal supaya sampel berikutnya sudah valid
        self._proc.cpu_percent(interval=None)
        self._io_awal = self._ambil_io()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _ambil_io(self):
        try:
            io = self._proc.io_counters()
            return io.read_bytes, io.write_bytes
        except Exception:
            return None  # tidak semua platform/permission mendukung io_counters

    def _loop(self):
        while self.aktif:
            try:
                cpu = self._proc.cpu_percent(interval=None)
                ram_mb = self._proc.memory_info().rss / (1024 * 1024)
                io_sekarang = self._ambil_io()
                if io_sekarang and self._io_awal:
                    read_mb = (io_sekarang[0] - self._io_awal[0]) / (1024 * 1024)
                    write_mb = (io_sekarang[1] - self._io_awal[1]) / (1024 * 1024)
                else:
                    read_mb = write_mb = None
                self.sampel.append({
                    "waktu": datetime.now(),
                    "cpu_percent": cpu,
                    "ram_mb": ram_mb,
                    "read_mb": read_mb,
                    "write_mb": write_mb,
                })
            except Exception:
                pass  # proses bisa saja sudah selesai di tengah sampling
            time.sleep(self.interval)

    def berhenti(self):
        self.aktif = False
        if self._thread is not None:
            self._thread.join(timeout=2)

    def ringkasan(self):
        """Kembalikan dict min/avg/max CPU% dan RAM (MB), plus total
        read/write disk (MB) selama sesi. None kalau tidak ada sampel."""
        if not self.sampel:
            return None
        cpu_vals = [s["cpu_percent"] for s in self.sampel]
        ram_vals = [s["ram_mb"] for s in self.sampel]
        io_vals = [s for s in self.sampel if s["read_mb"] is not None]
        hasil = {
            "jumlah_sampel": len(self.sampel),
            "durasi_detik": (self.sampel[-1]["waktu"] - self.sampel[0]["waktu"]).total_seconds(),
            "cpu_min": min(cpu_vals), "cpu_avg": sum(cpu_vals) / len(cpu_vals), "cpu_max": max(cpu_vals),
            "ram_min_mb": min(ram_vals), "ram_avg_mb": sum(ram_vals) / len(ram_vals), "ram_max_mb": max(ram_vals),
        }
        if io_vals:
            hasil["disk_read_total_mb"] = io_vals[-1]["read_mb"]
            hasil["disk_write_total_mb"] = io_vals[-1]["write_mb"]
        return hasil

    def teks_ringkasan(self):
        r = self.ringkasan()
        if r is None:
            return ""
        teks = (
            f"CPU  -> min {r['cpu_min']:.1f}% | avg {r['cpu_avg']:.1f}% | max {r['cpu_max']:.1f}%\n"
            f"RAM  -> min {r['ram_min_mb']:.1f} MB | avg {r['ram_avg_mb']:.1f} MB | max {r['ram_max_mb']:.1f} MB\n"
            f"Durasi -> {r['durasi_detik']:.1f} detik ({r['jumlah_sampel']} sampel)"
        )
        if "disk_read_total_mb" in r:
            teks += f"\nDisk -> baca {r['disk_read_total_mb']:.2f} MB | tulis {r['disk_write_total_mb']:.2f} MB"
        return teks

    def simpan_ke_csv(self, path_csv, judul_sesi):
        """Tambahkan satu baris ringkasan ke file log CSV kumulatif (dibuat
        otomatis kalau belum ada). Cocok untuk kumpulkan data dari beberapa
        kali uji (file kecil/sedang/besar) sebelum dianalisis di bab
        evaluasi skripsi."""
        r = self.ringkasan()
        if r is None:
            return False
        ada_file = os.path.exists(path_csv)
        with open(path_csv, "a", encoding="utf-8", newline="") as f:
            if not ada_file:
                f.write("waktu,judul_sesi,durasi_detik,jumlah_sampel,"
                        "cpu_min,cpu_avg,cpu_max,ram_min_mb,ram_avg_mb,ram_max_mb,"
                        "disk_read_total_mb,disk_write_total_mb\n")
            f.write(
                f"{datetime.now().isoformat(timespec='seconds')},{judul_sesi},"
                f"{r['durasi_detik']:.2f},{r['jumlah_sampel']},"
                f"{r['cpu_min']:.2f},{r['cpu_avg']:.2f},{r['cpu_max']:.2f},"
                f"{r['ram_min_mb']:.2f},{r['ram_avg_mb']:.2f},{r['ram_max_mb']:.2f},"
                f"{r.get('disk_read_total_mb', '')},{r.get('disk_write_total_mb', '')}\n"
            )
        return True


def buka_folder(path_folder):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path_folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path_folder])
        else:
            subprocess.Popen(["xdg-open", path_folder])
    except Exception:
        pass


HEADER_DIKENAL = [
    'NO', 'LP', 'TGL.LP', 'JAM LP', 'KASUS', 'PASAL', 'TGL.KEJ', 'JAM KEJ',
    'TKP', 'KORBAN', 'TERLAPOR', 'MO', 'URAIAN', 'KERUGIAN', 'KETERANGAN',
    'PENYIDIK', 'ASAL LAPORAN',
]

# Pemetaan header INPUT (mentah, seperti di Excel) -> nama kolom OUTPUT di
# processed_data hasil sistem ETL nyata. Hanya kolom yang hubungannya 1:1
# yang dimasukkan di sini -- kolom yang di-pipeline nyata dipecah jadi
# beberapa kolom turunan (mis. PASAL -> pasal_raw/keterangan_pasal/..., TKP
# -> desa_tkp/kecamatan_tkp/.../longitude, KORBAN/TERLAPOR -> beberapa
# kolom) sengaja TIDAK dimasukkan karena rename 1 header tidak bisa
# dipetakan secara jelas ke satu kolom output.
RAW_KE_OUTPUT = {
    'NO': 'no',
    'LP': 'nomor_laporan',
    'TGL.LP': 'tanggal_laporan',
    'JAM LP': 'jam_laporan',
    'KASUS': 'kasus',
    'MO': 'modus',
    'URAIAN': 'uraian_kejadian',
    'KERUGIAN': 'kerugian',
    'KETERANGAN': 'keterangan',
    'ASAL LAPORAN': 'asal_laporan',
}

# Header INPUT yang di pipeline nyata dipecah jadi BEBERAPA kolom output.
# Header-nya sendiri ditampilkan read-only di GUI (tidak masuk akal
# di-rename karena tidak mewakili satu kolom output), tapi tiap kolom
# turunannya bisa diberi nama baru satu per satu.
SPLIT_KOLOM = {
    'PASAL'   : ['pasal_raw', 'keterangan_pasal', 'ringkasan_pasal'],
    'TKP'     : ['desa_tkp', 'kecamatan_tkp', 'kabupaten_tkp', 'provinsi_tkp', 'latitude', 'longitude'],
    'TGL.KEJ' : ['tanggal_kejadian_mulai', 'tanggal_kejadian_selesai'],
    'KORBAN'  : ['nik_korban', 'umur_korban', 'jenis_kelamin_korban', 'kota_korban', 'provinsi_korban'],
    'TERLAPOR': ['nik_pelaku', 'umur_pelaku', 'jenis_kelamin_pelaku', 'kota_pelaku', 'provinsi_pelaku', 'status_pelaku'],
}

# Kategori fungsi disederhanakan supaya mudah dipahami pengguna awam.
# Nilai (kode) di kiri tetap dipakai sebagai kunci saat diekspor ke JSON;
# nanti tinggal dipetakan ke fungsi teknis yang sesuai di Cleaning_Data_TA.py.
FUNGSI_TERSEDIA = [
    ("", "— pilih fungsi —"),
    ("normalisasi_teks", "Normalisasi Teks (rapikan format tulisan)"),
    ("ekstrak_normalisasi_tanggal", "Ekstrak & Normalisasi Tanggal"),
    ("ekstrak_normalisasi_jam", "Ekstrak & Normalisasi Jam"),
    ("ekstrak_angka", "Ekstrak Angka / Nominal"),
    ("ambil_mentah", "Ambil Data Mentah (tanpa diubah)"),
    ("tinjau_manual", "Tinjau Manual"),
]
LABEL_FUNGSI = {val: label for val, label in FUNGSI_TERSEDIA}
VAL_DARI_LABEL = {label: val for val, label in FUNGSI_TERSEDIA}


def rekomendasi(nama_kolom):
    n = nama_kolom.strip().lower()
    if 'tgl' in n or 'tanggal' in n:
        return "ekstrak_normalisasi_tanggal"
    if 'jam' in n:
        return "ekstrak_normalisasi_jam"
    if 'kerugian' in n or 'nominal' in n or 'rugi' in n:
        return "ekstrak_angka"
    if any(k in n for k in ['kasus', 'pasal', 'asal', 'korban', 'pelaku', 'terlapor',
                             'tkp', 'wilayah', 'alamat', 'modus', 'uraian', 'keterangan']):
        return "normalisasi_teks"
    return ""


# =====================================================================
# DETEKSI TIPE DATA & PANJANG TEKS (untuk kolom baru)
# ---------------------------------------------------------------------
# Dipakai supaya rekomendasi tidak cuma menebak dari NAMA kolom, tapi juga
# lihat ISI datanya: tanggal, jam, angka, teks pendek, atau teks panjang
# (>10 kata rata-rata → lebih aman disarankan tinjau manual / ambil mentah).
# =====================================================================

_POLA_JAM = re.compile(r"^([01]?\d|2[0-3])[.:]([0-5]\d)\s*(WIB|WITA|WIT)?$", re.IGNORECASE)

_NAMA_BULAN = [
    'januari', 'februari', 'pebruari', 'maret', 'april', 'mei', 'juni',
    'juli', 'agustus', 'september', 'oktober', 'november', 'desember',
]


def deteksi_dari_data(series):
    """Kembalikan kode fungsi berdasarkan tipe & panjang isi kolom, atau ''
    kalau datanya kosong semua / tidak cukup untuk disimpulkan."""
    if series is None:
        return "", ""

    nilai = [str(v).strip() for v in series.dropna().tolist() if str(v).strip() != ""]
    if not nilai:
        return "", ""

    sampel = nilai[:50]  # cukup sampel 50 nilai pertama yang terisi

    # 1) Teks panjang (>10 kata rata-rata) -> lebih aman ditinjau manual
    jumlah_kata = [len(v.split()) for v in sampel]
    rata_kata = sum(jumlah_kata) / len(jumlah_kata)
    if rata_kata > 10:
        return "tinjau_manual", f"teks panjang, rata-rata {rata_kata:.0f} kata/baris"

    # 2) Tanggal dengan NAMA BULAN (mis. "01 Januari 2026", "Jumat, 16 Januari 2026")
    #    -> sinyal paling jelas, langsung direkomendasikan tanpa perlu parsing lebih lanjut
    def _ada_nama_bulan(v):
        vl = v.lower()
        return any(bulan in vl for bulan in _NAMA_BULAN)

    cocok_bulan = sum(1 for v in sampel if _ada_nama_bulan(v))
    if cocok_bulan / len(sampel) >= 0.5:
        return "ekstrak_normalisasi_tanggal", "mengandung nama bulan (mis. Januari, Februari, dst.)"

    # 3) Angka murni (nominal, kode, dsb.) -> buang dulu prefix mata uang/simbol
    #    umum (Rp, IDR, titik/koma pemisah ribuan, spasi, tanda baca di pinggir)
    def _bersihkan_untuk_cek_angka(v):
        s = v.strip()
        s = re.sub(r"^[\s:\-]+", "", s)                 # tanda di awal (":", "-", spasi)
        s = re.sub(r"(?i)^(rp\.?|idr\.?)\s*", "", s)     # prefix mata uang
        s = s.strip(" .,:;")                             # rapikan pinggir
        s = s.replace(".", "").replace(",", "").replace(" ", "")
        return s

    def _angka_murni(v):
        bersih = _bersihkan_untuk_cek_angka(v)
        return bersih.isdigit() and bersih != ""

    cocok_angka = sum(1 for v in sampel if _angka_murni(v))
    if cocok_angka / len(sampel) >= 0.7:
        return "ekstrak_angka", "isinya angka (nominal/kode)"

    # 4) Pola jam (HH:MM / HH.MM, jam 00-23 menit 00-59), boleh diikuti
    #    label zona waktu seperti "WIB"/"WITA"/"WIT"
    cocok_jam = sum(1 for v in sampel if _POLA_JAM.match(v))
    if cocok_jam / len(sampel) >= 0.6:
        return "ekstrak_normalisasi_jam", "polanya menyerupai jam"

    # 5) Tanggal format angka (mis. "01/02/2026", "01-02-2026") tanpa nama bulan
    def _mirip_tanggal(v):
        return bool(re.search(r"\d{1,4}[/\-.]\d{1,2}([/\-.]\d{1,4})?", v))

    kandidat_tanggal = [v for v in sampel if _mirip_tanggal(v)]
    if len(kandidat_tanggal) / len(sampel) >= 0.6:
        berhasil = 0
        for v in kandidat_tanggal:
            try:
                if pd.notna(pd.to_datetime(v, dayfirst=True, errors="raise")):
                    berhasil += 1
            except Exception:
                pass
        if berhasil / len(kandidat_tanggal) >= 0.7:
            return "ekstrak_normalisasi_tanggal", "polanya menyerupai tanggal"

    # 6) Default: teks biasa (pendek)
    return "normalisasi_teks", f"teks pendek, rata-rata {rata_kata:.0f} kata/baris"


def rekomendasi_gabungan(nama_kolom, series=None):
    """Gabungan: utamakan hasil deteksi dari ISI data; kalau data kosong/tidak
    cukup, jatuh kembali ke tebakan dari NAMA kolom."""
    kode_data, keterangan = deteksi_dari_data(series)
    if kode_data:
        return kode_data, keterangan
    kode_nama = rekomendasi(nama_kolom)
    return kode_nama, ("cocok dengan nama kolom" if kode_nama else "")


# =====================================================================
# GROUPING DATA (nilai unik per kolom)
# ---------------------------------------------------------------------
# Menampilkan variasi penulisan yang ada di suatu kolom, mis. kolom
# "kasus" berisi "CURAT" dan "PENCURIAN DENGAN PEMBERATAN" -> ditampilkan
# sebagai 2 grup terpisah dengan jumlah kemunculan masing-masing.
# =====================================================================

def hitung_grouping(series, batas_unik_ekstrem=300, maks_panjang_nilai=70):
    """Kembalikan (daftar_teks, total_unik, total_baris) untuk ditampilkan
    di dropdown grouping. daftar_teks diurutkan dari yang paling sering
    muncul, formatnya "nilai (jumlah)". Untuk kolom teks bebas yang jumlah
    variasinya sangat ekstrem (>batas_unik_ekstrem), hanya ambil yang
    paling sering muncul supaya dropdown tidak memuat ribuan entri unik."""
    if series is None:
        return [], 0, 0

    nilai = series.dropna().astype(str).str.strip()
    nilai = nilai[nilai != ""]
    total_baris = len(nilai)
    if total_baris == 0:
        return [], 0, 0

    counts = nilai.value_counts()
    total_unik = len(counts)

    counts_tampil = counts.head(batas_unik_ekstrem) if total_unik > batas_unik_ekstrem else counts

    daftar = []
    for nilai_unik, jumlah in counts_tampil.items():
        teks = nilai_unik if len(nilai_unik) <= maks_panjang_nilai else nilai_unik[:maks_panjang_nilai - 1] + "…"
        daftar.append(f"{teks}  ({jumlah})")

    if total_unik > batas_unik_ekstrem:
        daftar.append(f"... +{total_unik - batas_unik_ekstrem} variasi lain tidak ditampilkan")

    return daftar, total_unik, total_baris


def cari_baris_header(df_mentah):
    """Salinan ringan dari cari_header_row() di Cleaning_Data_TA.py."""
    for i in range(min(3, len(df_mentah))):
        vals = [str(v).strip().upper() for v in df_mentah.iloc[i] if pd.notna(v)]
        if 'LP' in vals or 'NO' in vals:
            return i
    return 0


# =====================================================================
# IMPLEMENTASI NYATA — FUNGSI TRANSFORMASI SEDERHANA
# ---------------------------------------------------------------------
# Ini versi ringkas/awam-friendly, dipakai untuk file "bersih" yang
# diunduh dari GUI. Bukan pengganti logika lengkap di Cleaning_Data_TA.py
# (mis. fuzzy wilayah, lookup pasal→UU), tapi cukup untuk pembersihan dasar.
# =====================================================================

def _kosong(v):
    return pd.isna(v) or str(v).strip() == ""


def normalisasi_teks(v):
    """Rapikan spasi berlebih & spasi di pinggir, tanpa mengubah isi teks."""
    if _kosong(v):
        return ""
    return re.sub(r"\s+", " ", str(v).strip())


def ekstrak_normalisasi_tanggal(v):
    """Parse berbagai format tanggal, keluarkan sebagai DD-MM-YYYY."""
    if _kosong(v):
        return ""
    try:
        dt = pd.to_datetime(v, dayfirst=True, errors="coerce")
        if pd.isna(dt):
            return str(v).strip()
        return dt.strftime("%d-%m-%Y")
    except Exception:
        return str(v).strip()


def ekstrak_normalisasi_jam(v):
    """Ambil pola jam:menit dari teks, keluarkan sebagai HH.MM."""
    if _kosong(v):
        return ""
    s = str(v).strip()
    m = re.search(r"(\d{1,2})[.:hH](\d{2})", s)
    if not m:
        m = re.search(r"(\d{1,2})(\d{2})$", s)
    if m:
        jam = int(m.group(1)) % 24
        menit = int(m.group(2)) % 60
        return f"{jam:02d}.{menit:02d}"
    return s


def ekstrak_angka(v):
    """Ambil digit angka saja dari teks (untuk kerugian/nominal)."""
    if _kosong(v):
        return ""
    digit = re.sub(r"[^\d]", "", str(v))
    return int(digit) if digit else ""


def ambil_mentah(v):
    """Tanpa transformasi, cuma rapikan spasi di pinggir."""
    return "" if pd.isna(v) else str(v).strip()


FUNGSI_IMPLEMENTASI = {
    "normalisasi_teks": normalisasi_teks,
    "ekstrak_normalisasi_tanggal": ekstrak_normalisasi_tanggal,
    "ekstrak_normalisasi_jam": ekstrak_normalisasi_jam,
    "ekstrak_angka": ekstrak_angka,
    "ambil_mentah": ambil_mentah,
    "tinjau_manual": ambil_mentah,   # tetap tampil mentah, hanya ditandai untuk ditinjau
    "": ambil_mentah,
}


# --- palet warna sederhana, biar konsisten dan mudah diubah ---
WARNA = {
    "aksen": "#d9a441",       # amber, untuk status "baru"
    "sukses": "#3ba272",      # hijau, untuk status "dikenal"
    "teks_dim": "#8a8a8a",
    "kartu": ("gray92", "gray17"),
}


class ColumnInspectorApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("System")     # ikut tema OS (light/dark)
        ctk.set_default_color_theme("blue")

        self.title("ETL Column Inspector — Data Polres Sleman")
        self.geometry("1300x660")
        self.minsize(1050, 480)

        self.headers = []
        self.baris_widget = []  # (nama_kolom, status, combobox_or_None)
        self.nama_laporan = "laporan"  # nama file asal (tanpa ekstensi), dipakai untuk nama file unduhan
        self.df_full = None  # data lengkap (semua baris) dari file yang diunggah
        self.path_file = None  # path file Excel yang terakhir dipilih
        self.pernah_dikenal = {}  # {nama_sekarang: nama_asli_yang_dikenal} — dilacak lintas rename
        self.rename_output_langsung = {}  # {nama_kolom_output_asli: nama_baru} untuk kolom turunan (mis. desa_tkp)
        self.modul_etl, self._path_modul_etl = muat_modul_etl()
        self.hasil_terakhir = None  # dict hasil jalankan_etl() paling akhir (punya csv_file/xlsx_file)

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._buat_bagian_atas()
        self._buat_bagian_tabel()
        self._buat_bagian_bawah()

    # -----------------------------------------------------------------
    def _buat_bagian_atas(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        top.grid_columnconfigure(2, weight=1)

        ctk.CTkButton(
            top, text="Pilih File Excel...", command=self.pilih_file, width=160
        ).grid(row=0, column=0, sticky="w")

        self.label_file = ctk.CTkLabel(
            top, text="Belum ada file dipilih", text_color=WARNA["teks_dim"], anchor="w"
        )
        self.label_file.grid(row=0, column=1, sticky="w", padx=12)

        etl_row = ctk.CTkFrame(top, fg_color="transparent")
        etl_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        etl_row.grid_columnconfigure(0, weight=1)

        if self.modul_etl is not None:
            status_txt = f"Modul ETL : {os.path.basename(self._path_modul_etl)}"
            status_warna = WARNA["sukses"]
        else:
            status_txt = "Modul ETL (Cleaning Data TA.py) tidak ditemukan di folder ini"
            status_warna = WARNA["aksen"]
        self.label_status_etl = ctk.CTkLabel(etl_row, text=status_txt, text_color=status_warna, anchor="w")
        self.label_status_etl.grid(row=0, column=0, sticky="w")

        self.btn_jalankan_etl = ctk.CTkButton(
            etl_row, text="Jalankan ETL",
            command=self.jalankan_etl_lengkap, state="disabled", width=230
        )
        self.btn_jalankan_etl.grid(row=0, column=1, sticky="e")

    # -----------------------------------------------------------------
    def _buat_bagian_tabel(self):
        container = ctk.CTkFrame(self, fg_color=WARNA["kartu"])
        container.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        container.grid_rowconfigure(1, weight=1)
        container.grid_columnconfigure(0, weight=1)

        header_row = ctk.CTkFrame(container, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        bold_font = ctk.CTkFont(weight="bold", size=12)
        ctk.CTkLabel(header_row, text="KOLOM", font=bold_font, width=220, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header_row, text="STATUS", font=bold_font, width=100, anchor="w").grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(header_row, text="FUNGSI PENGOLAHAN", font=bold_font, width=250, anchor="w").grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(header_row, text="GROUPING DATA (nilai unik)", font=bold_font, anchor="w").grid(row=0, column=3, sticky="w")

        self.scroll_frame = ctk.CTkScrollableFrame(container, fg_color="transparent")
        self.scroll_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.scroll_frame.grid_columnconfigure(3, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.scroll_frame, text="Belum ada file yang diunggah.", text_color=WARNA["teks_dim"]
        )
        self.empty_label.grid(row=0, column=0, sticky="w", padx=8, pady=8)

    # -----------------------------------------------------------------
    def _buat_bagian_bawah(self):
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 16))
        bottom.grid_columnconfigure(0, weight=1)

        self.label_ringkasan = ctk.CTkLabel(bottom, text="", anchor="w")
        self.label_ringkasan.grid(row=0, column=0, sticky="w")

        tombol_frame = ctk.CTkFrame(bottom, fg_color="transparent")
        tombol_frame.grid(row=0, column=1, sticky="e")

        self.btn_csv = ctk.CTkButton(
            tombol_frame, text="Download CSV", command=self.download_csv, state="disabled", width=120
        )
        self.btn_csv.grid(row=0, column=0, padx=(0, 6))

        self.btn_xlsx = ctk.CTkButton(
            tombol_frame, text="Download XLSX", command=self.download_xlsx, state="disabled", width=120
        )
        self.btn_xlsx.grid(row=0, column=1)

        self.btn_gsheet = ctk.CTkButton(
            tombol_frame, text="Upload ke Google Sheets", command=self.upload_ke_gsheet,
            state="disabled", width=170
        )
        self.btn_gsheet.grid(row=0, column=2, padx=(6, 0))

    # -----------------------------------------------------------------
    def pilih_file(self):
        path = filedialog.askopenfilename(
            title="Pilih Data_TA_proses.xlsx",
            filetypes=[("Excel files", "*.xlsx *.xls")]
        )
        if not path:
            return
        try:
            df_mentah = pd.read_excel(path, sheet_name=0, header=None)
        except Exception as e:
            messagebox.showerror("Gagal membaca file", str(e))
            return

        baris_header = cari_baris_header(df_mentah)
        headers = [str(v).strip() for v in df_mentah.iloc[baris_header] if pd.notna(v) and str(v).strip() != ""]

        try:
            df_full = pd.read_excel(path, sheet_name=0, header=baris_header)
        except Exception as e:
            messagebox.showerror("Gagal membaca isi data", str(e))
            return
        df_full = df_full[[c for c in df_full.columns if not str(c).startswith("Unnamed")]]
        df_full.columns = [str(c).strip() for c in df_full.columns]

        self.headers = headers
        self.df_full = df_full
        self.path_file = path
        self.pernah_dikenal = {}  # reset: file baru, belum ada rename apapun
        self.rename_output_langsung = {}  # reset juga
        self.nama_laporan = os.path.splitext(os.path.basename(path))[0]
        self.label_file.configure(
            text=f"{os.path.basename(path)}  ({len(headers)} kolom, {len(df_full)} baris data, header baris {baris_header + 1})"
        )
        self.render_tabel(headers)
        if self.modul_etl is not None:
            self.btn_jalankan_etl.configure(state="normal")

    # -----------------------------------------------------------------
    def render_tabel(self, headers, pilihan_sebelumnya=None):
        if pilihan_sebelumnya is None:
            pilihan_sebelumnya = {
                nama: combo.get() for nama, status, combo in self.baris_widget
                if status == "baru" and combo is not None
            }

        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self.baris_widget = []

        dikenal_norm = {h.strip().upper() for h in HEADER_DIKENAL}
        jumlah_baru = 0
        baris_grid = 0  # baris grid aktual; bertambah lebih dari 1 untuk kolom bersub-baris turunan

        for nama in headers:
            is_dikenal = (nama.strip().upper() in dikenal_norm) or (nama in self.pernah_dikenal)
            if not is_dikenal:
                jumlah_baru += 1

            series = self.df_full[nama] if (self.df_full is not None and nama in self.df_full.columns) else None
            kolom_turunan = SPLIT_KOLOM.get(nama.strip().upper())

            if kolom_turunan:
                # --- header INPUT yang dipecah jadi banyak kolom output: read-only ---
                ctk.CTkLabel(self.scroll_frame, text=nama, width=220, anchor="w", text_color=WARNA["teks_dim"]).grid(
                    row=baris_grid, column=0, sticky="w", padx=8, pady=4
                )
                ctk.CTkLabel(
                    self.scroll_frame, text="● dikenal", text_color=WARNA["sukses"], width=100, anchor="w"
                ).grid(row=baris_grid, column=1, sticky="w")
                ctk.CTkLabel(
                    self.scroll_frame, text=f"— dipecah otomatis jadi {len(kolom_turunan)} kolom output —",
                    text_color=WARNA["teks_dim"], anchor="w"
                ).grid(row=baris_grid, column=2, sticky="w")

                daftar_grup, total_unik, _ = hitung_grouping(series)
                if daftar_grup:
                    btn_grup = ctk.CTkButton(
                        self.scroll_frame, text=f"{total_unik} variasi ▾ (klik untuk lihat semua)",
                        width=340, fg_color=WARNA["kartu"], text_color=WARNA["teks_dim"],
                        hover_color=("gray85", "gray25"), anchor="w",
                        command=lambda nm=nama, dg=daftar_grup: self._tampilkan_grouping(nm, dg)
                    )
                    btn_grup.grid(row=baris_grid, column=3, sticky="w", padx=(8, 8), pady=4)
                else:
                    ctk.CTkLabel(
                        self.scroll_frame, text="(tidak ada data)", text_color=WARNA["teks_dim"],
                        font=ctk.CTkFont(size=10)
                    ).grid(row=baris_grid, column=3, sticky="w", padx=(8, 8), pady=4)

                self.baris_widget.append((nama, "dikenal", None))
                baris_grid += 1

                # --- sub-baris: tiap kolom turunan bisa di-rename sendiri ---
                for nama_output in kolom_turunan:
                    nama_tampil = self.rename_output_langsung.get(nama_output, nama_output)

                    entry_turunan = ctk.CTkEntry(self.scroll_frame, width=200)
                    entry_turunan.insert(0, nama_tampil)
                    entry_turunan.grid(row=baris_grid, column=0, sticky="w", padx=(28, 8), pady=2)
                    entry_turunan.bind("<Return>", lambda e: e.widget.master.focus_set())
                    entry_turunan.bind(
                        "<FocusOut>", lambda e, asli=nama_output: self._commit_rename_output(asli, e.widget)
                    )

                    ctk.CTkLabel(
                        self.scroll_frame, text="turunan", text_color=WARNA["teks_dim"], width=100, anchor="w",
                        font=ctk.CTkFont(size=10)
                    ).grid(row=baris_grid, column=1, sticky="w")
                    ctk.CTkLabel(
                        self.scroll_frame, text="— nama kolom output —", text_color=WARNA["teks_dim"],
                        font=ctk.CTkFont(size=10), anchor="w"
                    ).grid(row=baris_grid, column=2, sticky="w")
                    ctk.CTkLabel(
                        self.scroll_frame, text="(rincian tidak tersedia di pratinjau)", text_color=WARNA["teks_dim"],
                        font=ctk.CTkFont(size=10)
                    ).grid(row=baris_grid, column=3, sticky="w", padx=(8, 8))

                    baris_grid += 1

                continue

            # --- kolom biasa (bukan kolom yang dipecah) ---
            entry_nama = ctk.CTkEntry(self.scroll_frame, width=220)
            entry_nama.insert(0, nama)
            entry_nama.grid(row=baris_grid, column=0, sticky="w", padx=8, pady=4)
            entry_nama.bind("<Return>", lambda e: e.widget.master.focus_set())
            entry_nama.bind("<FocusOut>", lambda e, lama=nama: self._commit_rename(lama, e.widget))

            if is_dikenal:
                ctk.CTkLabel(
                    self.scroll_frame, text="● dikenal", text_color=WARNA["sukses"], width=100, anchor="w"
                ).grid(row=baris_grid, column=1, sticky="w")
                ctk.CTkLabel(
                    self.scroll_frame, text="— sudah tertangani —", text_color=WARNA["teks_dim"], anchor="w"
                ).grid(row=baris_grid, column=2, sticky="w")
                self.baris_widget.append((nama, "dikenal", None))
            else:
                ctk.CTkLabel(
                    self.scroll_frame, text="◆ BARU", text_color=WARNA["aksen"], width=100, anchor="w",
                    font=ctk.CTkFont(weight="bold")
                ).grid(row=baris_grid, column=1, sticky="w")

                rekom, keterangan = rekomendasi_gabungan(nama, series)

                sel_frame = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
                sel_frame.grid(row=baris_grid, column=2, sticky="w", pady=4)

                combo = ctk.CTkComboBox(
                    sel_frame,
                    values=[label for _, label in FUNGSI_TERSEDIA],
                    state="readonly",
                    width=320,
                )
                if nama in pilihan_sebelumnya:
                    combo.set(pilihan_sebelumnya[nama])
                else:
                    combo.set(LABEL_FUNGSI.get(rekom, LABEL_FUNGSI[""]))
                combo.pack(anchor="w")

                catatan_txt = f"terdeteksi: {keterangan}" if keterangan else "tidak ada rekomendasi — pilih manual"
                ctk.CTkLabel(
                    sel_frame, text=catatan_txt, text_color=WARNA["aksen"] if keterangan else WARNA["teks_dim"],
                    font=ctk.CTkFont(size=10), anchor="w"
                ).pack(anchor="w")

                self.baris_widget.append((nama, "baru", combo))

            # --- kolom grouping data (tombol -> jendela pop-up scrollable berisi semua variasi) ---
            daftar_grup, total_unik, total_baris_isi = hitung_grouping(series)
            if daftar_grup:
                btn_grup = ctk.CTkButton(
                    self.scroll_frame, text=f"{total_unik} variasi ▾ (klik untuk lihat semua)",
                    width=340, fg_color=WARNA["kartu"], text_color=WARNA["teks_dim"],
                    hover_color=("gray85", "gray25"), anchor="w",
                    command=lambda nm=nama, dg=daftar_grup: self._tampilkan_grouping(nm, dg)
                )
                btn_grup.grid(row=baris_grid, column=3, sticky="w", padx=(8, 8), pady=4)
            else:
                ctk.CTkLabel(
                    self.scroll_frame, text="(tidak ada data)", text_color=WARNA["teks_dim"],
                    font=ctk.CTkFont(size=10)
                ).grid(row=baris_grid, column=3, sticky="w", padx=(8, 8), pady=4)

            baris_grid += 1

        if not headers:
            self.empty_label.grid(row=0, column=0, sticky="w", padx=8, pady=8)

        total = len(headers)
        self.label_ringkasan.configure(
            text=f"Total kolom: {total}   |   Dikenal: {total - jumlah_baru}   |   Kolom baru: {jumlah_baru}"
        )
        state_download = "normal" if (total > 0 and self.modul_etl is not None) else "disabled"
        self.btn_csv.configure(state=state_download)
        self.btn_xlsx.configure(state=state_download)

    # -----------------------------------------------------------------
    def _tampilkan_grouping(self, nama_kolom, daftar_grup):
        win = ctk.CTkToplevel(self)
        win.title(f"Grouping — {nama_kolom}")
        win.geometry("460x420")

        ctk.CTkLabel(
            win, text=f"Semua variasi nilai pada kolom \"{nama_kolom}\":",
            font=ctk.CTkFont(weight="bold")
        ).pack(anchor="w", padx=14, pady=(14, 6))

        frame_scroll = ctk.CTkScrollableFrame(win)
        frame_scroll.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        for teks in daftar_grup:
            ctk.CTkLabel(frame_scroll, text=teks, anchor="w", justify="left").pack(
                fill="x", anchor="w", padx=4, pady=2
            )

    # -----------------------------------------------------------------
    def _commit_rename(self, nama_lama, entry_widget):
        if self.df_full is None:
            return
        try:
            nama_baru = entry_widget.get().strip()
        except tk.TclError:
            return

        if not nama_baru or nama_baru == nama_lama:
            return

        if nama_baru in self.df_full.columns:
            messagebox.showwarning("Nama sudah dipakai", f'Kolom "{nama_baru}" sudah ada. Pilih nama lain.')
            try:
                entry_widget.delete(0, "end")
                entry_widget.insert(0, nama_lama)
            except tk.TclError:
                pass
            return

        # kalau kolom LAMA statusnya "dikenal" (baik dari nama asli maupun
        # karena rename sebelumnya), status & identitas aslinya diwariskan
        # ke nama BARU -- jadi rename kolom lama TIDAK akan terdeteksi
        # sebagai kolom baru, dan fungsi otomatisnya tetap akurat
        dikenal_norm = {h.strip().upper() for h in HEADER_DIKENAL}
        asal_lama = self.pernah_dikenal.pop(nama_lama, None)
        lama_dikenal = (nama_lama.strip().upper() in dikenal_norm) or (asal_lama is not None)
        if lama_dikenal:
            self.pernah_dikenal[nama_baru] = asal_lama if asal_lama else nama_lama

        self.df_full = self.df_full.rename(columns={nama_lama: nama_baru})
        self.headers = [nama_baru if h == nama_lama else h for h in self.headers]
        self.render_tabel(self.headers)

    def _commit_rename_output(self, nama_output_asli, entry_widget):
        """Rename langsung untuk kolom TURUNAN (mis. desa_tkp), tidak
        mengubah df_full/headers karena kolom ini memang tidak ada di data
        input mentah -- baru terbentuk setelah pipeline ETL nyata berjalan."""
        try:
            nama_baru = entry_widget.get().strip()
        except tk.TclError:
            return

        if not nama_baru or nama_baru == nama_output_asli:
            self.rename_output_langsung.pop(nama_output_asli, None)
            return

        semua_nama_turunan = {k for v in SPLIT_KOLOM.values() for k in v}
        nama_terpakai = {
            v for k, v in self.rename_output_langsung.items() if k != nama_output_asli
        } | (semua_nama_turunan - {nama_output_asli})
        if nama_baru in nama_terpakai:
            messagebox.showwarning("Nama sudah dipakai", f'Nama "{nama_baru}" sudah dipakai kolom lain. Pilih nama lain.')
            try:
                entry_widget.delete(0, "end")
                entry_widget.insert(0, self.rename_output_langsung.get(nama_output_asli, nama_output_asli))
            except tk.TclError:
                pass
            return

        self.rename_output_langsung[nama_output_asli] = nama_baru

    def _bangun_peta_rename_output(self):
        """Terjemahkan rename kolom di GUI (nama INPUT) jadi rename kolom
        OUTPUT untuk sistem ETL nyata, hanya untuk kolom yang pemetaannya
        1:1 jelas (lihat RAW_KE_OUTPUT), digabung dengan rename langsung
        pada kolom-kolom turunan (lihat SPLIT_KOLOM)."""
        peta = {}
        for nama_sekarang, nama_asli in self.pernah_dikenal.items():
            if nama_sekarang == nama_asli:
                continue
            kolom_output = RAW_KE_OUTPUT.get(nama_asli.strip().upper())
            if kolom_output:
                peta[kolom_output] = nama_sekarang
        peta.update(self.rename_output_langsung)
        return peta

    # -----------------------------------------------------------------
    def _jalankan_pipeline_nyata(self, simpan_csv, simpan_xlsx, judul):
        if self.modul_etl is None or self.path_file is None:
            return

        log_win = ctk.CTkToplevel(self)
        log_win.title(judul)
        log_win.geometry("640x420")
        ctk.CTkLabel(log_win, text=f"{judul} — log tampil live di bawah", text_color=WARNA["teks_dim"]).pack(anchor="w", padx=12, pady=(12, 4))
        log_box = ctk.CTkTextbox(log_win, font=("Consolas", 11))
        log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        log_box.configure(state="disabled")

        for btn in (self.btn_jalankan_etl, self.btn_csv, self.btn_xlsx, self.btn_gsheet):
            btn.configure(state="disabled")

        peta_rename_output = self._bangun_peta_rename_output()

        antrian = queue.Queue()

        class StreamKeAntrian:
            def write(self_stream, s):
                if s:
                    antrian.put(("log", s))

            def flush(self_stream):
                pass

        def tulis_log(teks):
            log_box.configure(state="normal")
            log_box.insert("end", teks)
            log_box.see("end")
            log_box.configure(state="disabled")

        monitor = MonitorSpesifikasi(interval=0.5) if PSUTIL_TERSEDIA else None

        def worker():
            hasil, error = None, None
            if monitor is not None:
                monitor.mulai()
            try:
                with contextlib.redirect_stdout(StreamKeAntrian()):
                    hasil = self.modul_etl.jalankan_etl(
                        self.path_file,
                        simpan_csv=simpan_csv,
                        simpan_xlsx=simpan_xlsx,
                        peta_rename_output=peta_rename_output,
                    )
            except Exception as e:
                error = e
            finally:
                if monitor is not None:
                    monitor.berhenti()
            antrian.put(("selesai", (hasil, error)))

        def cek_antrian():
            try:
                while True:
                    jenis, isi = antrian.get_nowait()
                    if jenis == "log":
                        tulis_log(isi)
                    elif jenis == "selesai":
                        selesai(*isi)
                        return
            except queue.Empty:
                pass
            log_win.after(150, cek_antrian)

        def selesai(hasil, error):
            for btn in (self.btn_jalankan_etl, self.btn_csv, self.btn_xlsx):
                btn.configure(state="normal")
            if error is not None:
                tulis_log(f"\n\n[GAGAL] {error}")
                messagebox.showerror("ETL gagal", f"Terjadi kesalahan saat menjalankan ETL:\n{error}")
                return

            self.hasil_terakhir = hasil
            # Tombol upload cuma aktif kalau ada file CSV yang baru dihasilkan
            # (fitur upload ke Google Sheets sumbernya dari CSV)
            if hasil.get("csv_file"):
                self.btn_gsheet.configure(state="normal")

            file_dihasilkan = [f for f in (hasil.get("csv_file"), hasil.get("xlsx_file")) if f]
            daftar_path = "\n".join(os.path.join(os.getcwd(), f) for f in file_dihasilkan)
            tulis_log(f"\n\n[SELESAI] File tersimpan:\n{daftar_path}")

            if monitor is not None and monitor.ringkasan() is not None:
                tulis_log(f"\n\n[SPESIFIKASI RESOURCE]\n{monitor.teks_ringkasan()}")
                path_log_spek = os.path.join(self._folder_app(), "log_spesifikasi_minimum.csv")
                monitor.simpan_ke_csv(path_log_spek, judul)
                tulis_log(f"\n(disimpan ke {path_log_spek})")
            elif not PSUTIL_TERSEDIA:
                tulis_log(
                    "\n\n[INFO] Logging CPU/RAM tidak aktif -- library 'psutil' belum "
                    "terinstall. Jalankan: pip install psutil"
                )

            jawab = messagebox.askyesno(
                "ETL selesai",
                "Proses selesai.\n\nFile dihasilkan:\n" + "\n".join(file_dihasilkan) + "\n\nBuka folder hasil sekarang?"
            )
            if jawab:
                buka_folder(os.getcwd())

        threading.Thread(target=worker, daemon=True).start()
        log_win.after(150, cek_antrian)

    # -----------------------------------------------------------------
    def jalankan_etl_lengkap(self):
        self._jalankan_pipeline_nyata(simpan_csv=True, simpan_xlsx=True, judul="Menjalankan ETL Lengkap (CSV + XLSX)")

    def download_csv(self):
        self._jalankan_pipeline_nyata(simpan_csv=True, simpan_xlsx=False, judul="Menjalankan ETL — Download CSV")

    def download_xlsx(self):
        self._jalankan_pipeline_nyata(simpan_csv=False, simpan_xlsx=True, judul="Menjalankan ETL — Download XLSX Terstruktur")

    # =====================================================================
    # UPLOAD KE GOOGLE SHEETS
    # ---------------------------------------------------------------------
    # Kredensial (service account JSON) & konfigurasi (spreadsheet id + nama
    # sheet) disimpan sebagai file di folder yang sama dengan aplikasi, jadi
    # cukup diisi/dibuat SEKALI saja:
    #   - gsheets_credentials.json  -> kredensial service account
    #   - gsheet_config.json        -> {"spreadsheet_id": ..., "worksheet_name": ...}
    # =====================================================================

    def _folder_app(self):
        return os.getcwd()

    def _path_kredensial_gsheet(self):
        return os.path.join(self._folder_app(), "gsheets_credentials.json")

    def _path_config_gsheet(self):
        return os.path.join(self._folder_app(), "gsheet_config.json")

    def _muat_config_gsheet(self):
        path = self._path_config_gsheet()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("spreadsheet_id") and cfg.get("worksheet_name"):
                return cfg
        except Exception:
            pass
        return None

    def _simpan_config_gsheet(self, spreadsheet_id, nama_sheet):
        with open(self._path_config_gsheet(), "w", encoding="utf-8") as f:
            json.dump({"spreadsheet_id": spreadsheet_id, "worksheet_name": nama_sheet}, f, indent=2)

    def _minta_config_gsheet(self):
        """Tanya spreadsheet tujuan & nama sheet ke user (dipanggil sekali,
        hasilnya disimpan ke gsheet_config.json supaya tidak ditanya lagi)."""
        dlg1 = ctk.CTkInputDialog(
            text="Tempel LINK atau ID Google Spreadsheet tujuan:",
            title="Hubungkan ke Google Sheets"
        )
        input_link = dlg1.get_input()
        if not input_link:
            return None
        input_link = input_link.strip()

        m = re.search(r'/d/([a-zA-Z0-9-_]+)', input_link)
        spreadsheet_id = m.group(1) if m else input_link

        dlg2 = ctk.CTkInputDialog(
            text="Nama sheet/tab tujuan (kosongkan untuk 'Sheet1'):",
            title="Hubungkan ke Google Sheets"
        )
        nama_sheet = (dlg2.get_input() or "").strip() or "Sheet1"

        self._simpan_config_gsheet(spreadsheet_id, nama_sheet)
        return {"spreadsheet_id": spreadsheet_id, "worksheet_name": nama_sheet}

    def upload_ke_gsheet(self):
        if not GSPREAD_TERSEDIA:
            messagebox.showerror(
                "Library belum terinstall",
                "Fitur ini butuh library tambahan. Jalankan dulu di terminal:\n\n"
                "pip install gspread google-auth\n\n"
                "Lalu buka ulang aplikasinya."
            )
            return

        if not self.hasil_terakhir or not self.hasil_terakhir.get("csv_file"):
            messagebox.showwarning(
                "Belum ada CSV",
                "Jalankan dulu ETL (Download CSV / ETL Lengkap) sebelum upload ke Google Sheets."
            )
            return

        path_kredensial = self._path_kredensial_gsheet()
        if not os.path.exists(path_kredensial):
            messagebox.showerror(
                "Kredensial belum ada",
                "File 'gsheets_credentials.json' belum ditemukan di folder aplikasi:\n\n"
                f"{self._folder_app()}\n\n"
                "Cara buatnya:\n"
                "1. Buka console.cloud.google.com, buat/pilih project.\n"
                "2. Aktifkan 'Google Sheets API' dan 'Google Drive API'.\n"
                "3. Menu IAM & Admin > Service Accounts > Create Service Account.\n"
                "4. Buka service account itu > tab 'Keys' > Add Key > Create new key > JSON.\n"
                "5. File JSON otomatis terunduh -- rename jadi 'gsheets_credentials.json',\n"
                "   taruh di folder yang sama dengan aplikasi ini.\n"
                "6. Buka Google Sheet tujuan > tombol Share > tempel email\n"
                "   'client_email' yang ada di dalam file JSON itu, beri akses Editor.\n\n"
                "Setelah selesai, klik tombol ini lagi."
            )
            return

        config = self._muat_config_gsheet()
        if config is None:
            config = self._minta_config_gsheet()
            if config is None:
                return  # user membatalkan dialog

        path_csv = self.hasil_terakhir["csv_file"]

        log_win = ctk.CTkToplevel(self)
        log_win.title("Upload ke Google Sheets")
        log_win.geometry("480x160")
        label = ctk.CTkLabel(log_win, text="Mengupload data ke Google Sheets...", text_color=WARNA["teks_dim"])
        label.pack(anchor="w", padx=16, pady=(16, 8))
        progress = ctk.CTkProgressBar(log_win, mode="indeterminate")
        progress.pack(fill="x", padx=16, pady=(0, 16))
        progress.start()

        self.btn_gsheet.configure(state="disabled")
        antrian = queue.Queue()

        def worker():
            try:
                jumlah_baru, jumlah_duplikat = self._kirim_csv_ke_gsheet(
                    path_csv, path_kredensial,
                    config["spreadsheet_id"], config["worksheet_name"]
                )
                antrian.put(("ok", (jumlah_baru, jumlah_duplikat)))
            except Exception as e:
                antrian.put(("error", str(e)))

        def cek_antrian():
            try:
                status, isi = antrian.get_nowait()
            except queue.Empty:
                log_win.after(150, cek_antrian)
                return
            progress.stop()
            log_win.destroy()
            self.btn_gsheet.configure(state="normal")
            if status == "ok":
                jumlah_baru, jumlah_duplikat = isi
                pesan = f"{jumlah_baru} baris berhasil ditambahkan ke sheet '{config['worksheet_name']}'."
                if jumlah_duplikat:
                    pesan += f"\n{jumlah_duplikat} baris dilewati karena sudah ada di sheet (duplikat)."
                messagebox.showinfo("Upload berhasil", pesan)
            else:
                messagebox.showerror("Upload gagal", f"Terjadi kesalahan saat upload:\n{isi}")

        threading.Thread(target=worker, daemon=True).start()
        log_win.after(150, cek_antrian)

    def _kirim_csv_ke_gsheet(self, path_csv, path_kredensial, spreadsheet_id, nama_sheet):
        """Baca file CSV lalu APPEND isinya ke sheet tujuan (baris lama tidak
        dihapus/ditimpa). Baris yang NOMOR_LAPORAN-nya sudah ada di sheet
        otomatis dilewati (dianggap duplikat), jadi tidak dobel walau file
        yang sama diupload berkali-kali. Kalau kolom 'nomor_laporan' tidak
        ketemu di CSV (mis. sudah di-rename lewat GUI), otomatis jatuh balik
        ke pengecekan baris-persis-sama (exact match) supaya tetap aman.
        Kalau sheet masih kosong, header ikut ditulis dulu di baris pertama.
        Return (jumlah_baris_ditambahkan, jumlah_duplikat)."""
        scope = ["https://www.googleapis.com/auth/spreadsheets",
                 "https://www.googleapis.com/auth/drive"]
        creds = GCredentials.from_service_account_file(path_kredensial, scopes=scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            sheet = spreadsheet.worksheet(nama_sheet)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=nama_sheet, rows=1000, cols=50)

        df_csv = pd.read_csv(path_csv, dtype=str).fillna("")
        header = df_csv.columns.tolist()
        baris_data = df_csv.astype(str).values.tolist()

        data_sheet_saat_ini = sheet.get_all_values()
        sheet_kosong = (not data_sheet_saat_ini) or all(c == "" for c in data_sheet_saat_ini[0])

        def _cari_kolom(nama_header, nama_kolom):
            for i, kol in enumerate(nama_header):
                if kol.strip().lower() == nama_kolom:
                    return i
            return None

        idx_nomor_csv = _cari_kolom(header, "nomor_laporan")
        idx_nomor_sheet = _cari_kolom(data_sheet_saat_ini[0], "nomor_laporan") if data_sheet_saat_ini else None

        baris_baru = []
        jumlah_duplikat = 0

        if idx_nomor_csv is not None and idx_nomor_sheet is not None:
            # --- Mode: cek berdasarkan nomor_laporan saja ---
            nomor_lama_set = set()
            if not sheet_kosong and len(data_sheet_saat_ini) > 1:
                for row in data_sheet_saat_ini[1:]:
                    if idx_nomor_sheet < len(row) and row[idx_nomor_sheet] != "":
                        nomor_lama_set.add(row[idx_nomor_sheet])

            for row in baris_data:
                nomor = row[idx_nomor_csv] if idx_nomor_csv < len(row) else ""
                if nomor != "" and nomor in nomor_lama_set:
                    jumlah_duplikat += 1
                    continue
                baris_baru.append(row)
                if nomor != "":
                    nomor_lama_set.add(nomor)  # cegah duplikat di dalam file yg sama juga
        else:
            # --- Fallback: kolom nomor_laporan tidak ketemu -> exact match ---
            baris_lama_set = set()
            if not sheet_kosong and len(data_sheet_saat_ini) > 1:
                for row in data_sheet_saat_ini[1:]:
                    baris_lama_set.add(tuple(row))
            panjang_acuan = len(header)

            def _kunci(row):
                row_str = [str(v) for v in row]
                row_str = (row_str + [""] * panjang_acuan)[:panjang_acuan]
                return tuple(row_str)

            for row in baris_data:
                kunci = _kunci(row)
                if kunci in baris_lama_set:
                    jumlah_duplikat += 1
                    continue
                baris_baru.append(row)
                baris_lama_set.add(kunci)

        if sheet_kosong:
            # value_input_option="RAW" supaya teks/kode dengan angka nol di
            # depan (mis. NIK tersensor, nomor pasal) TIDAK diubah otomatis
            # jadi angka oleh Google Sheets -- disimpan persis apa adanya.
            sheet.append_row(header, value_input_option="RAW")

        if baris_baru:
            sheet.append_rows(baris_baru, value_input_option="RAW")

        return len(baris_baru), jumlah_duplikat


if __name__ == "__main__":
    app = ColumnInspectorApp()
    app.mainloop()
