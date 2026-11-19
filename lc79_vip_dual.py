import os
import re
import io
import secrets
import math
import json
import base64
import hashlib
import html
import logging
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone, timedelta

import requests
import socketio
from flask import Flask, Response, jsonify, request, send_from_directory

BOT_NAME = "LC79 VIP"

# =========================
# FLASK KEEP ALIVE (RENDER)
# =========================
app = Flask(__name__)


@app.route("/")
def home():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


@app.route("/dashboard")
def dashboard():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


@app.route("/logo_lc79.jpg")
def logo_lc79():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "logo_lc79.jpg")


@app.route("/health")
def health():
    return {"status": "online", "bot": "LC79", "server": "Render", "dashboard": "/dashboard"}


# Phiên web sau đăng nhập LC79 (không bắt buộc DASHBOARD_TOKEN)
web_sessions = {}  # token -> {cid, username, nickname, exp}
WEB_SESSION_TTL = 7 * 24 * 3600


def _web_cid(username: str) -> str:
    return "web:" + str(username).strip().lower()


def create_web_session(username: str, nickname: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    web_sessions[token] = {
        "cid": _web_cid(username),
        "username": username,
        "nickname": nickname or username,
        "exp": now + WEB_SESSION_TTL,
    }
    for k, v in list(web_sessions.items()):
        if v.get("exp", 0) < now:
            web_sessions.pop(k, None)
    return token


def session_from_request():
    token = (
        request.headers.get("X-Session-Token")
        or request.headers.get("X-Dashboard-Token")
        or ""
    ).strip()
    if not token:
        return None
    sess = web_sessions.get(token)
    if not sess:
        return None
    if sess.get("exp", 0) < time.time():
        web_sessions.pop(token, None)
        return None
    return sess


def dashboard_authorized():
    return session_from_request() is not None


def dashboard_chat_id():
    sess = session_from_request()
    if sess:
        return sess["cid"]
    configured = os.environ.get("DASHBOARD_CHAT_ID", "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            return configured
    ids = list(user_states.keys()) if "user_states" in globals() else []
    return ids[0] if len(ids) == 1 else None


@app.route("/api/dashboard/login", methods=["POST"])
def dashboard_login():
    """Đăng nhập bằng tài khoản LC79 — không cần DASHBOARD_TOKEN."""
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or data.get("user") or "").strip()
    password = str(data.get("password") or data.get("pass") or "")
    if not username or not password:
        return jsonify({"ok": False, "error": "Nhập tài khoản và mật khẩu LC79"}), 400
    res = login_and_get_token(username, password)
    if "_error" in res:
        return jsonify({"ok": False, "error": res["_error"]}), 401

    cid = _web_cid(username)
    # Dừng phiên cũ của cùng tài khoản web (nếu có)
    try:
        stop_user(cid)
    except Exception:
        pass

    for table in ("md5", "tx"):
        st = get_state(cid, table)
        with state_lock:
            st["balance"] = int(res["money"] or 0)
            st["initial_balance"] = int(res["money"] or 0)
            st["profit"] = 0
            st["access_token"] = res["access"]
            st["jwt"] = res["token"]
            st["nickname"] = res["nickname"]
            st["running"] = True
            st["login_at"] = time.time()
            st["gen"] += 1
            generation = st["gen"]
        threading.Thread(target=start_websocket, args=(cid, res["token"], table), daemon=True).start()
        threading.Thread(target=sync_loop, args=(cid, table, generation), daemon=True).start()

    token = create_web_session(username, res.get("nickname") or username)
    return jsonify({
        "ok": True,
        "token": token,
        "nickname": res.get("nickname") or username,
        "username": username,
        "balance": int(res.get("money") or 0),
        "message": "Đăng nhập LC79 thành công",
    })


@app.route("/api/dashboard/logout", methods=["POST"])
def dashboard_logout():
    token = (
        request.headers.get("X-Session-Token")
        or request.headers.get("X-Dashboard-Token")
        or ""
    ).strip()
    if token:
        web_sessions.pop(token, None)
    return jsonify({"ok": True, "message": "Đã đăng xuất"})


@app.route("/api/dashboard/status", methods=["GET"])
def dashboard_status():
    if not dashboard_authorized():
        return jsonify({"error": "Unauthorized. Vui lòng đăng nhập tài khoản LC79."}), 401
    cid = dashboard_chat_id()
    if cid is None:
        return jsonify({"error": "Phiên đăng nhập không hợp lệ."}), 400

    cache = globals().setdefault("_dashboard_balance_cache", {})
    now = time.time()
    last = cache.get(cid, 0)
    if now - last > 20:
        st_auth = get_state(cid, "md5")
        if st_auth.get("access_token"):
            refresh_state_balance(st_auth)
            cache[cid] = now

    result = {"server": "online", "chat_id": str(cid), "tables": {}}
    for table in ("md5", "tx"):
        st = get_state(cid, table)
        total = st["total_win"] + st["total_lose"]
        result["tables"][table] = {
            "label": TABLES[table]["label"],
            "connected": bool(st.get("ws_connected")),
            "running": bool(st["running"]),
            "auto": bool(st["auto"]),
            "mode": st["mode"],
            "mode_label": MODES[st["mode"]]["label"],
            "balance": int(st.get("balance") or 0),
            "initial_balance": int(st.get("initial_balance") or 0),
            "profit": int(st.get("profit") or 0),
            "base_bet": int(st.get("base_bet") or 0),
            "stop_loss": int(st.get("stop_loss") or 0),
            "stop_win": int(st.get("stop_win") or 0),
            "daily_limit": int(st.get("daily_limit") or 0),
            "wins": int(st["total_win"]),
            "losses": int(st["total_lose"]),
            "win_streak": int(st["win_streak"]),
            "lose_streak": int(st["lose_streak"]),
            "accuracy": round(st["engine"].accuracy(), 2),
            "samples": len(st["engine"].perf),
            "history_count": len(st["engine"].recs),
            "last_result": st.get("last_result"),
            "recent": list(st["bet_log"])[-30:],
            "pending_reason": (st.get("pending") or {}).get("reason") or "",
            "pending_pred": (st.get("pending") or {}).get("pred"),
            "pending_sid": (st.get("pending") or {}).get("sid"),
            "logged_in": bool(st.get("access_token")),
            "martingale": bool(st.get("martingale")),
            "mg_mult": float(st.get("mg_mult") or 2),
            "mg_step": int(st.get("mg_step") or 0),
            "current_bet": int(current_bet(st)),
        }
    # Lịch sử trộn 2 bàn, mới nhất trước
    mixed = []
    for table in ("md5", "tx"):
        st = get_state(cid, table)
        for r in list(st.get("bet_log") or [])[-40:]:
            item = dict(r)
            item["table"] = table
            item["table_label"] = TABLES[table]["label"]
            mixed.append(item)
    mixed.sort(key=lambda x: (str(x.get("time") or ""), str(x.get("sid") or "")), reverse=True)
    result["history"] = mixed[:50]
    # Settings chung lấy từ md5 (đã đồng bộ khi lưu)
    st0 = get_state(cid, "md5")
    result["shared"] = {
        "base_bet": int(st0.get("base_bet") or 10000),
        "mode": st0.get("mode") or DEFAULT_MODE,
        "stop_loss": int(st0.get("stop_loss") or 0),
        "stop_win": int(st0.get("stop_win") or 0),
        "daily_limit": int(st0.get("daily_limit") or 0),
        "martingale": bool(st0.get("martingale")),
        "mg_mult": float(st0.get("mg_mult") or 2),
        "auto_any": any(get_state(cid, t).get("auto") for t in ("md5", "tx")),
    }
    root = user_states.get(cid) or {}
    result["events"] = list(root.get("events") or [])[-15:]
    return jsonify(result)


@app.route("/api/dashboard/control", methods=["POST"])
def dashboard_control():
    if not dashboard_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    cid = dashboard_chat_id()
    if cid is None:
        return jsonify({"error": "Phiên đăng nhập không hợp lệ. Hãy đăng nhập lại."}), 400
    data = request.get_json(silent=True) or {}
    # Mặc định áp dụng cả hai bàn
    # tables: list ["md5","tx"] hoặc table=all/md5/tx
    if isinstance(data.get("tables"), list) and data.get("tables"):
        tables = []
        for t in data["tables"]:
            nt = normalize_table(str(t))
            if nt and nt not in tables:
                tables.append(nt)
    else:
        table_arg = str(data.get("table") or "all").lower()
        tables = ["md5", "tx"] if table_arg in ("all", "both", "2", "") else [normalize_table(table_arg)]
    if not tables or not all(tables):
        return jsonify({"error": "Chọn ít nhất một bàn (MD5 hoặc TX)"}), 400
    action = str(data.get("action", "settings")).lower()

    try:
        params = {}
        for key in ("stop_loss", "stop_win", "daily_limit"):
            if key in data:
                params[{"stop_loss": "sl", "stop_win": "sw", "daily_limit": "limit"}[key]] = str(data[key] or "0")
        if "mode" in data:
            params["mode"] = str(data["mode"])
        amount = data.get("base_bet")
        martingale = data.get("martingale")
        mg_mult = data.get("mg_mult")
        messages = []
        for table in tables:
            st = get_state(cid, table)
            if amount is not None:
                value = parse_amount(str(amount))
                if value <= 0:
                    raise ValueError("Mức cược phải lớn hơn 0")
                st["base_bet"] = value
            if martingale is not None:
                st["martingale"] = bool(martingale) if not isinstance(martingale, str) else martingale.lower() in ("1", "true", "yes", "on")
                if not st["martingale"]:
                    st["mg_step"] = 0
            if mg_mult is not None:
                try:
                    st["mg_mult"] = max(1.5, min(3.0, float(mg_mult)))
                except (TypeError, ValueError):
                    st["mg_mult"] = 2.0
            if action == "start":
                ok, message = _set_auto(cid, table, True, str(st["base_bet"]), params)
                if not ok:
                    return jsonify({"ok": False, "message": f"{TABLES[table]['label']}: {message}"}), 409
                messages.append(message)
            elif action == "stop":
                _apply_auto_settings(st, params)
                ok, message = _set_auto(cid, table, False)
                messages.append(message)
            elif action == "settings":
                _apply_auto_settings(st, params)
                messages.append(f"Đã lưu {TABLES[table]['label']}")
            elif action == "baseline":
                current = refresh_state_balance(st)
                if current is None:
                    return jsonify({"ok": False, "message": "Không đọc được số dư thật để đặt mốc vốn."}), 409
                for table_key in ("md5", "tx"):
                    other = get_state(cid, table_key)
                    other["initial_balance"] = int(current)
                    other["profit"] = 0
                messages.append("Đã đặt mốc vốn theo số dư hiện tại")
                break
            else:
                return jsonify({"error": "action must be start, stop, settings or baseline"}), 400
        return jsonify({"ok": True, "message": " · ".join(messages)})
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        log.exception("dashboard_control failed")
        return jsonify({"ok": False, "message": f"Lỗi server: {exc}"}), 500


@app.route("/api/dashboard/export.txt", methods=["GET"])
def dashboard_export():
    if not dashboard_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    cid = dashboard_chat_id()
    if cid is None:
        return jsonify({"error": "Phiên đăng nhập không hợp lệ. Hãy đăng nhập lại."}), 400
    table_arg = request.args.get("table", "all")
    tables = ["md5", "tx"] if table_arg == "all" else [normalize_table(table_arg)]
    if not all(tables):
        return jsonify({"error": "table must be md5, tx or all"}), 400
    chunks = ["LC79 VIP PRO - LICH SU DU DOAN VA CUOC", f"Xuat luc: {vn_now().isoformat()}", ""]
    for table in tables:
        st = get_state(cid, table)
        chunks.append(f"===== {TABLES[table]['label']} =====")
        chunks.append(format_history_txt(list(st["bet_log"]), table))
        chunks.append("")
    payload = "\n".join(chunks).encode("utf-8")
    return Response(payload, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="lc79_history_{table_arg}.txt"'})




def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# =========================
# CẤU HÌNH
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("lc79")
logging.getLogger("engineio").setLevel(logging.WARNING)
logging.getLogger("socketio").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Web-only runtime

# Hai bàn độc lập: Tài/Xỉu MD5 và Tài/Xỉu thường.
# Từng bàn có API lịch sử + WebSocket + engine + trạng thái Auto riêng.
TABLES = {
    "md5": {
        "key": "txmd5", "label": "TÀI XỈU MD5",
        "history": os.environ.get("LC79_MD5_HISTORY_API_URL", "https://wtxmd52.tele68.com/v1/txmd5/sessions"),
        "fallback": os.environ.get("LC79_MD5_HISTORY_API_FALLBACK", "https://living-telecommunications-start-consoles.trycloudflare.com/api/txmd5"),
        "ws_url": os.environ.get("LC79_MD5_WS_URL", "https://wtxmd52.tele68.com"),
        "ws_path": os.environ.get("LC79_MD5_WS_PATH", "txmd5/"),
        "namespace": os.environ.get("LC79_MD5_WS_NAMESPACE", "/txmd5"),
        "origin": os.environ.get("LC79_MD5_WS_ORIGIN", "https://lc79b.bet"),
    },
    "tx": {
        "key": "tx", "label": "TÀI XỈU THƯỜNG",
        "history": os.environ.get("LC79_TX_HISTORY_API_URL", "https://wtx.tele68.com/v1/tx/sessions"),
        "fallback": os.environ.get("LC79_TX_HISTORY_API_FALLBACK", ""),
        "ws_url": os.environ.get("LC79_TX_WS_URL", "https://wtx.tele68.com"),
        "ws_path": os.environ.get("LC79_TX_WS_PATH", "tx/"),
        "namespace": os.environ.get("LC79_TX_WS_NAMESPACE", "/tx"),
        "origin": os.environ.get("LC79_TX_WS_ORIGIN", "https://lc79c.vin"),
    },
}
DEFAULT_TABLE = "md5"
MAX_HISTORY = max(100, min(int(os.environ.get("LC79_MAX_HISTORY", "10000")), 30000))
SYNC_INTERVAL = max(5, int(os.environ.get("LC79_SYNC_INTERVAL", "15")))

ACCOUNT_URL = "https://athirdparty.tele68.com/v1/via/account"

# Tạm dừng khi dữ liệu/các mô hình bất đồng, sau đó có nhịp hoạt động trở lại.
MAX_PAUSE_SESSIONS = 3
PAUSE_SESSIONS = 2
ACTIVE_SESSIONS_AFTER_PAUSE = 3

# Tỷ lệ trả thưởng: thắng lãi 1.98 lần cược, thua mất 1 lần cược.
WIN_PROFIT = 1.98
LOSE_LOSS = 1.0
# Tỷ lệ thắng tối thiểu để cược có lợi về lâu dài: p * W - (1 - p) * L > 0.
BREAK_EVEN = LOSE_LOSS / (LOSE_LOSS + WIN_PROFIT)

TAI, XIU = "TAI", "XIU"

MODES = {
    "canbang": {"label": "Cân bằng · ≥51%", "post": 0.51, "min_sessions": 12, "p_side_min": 0.51},
    "antoan": {"label": "An toàn · ≥58%", "post": 0.58, "min_sessions": 18, "p_side_min": 0.58},
    "maohiem": {"label": "Mạo hiểm · mọi phiên", "post": 0.50, "min_sessions": 6, "p_side_min": 0.50, "aggressive": True},
}
DEFAULT_MODE = "canbang"
MAX_SKIPS_PER_100 = 8  # ít bỏ qua hơn
LOSS_STREAK_PAUSE = 3  # thua ≥3 tay → chỉ đánh khi đủ chắc
LOSS_RECOVERY_P = 0.58  # ngưỡng chắc khi đang phục hồi sau chuỗi thua

state_lock = threading.RLock()
active_sockets = {}  # khóa (chat_id, table_key)
user_states = {}


# ============================================================
# LOG + SỰ KIỆN THÔNG BÁO WEB
# ============================================================
def esc(s):
    return html.escape(str(s), quote=False)


def push_event(cid, title, body="", kind="info"):
    try:
        root = user_states.get(cid)
        if root is None:
            return
        events = root.setdefault("events", [])
        events.append({
            "id": f"{time.time():.0f}-{len(events)}",
            "ts": time.time(),
            "title": str(title)[:80],
            "body": str(body)[:160],
            "kind": kind,
        })
        if len(events) > 30:
            del events[:-30]
    except Exception as e:
        log.debug("push_event: %s", e)


def say(cid, text, force=False):
    msg = re.sub(r"<[^>]+>", "", str(text))
    log.info("[%s] %s", cid, short(msg, 160))
    if force:
        push_event(cid, "Hệ thống", short(msg, 120), "info")


def kv(rows):
    width = max((len(k) for k, _ in rows), default=0)
    return chr(10).join(f"{k:<{width}}  {v}" for k, v in rows)


# ============================================================
# TIỆN ÍCH
# ============================================================
VN_TZ = timezone(timedelta(hours=7))


def vn_now():
    return datetime.now(VN_TZ)


def sid_sort_key(value):
    text = str(value)
    try:
        return (0, int(text))
    except (TypeError, ValueError):
        return (1, text)


def normalize_sid(value):
    text = str(value).strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return text


def norm(s):
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def parse_amount(s):
    s = str(s or "").lower().strip().replace(" ", "").replace(",", "")
    # Bỏ dấu chấm ngăn nghìn kiểu VN (1.000 / 100.000)
    if s.count(".") == 1 and len(s.split(".")[-1]) == 3 and s.replace(".", "").isdigit():
        s = s.replace(".", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    if not s or not s.replace(".", "").isdigit():
        # cho phép số nguyên thuần
        try:
            value = int(float(s)) * mult
        except Exception:
            raise ValueError("Số tiền không hợp lệ")
    else:
        value = int(float(s)) * mult
    if value < 0:
        raise ValueError("số tiền không được âm")
    return value


def parse_kv(text):
    out = {}
    for tok in text.split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[norm(k)] = v.strip()
    return out


def fmt(n):
    return f"{int(round(n)):,}".replace(",", ".")


def signed(n):
    n = int(round(n))
    if n > 0:
        return f"+{fmt(n)}"
    if n < 0:
        return f"-{fmt(-n)}"
    return "0"


def bar(p, width=10):
    p = max(0.0, min(1.0, p))
    filled = round(p * width)
    return "█" * filled + "░" * (width - filled)


def side_label(side):
    return "TÀI" if side == TAI else "XỈU"


def side_icon(side):
    return "⚫" if side == TAI else "⚪"


def trail(seq, n=10):
    return "".join("🔵" if x == TAI else "🔴" for x in seq[-n:])




def dice_faces(dice):
    """Xúc xắc hiển thị bằng icon 🎲 kèm số mặt, không dùng glyph ⚀-⚅ (đen trắng)."""
    if not dice:
        return "?"
    return " ".join(f"🎲{x}" for x in dice)


# ============================================================
# TOÁN
# ============================================================
ORDER_NAMES = {0: "Tần suất", 1: "Markov 1", 2: "Markov 2", 3: "Markov 3"}


def expert_name(key):
    order, hl = key
    return f"{ORDER_NAMES[order]} {'nhanh' if hl < 100 else 'chậm'}"


def beta_prob_above(a, b, x0=0.5, steps=400):
    """P(X > x0) với X ~ Beta(a, b), tích phân Simpson. Yêu cầu a, b >= 1."""
    if a <= 0 or b <= 0:
        return 0.5
    lnB = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)

    def f(x):
        if x <= 0.0 or x >= 1.0:
            return 0.0
        return math.exp((a - 1) * math.log(x) + (b - 1) * math.log(1 - x) - lnB)

    h = (1.0 - x0) / steps
    total = f(x0) + f(1.0)
    for i in range(1, steps):
        total += (4 if i % 2 else 2) * f(x0 + i * h)
    return min(1.0, max(0.0, total * h / 3))


def gammaincc(a, x):
    """Q(a, x) = phần đuôi của hàm gamma không hoàn chỉnh chuẩn hóa."""
    if x <= 0:
        return 1.0
    log_pref = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(500):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-12:
                break
        return max(0.0, 1.0 - total * math.exp(log_pref))
    b = x + 1 - a
    c = 1e300
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1.0 / d if abs(d) > 1e-300 else 1e300
        c = b + an / c
        delta = c * d
        h *= delta
        if abs(delta - 1) < 1e-12:
            break
    return min(1.0, math.exp(log_pref) * h)


def chi2_sf(x, df):
    """P(chi-bình phương > x) với df bậc tự do: giá trị p của kiểm định lệch."""
    return gammaincc(df / 2.0, x / 2.0)


def binom_p_le(k, n):
    """P(X <= k) với X ~ Binomial(n, 1/2): xác suất đoán đúng ít nhất như vậy nếu chỉ là may rủi."""
    if n <= 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)


def danh_gia(k, n):
    """Kết luận về độ đúng của dự đoán so với tung đồng xu."""
    if n < 30:
        return f"chưa đủ dữ liệu ({n} phiên)"
    p_lo = binom_p_le(k, n)
    if p_lo < 0.05:
        return f"kém ngẫu nhiên (p={p_lo:.3f})"
    p_hi = 1 - binom_p_le(k - 1, n)
    if p_hi < 0.05:
        return f"tốt hơn ngẫu nhiên (p={p_hi:.3f})"
    return "chưa khác ngẫu nhiên"


def describe_p(p):
    if p is None:
        return "chưa đủ dữ liệu"
    if p < 0.05:
        return f"có dấu hiệu (p={p:.3f})"
    return f"chưa thấy lệch (p={p:.2f})"


# ---- Bảng công thức xúc xắc (theo file người dùng cung cấp) ----
# Khóa: bộ 3 xúc xắc của ván VỪA KẾT THÚC, theo đúng thứ tự tung (ví dụ (1, 2, 4)).
# Giá trị: phía của ván KẾ TIẾP. Bộ nào không có trong bảng -> không áp dụng công thức (bỏ qua).
_T, _X = "TAI", "XIU"
DICE_RULES = {}
def _add(side, combos):
    for c in combos:
        DICE_RULES[tuple(int(ch) for ch in c)] = side

_add(_X, ["111", "112", "121", "211", "113", "131", "311", "122", "212", "221"])  # tổng 3,4,5 mặc định Xỉu ở xử lý dưới
_add(_T, ["123", "114", "132", "213"])
_add(_X, ["222", "411"])
_add(_T, ["124", "133", "331"])
_add(_X, ["223", "232", "115", "151", "511", "241"])
_add(_T, ["134", "224", "323"])
_add(_X, ["116", "125", "233", "143", "251", "512", "611", "431"])
_add(_T, ["126", "135", "144", "333"])
_add(_X, ["225", "234", "261", "252", "315", "153"])
_add(_T, ["136", "145", "244", "343"])
_add(_X, ["226", "235", "334", "163", "316", "424", "613", "451"])
_add(_T, ["236", "344", "434"])
_add(_X, ["632", "263", "326", "155", "461", "146", "614", "452", "254", "353", "533"])
_add(_T, ["156", "255", "336", "345", "651", "561", "165", "516", "615", "552", "354", "543", "633", "363", "426", "624"])
_add(_X, ["246", "444"])
_add(_T, ["346", "355", "445"])
_add(_X, ["166", "256"])
_add(_T, ["356", "455"])
_add(_X, ["266", "446", "554"])
_add(_T, ["456", "555"])
_add(_X, ["366", "465"])
_add(_X, ["556", "466"])
_add(_T, ["664", "646"])
_add(_T, ["566"])
# Tổng 3, 4, 5 (Xỉu) mặc định: mọi bộ có tổng đó đều cho Xỉu
for _a in range(1, 7):
    for _b in range(1, 7):
        for _c in range(1, 7):
            t = _a + _b + _c
            if t in (3, 4, 5) and (_a, _b, _c) not in DICE_RULES:
                DICE_RULES[(_a, _b, _c)] = _X

def dice_rule_pred(dice):
    """Dự đoán ván kế tiếp theo công thức xúc xắc, hoặc None nếu bộ xúc xắc không có trong bảng."""
    try:
        key = tuple(int(x) for x in dice)
    except Exception:
        return None
    if len(key) != 3:
        return None
    return DICE_RULES.get(key)

def vn_hour():
    """Giờ Việt Nam (UTC+7), không phụ thuộc múi giờ máy chủ."""
    return int(((time.time() + 7 * 3600) % 86400) // 3600)

def in_blackout():
    """Khung 0h-5h sáng không áp dụng công thức."""
    return 0 <= vn_hour() < 5

# ---- Mẫu cầu từ các file thuật toán người dùng cung cấp ----
# Mẫu 1: pattern database trong thuattoan123.js. Mẫu khớp (trừ phần tử cuối) -> dự đoán phần tử cuối.
PATTERN_DB = {
    "1-1": (["T", "X", "T", "X"], 0.70), "1-2-1": (["T", "X", "X", "T"], 0.65),
    "2-1-2": (["T", "T", "X", "T", "T"], 0.68), "3-1": (["T", "T", "T", "X"], 0.72),
    "1-3": (["T", "X", "X", "X"], 0.72), "2-2": (["T", "T", "X", "X"], 0.66),
    "2-3": (["T", "T", "X", "X", "X"], 0.71), "3-2": (["T", "T", "T", "X", "X"], 0.73),
    "4-1": (["T", "T", "T", "T", "X"], 0.76), "1-4": (["T", "X", "X", "X", "X"], 0.76),
}


def db_pattern_pred(results):
    """Mẫu khớp dài nhất trong PATTERN_DB; nếu không có thì thử hai luật động trong thuattoan123.js."""
    results = ["T" if x == TAI else "X" for x in results]
    best = None
    for pat, prob in PATTERN_DB.values():
        L = len(pat) - 1
        if len(results) >= L and results[len(results) - L:] == pat[:-1]:
            cand = (L, prob, pat[-1])
            if best is None or cand[:2] > best[:2]:
                best = cand
    if best is not None:
        return TAI if best[2] == "T" else XIU
    l6 = results[-6:]
    if len(results) >= 6 and l6.count("T") == 4 and l6[-1] == "T":
        return XIU
    l8 = results[-8:]
    if len(results) >= 8 and l8.count("T") >= 6 and l8[-1] == "T":
        return XIU
    return None


# Mẫu 2: bảng 8 ván. Đếm trực tiếp từ lịch sử của chính bot (không dùng bảng tĩnh,
# vì bảng tĩnh được đếm trên toàn bộ dữ liệu nên nhìn thấy tương lai và cho kết quả giả).
def bang8_counts_from(seq):
    counts = {}
    for i in range(len(seq) - 8):
        key = "".join("T" if x == TAI else "X" for x in seq[i:i + 8])
        c = counts.setdefault(key, [0, 0])
        c[0 if seq[i + 8] == TAI else 1] += 1
    return counts


def bang8_pred(results, counts):
    """Ván kế tiếp theo bảng 8 ván; chỉ dùng khi đủ mẫu và không hòa."""
    if len(results) < 8:
        return None
    key = "".join("T" if x == TAI else "X" for x in results[-8:])
    c = counts.get(key)
    if not c or c[0] + c[1] < 5 or c[0] == c[1]:
        return None
    return TAI if c[0] > c[1] else XIU


def streak_break_rate(seq, L):
    """Sau một bệt đã dài ≥ L, bao nhiêu lần kết quả tiếp tục bệt và bao nhiêu lần bị bẻ."""
    cont = brk = 0
    # run_len[i] = độ dài bệt kết thúc tại i
    run_len = [0] * len(seq)
    for i in range(len(seq)):
        run_len[i] = run_len[i - 1] + 1 if i > 0 and seq[i] == seq[i - 1] else 1
    for i in range(1, len(seq)):
        if run_len[i - 1] >= L:
            if seq[i] == seq[i - 1]:
                cont += 1
            else:
                brk += 1
    return cont, brk


def bet_dai_pred(seq, min_run=3, min_samples=20, edge=0.55):
    """Bẻ hay theo bệt dài: dựa trên tần suất bệt tiếp/bị bẻ đã quan sát với độ dài bệt hiện tại."""
    if len(seq) < 10:
        return None
    L = 1
    for i in range(len(seq) - 1, 0, -1):
        if seq[i] == seq[i - 1]:
            L += 1
        else:
            break
    if L < min_run:
        return None
    cont, brk = streak_break_rate(seq, L)
    n = cont + brk
    if n < min_samples:
        return None
    last = seq[-1]
    opposite = XIU if last == TAI else TAI
    if brk / n >= edge:
        return opposite
    if cont / n >= edge:
        return last
    return None


def runs_test(seq):
    """Kiểm định chạy (Wald-Wolfowitz): chuỗi có ngẫu nhiên không. Trả về (p, số chạy, kỳ vọng)."""
    n1 = sum(1 for x in seq if x == TAI)
    n2 = len(seq) - n1
    n = n1 + n2
    if n1 == 0 or n2 == 0 or n < 20:
        return None
    R = 1 + sum(1 for i in range(1, n) if seq[i] != seq[i - 1])
    mu = 2 * n1 * n2 / n + 1
    var = 2 * n1 * n2 * (2 * n1 * n2 - n) / (n * n * (n - 1))
    if var <= 0:
        return None
    z = (R - mu) / math.sqrt(var)
    p = math.erfc(abs(z) / math.sqrt(2))
    return p, R, mu


class CauEngine:
    """8 mô hình (tần suất, Markov bậc 1-3, mỗi loại nhanh và chậm), trọng số học theo lỗ."""

    ORDERS = (0, 1, 2, 3)
    HALF_LIVES = (40.0, 200.0)
    BETA = 4.0
    ETA = 6.0
    GAMMA = 0.98
    EPS = 1e-4

    def __init__(self):
        self.recs = {}
        self.perf = deque(maxlen=120)
        self.calib = deque(maxlen=300)
        self.skips = 0
        self.extra_post = 0.0
        self.loss = {}
        self.skip_streak = 0
        self.pause_remaining = 0
        self.pause_total = 0
        self.active_after_pause = 0
        self.pause_events = 0
        self.shadow = {}
        self.bang8_counts = {}

    def ingest(self, records):
        added = 0
        for rec in records:
            sid, res = rec.get("session"), rec.get("result")
            if sid is None or res not in (TAI, XIU) or sid in self.recs:
                continue
            self.recs[sid] = (res, rec.get("total", 0), rec.get("dice") or [])
            added += 1
        if added:
            ordered = sorted(self.recs.items(), key=lambda kv_: sid_sort_key(kv_[0]))[-MAX_HISTORY:]
            self.recs = dict(ordered)
            self.bang8_counts = bang8_counts_from(self.results())
        return added

    def dices(self):
        return [v[2] if len(v) > 2 else [] for v in self.recs.values()]

    def results(self):
        return [v[0] for v in self.recs.values()]

    @staticmethod
    def _counts(seq, order, hl):
        n = len(seq)
        w = {TAI: 0.0, XIU: 0.0}
        if order == 0:
            for i in range(n):
                w[seq[i]] += 0.5 ** ((n - 1 - i) / hl)
            return w
        ctx = seq[-order:]
        for i in range(order, n):
            if seq[i - order:i] == ctx:
                w[seq[i]] += 0.5 ** ((n - 1 - i) / hl)
        return w

    def _estimate(self, seq, hl, max_order):
        base = self._counts(seq, 0, hl)
        p = (base[TAI] + 1) / (base[TAI] + base[XIU] + 2)
        n_last = base[TAI] + base[XIU]
        for k in range(1, max_order + 1):
            if len(seq) <= k:
                break
            w = self._counts(seq, k, hl)
            n_k = w[TAI] + w[XIU]
            p = (w[TAI] + self.BETA * p) / (n_k + self.BETA)
            n_last = n_k
        return p, n_last

    def experts(self, seq):
        out = {}
        for hl in self.HALF_LIVES:
            for k in self.ORDERS:
                out[(k, hl)] = self._estimate(seq, hl, k)
        return out

    def calibrate_probability(self, p_tai):
        """Hiệu chỉnh xác suất bằng dự báo walk-forward đã được chốt kết quả.
        Chỉ bật khi có đủ mẫu; shrink về xác suất gốc để tránh overfit."""
        samples = list(self.calib)[-300:]
        if len(samples) < 30:
            return min(0.999, max(0.001, p_tai))
        lo = max(0.0, min(0.9, math.floor(p_tai * 10) / 10))
        bucket = [(p, y) for p, y in samples if lo <= p < lo + 0.1]
        if len(bucket) < 12:
            return min(0.999, max(0.001, p_tai))
        observed = (sum(1 for _, y in bucket if y) + 2.0) / (len(bucket) + 4.0)
        # Trọng số tăng dần theo số mẫu, tối đa 50% để không phản ứng quá mức.
        strength = min(0.5, len(bucket) / (len(bucket) + 30.0))
        calibrated = p_tai * (1.0 - strength) + observed * strength
        return min(0.999, max(0.001, calibrated))

    def weights(self, keys):
        losses = {key: self.loss.get(key, 0.0) for key in keys}
        m = min(losses.values())
        raw = {key: math.exp(-self.ETA * (losses[key] - m)) for key in keys}
        z = sum(raw.values())
        return {key: raw[key] / z for key in keys}

    STRATS = ("theo_cau", "dao_cau", "runlen", "markov1", "markov2", "markov3", "tan_suat", "tong_hop",
              "cong_thuc", "db_mau", "bang8", "bet_dai")
    STRAT_NAMES = {"theo_cau": "Bệt", "dao_cau": "1-1", "runlen": "Nhịp theo độ dài",
                   "markov1": "Markov 1", "markov2": "Markov 2", "markov3": "Markov 3",
                   "tan_suat": "Tần suất", "tong_hop": "Tổng hợp", "cong_thuc": "Công thức xúc xắc",
                   "db_mau": "Mẫu cầu", "bang8": "Bảng 8 ván", "bet_dai": "Bẻ bệt dài"}
    SHADOW_WINDOW = 80
    SHADOW_MIN = 30

    def run_length_pred(self, seq):
        """Cầu nhịp: học xác suất đổi cầu theo độ dài chuỗi hiện tại (bệt 1, nhịp đôi 2, nhịp ba 3...)."""
        last = seq[-1]
        n = len(seq)
        cnt = {}
        run = 0
        for j in range(n - 1):
            run = run + 1 if j > 0 and seq[j] == seq[j - 1] else 1
            c = cnt.setdefault(run, [0, 0])
            c[1] += 1
            c[0] += 1 if seq[j + 1] != seq[j] else 0
        r = 0
        for x in reversed(seq):
            if x == last:
                r += 1
            else:
                break
        opposite = XIU if last == TAI else TAI
        if r in cnt and cnt[r][1] >= 10:
            p_switch = (cnt[r][0] + 1) / (cnt[r][1] + 2)
            return opposite if p_switch >= 0.5 else last
        return last

    def strategy_preds(self, seq, p_tai, dices=None):
        """Dự đoán của từng chiến lược cho ván hiện tại (chỉ dùng dữ liệu đã có)."""
        last = seq[-1]
        hl = self.HALF_LIVES[1]
        p1, _ = self._estimate(seq, hl, 1)
        p2, _ = self._estimate(seq, hl, 2)
        p3, _ = self._estimate(seq, hl, 3)
        p0, _ = self._estimate(seq, hl, 0)
        return {
            "theo_cau": last,
            "dao_cau": XIU if last == TAI else TAI,
            "runlen": self.run_length_pred(seq),
            "markov1": TAI if p1 >= 0.5 else XIU,
            "markov2": TAI if p2 >= 0.5 else XIU,
            "markov3": TAI if p3 >= 0.5 else XIU,
            "tan_suat": TAI if p0 >= 0.5 else XIU,
            "tong_hop": TAI if p_tai >= 0.5 else XIU,
            "cong_thuc": dice_rule_pred(dices[-1]) if dices else None,
            "db_mau": db_pattern_pred(seq),
            "bang8": bang8_pred(seq, self.bang8_counts),
            "bet_dai": bet_dai_pred(seq),
        }

    def pick_strategy(self):
        """Chọn chiến lược đúng nhiều nhất gần đây, chỉ khi vượt ngẫu nhiên sau hiệu chỉnh nhiều lần thử."""
        best = None
        for name in self.STRATS:
            hits = list(self.shadow.get(name, []))[-self.SHADOW_WINDOW:]
            n = len(hits)
            if n < self.SHADOW_MIN:
                continue
            k = sum(hits)
            p_adj = min(1.0, len(self.STRATS) * (1 - binom_p_le(k - 1, n)))
            cand = (name, k, n, p_adj)
            if best is None or (p_adj, -k / n) < (best[3], -best[1] / best[2]):
                best = cand
        if best is None or best[3] >= 0.05:
            return None
        return best

    def predict(self, mode=DEFAULT_MODE):
        cfg = MODES.get(mode, MODES[DEFAULT_MODE])
        never_skip = cfg.get("never_skip", False)
        seq = self.results()
        n = len(seq)
        ex = self.experts(seq)
        keys = list(ex.keys())
        w = self.weights(keys)
        raw_p_tai = sum(w[k] * ex[k][0] for k in keys)
        p_tai = self.calibrate_probability(raw_p_tai)
        top = max(keys, key=lambda k: w[k])

        fast, slow = (3, self.HALF_LIVES[0]), (3, self.HALF_LIVES[1])
        n_eff = (ex[fast][1] + ex[slow][1]) / 2

        side = TAI if p_tai >= 0.5 else XIU
        p_side = max(p_tai, 1 - p_tai)
        # Độ kiểm chứng lấy từ dự đoán walk-forward đã có kết quả, không lấy
        # chính xác suất Markov làm dữ liệu tự chứng minh cho nó.
        checked = list(self.perf)[-self.PAUSE_WINDOW:]
        checked_n = len(checked)
        checked_hits = sum(1 for predicted, actual, _prob in checked if predicted == actual)
        post = beta_prob_above(checked_hits + 1, checked_n - checked_hits + 1, x0=0.5)
        ev = p_side * WIN_PROFIT - (1 - p_side) * LOSE_LOSS
        strat = self.strategy_preds(seq, p_tai, self.dices()) if n else {}
        pick = self.pick_strategy() if n else None
        if pick and strat.get(pick[0]) is None:
            pick = None  # chiến lược đang dẫn không áp dụng được cho ván này

        out = {
            "side": None, "p": p_tai, "raw_p_tai": raw_p_tai,
            "calibrated": len(self.calib) >= 30, "post": post, "n_eff": n_eff,
            "checked_n": checked_n, "checked_hits": checked_hits, "ev": ev,
            "reason": "", "agree": True, "p_tai": raw_p_tai, "p_calibrated": p_tai, "strat": strat,
            "ctx": f"{expert_name(top)} {w[top]:.0%}",
            "experts": {k: ex[k][0] for k in keys},
        }
        self.last_info = out
        if pick:
            name, k, pn, _ = pick
            out["ctx"] = f"{self.STRAT_NAMES[name]} · đúng {k}/{pn}"
        else:
            out["ctx"] = "Chưa cách nào vượt ngẫu nhiên"

        p_min = float(cfg.get("p_side_min") or cfg.get("post") or 0.51)
        need = max(p_min, float(cfg.get("post") or 0.51) + self.extra_post)

        # Ngân sách nghỉ: tối đa MAX_SKIPS_PER_100 trong 100 phiên gần
        recent_decisions = list(getattr(self, "decision_log", []))[-100:]
        skips_recent = sum(1 for d in recent_decisions if d == "skip")
        skip_budget_left = max(0, MAX_SKIPS_PER_100 - skips_recent)

        if n < cfg.get("min_sessions", 0):
            out["reason"] = f"Cần thêm dữ liệu, mới có {n}/{cfg['min_sessions']} phiên"
            self._log_decision("skip")
            return out

        block = None
        if in_blackout():
            block = "Khung 0h-5h không áp dụng công thức"
        elif p_side < need:
            block = f"Chưa đủ chắc ({p_side:.0%}, cần ≥{need:.0%})"
        # Bỏ điều kiện "chưa cách nào / bất đồng" để giảm bỏ qua

        # Hết ngân sách nghỉ → buộc đánh
        if block and skip_budget_left <= 0 and not in_blackout():
            block = None
            out["reason"] = f"Hết suất nghỉ ({skips_recent}/100) · buộc đánh"
        
        # Nếu đang trong chu kỳ tạm nghỉ, nghỉ đủ 2-3 phiên rồi mới hoạt động lại.
        if self.pause_remaining > 0 and skip_budget_left > 0:
            self.pause_remaining -= 1
            self.skip_streak += 1
            done = self.pause_total - self.pause_remaining
            out["reason"] = f"Cầu khó · tạm nghỉ {done}/{self.pause_total} phiên"
            out["pause"] = True
            self._log_decision("skip")
            return out

        if block is None:
            self.skip_streak = 0
            if self.active_after_pause > 0:
                self.active_after_pause -= 1
            out["side"] = side
            out["reason"] = f"Đủ điều kiện ({max(post, p_side):.0%} ≥ {need:.0%}), có thể cược"
            self._log_decision("bet")
            return out

        # Sau mỗi chu kỳ nghỉ phải có ít nhất 3 phiên hoạt động, tránh kiểu
        # đánh một phiên rồi lại nghỉ ngay. Trong nhịp hoạt động, vẫn báo tín hiệu
        # tổng hợp nhưng ghi rõ đây là tín hiệu yếu hơn chuẩn.
        if self.active_after_pause > 0:
            self.active_after_pause -= 1
            self.skip_streak = 0
            out["side"] = side
            out["reason"] = (
                f"Đã qua nhịp nghỉ · hoạt động {ACTIVE_SESSIONS_AFTER_PAUSE - self.active_after_pause}/"
                f"{ACTIVE_SESSIONS_AFTER_PAUSE}; tín hiệu thận trọng ({post:.0%})"
            )
            out["active_cycle"] = True
            return out

        # Chỉ nghỉ khi cầu khó rõ rệt. Mỗi đợt nghỉ 2 hoặc tối đa 3 phiên.
        votes = [v for v in strat.values() if v in (TAI, XIU)]
        agreement = max(votes.count(TAI), votes.count(XIU)) / len(votes) if votes else 0.5
        severe = (
            post < max(0.50, need - 0.10)
            or agreement < 0.55
            or self.worse_than_chance()
            or p_side < 0.54
        )
        duration = min(MAX_PAUSE_SESSIONS, 3 if severe else PAUSE_SESSIONS)
        self.pause_total = duration
        self.pause_remaining = duration - 1
        self.active_after_pause = ACTIVE_SESSIONS_AFTER_PAUSE
        self.pause_events += 1
        self.skip_streak = 1
        self.skips += 1
        out["reason"] = f"Cầu khó · tạm nghỉ 1/{duration} phiên; sau đó hoạt động ít nhất {ACTIVE_SESSIONS_AFTER_PAUSE} phiên"
        out["pause"] = True
        self._log_decision("skip")
        return out

    def record_shadow(self, strat, actual):
        """Ghi nhận từng chiến lược đúng hay sai sau khi biết kết quả."""
        for name, pred in (strat or {}).items():
            dq = self.shadow.setdefault(name, deque(maxlen=self.SHADOW_WINDOW * 2))
            if pred is None:
                continue
            dq.append(1 if pred == actual else 0)

    def bias_report(self):
        """p-value của hai kiểm định: xúc xắc có lệch mặt không, Tài/Xỉu có lệch 50/50 không."""
        recs = list(self.recs.values())
        faces = [0] * 6
        for _, _, dice in recs[-300:]:
            for x in dice:
                if 1 <= x <= 6:
                    faces[x - 1] += 1
        total = sum(faces)
        p_face = None
        if total >= 60:
            exp = total / 6.0
            chi = sum((o - exp) ** 2 / exp for o in faces)
            p_face = chi2_sf(chi, 5)
        seq = self.results()[-500:]
        n = len(seq)
        p_tx = None
        if n >= 30:
            k = sum(1 for x in seq if x == TAI)
            z = (k - n / 2.0) / math.sqrt(n / 4.0)
            p_tx = math.erfc(abs(z) / math.sqrt(2))
        return p_face, p_tx

    def learn(self, experts_p, p_tai, actual):
        if not experts_p:
            return
        y = actual == TAI
        for key, p in experts_p.items():
            p = min(max(p, self.EPS), 1 - self.EPS)
            loss = -math.log(p if y else 1 - p)
            self.loss[key] = self.GAMMA * self.loss.get(key, 0.0) + loss
        pt = min(max(p_tai, self.EPS), 1 - self.EPS)
        self.calib.append((pt, y))

    def record(self, pred, p_side, actual):
        if pred is None:
            return
        self.perf.append((pred, actual, p_side))
        if len(self.perf) >= 20:
            acc = self.accuracy() / 100
            if acc < 0.47:
                self.extra_post = min(0.04, self.extra_post + 0.01)
            elif acc > 0.55:
                self.extra_post = max(0.0, self.extra_post - 0.005)

    def _log_decision(self, kind):
        if not hasattr(self, "decision_log"):
            self.decision_log = deque(maxlen=200)
        self.decision_log.append(kind)

    PAUSE_WINDOW = 80
    PAUSE_P = 0.05

    def recent_hits(self):
        recent = list(self.perf)[-self.PAUSE_WINDOW:]
        n = len(recent)
        k = sum(1 for pr, ac, _ in recent if pr == ac)
        return k, n

    def worse_than_chance(self):
        """Tạm dừng cược khi dự đoán gần đây kém ngẫu nhiên có ý nghĩa thống kê."""
        k, n = self.recent_hits()
        return n >= 30 and binom_p_le(k, n) < self.PAUSE_P

    def accuracy(self):
        if not self.perf:
            return 0.0
        hits = sum(1 for pr, ac, _ in self.perf if pr == ac)
        return hits / len(self.perf) * 100

    def logloss(self):
        if not self.calib:
            return None
        return sum(-math.log(p if y else 1 - p) for p, y in self.calib) / len(self.calib)

    def expected_roi(self):
        if not self.perf:
            return 0.0
        return sum(p * WIN_PROFIT - (1 - p) * LOSE_LOSS for _, _, p in self.perf) / len(self.perf)


# ============================================================
# TRẠNG THÁI
# ============================================================
def normalize_table(value):
    value = norm(str(value or "")).replace("_", "")
    if value in ("md5", "txmd5", "taixiumd5"):
        return "md5"
    if value in ("tx", "thuong", "taixiu", "tx thuong", "taixiu thuong"):
        return "tx"
    return None


DATA_DIR = os.environ.get("LC79_DATA_DIR", "/data").strip() or "/data"


def _log_path(cid, table):
    safe_cid = str(cid).replace("-", "_")
    return os.path.join(DATA_DIR, f"history_{safe_cid}_{table}.json")


def load_persisted_logs(cid, table):
    try:
        with open(_log_path(cid, table), "r", encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError, TypeError):
        return []


def persist_logs(cid, table, rows):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = _log_path(cid, table)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(rows)[-3000:], f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Không lưu được lịch sử %s: %s", table, exc)


def format_history_txt(rows, table):
    lines = [
        f"Ban: {TABLES[table]['label']}",
        f"Tong ban ghi: {len(rows)}",
        "PHIEN | DU DOAN | KET QUA | DUNG/SAI | CUOC | LAI/LO | XUC XAC",
        "-" * 90,
    ]
    for r in rows:
        pred = side_label(r.get("pred")) if r.get("pred") else "BO QUA"
        actual = side_label(r.get("res")) if r.get("res") else "?"
        check = "DUNG" if r.get("pred_ok") else ("SAI" if r.get("pred") else "N/A")
        bet = fmt(r.get("bet", 0)) if r.get("placed") else "0"
        delta = signed(r.get("delta")) if r.get("delta") is not None else "CHUA DOI SOAT"
        dice = "-".join(str(x) for x in (r.get("dice") or [])) or "-"
        lines.append(f"{r.get('sid','?')} | {pred} | {actual} | {check} | {bet} | {delta} | {dice}")
    return "\n".join(lines)


def new_state(table="md5"):
    return {
        "table": table, "engine": CauEngine(), "running": False, "gen": 0,
        "auto": False,
        "base_bet": 10000, "mode": DEFAULT_MODE, "notify": True,
        "martingale": False, "mg_mult": 2.0, "mg_step": 0,
        "stop_loss": 0, "stop_win": 0, "daily_limit": 0,
        "day": vn_now().strftime("%Y-%m-%d"), "day_bet": 0,
        "session_id": None, "last_open_sid": None, "pending": None, "last_result": None,
        "bet_log": deque(maxlen=3000),
        "balance": 0, "initial_balance": 0, "profit": 0,
        "access_token": "", "jwt": "", "total_win": 0, "total_lose": 0,
        "win_streak": 0, "lose_streak": 0, "total_bet": 0, "nickname": "",
        "login_at": 0, "ws_seen": 0, "ws_connected": False, "poll_last_id": None, "poll_on": False,
    }


def get_state(cid, table=DEFAULT_TABLE):
    table = normalize_table(table) or DEFAULT_TABLE
    with state_lock:
        if cid not in user_states:
            user_states[cid] = {"tables": {"md5": new_state("md5"), "tx": new_state("tx")}, "reserved_bets": 0}
            for table_key in ("md5", "tx"):
                user_states[cid]["tables"][table_key]["bet_log"].extend(load_persisted_logs(cid, table_key)[-3000:])
        root = user_states[cid]
        if "tables" not in root:  # nâng cấp state cũ nếu tiến trình còn giữ dữ liệu
            legacy = root
            root = {"tables": {"md5": legacy, "tx": new_state("tx")}, "reserved_bets": 0}
            legacy["table"] = "md5"
            user_states[cid] = root
        return root["tables"][table]


def current_bet(st):
    base = int(st.get("base_bet") or 0)
    if not st.get("martingale"):
        return base
    step = int(st.get("mg_step") or 0)
    mult = float(st.get("mg_mult") or 2)
    if step <= 0:
        return base
    # Gấp thếp: base * mult^step, trần 20 lần base
    amt = int(round(base * (mult ** step)))
    return min(amt, base * 20)


def risk_block(st, amount):
    today = vn_now().strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"], st["day_bet"] = today, 0
    if st["stop_loss"] and st["profit"] <= -st["stop_loss"]:
        return "Đã chạm mức dừng lỗ"
    if st["stop_win"] and st["profit"] >= st["stop_win"]:
        return "Đã chạm mức dừng lãi"
    if st["daily_limit"] and st["day_bet"] + amount > st["daily_limit"]:
        return "Vượt giới hạn cược ngày"
    if amount > int(st.get("balance") or 0):
        return "Số dư thật không đủ hoặc chưa đọc được"
    return None


def _apply_auto_settings(st, params):
    """Áp dụng mode / stop-loss / stop-win / daily limit từ web."""
    params = params or {}
    if "mode" in params:
        mode = norm(str(params["mode"]))
        if mode in MODES:
            st["mode"] = mode
        else:
            raise ValueError("mode nhận antoan, canbang hoặc maohiem")
    for src, dst in (("sl", "stop_loss"), ("sw", "stop_win"), ("limit", "daily_limit")):
        if src in params and str(params[src]).strip() != "":
            st[dst] = parse_amount(str(params[src]))


def _set_auto(cid, table, enabled, amount=None, params=None):
    """Bật/tắt auto cho một bàn (web)."""
    st = get_state(cid, table)
    if enabled:
        if not st.get("running"):
            return False, "Chưa đăng nhập hoặc bàn chưa kết nối"
        try:
            _apply_auto_settings(st, params or {})
            if amount is not None and str(amount).strip() != "":
                value = parse_amount(str(amount))
                if value <= 0:
                    raise ValueError("Mức cược phải lớn hơn 0")
                st["base_bet"] = value
        except ValueError as exc:
            return False, str(exc)
        if int(st.get("base_bet") or 0) <= 0:
            return False, "Mức cược phải lớn hơn 0"
        balance = refresh_state_balance(st)
        if balance is None:
            return False, "Không xác minh được số dư thật từ API"
        block = risk_block(st, current_bet(st))
        if block:
            return False, block
        st["auto"] = True
        push_event(cid, f"Auto {TABLES[table]['label']}", f"BẬT · {fmt(st['base_bet'])} · {MODES[st['mode']]['label']}", "ok")
        return True, f"Đã bật {TABLES[table]['label']} · {fmt(st['base_bet'])} · {MODES[st['mode']]['label']}"
    st["auto"] = False
    push_event(cid, f"Auto {TABLES[table]['label']}", "ĐÃ TẮT", "warn")
    return True, f"Đã tắt Auto {TABLES[table]['label']}"


# ============================================================
# NỘI DUNG CÁC Ô
# ============================================================
RULE = "─" * 30


def confidence_label(c):
    if c >= 0.90:
        return "rất mạnh"
    if c >= 0.75:
        return "mạnh"
    if c >= 0.55:
        return "vừa"
    return "yếu"


def hx(v):
    return html.escape(str(v), quote=False)


def streak_icons(count, icon, limit=5):
    if count <= 0:
        return "—"
    return f"{icon * min(count, limit)}  ×{count}"


def prediction_text(sid, info, st):
    """Thẻ tín hiệu gọn: chỉ bàn, phiên, cửa dự đoán và mức cược."""
    mode = MODES[st["mode"]]
    if info.get("side") is None:
        return "\n".join([
            f"✨ <b>{hx(TABLES[st['table']]['label'])}</b>",
            f"🧾 Phiên <b>#{hx(sid)}</b>",
            "⏸️ <b>TẠM BỎ QUA</b>",
        ])
    side = info["side"]
    return "\n".join([
        f"╭━━━ ✨ <b>LC79 VIP PRO</b> ━━━╮",
        f"{('🔷' if st['table'] == 'md5' else '🟣')} <b>{hx(TABLES[st['table']]['label'])}</b>",
        f"🧾 Phiên <b>#{hx(sid)}</b>",
        "",
        f"🎯 DỰ ĐOÁN  <b>{hx(side_label(side))} {side_icon(side)}</b>",
        f"💎 MỨC CƯỢC  <b>{fmt(current_bet(st))}</b>",
        f"⚙️ CHẾ ĐỘ  <b>{hx(mode['label'])}</b>",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
    ])


def result_text(rec, st):
    """Thẻ kết quả: kết quả và số liệu in đậm, mô tả nhạt."""
    eng = st["engine"]
    total = f"  ·  tổng {rec['total']}" if rec.get("total") is not None else ""
    lines = [f"<b>🎲 Kết quả phiên #{hx(rec['sid'])}</b>",
             f"{hx(dice_faces(rec['dice']))}{total}  →  <b>{hx(side_label(rec['res']))}</b>", ""]
    if rec["pred"] is None:
        det = "<i>Phiên này bot không đoán.</i>"
    elif rec["pred_ok"]:
        det = f"Bot đoán <b>{hx(side_label(rec['pred']))}</b>  →  <b>đúng ✅</b>"
    else:
        det = f"Bot đoán <b>{hx(side_label(rec['pred']))}</b>  →  <b>sai ❌</b>"
    if rec["placed"]:
        verdict = "thắng ✅" if rec["won"] else "thua ❌"
        delta_txt = signed(rec["delta"]) if rec.get("delta") is not None else "chưa đối soát số dư thật"
        cuoc = f"Bạn cược <b>{fmt(rec['bet'])}</b>  →  <b>{verdict}</b> · biến động <b>{hx(delta_txt)}</b>"
    else:
        cuoc = "<i>Phiên này chưa đặt cược.</i>"
    total_bets = st["total_win"] + st["total_lose"]
    wr = st["total_win"] / total_bets * 100 if total_bets else 0
    lines += [f"<blockquote>{det}\n{cuoc}</blockquote>",
              f"<blockquote>Lãi/lỗ tài khoản <b>{signed(st['profit'])}</b>\n"
              f"🔥 Thắng liên <b>{hx(streak_icons(st['win_streak'], '🔥'))}</b>  ·  🧊 Thua liên <b>{hx(streak_icons(st['lose_streak'], '🧊'))}</b>\n"
              f"Tỷ lệ thắng <b>{wr:.1f}%</b> <i>({st['total_win']}/{total_bets})</i>\n"
              f"Đoán đúng <b>{eng.accuracy():.1f}%</b> <i>({len(eng.perf)} lần)</i></blockquote>",
              trail(eng.results(), 10) or "—"]
    return "\n".join(lines)


def line(icon, label, value):
    return f"{icon} {hx(label)}  <b>{hx(value)}</b>"


def card(icon, title, rows=None, note=None, footer=None):
    out = [f"{icon} <b>{hx(title)}</b>"]
    if rows:
        out.append("<blockquote>" + "\n".join(rows) + "</blockquote>")
    if note:
        out.append(f"<i>{hx(note)}</i>")
    if footer:
        out += ["", footer]
    return "\n".join(out)


def _runs_text(seq):
    r = runs_test(seq[-200:])
    if r is None:
        return "chưa đủ dữ liệu"
    p, R, mu = r
    verdict = "bất thường ⚠️" if p < 0.05 else "bình thường ✅"
    return f"p={p:.3f} · {verdict}"


def apply_shared_balance(st, balance):
    """Cập nhật số dư API thật cho cả hai bàn dùng cùng tài khoản."""
    if balance is None:
        return
    access, jwt = st.get("access_token"), st.get("jwt")
    for root in list(user_states.values()):
        if "tables" not in root:
            continue
        for other in root["tables"].values():
            if other.get("access_token") == access and other.get("jwt") == jwt:
                other["balance"] = int(balance)
                other["profit"] = int(balance) - int(other.get("initial_balance") or 0)


def refresh_state_balance(st):
    """Đồng bộ số dư thật; cùng tài khoản nên cập nhật hiển thị cho cả hai bàn."""
    balance = fetch_account_balance(st.get("access_token"), st.get("jwt"))
    if balance is not None:
        apply_shared_balance(st, balance)
    return balance


def account_text(st):
    eng = st["engine"]
    total = st["total_win"] + st["total_lose"]
    wr = st["total_win"] / total * 100 if total else 0
    ll = eng.logloss()
    ll_txt = f"{ll:.3f}  (ngẫu nhiên 0.693)" if ll is not None else "chưa đủ dữ liệu"
    head = f"👤 <b>Tài khoản {hx(st['nickname'] or '—')}</b>"
    rows1 = [
        line("🔌", "Kết nối", "đang chạy" if st["running"] else "chưa đăng nhập"),
        line("🤖", "Tự động", "đang bật" if st["auto"] else "đang tắt"),
        line("🧭", "Chế độ", MODES[st["mode"]]["label"]),
        line("💰", "Cược gốc", fmt(st["base_bet"])),
        line("💳", "Số dư", fmt(st["balance"])),
        line("📈", "Lãi/lỗ", signed(st["profit"])),
    ]
    rows2 = [
        line("🏆", "Thắng/thua", f"{st['total_win']} / {st['total_lose']}  ({wr:.1f}%)"),
        line("🔥", "Thắng liên", streak_icons(st["win_streak"], "🔥")),
        line("🧊", "Thua liên", streak_icons(st["lose_streak"], "🧊")),
        line("🎯", "Đoán đúng", f"{eng.accuracy():.1f}%  trên {len(eng.perf)} phiên"),
        line("📐", "EV dự báo", f"{eng.expected_roi() * 100:+.1f}% (ước tính mô hình)"),
        line("📉", "Sai số", ll_txt),
        line("🧬", "Tính ngẫu nhiên", _runs_text(eng.results())),
        line("📚", "Đã nạp", f"{len(eng.recs)} phiên · tạm nghỉ {eng.pause_events} đợt"),
    ]
    return "\n".join([
        head, "",
        "<blockquote>" + "\n".join(rows1) + "</blockquote>",
        "<blockquote>" + "\n".join(rows2) + "</blockquote>",
        trail(eng.results(), 10) or "—",
    ])


def lichsu_text(rows, n):
    recent = rows[-n:]
    lines_ = []
    for r in reversed(recent):
        icon = "⏭️" if r["pred"] is None else ("✅" if r["pred_ok"] else "❌")
        pred = "không đoán" if r["pred"] is None else f"đoán {side_label(r['pred'])}"
        delta_txt = signed(r["delta"]) if r.get("delta") is not None else "chưa đối soát"
        bet = "không cược" if not r["placed"] else f"cược {fmt(r['bet'])} · {delta_txt}"
        lines_.append(f"{icon} <b>#{hx(r['sid'])}</b>  KQ {hx(side_label(r['res']))}  ·  {hx(pred)}  ·  {hx(bet)}")
    scored = [r for r in rows if r["pred"] is not None]
    hit = sum(1 for r in scored if r["pred_ok"])
    placed = [r for r in rows if r["placed"]]
    wins = sum(1 for r in placed if r["won"])
    known_deltas = [r["delta"] for r in placed if r.get("delta") is not None]
    profit = sum(known_deltas)
    unverified = len(placed) - len(known_deltas)
    pct = hit / len(scored) * 100 if scored else 0
    summary = [
        line("🎯", "Đoán đúng", f"{hit} / {len(scored)}  ({pct:.1f}%)"),
        line("💵", "Đã cược", f"{len(placed)} lần · 🏆 {wins} thắng · 💥 {len(placed) - wins} thua"),
        line("📈", "Biến động đã đối soát", f"{signed(profit)} · chưa đối soát {unverified}"),
    ]
    return "\n".join([
        f"📜 <b>Lịch sử {len(recent)} phiên gần nhất</b>", "",
        "<blockquote>" + "\n".join(lines_) + "</blockquote>",
        "<blockquote>" + "\n".join(summary) + "</blockquote>",
    ])




# ============================================================
# NẠP PHIÊN + ĐĂNG NHẬP
# ============================================================
def _payload_items(data):
    """Lấy danh sách phiên từ nhiều kiểu JSON LC79 từng trả về."""
    if isinstance(data, list):
        return data, {}
    if not isinstance(data, dict):
        return [], {}
    for key in ("list", "sessions", "items", "records"):
        if isinstance(data.get(key), list):
            return data[key], data
    nested = data.get("data")
    if isinstance(nested, list):
        return nested, data
    if isinstance(nested, dict):
        for key in ("list", "sessions", "items", "records"):
            if isinstance(nested.get(key), list):
                return nested[key], nested
    return [], data


def _normalize_result(value):
    value = norm(str(value or "")).upper().replace(" ", "")
    if value in ("TAI", "BIG", "OVER", "HIGH"):
        return TAI
    if value in ("XIU", "SMALL", "UNDER", "LOW"):
        return XIU
    return None


def parse_lines(data):
    """Chuẩn hóa lịch sử cả API txmd5 và API tx cũ từ lc79.py."""
    items, _meta = _payload_items(data)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("id", it.get("session", it.get("Phien", it.get("sessionId"))))
        if sid is None:
            continue
        result = _normalize_result(
            it.get("resultTruyenThong", it.get("Ket_qua", it.get("result", it.get("outcome"))))
        )
        if result not in (TAI, XIU):
            continue
        dice = it.get("dices")
        if not isinstance(dice, (list, tuple)) or len(dice) != 3:
            dice = [it.get("Xuc_xac_1"), it.get("Xuc_xac_2"), it.get("Xuc_xac_3")]
        try:
            dice = [int(x) for x in dice]
            if len(dice) != 3 or any(x < 1 or x > 6 for x in dice):
                dice = []
        except (TypeError, ValueError):
            dice = []
        raw_total = it.get("point", it.get("Tong", it.get("total")))
        try:
            total = int(raw_total) if raw_total is not None else (sum(dice) if dice else None)
        except (TypeError, ValueError):
            total = sum(dice) if dice else None
        out.append({"session": normalize_sid(sid), "dice": dice, "total": total, "result": result})
    # Gộp trùng ID và sắp xếp từ cũ đến mới, không phụ thuộc API trả tăng hay giảm.
    unique = {}
    for item in out:
        unique[item["session"]] = item
    return sorted(unique.values(), key=lambda x: sid_sort_key(x["session"]))


def _fetch_paginated(url, origin):
    headers = {"User-Agent": "Mozilla/5.0", "Origin": origin}
    response = requests.get(url, headers=headers, timeout=12)
    if response.status_code != 200:
        return []
    payload = response.json()
    items, meta = _payload_items(payload)
    collected = list(items)
    # Chỉ phân trang khi API có metadata xác nhận; không đoán tham số khi API không hỗ trợ.
    pagination = meta.get("pagination", meta.get("meta", {})) if isinstance(meta, dict) else {}
    if isinstance(pagination, dict):
        current = pagination.get("current_page", pagination.get("currentPage", pagination.get("page", 1)))
        last = pagination.get("last_page", pagination.get("total_pages", pagination.get("totalPages", current)))
        try:
            current, last = int(current), min(int(last), 100)
        except (TypeError, ValueError):
            current, last = 1, 1
        for page in range(current + 1, last + 1):
            page_response = requests.get(url, params={"page": page}, headers=headers, timeout=12)
            if page_response.status_code != 200:
                break
            page_items, _ = _payload_items(page_response.json())
            if not page_items:
                break
            collected.extend(page_items)
    return parse_lines(collected)


def fetch_history_from_api(table=DEFAULT_TABLE, limit=None):
    """Nạp lịch sử từ API riêng của bàn được chọn."""
    table = normalize_table(table) or DEFAULT_TABLE
    cfg = TABLES[table]
    urls = [u for u in (cfg["history"], cfg["fallback"]) if u]
    for url in urls:
        try:
            arr = _fetch_paginated(url, cfg["origin"])
            if arr:
                return arr if limit is None else arr[-limit:]
        except Exception as exc:
            log.warning("Lỗi lấy lịch sử %s: %s", url, short(exc, 100) if 'short' in globals() else str(exc)[:100])
    return []


def fetch_latest_results(table=DEFAULT_TABLE, expect=None, retries=4):
    arr = []
    for _ in range(retries):
        arr = fetch_history_from_api(table, MAX_HISTORY)
        if arr and (expect is None or arr[-1]["result"] == expect):
            return arr
        time.sleep(1.0)
    return arr


def md5_hash(t):
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def fetch_account_balance(access_token, jwt):
    """Đọc số dư thật từ endpoint tài khoản; None nếu API không xác nhận được."""
    if not access_token or not jwt:
        return None
    try:
        response = requests.get(
            ACCOUNT_URL,
            params={"cp": "R", "cl": "R", "pf": "web", "at": access_token},
            headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {jwt}"},
            timeout=8,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        candidates = [data]
        if isinstance(data, dict):
            for key in ("data", "account", "result", "remoteLoginResp"):
                if isinstance(data.get(key), dict):
                    candidates.append(data[key])
        for candidate in candidates:
            for key in ("balance", "money", "availableBalance", "wallet"):
                value = candidate.get(key) if isinstance(candidate, dict) else None
                if value is not None:
                    return int(float(value))
    except Exception as exc:
        log.warning("Không đọc được số dư thật: %s", str(exc)[:100])
    return None


def login_and_get_token(u, p):
    try:
        login_response = requests.get(
            "https://apifo88daigia.tele68.com/api",
            params={"c": "3", "un": u, "pw": md5_hash(p), "cp": "R", "cl": "R", "pf": "web", "at": ""},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
        ).json()
        if not login_response.get("success"):
            return {"_error": "Sai tài khoản hoặc mật khẩu"}
        access = login_response.get("accessToken")
        session_key = str(login_response.get("sessionKey", ""))
        padded = session_key + "=" * (-len(session_key) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        nickname = payload.get("nickname")
        if not access or not nickname:
            return {"_error": "API đăng nhập thiếu accessToken hoặc nickname"}
        lobby = requests.post(
            "https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=",
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            json={"nickName": nickname, "accessToken": access}, timeout=12,
        ).json()
        jwt = lobby.get("token")
        if not jwt:
            return {"_error": "Không nhận được token phiên LC79"}
        money = fetch_account_balance(access, jwt)
        if money is None:
            remote = lobby.get("remoteLoginResp", {})
            try:
                money = int(float(remote.get("money", 0)))
            except (TypeError, ValueError):
                money = 0
        return {"token": jwt, "access": access, "nickname": nickname, "money": money}
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


# ============================================================
# CHỐT PHIÊN, CƯỢC, WEBSOCKET
# ============================================================
def open_round(cid, sid, table=DEFAULT_TABLE):
    """Mở một ván trong đúng bàn, không dùng nhầm lịch sử bàn còn lại."""
    table = normalize_table(table) or DEFAULT_TABLE
    st = get_state(cid, table)
    with state_lock:
        if sid is None:
            return None
        if st.get("last_open_sid") is not None and str(st["last_open_sid"]) == str(sid):
            return st.get("pending")
        st["session_id"] = sid
        st["last_open_sid"] = sid
        # Chỉ dự đoán khi Auto đã bật (web)
        if not st.get("auto"):
            st["pending"] = None
            return None
        info = st["engine"].predict(st["mode"])
        p_side = info["p"] if info["side"] == TAI else 1 - info["p"]
        # Thua ≥3 tay liên tiếp → tạm dừng, chỉ đánh lại khi đủ chắc
        lose_n = int(st.get("lose_streak") or 0)
        if info.get("side") and lose_n >= LOSS_STREAK_PAUSE:
            need_rec = max(LOSS_RECOVERY_P, float(MODES.get(st["mode"], {}).get("p_side_min") or 0.55) + 0.04)
            if p_side < need_rec:
                info["side"] = None
                info["reason"] = f"Thua {lose_n} tay · chờ chắc ≥{need_rec:.0%} (hiện {p_side:.0%})"
                p_side = info["p"] if info.get("p_tai", 0.5) >= 0.5 else 1 - info.get("p_tai", 0.5)
        text = f"<b>{TABLES[table]['label']}</b>\n" + prediction_text(sid, info, st)
        pend = {"sid": sid, "table": table, "pred": info["side"], "p_side": p_side, "p_tai": info["p_tai"],
                "experts": info["experts"], "strat": info.get("strat", {}), "placed": False, "sent": False, "bet": 0,
                "base": text, "status": [], "msg_id": None, "done": False,
                "balance_before": int(st.get("balance") or 0), "balance_after_bet": None,
                "reason": info.get("reason") or info.get("ctx") or ""}
        st["pending"] = pend
        # Hiện ngay trên lịch sử: dự đoán trước khi có kết quả
        pre = {
            "sid": sid,
            "pred": info.get("side"),
            "res": None,
            "dice": None,
            "total": None,
            "pred_ok": None,
            "placed": False,
            "bet": 0,
            "won": None,
            "delta": None,
            "reason": info.get("reason") or info.get("ctx") or "",
            "time": vn_now().strftime("%H:%M:%S %d/%m/%Y"),
            "status": "pending",
        }
        # cập nhật nếu sid đã có, không thì append
        logs = st["bet_log"]
        updated = False
        for i in range(len(logs) - 1, -1, -1):
            if str(logs[i].get("sid")) == str(sid):
                logs[i] = pre
                updated = True
                break
        if not updated:
            logs.append(pre)
    return pend


def poll_round(cid, arr, table=DEFAULT_TABLE):
    """Dự phòng khi websocket không gửi sự kiện phiên mới cho bàn này."""
    st = get_state(cid, table)
    now = time.time()
    if st.get("ws_seen", 0) and now - st["ws_seen"] < 90:
        return
    if now - st.get("login_at", now) < 60:
        return
    last = arr[-1]
    sid_last = last["session"]
    if not st.get("poll_on"):
        st["poll_on"] = True
        say(cid, "Websocket chưa gửi phiên mới. Bot đang theo dõi qua lịch sử. "
                 "Cược tự động cần websocket hoạt động.", force=True)
    to_settle = None
    with state_lock:
        if st.get("poll_last_id") == sid_last:
            return
        st["poll_last_id"] = sid_last
        if st["pending"] is not None:
            to_settle = st["pending"]
            st["pending"] = None
            to_settle["done"] = True
    if to_settle is not None:
        settle_round(cid, last["result"], last["dice"], to_settle, table)
    try:
        next_sid = int(sid_last) + 1
    except (TypeError, ValueError):
        next_sid = None
    if next_sid is not None:
        open_round(cid, next_sid, table)


def render_pending(pend):
    if not pend["status"]:
        return pend["base"]
    return pend["base"] + "\n\n<b>Trạng thái</b>\n" + "\n".join(hx(x) for x in pend["status"])


def card_edit(cid, mid, text):
    return



def pend_status(cid, pend, line):
    """Thêm một dòng trạng thái vào đúng tin nhắn dự đoán của ván đó, rồi sửa tin."""
    if pend is None:
        return
    with state_lock:
        if pend.get("done"):
            return
        pend["status"] = (pend["status"] + [line])[-4:]
        text = render_pending(pend)
        mid = pend.get("msg_id")
    if mid:
        card_edit(cid, mid, text)


def short(x, n=28):
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def settle_round(cid, res, ws_dices, pend, table=DEFAULT_TABLE):
    table = normalize_table(table) or (pend.get("table") if pend else DEFAULT_TABLE)
    st = get_state(cid, table)
    sid = pend["sid"] if pend else st.get("session_id")
    dice = ws_dices or []

    # Học ngay từ sự kiện kết quả websocket để phiên tiếp theo không dùng lịch sử cũ.
    with state_lock:
        if pend is not None:
            if sid is not None:
                st["engine"].ingest([{"session": normalize_sid(sid), "result": res,
                                      "total": sum(dice) if dice else None, "dice": dice}])
            st["engine"].learn(pend.get("experts", {}), pend.get("p_tai", 0.5), res)
            st["engine"].record_shadow(pend.get("strat", {}), res)
            if pend.get("pred") is not None:
                st["engine"].record(pend["pred"], pend.get("p_side", 0.5), res)
            pend["done"] = True
        mid = pend.get("msg_id") if pend else None
        pred = pend.get("pred") if pend else None
        pred_ok = None if pred is None else (pred == res)
        placed = bool(pend and pend.get("placed"))
        bet = int(pend.get("bet", 0)) if pend else 0
        balance_before = pend.get("balance_before") if pend else None
        won = (pred == res) if placed else None
        if placed:
            if won:
                st["total_win"] += 1
                st["win_streak"] += 1
                st["lose_streak"] = 0
            else:
                st["total_lose"] += 1
                st["lose_streak"] += 1
                st["win_streak"] = 0

    # API lịch sử bổ sung xúc xắc/phiên; API tài khoản xác nhận lãi lỗ thật, không mô phỏng.
    history = fetch_latest_results(table, expect=res, retries=2)
    matched = None
    for item in history:
        if str(item["session"]) == str(sid):
            matched = item
            break
    if matched is None and sid is None and history and history[-1]["result"] == res:
        matched = history[-1]
    if matched:
        dice = matched.get("dice") or dice
        if sid is not None and dice:
            with state_lock:
                key = normalize_sid(sid)
                if key in st["engine"].recs:
                    st["engine"].recs[key] = (res, matched.get("total"), dice)

    real_balance = None
    if placed:
        for attempt in range(3):
            real_balance = fetch_account_balance(st.get("access_token"), st.get("jwt"))
            if real_balance is not None:
                break
            if attempt < 2:
                time.sleep(1.0)

    with state_lock:
        delta = None
        if real_balance is not None:
            apply_shared_balance(st, real_balance)
            if balance_before is not None:
                delta = real_balance - int(balance_before)
        elif placed:
            # Không có số dư đối soát: khóa Auto cả hai bàn thay vì tiếp tục bằng số dư cũ.
            for root in list(user_states.values()):
                if "tables" in root:
                    for other in root["tables"].values():
                        if other.get("access_token") == st.get("access_token") and other.get("jwt") == st.get("jwt"):
                            other["auto"] = False
            say(cid, "⚠️ Không đối soát được số dư sau cược. Auto hai bàn đã tạm tắt để tránh đặt tiếp bằng số dư cũ.", force=True)
        release_bet_reservation(cid, pend)
        reason = ""
        try:
            if pend:
                reason = str(pend.get("reason") or "")
            if not reason and st.get("engine"):
                info = getattr(st["engine"], "last_info", None) or {}
                reason = str(info.get("reason") or "")
        except Exception:
            reason = ""
        rec = {
            "sid": sid if sid is not None else "?",
            "pred": pred,
            "res": res,
            "dice": dice,
            "total": sum(dice) if dice else (matched.get("total") if matched else None),
            "pred_ok": pred_ok,
            "placed": placed,
            "bet": bet,
            "won": won,
            "delta": delta,
            "reason": reason,
            "time": vn_now().strftime("%H:%M:%S %d/%m/%Y"),
            "status": "done",
        }
        # Ghi đè dòng pending cùng sid (đã hiện dự đoán trước đó)
        logs = st["bet_log"]
        updated = False
        for i in range(len(logs) - 1, -1, -1):
            if str(logs[i].get("sid")) == str(rec["sid"]):
                logs[i] = rec
                updated = True
                break
        if not updated:
            logs.append(rec)
        st["last_result"] = rec
        # Gấp thếp: thua thì tăng bậc, thắng/reset khi đúng hoặc không đặt
        if st.get("martingale"):
            if placed and pred_ok is False:
                st["mg_step"] = min(8, int(st.get("mg_step") or 0) + 1)
            else:
                st["mg_step"] = 0
        persist_logs(cid, table, st["bet_log"])
        if placed:
            if pred_ok:
                push_event(cid, f"Thắng {TABLES[table]['label']}", f"{side_label(pred)} · {fmt(bet)}", "ok")
            else:
                push_event(cid, f"Thua {TABLES[table]['label']}", f"{side_label(pred)} · {fmt(bet)}", "warn")
        text = f"<b>{TABLES[table]['label']}</b>\n" + result_text(rec, st)
    if mid:
        card_edit(cid, mid, text)



def handle_ack(cid, data, pend, table=DEFAULT_TABLE):
    st = get_state(cid, table)
    if pend is None or pend.get("done"):
        return
    rejected = isinstance(data, dict) and (
        bool(data.get("error")) or data.get("success") is False
        or data.get("status") in (False, 0, "error", "fail", "failed"))
    if rejected:
        release_bet_reservation(cid, pend)
        line = f"❌ Server từ chối: {short(data)}"
    elif data is None:
        line = "⏳ Chờ server xác nhận..."
    else:
        line = "✅ Server đã nhận cược"
        with state_lock:
            if not pend["placed"]:
                pend["placed"] = True
                pend.setdefault("balance_before", int(st.get("balance") or 0))
                st["day_bet"] += pend["bet"]
                st["total_bet"] += pend["bet"]
    pend_status(cid, pend, line)


def start_websocket(cid, token, table=DEFAULT_TABLE):
    table = normalize_table(table) or DEFAULT_TABLE
    cfg = TABLES[table]
    st = get_state(cid, table)
    socket_key = (cid, table)
    old = active_sockets.pop(socket_key, None)
    if old is not None:
        try:
            old.disconnect()
        except Exception:
            pass

    sio = socketio.Client(reconnection=True, reconnection_attempts=99999, reconnection_delay=3,
                          logger=False, engineio_logger=False)
    active_sockets[socket_key] = sio

    @sio.event(namespace=cfg["namespace"])
    def connect():
        st["ws_connected"] = True
        arr = fetch_history_from_api(table, MAX_HISTORY)
        if arr:
            with state_lock:
                st["engine"].ingest(arr)
            say(cid, f"{TABLES[table]['label']} đã kết nối · {len(st['engine'].recs)} phiên", force=True)
        else:
            say(cid, f"⚠️ {TABLES[table]['label']} đã nối WebSocket nhưng API lịch sử chưa trả phiên; bot sẽ thử lại.", force=True)

    @sio.on("new-session", namespace=cfg["namespace"])
    def new_sess(d):
        st["ws_seen"] = time.time()
        open_round(cid, d.get("id"), table)

    @sio.on("tick-update", namespace=cfg["namespace"])
    def tk(d):
        if str(d.get("state", "")).upper() != "BETTING":
            return
        block, amount, pred, pend = None, 0, None, None
        with state_lock:
            pend = st["pending"]
            if not st["auto"] or pend is None or pend["pred"] is None or pend["sent"]:
                return
            # Khóa an toàn: đang chuỗi thua ≥3 và tín hiệu chưa đủ mạnh
            lose_n = int(st.get("lose_streak") or 0)
            if lose_n >= LOSS_STREAK_PAUSE:
                ps = float(pend.get("p_side") or 0)
                need_rec = max(LOSS_RECOVERY_P, float(MODES.get(st["mode"], {}).get("p_side_min") or 0.55) + 0.04)
                if ps < need_rec:
                    return
            amount = current_bet(st)
            block = risk_block(st, amount)
            root = user_states.get(cid, {})
            reserved = int(root.get("reserved_bets", 0)) if "tables" in root else 0
            available = int(st.get("balance") or 0) - reserved
            if block is None and amount > available:
                block = "Số dư khả dụng đã được bàn còn lại giữ chỗ"
            if block:
                st["auto"] = False
            else:
                pend["sent"] = True
                pend["bet"] = amount
                pend["reservation"] = True
                root["reserved_bets"] = reserved + amount
                pend["balance_before"] = int(st.get("balance") or 0)
                pred = pend["pred"]
        if block:
            pend_status(cid, pend, f"⛔ Tự tắt: {short(block, 24)}")
            return
        pend_status(cid, pend, f"Gửi {side_label(pred)} {fmt(amount)}")
        push_event(cid, f"Cược {TABLES[table]['label']}", f"{side_label(pred)} · {fmt(amount)}", "bet")
        with state_lock:
            for i in range(len(st["bet_log"]) - 1, -1, -1):
                if str(st["bet_log"][i].get("sid")) == str(pend.get("sid")):
                    st["bet_log"][i]["placed"] = True
                    st["bet_log"][i]["bet"] = amount
                    st["bet_log"][i]["status"] = "betting"
                    break
        sio.emit("bet", {"type": pred, "amount": amount}, namespace=cfg["namespace"],
                 callback=lambda *args, _p=pend: handle_ack(cid, args[0] if args else None, _p, table))

    @sio.on("bet-result", namespace=cfg["namespace"])
    def br(d):
        confirmed = False
        with state_lock:
            pend = st["pending"]
            if "postBalance" in d:
                try:
                    apply_shared_balance(st, int(float(d["postBalance"])))
                    if pend is not None:
                        pend["balance_after_bet"] = st["balance"]
                        release_bet_reservation(cid, pend)
                except (TypeError, ValueError):
                    pass
                if pend and pend["sent"] and not pend["placed"]:
                    pend["placed"] = True
                    st["day_bet"] += pend["bet"]
                    st["total_bet"] += pend["bet"]
                    confirmed = True
        if confirmed:
            pend_status(cid, pend, f"✅ Cược thành công, số dư {fmt(d['postBalance'])}")
        elif pend is not None:
            pend_status(cid, pend, f"Server: {short(d)}")
        else:
            say(cid, f"Phản hồi từ server:\n{d}", force=True)

    @sio.on("session-result", namespace=cfg["namespace"])
    def sr(d):
        res = d.get("resultTruyenThong")
        if res not in (TAI, XIU):
            return
        # Lấy đúng thẻ của ván vừa kết thúc tại thời điểm nhận kết quả, để ván mới không bị ghi đè.
        with state_lock:
            pend = st["pending"]
            st["pending"] = None
        threading.Thread(target=settle_round, args=(cid, res, d.get("dices"), pend, table), daemon=True).start()

    @sio.event(namespace=cfg["namespace"])
    def disconnect():
        st["ws_connected"] = False

    @sio.on("connect_error")
    def connect_error(data):
        st["ws_connected"] = False
        say(cid, f"Không kết nối được websocket: {str(data)[:80]}", force=True)

    try:
        sio.connect(cfg["ws_url"], socketio_path=cfg["ws_path"], namespaces=[cfg["namespace"]],
                    transports=["websocket"], auth={"token": token},
                    headers={"User-Agent": "Mozilla/5.0", "Origin": cfg["origin"]})
        sio.wait()
    except Exception as e:
        st["ws_connected"] = False
        say(cid, f"Mất kết nối WebSocket: {e}", force=True)


def release_bet_reservation(cid, pend):
    """Giải phóng phần số dư đang được một bàn giữ chỗ để tránh hai bàn cược vượt số dư."""
    if not pend or not pend.get("reservation"):
        return
    with state_lock:
        root = user_states.get(cid)
        if root and "tables" in root:
            root["reserved_bets"] = max(0, int(root.get("reserved_bets", 0)) - int(pend.get("bet", 0)))
        pend["reservation"] = False


def stop_user(cid, table=None):
    targets = [normalize_table(table)] if table else ["md5", "tx"]
    for key in targets:
        if not key:
            continue
        st = get_state(cid, key)
        release_bet_reservation(cid, st.get("pending"))
        st["pending"] = None
        st["running"] = False
        st["ws_connected"] = False
        st["gen"] += 1
        st["auto"] = False
        sio = active_sockets.pop((cid, key), None)
        if sio is not None:
            try:
                sio.disconnect()
            except Exception:
                pass


def sync_loop(cid, table, generation):
    """Đồng bộ lịch sử định kỳ cho bàn tương ứng, đồng thời dự phòng khi WS im lặng."""
    st = get_state(cid, table)
    while st.get("running") and st.get("gen") == generation:
        try:
            records = fetch_history_from_api(table, MAX_HISTORY)
            if records:
                with state_lock:
                    st["engine"].ingest(records)
                poll_round(cid, records, table)
        except Exception as exc:
            log.warning("Sync %s của chat %s lỗi: %s", table, cid, short(exc, 120))
        time.sleep(SYNC_INTERVAL)



# ============================================================
# LỆNH
# ============================================================
def auto_status_text(cid):
    rows = []
    for table in ("md5", "tx"):
        st = get_state(cid, table)
        rows.append(line("🔷" if table == "md5" else "🟣", TABLES[table]["label"],
                         ("🟢 AUTO ON" if st["auto"] else "⚪ AUTO OFF") + f" · cược {fmt(st['base_bet'])}"))
    return card("🎛️", "BẢNG ĐIỀU KHIỂN AUTO", rows,
                note="Hai bàn có lịch sử, thuật toán và streak riêng; số dư tài khoản dùng chung.")



# ENTRYPOINT RENDER: chạy Flask web dashboard
# ============================================================
def main():
    log.info("LC79 VIP Web-only · Flask trên PORT=%s", os.environ.get("PORT", "10000"))
    run_web()


if __name__ == "__main__":
    main()
