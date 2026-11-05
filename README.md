# RentalKu — Website Rental Mobil (MVP)
Tanpa dependensi eksternal: Python 3.10+ (stdlib) + SQLite.

## Jalankan
    python3 server.py        # http://localhost:8000 (DB & seed dibuat otomatis)
    python3 test_api.py      # uji integrasi

Akun demo (fiktif): user@demo.test / User#12345 · admin@demo.test / Admin#12345

## Env (lihat nilai default di server.py)
PAYMENT_WEBHOOK_SECRET (WAJIB diganti di produksi), PAYMENT_HOLD_MINUTES=30, CLEANING_BUFFER_MINUTES=60, TAX_RATE=0.11, DB_PATH, PORT.

## Aturan bisnis
- Rentang waktu **[mulai, selesai)** (akhir eksklusif), disimpan UTC, + jeda pembersihan.
- Harga dihitung ulang di server; snapshot harga/kendaraan disimpan di booking. Deposit dipisah dari pendapatan sewa.
- Anti double booking: `BEGIN IMMEDIATE` + cek ulang ketersediaan (teruji dengan 5 request paralel).
- Pembayaran hanya sah lewat webhook ber-HMAC (idempotent, nominal diverifikasi).
- Transisi status dibatasi (`TRANSITIONS`), aksi admin masuk `audit_logs`.

## Belum selesai / placeholder (jujur)
Midtrans/Xendit (saat ini sandbox + webhook HMAC), email/notifikasi, upload & verifikasi dokumen, invoice PDF, refund, add-on, tarif akhir pekan/musim, CRUD admin armada/promo/cabang di UI, laporan CSV, ulasan, tiket bantuan, reset password, rate limiting, halaman Cara Sewa/Promo/Tentang, HTTPS & CSRF token (cookie SameSite=Strict sudah ada), migrasi ke PostgreSQL, tes E2E/lint/build.
