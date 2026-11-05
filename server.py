"""Rental mobil - backend tanpa dependensi (Python stdlib + SQLite). Jalankan: python3 server.py"""
import json, sqlite3, os, hmac, hashlib, secrets, math, re, threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB = os.environ.get("DB_PATH", "rental.db")
WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "dev-only-secret").encode()
HOLD_MIN = int(os.environ.get("PAYMENT_HOLD_MINUTES", "30"))
BUFFER_MIN = int(os.environ.get("CLEANING_BUFFER_MINUTES", "60"))
TAX = float(os.environ.get("TAX_RATE", "0.11"))
DRIVER_FEE = 200000  # per hari, bisa dipindah ke settings admin
BLOCKING = ("pending_payment", "confirmed", "ready_for_pickup", "ongoing")
TRANSITIONS = {  # transisi status yang diizinkan
    "pending_payment": {"confirmed", "expired", "cancelled"},
    "confirmed": {"ready_for_pickup", "cancellation_requested", "cancelled"},
    "ready_for_pickup": {"ongoing", "cancelled"},
    "ongoing": {"completed"},
    "cancellation_requested": {"cancelled", "confirmed"},
    "cancelled": {"refunded"}, "completed": set(), "expired": set(), "refunded": set()}
lock = threading.Lock()

def db():
    c = sqlite3.connect(DB, isolation_level=None); c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL"); return c

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
 phone TEXT, pw TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS branches(id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT, hours TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY, branch_id INTEGER REFERENCES branches(id), category TEXT NOT NULL,
 brand TEXT, model TEXT, year INTEGER, plate TEXT UNIQUE, seats INTEGER, transmission TEXT, fuel TEXT,
 daily_rate INTEGER NOT NULL CHECK(daily_rate>0), deposit INTEGER DEFAULT 0, with_driver INTEGER DEFAULT 1,
 self_drive INTEGER DEFAULT 1, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, percent INTEGER, min_total INTEGER DEFAULT 0, quota INTEGER, used INTEGER DEFAULT 0, valid_until TEXT);
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, user_id INTEGER REFERENCES users(id),
 vehicle_id INTEGER REFERENCES vehicles(id), branch_id INTEGER, start_at TEXT NOT NULL, end_at TEXT NOT NULL, service TEXT NOT NULL,
 status TEXT NOT NULL, subtotal INTEGER, discount INTEGER, tax INTEGER, deposit INTEGER, total INTEGER, snapshot TEXT,
 hold_until TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_book_veh ON bookings(vehicle_id,start_at,end_at);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, booking_id INTEGER REFERENCES bookings(id), provider TEXT,
 reference TEXT UNIQUE, amount INTEGER, status TEXT, paid_at TEXT);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT, entity TEXT, entity_id INTEGER, at TEXT DEFAULT CURRENT_TIMESTAMP, meta TEXT);
"""

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
def check_pw(pw, stored):
    salt, _ = stored.split("$"); return hmac.compare_digest(hash_pw(pw, salt), stored)

def seed():
    c = db(); c.executescript(SCHEMA)
    if c.execute("SELECT 1 FROM users").fetchone(): return
    c.execute("INSERT INTO users(name,email,pw,role) VALUES('Admin Demo','admin@demo.test',?, 'admin')", (hash_pw("Admin#12345"),))
    c.execute("INSERT INTO users(name,email,pw) VALUES('Pelanggan Demo','user@demo.test',?)", (hash_pw("User#12345"),))
    c.executemany("INSERT INTO branches(name,address,hours) VALUES(?,?,?)", [
        ("Cabang Kupang", "Jl. Contoh No. 1 (fiktif)", "07:00-21:00"), ("Cabang Bandara", "Terminal Kedatangan (fiktif)", "06:00-22:00")])
    c.executemany("INSERT INTO vehicles(branch_id,category,brand,model,year,plate,seats,transmission,fuel,daily_rate,deposit,with_driver) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1,"Ekonomi","Toyota","Agya",2022,"DD 1001 AA",5,"Manual","Bensin",250000,500000,1),
        (1,"Keluarga","Toyota","Avanza",2022,"DD 1002 AA",7,"Manual","Bensin",400000,750000,1),
        (2,"Premium","Toyota","Fortuner",2023,"DD 1003 AA",7,"Otomatis","Diesel",750000,1500000,1),
        (2,"Keluarga","Mitsubishi","Xpander",2023,"DD 1004 AA",7,"Otomatis","Bensin",450000,750000,0)])
    c.execute("INSERT INTO promos VALUES('HEMAT10',10,300000,100,0,'2030-12-31')")

def parse_dt(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
def iso(d): return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def now(): return datetime.now(timezone.utc)

class ApiError(Exception):
    def __init__(s, code, msg): s.code, s.msg = code, msg

def expire_holds(c):
    # Waktu akhir EKSKLUSIF: [start, end). Hold kedaluwarsa melepas kendaraan.
    c.execute("UPDATE bookings SET status='expired' WHERE status='pending_payment' AND hold_until<?", (iso(now()),))

def is_free(c, vid, start, end, ignore=None):
    b = timedelta(minutes=BUFFER_MIN)
    r = c.execute("""SELECT 1 FROM bookings WHERE vehicle_id=? AND status IN (%s) AND start_at<? AND end_at>? AND id IS NOT ?"""
        % ",".join("?" * len(BLOCKING)), (vid, *BLOCKING, iso(end + b), iso(start - b), ignore)).fetchone()
    return r is None

def validate_range(start_s, end_s):
    try: s, e = parse_dt(start_s), parse_dt(end_s)
    except Exception: raise ApiError(400, "Format tanggal tidak valid (gunakan ISO 8601)")
    if e <= s: raise ApiError(400, "Waktu selesai harus setelah waktu mulai")
    if s < now() - timedelta(minutes=5): raise ApiError(400, "Waktu mulai tidak boleh di masa lalu")
    return s, e

def quote(c, v, s, e, service, promo=None):
    # Semua harga dihitung di server; total dari browser tidak pernah dipercaya.
    if service == "dengan_sopir" and not v["with_driver"]: raise ApiError(400, "Kendaraan ini tidak tersedia dengan sopir")
    if service == "lepas_kunci" and not v["self_drive"]: raise ApiError(400, "Kendaraan ini tidak tersedia lepas kunci")
    days = max(1, math.ceil((e - s).total_seconds() / 86400))
    subtotal = days * v["daily_rate"] + (days * DRIVER_FEE if service == "dengan_sopir" else 0)
    discount = 0
    if promo:
        p = c.execute("SELECT * FROM promos WHERE code=?", (promo.upper(),)).fetchone()
        if not p or p["valid_until"] < iso(now())[:10] or (p["quota"] is not None and p["used"] >= p["quota"]) or subtotal < p["min_total"]:
            raise ApiError(422, "Kode promo tidak valid atau syarat tidak terpenuhi")
        discount = subtotal * p["percent"] // 100
    tax = round((subtotal - discount) * TAX)
    return dict(days=days, subtotal=subtotal, discount=discount, tax=tax, deposit=v["deposit"],
                rent_total=subtotal - discount + tax, total=subtotal - discount + tax + v["deposit"], currency="IDR")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, data, headers=None):
        body = json.dumps(data).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def user(self, c):
        m = re.search(r"sid=([\w-]+)", self.headers.get("Cookie", ""))
        if not m: return None
        return c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=? AND expires>?", (m[1], iso(now()))).fetchone()
    def handle_any(self, method):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}; path = u.path
        raw = b""
        if method == "POST":
            raw = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1_000_000))
        if not path.startswith("/api/"):
            f = "index.html" if path in ("/", "") else None
            if not f: return self.send(404, {"error": "Tidak ditemukan"})
            body = open(os.path.join(os.path.dirname(__file__), f), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); return self.wfile.write(body)
        try:
            body = json.loads(raw or b"{}") if path != "/api/payments/webhook" else {}
            with lock:  # SQLite tunggal-penulis; BEGIN IMMEDIATE di bawah menjaga atomisitas
                c = db()
                try: self.route(c, method, path, q, body, raw)
                finally: c.close()
        except ApiError as e: self.send(e.code, {"error": e.msg})
        except json.JSONDecodeError: self.send(400, {"error": "JSON tidak valid"})
        except Exception: self.send(500, {"error": "Kesalahan server"})  # detail tidak dibocorkan
    do_GET = lambda s: s.handle_any("GET"); do_POST = lambda s: s.handle_any("POST")

    def route(self, c, m, p, q, b, raw):
        expire_holds(c); me = self.user(c)
        def need(admin=False):
            if not me: raise ApiError(401, "Silakan login")
            if admin and me["role"] != "admin": raise ApiError(403, "Akses ditolak")
        if p == "/api/auth/register" and m == "POST":
            if not re.match(r"[^@\s]+@[^@\s]+\.\w+$", b.get("email", "")) or len(b.get("password", "")) < 8 or not b.get("name"):
                raise ApiError(422, "Nama, email valid, dan password minimal 8 karakter wajib diisi")
            try: c.execute("INSERT INTO users(name,email,phone,pw) VALUES(?,?,?,?)", (b["name"], b["email"].lower(), b.get("phone"), hash_pw(b["password"])))
            except sqlite3.IntegrityError: raise ApiError(409, "Email sudah terdaftar")
            return self.send(201, {"ok": True})
        if p == "/api/auth/login" and m == "POST":
            r = c.execute("SELECT * FROM users WHERE email=?", (b.get("email", "").lower(),)).fetchone()
            if not r or not check_pw(b.get("password", ""), r["pw"]): raise ApiError(401, "Email atau password salah")
            t = secrets.token_urlsafe(32)
            c.execute("INSERT INTO sessions VALUES(?,?,?)", (t, r["id"], iso(now() + timedelta(days=7))))
            return self.send(200, {"name": r["name"], "role": r["role"]}, {"Set-Cookie": f"sid={t}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800"})
        if p == "/api/auth/logout" and m == "POST":
            c.execute("DELETE FROM sessions WHERE token=?", (re.search(r"sid=([\w-]+)", self.headers.get("Cookie", "") or "sid=x")[1],))
            return self.send(200, {"ok": True}, {"Set-Cookie": "sid=; Max-Age=0; Path=/"})
        if p == "/api/me": return self.send(200, {"name": me["name"], "role": me["role"]} if me else {})
        if p == "/api/branches": return self.send(200, [dict(r) for r in c.execute("SELECT * FROM branches WHERE active=1")])
        if p == "/api/vehicles":
            s, e = validate_range(q["start"], q["end"]) if q.get("start") else (None, None)
            sql, a = "SELECT * FROM vehicles WHERE status='active'", []
            for k, col in (("branch", "branch_id"), ("category", "category"), ("transmission", "transmission")):
                if q.get(k): sql += f" AND {col}=?"; a.append(q[k])
            if q.get("seats"): sql += " AND seats>=?"; a.append(int(q["seats"]))
            if q.get("max_price"): sql += " AND daily_rate<=?"; a.append(int(q["max_price"]))
            sql += " ORDER BY daily_rate"
            out = []
            for v in c.execute(sql, a):
                d = dict(v); d["available"] = is_free(c, v["id"], s, e) if s else None
                if s and q.get("service"):
                    try: d["quote"] = quote(c, v, s, e, q["service"])
                    except ApiError: continue
                out.append(d)
            return self.send(200, out)
        if p == "/api/pricing/quote":
            v = c.execute("SELECT * FROM vehicles WHERE id=?", (q.get("vehicle_id"),)).fetchone()
            if not v: raise ApiError(404, "Kendaraan tidak ditemukan")
            s, e = validate_range(q["start"], q["end"]); return self.send(200, quote(c, v, s, e, q.get("service", "lepas_kunci"), q.get("promo")))
        if p == "/api/bookings" and m == "POST":
            need(); v = c.execute("SELECT * FROM vehicles WHERE id=? AND status='active'", (b.get("vehicle_id"),)).fetchone()
            if not v: raise ApiError(404, "Kendaraan tidak ditemukan")
            s, e = validate_range(b.get("start", ""), b.get("end", ""))
            qt = quote(c, v, s, e, b.get("service", ""), b.get("promo"))
            c.execute("BEGIN IMMEDIATE")  # cek ulang ketersediaan + insert atomik (anti double booking)
            try:
                if not is_free(c, v["id"], s, e): raise ApiError(409, "Kendaraan sudah dipesan pada rentang waktu tersebut")
                code = "RM-" + secrets.token_hex(3).upper()
                c.execute("""INSERT INTO bookings(code,user_id,vehicle_id,branch_id,start_at,end_at,service,status,subtotal,discount,tax,deposit,total,snapshot,hold_until)
                  VALUES(?,?,?,?,?,?,?,'pending_payment',?,?,?,?,?,?,?)""", (code, me["id"], v["id"], v["branch_id"], iso(s), iso(e), b["service"],
                  qt["subtotal"], qt["discount"], qt["tax"], qt["deposit"], qt["total"], json.dumps({"vehicle": dict(v), "quote": qt}), iso(now() + timedelta(minutes=HOLD_MIN))))
                if b.get("promo"): c.execute("UPDATE promos SET used=used+1 WHERE code=?", (b["promo"].upper(),))
                c.execute("COMMIT")
            except BaseException: c.execute("ROLLBACK"); raise
            return self.send(201, {"code": code, "status": "pending_payment", **qt})
        if p == "/api/bookings":
            need(); rows = c.execute("SELECT b.*,v.brand,v.model FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id WHERE user_id=? ORDER BY b.id DESC", (me["id"],))
            return self.send(200, [{k: r[k] for k in r.keys() if k != "snapshot"} for r in rows])
        mm = re.match(r"/api/bookings/([\w-]+)/cancel$", p)
        if mm and m == "POST":
            need(); r = c.execute("SELECT * FROM bookings WHERE code=? AND user_id=?", (mm[1], me["id"])).fetchone()
            if not r: raise ApiError(404, "Booking tidak ditemukan")
            new = "cancelled" if r["status"] == "pending_payment" else "cancellation_requested"
            if new not in TRANSITIONS.get(r["status"], ()): raise ApiError(409, "Status tidak dapat dibatalkan")
            c.execute("UPDATE bookings SET status=? WHERE id=?", (new, r["id"])); return self.send(200, {"status": new})
        if p == "/api/payments/checkout" and m == "POST":
            need(); r = c.execute("SELECT * FROM bookings WHERE code=? AND user_id=? AND status='pending_payment'", (b.get("code"), me["id"])).fetchone()
            if not r: raise ApiError(404, "Booking tidak ditemukan atau tidak menunggu pembayaran")
            ref = "SBX-" + secrets.token_hex(6)  # SANDBOX: ganti dengan Midtrans/Xendit (butuh kredensial resmi)
            c.execute("INSERT INTO payments(booking_id,provider,reference,amount,status) VALUES(?,?,?,?,'pending')", (r["id"], "sandbox", ref, r["total"]))
            return self.send(201, {"reference": ref, "amount": r["total"], "note": "Sandbox. Pembayaran sah hanya lewat webhook bertanda tangan."})
        if p == "/api/payments/webhook" and m == "POST":
            sig = self.headers.get("X-Signature", "")
            if not hmac.compare_digest(sig, hmac.new(WEBHOOK_SECRET, raw, hashlib.sha256).hexdigest()): raise ApiError(401, "Signature tidak valid")
            ev = json.loads(raw); pay = c.execute("SELECT * FROM payments WHERE reference=?", (ev.get("reference"),)).fetchone()
            if not pay: raise ApiError(404, "Referensi tidak dikenal")
            if pay["status"] == "paid": return self.send(200, {"ok": True, "duplicate": True})  # idempotent
            if ev.get("status") != "paid": c.execute("UPDATE payments SET status='failed' WHERE id=?", (pay["id"],)); return self.send(200, {"ok": True})
            if int(ev.get("amount", -1)) != pay["amount"]: raise ApiError(422, "Nominal tidak sesuai")
            c.execute("UPDATE payments SET status='paid',paid_at=? WHERE id=?", (iso(now()), pay["id"]))
            c.execute("UPDATE bookings SET status='confirmed' WHERE id=? AND status='pending_payment'", (pay["booking_id"],))
            return self.send(200, {"ok": True})
        if p == "/api/admin/summary":
            need(True); g = lambda s: c.execute(s).fetchone()[0]
            return self.send(200, {"booking_aktif": g("SELECT COUNT(*) FROM bookings WHERE status IN ('confirmed','ready_for_pickup','ongoing')"),
                "pending": g("SELECT COUNT(*) FROM bookings WHERE status='pending_payment'"),
                "pendapatan_sewa": g("SELECT COALESCE(SUM(total-deposit),0) FROM bookings WHERE status IN ('confirmed','ready_for_pickup','ongoing','completed')")})
        if p == "/api/admin/bookings": need(True); return self.send(200, [dict(r) for r in c.execute("SELECT id,code,vehicle_id,start_at,end_at,status,total FROM bookings ORDER BY id DESC LIMIT 100")])
        mm = re.match(r"/api/admin/bookings/(\d+)/status$", p)
        if mm and m == "POST":
            need(True); r = c.execute("SELECT * FROM bookings WHERE id=?", (mm[1],)).fetchone()
            if not r: raise ApiError(404, "Booking tidak ditemukan")
            if b.get("status") not in TRANSITIONS.get(r["status"], ()): raise ApiError(409, f"Transisi {r['status']} -> {b.get('status')} tidak diizinkan")
            c.execute("UPDATE bookings SET status=? WHERE id=?", (b["status"], r["id"]))
            c.execute("INSERT INTO audit_logs(actor_id,action,entity,entity_id,meta) VALUES(?,?,?,?,?)", (me["id"], "status_change", "booking", r["id"], json.dumps({"from": r["status"], "to": b["status"]})))
            return self.send(200, {"status": b["status"]})
        raise ApiError(404, "Endpoint tidak ditemukan")

if __name__ == "__main__":
    seed(); port = int(os.environ.get("PORT", 8000))
    print(f"Berjalan di http://localhost:{port}"); ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
