# CodexCleaner — audit penyimpanan dan pemulihan

[English](README.md) · [Panduan asli berbahasa Mandarin](README.zh-CN.md)

Cari folder yang memenuhi disk, bedakan cache dari hasil pekerjaan, lalu karantina hanya file yang sudah dipastikan boleh dibuang. Folder bernama `Temp`, berukuran besar, atau sudah lama tidak otomatis aman dihapus.

> **Panduan fork:** proyek ini dikembangkan dari [Kynepen/codexcleaner](https://github.com/Kynepen/codexcleaner). Untuk pembersihan penyimpanan, gunakan README ini, [SKILL.md](SKILL.md), dan [storage-safety.md](references/storage-safety.md). Dokumentasi Mandarin serta referensi lama tetap disimpan sebagai materi upstream. Penghapusan riwayat tugas harus diminta secara terpisah dan eksplisit.

## 1. Periksa dahulu tanpa mengubah file

Gunakan Python 3.10 atau lebih baru. Tidak diperlukan paket Python tambahan. Dari folder repositori ini, jalankan:

```powershell
New-Item -ItemType Directory -Path work -Force | Out-Null
python scripts/storage_audit.py --only-roots --root "$env:LOCALAPPDATA" --json --output "work/storage-audit.json" --markdown "work/storage-audit.md"
```

Tambahkan `--root` lagi untuk lokasi lain. `--only-roots` membatasi audit pada lokasi tersebut; tanpa opsi ini, penemuan bawaan menambahkan lokasi aplikasi yang dikenal. Gunakan nama laporan baru setiap kali menjalankan audit karena laporan lama tidak ditimpa. Audit membaca metadata, bukan isi dokumen atau percakapan. Laporan mencatat lokasi yang tidak dapat diakses dan tautan yang dilewati. Laporan memuat lokasi file pribadi; simpan secara lokal.

Periksa penanda kelengkapan dan batas pemindaian pada laporan. Audit dibatasi waktu, jumlah entri/direktori, dan kedalaman. Audit yang belum lengkap menghasilkan status keluar 1. Pilih lokasi yang lebih sempit atau sesuaikan batas melalui opsi di `--help` sebelum menganggapnya audit lengkap.

Ukuran folder adalah **ukuran logis per lokasi**, bukan janji ruang yang akan kembali. Hardlink, alias profil aplikasi, file sparse, dan subfolder yang saling mencakup dapat menyebabkan hitungan ganda. File `.vhdx` adalah disk virtual yang mungkin berisi seluruh lingkungan kerja.

## 2. Lindungi hasil pekerjaan

Lindungi proyek, repositori, worktree, kode, dokumen, database, laporan, gambar hasil ekspor, arsip, riwayat tugas, data login, dan state browser. Data tersebut bukan cache umum.

Temp juga bisa berisi satu-satunya laporan atau backup. Pola `neuroguide-report-*` dan `neuroguide-research-*` hanya membantu menemukan bahan untuk ditinjau. Pola tersebut **bukan izin menghapus semuanya**. Perlindungan bawaan membantu mengenali proyek dan state, tetapi tidak dapat menentukan nilai setiap file; tambahkan lokasi penting ke kebijakan perlindungan.

## 3. Buat rencana dengan target kosong sebagai awal

Simpan sebagai `policy.json`:

```json
{
  "version": 1,
  "protected_roots": [],
  "rules": []
}
```

Masukkan lokasi proyek dan hasil pekerjaan ke `protected_roots`. Tambahkan aturan hanya untuk direktori tepat yang sudah diperiksa dan dipastikan boleh dibuang. Lihat format dan contoh di [panduan kebijakan](references/storage-safety.md).

```powershell
python scripts/storage_guard.py plan --policy policy.json --output plan.json
```

Perintah ini belum membersihkan apa pun. Gunakan nama keluaran baru jika membuat rencana lagi. `rules` kosong berarti tidak ada target. Tinjau daftar file, item yang dilindungi/dilewati, ukuran, dan ID rencana. Batas umur file minimal **7 hari**; rencana berlaku **24 jam**. File diperiksa ulang sebelum tindakan, dan file yang berubah dilewati. Direktori `reviewed_temp` dikeluarkan seluruhnya dari tindakan bila pemeriksaan awal mendeteksi perubahan atau item yang tidak memenuhi syarat.

Audit hanya memakai metadata. Pembuatan rencana membaca byte file kandidat untuk pemeriksaan perlindungan dan sidik SHA-256; karantina dan pemulihan memverifikasi keutuhan isi. Semua proses berjalan lokal tanpa API key atau unggahan jaringan.

## 4. Karantina setelah cakupan disetujui

Selesaikan pekerjaan aktif dan tutup aplikasi AI terkait secara normal. Jalankan dari **PowerShell di luar aplikasi AI**. Guard menolak tindakan bila proses AI/tool terkait masih berjalan atau pemeriksaan proses gagal. Guard tidak menutup paksa aplikasi.

```powershell
python scripts/storage_guard.py apply --plan plan.json --policy policy.json --quarantine-root "D:\CodexCleaner-Quarantine" --confirm PLAN_ID
```

Ganti `PLAN_ID` dengan ID yang dicetak oleh `plan`. Gunakan volume tujuan yang tersedia dan memiliki cukup ruang. Karantina harus berada di luar sumber dan lokasi yang dilindungi.

Salinan ke volume lain diverifikasi sebelum sumber dilepas. Simpan **seluruh direktori run karantina di lokasi aslinya**, termasuk `manifest.json`, `journal.jsonl`, dan `files`. Jurnal mencatat kemajuan sebelum pelepasan sumber dan membantu pemulihan bila proses terputus. Karantina di drive C yang sama **belum menambah ruang kosong drive C**. Mekanisme ini untuk file yang boleh dibuang, bukan backup lengkap: izin NTFS/ACL asli tidak dicadangkan.

Untuk satu direktori Temp yang ditinjau, guard mengunci dan memverifikasi salinan semua file terpilih sebelum melepas sumber pertama. Namun, tindakan ini bukan transaksi sistem berkas yang serentak dan utuh. Crash atau perubahan bersamaan masih dapat membuat sebagian tindakan selesai dan sebagian belum. Pertahankan salinan terverifikasi dan periksa hasil yang tercatat.

## 5. Pulihkan bila diperlukan

Gunakan direktori dan ID run yang tercatat dalam manifest:

```powershell
python scripts/storage_guard.py restore --manifest "D:\CodexCleaner-Quarantine\RUN_DIRECTORY\manifest.json" --confirm RUN_ID
```

Jika lokasi asal sudah berisi file baru atau salinan karantina berubah, periksa laporan konflik. Pertahankan salinan pemulihan; jangan menimpa data secara manual untuk memaksa pemulihan.

## 6. Hapus permanen sebagai keputusan terpisah

Setelah aplikasi dan hasil pekerjaan diverifikasi, salinan karantina yang sudah disimpan setidaknya **7 hari** dapat dipertimbangkan untuk purge:

```powershell
python scripts/storage_guard.py purge --manifest "D:\CodexCleaner-Quarantine\RUN_DIRECTORY\manifest.json" --confirm RUN_ID
```

Purge menghapus file karantina yang tercatat, bukan lokasi sumber. Persetujuan karantina tidak otomatis menjadi persetujuan penghapusan permanen. Tidak ada pembersihan berkala otomatis.

## Memakai skill

Pilih cabang fitur ini secara eksplisit selama perubahan storage guard belum digabungkan:

```powershell
git clone --branch feature/windows-storage-guard https://github.com/arthamegayasa/codexcleaner.git "$env:USERPROFILE\.codex\skills\codexcleaner"
```

```text
$codexcleaner Periksa folder terbesar di C tanpa menghapus apa pun. Pisahkan cache yang dapat dibuat ulang dari proyek, riwayat, state aplikasi, serta laporan dan hasil pekerjaan. Tunjukkan audit dan usulan targetnya.
```

Audit metadata riwayat Codex tetap tersedia melalui `python scripts/audit_codex.py --json`. Pengarsipan atau penghapusan tugas harus diminta secara terpisah dan memakai operasi resmi yang tersedia. Perbaikan database langsung bukan bagian alur yang direkomendasikan.

Gunakan `storage_guard.py` untuk pembersihan fork ini, termasuk karantina terverifikasi ke volume lain. Helper cache lama dipertahankan: launcher hanya mengaudit secara bawaan, memerlukan `--apply` untuk perubahan, dan menjaga seluruh data Service Worker. Helper lama memindahkan cache lewat rename pada volume yang sama, menolak backup lintas volume, dan tidak menghapus backup secara otomatis. Pemindahan itu tidak menambah ruang kosong.

Manifest backup helper lama tidak dapat dipertukarkan dengan manifest/jurnal `storage_guard.py`. Lihat [referensi lama berbahasa Inggris yang diperbarui](references/cache-maintenance.en.md) hanya bila helper tersebut memang diperlukan; dokumentasi Mandarin tetap menjadi arsip upstream.

## Pengujian dan atribusi

Jalankan `python -m unittest discover -s tests -v`. CI menguji Windows dan Ubuntu dengan Python 3.10 serta 3.13; apply/restore/purge hanya didukung pada Windows.

Sumber awal: [Kynepen/codexcleaner](https://github.com/Kynepen/codexcleaner). Fork: [arthamegayasa/codexcleaner](https://github.com/arthamegayasa/codexcleaner). Riwayat upstream dan dokumentasi Mandarin dipertahankan. Snapshot upstream tidak memiliki file `LICENSE`; fork ini tidak menambahkan atau mengasumsikan pemberian lisensi.
