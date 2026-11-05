"""Uji integrasi: python3 test_api.py  (memakai DB sementara & server nyata)"""
import os, json, hmac, hashlib, threading, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
os.environ["DB_PATH"] = "/tmp/test_rental.db"
if os.path.exists("/tmp/test_rental.db"): os.remove("/tmp/test_rental.db")
import server
server.seed(); srv = server.ThreadingHTTPServer(("127.0.0.1", 8765), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(.3)
U = "http://127.0.0.1:8765"
def call(path, body=None, cookie=None, raw=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if cookie: h["Cookie"] = cookie
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    try:
        r = urllib.request.urlopen(urllib.request.Request(U + path, data, h)); return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read()), e.headers
def t(d): return (datetime.now(timezone.utc) + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
s, _, h = call("/api/auth/login", {"email": "user@demo.test", "password": "User#12345"}); ck = h["Set-Cookie"].split(";")[0]
assert s == 200 and "HttpOnly" in h["Set-Cookie"]
assert call("/api/auth/login", {"email": "user@demo.test", "password": "salah"})[0] == 401
s, q, _ = call(f"/api/pricing/quote?vehicle_id=1&start={t(1)}&end={t(3)}&service=lepas_kunci")
assert q["subtotal"] == 500000 and q["tax"] == 55000 and q["total"] == 1055000, q   # 2 hari x 250rb + PPN 11% + deposit
assert call(f"/api/pricing/quote?vehicle_id=1&start={t(3)}&end={t(1)}&service=lepas_kunci")[0] == 400
assert call("/api/bookings", {"vehicle_id": 1}, None)[0] == 401
bk = {"vehicle_id": 1, "start": t(1), "end": t(3), "service": "lepas_kunci", "total": 1}  # total dari klien diabaikan
s, b, _ = call("/api/bookings", bk, ck); assert s == 201 and b["total"] == 1055000
assert call("/api/bookings", bk, ck)[0] == 409, "double booking harus ditolak"
bk2 = dict(bk, start=t(3), end=t(4)); assert call("/api/bookings", bk2, ck)[0] == 409, "buffer pembersihan harus berlaku"
# race: 5 permintaan bersamaan, hanya 1 sukses
res = []; bk3 = dict(bk, vehicle_id=2)
ts = [threading.Thread(target=lambda: res.append(call("/api/bookings", bk3, ck)[0])) for _ in range(5)]
[x.start() for x in ts]; [x.join() for x in ts]; assert res.count(201) == 1, res
# pembayaran + webhook
s, p, _ = call("/api/payments/checkout", {"code": b["code"]}, ck)
ev = json.dumps({"reference": p["reference"], "status": "paid", "amount": p["amount"]}).encode()
assert call("/api/payments/webhook", raw=ev, headers={"X-Signature": "bad"})[0] == 401
sig = hmac.new(server.WEBHOOK_SECRET, ev, hashlib.sha256).hexdigest()
assert call("/api/payments/webhook", raw=ev, headers={"X-Signature": sig})[0] == 200
assert call("/api/payments/webhook", raw=ev, headers={"X-Signature": sig})[1].get("duplicate")
assert call("/api/bookings", None, ck)[1][-1]["status"] in ("confirmed", "pending_payment")
# RBAC & transisi
assert call("/api/admin/summary", None, ck)[0] == 403
s, _, h = call("/api/auth/login", {"email": "admin@demo.test", "password": "Admin#12345"}); ad = h["Set-Cookie"].split(";")[0]
bid = call("/api/admin/bookings", None, ad)[1][-1]["id"]
assert call(f"/api/admin/bookings/{bid}/status", {"status": "completed"}, ad)[0] == 409, "transisi ilegal"
assert call(f"/api/admin/bookings/{bid}/status", {"status": "ready_for_pickup"}, ad)[0] == 200
print("SEMUA TES LULUS")
