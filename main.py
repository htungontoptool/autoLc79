import os
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
from datetime import datetime

import requests
import socketio
import telebot
from flask import Flask

BOT_NAME = "AUTO CƯỢC LC79"

# =========================
# FLASK KEEP ALIVE (RENDER)
# =========================
app = Flask(__name__)


@app.route("/")
def home():
    return f"🤖 {BOT_NAME} ONLINE"


@app.route("/health")
def health():
    return {"status": "online", "bot": "LC79", "server": "Render"}


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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
bot = telebot.TeleBot(BOT_TOKEN)

HISTORY_API_URL = "https://wtxmd52.tele68.com/v1/txmd5/sessions"
API_URL_FALLBACK = "https://living-telecommunications-start-consoles.trycloudflare.com/api/txmd5"
WS_URL = "https://wtxmd52.tele68.com"
MAX_HISTORY = 500
SYNC_INTERVAL = 20
MAX_MARTINGALE_LEVEL = 6
MARTINGALE_MULTIPLIER = 2

# Tỷ lệ trả thưởng: thắng lãi 1.98 lần cược, thua mất 1 lần cược.
WIN_PROFIT = 1.98
LOSE_LOSS = 1.0
# Tỷ lệ thắng tối thiểu để cược có lợi về lâu dài: p * W - (1 - p) * L > 0.
BREAK_EVEN = LOSE_LOSS / (LOSE_LOSS + WIN_PROFIT)

TAI, XIU = "TAI", "XIU"

MODES = {
    "antoan": {"label": "An toàn", "post": 0.75, "min_sessions": 80},
    "canbang": {"label": "Cân bằng", "post": 0.55, "min_sessions": 50},
    "maohiem": {"label": "Mạo hiểm", "post": 0.0, "min_sessions": 1, "never_skip": True},
}
DEFAULT_MODE = "canbang"

bot.set_my_commands([
    telebot.types.BotCommand("start", "Hướng dẫn và danh sách lệnh"),
    telebot.types.BotCommand("dangnhap", "Đăng nhập tài khoản LC79"),
    telebot.types.BotCommand("dung", "Ngắt kết nối an toàn"),
    telebot.types.BotCommand("tudong", "Bật hoặc tắt tự động cược"),
    telebot.types.BotCommand("x2", "Bật hoặc tắt nhân đôi khi thua"),
    telebot.types.BotCommand("caidat", "Xem hoặc đổi cài đặt"),
    telebot.types.BotCommand("thongtin", "Tài khoản và thống kê"),
    telebot.types.BotCommand("lichsu", "Lịch sử đúng sai, lãi lỗ"),
    telebot.types.BotCommand("kiemthu", "Thử thuật toán trên lịch sử"),
])

state_lock = threading.RLock()
active_sockets = {}
user_states = {}


# ============================================================
# HIỂN THỊ: mọi tin nhắn đều nằm trong ô monospace
# ============================================================
def esc(s):
    return html.escape(str(s), quote=False)


def box(text):
    return f"<pre>{esc(text)}</pre>"


def answer(m, text):
    bot.reply_to(m, box(text), parse_mode="HTML")


def edit(text, cid, mid):
    bot.edit_message_text(box(text), cid, mid, parse_mode="HTML")


def say(cid, text, force=False):
    st = get_state(cid)
    if not (force or st["notify"]):
        return
    try:
        bot.send_message(cid, box(text), parse_mode="HTML")
    except Exception as e:
        log.warning("Không gửi được tin cho %s: %s", cid, e)


def kv(rows):
    """Các dòng 'nhãn  giá trị' thẳng cột."""
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


# ============================================================
# TIỆN ÍCH
# ============================================================
def norm(s):
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def parse_amount(s):
    s = s.lower().replace(",", "").strip()
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    value = int(s) * mult
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
            ordered = sorted(self.recs.items(), key=lambda kv_: kv_[0])[-MAX_HISTORY:]
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

    def weights(self, keys):
        losses = {key: self.loss.get(key, 0.0) for key in keys}
        m = min(losses.values())
        raw = {key: math.exp(-self.ETA * (losses[key] - m)) for key in keys}
        z = sum(raw.values())
        return {key: raw[key] / z for key in keys}

    MAX_SKIP_STREAK = 2
    STRATS = ("theo_cau", "dao_cau", "runlen", "markov1", "markov2", "markov3", "tan_suat", "tong_hop",
              "cong_thuc", "db_mau", "bang8")
    STRAT_NAMES = {"theo_cau": "Bệt", "dao_cau": "1-1", "runlen": "Nhịp theo độ dài",
                   "markov1": "Markov 1", "markov2": "Markov 2", "markov3": "Markov 3",
                   "tan_suat": "Tần suất", "tong_hop": "Tổng hợp", "cong_thuc": "Công thức xúc xắc",
                   "db_mau": "Mẫu cầu", "bang8": "Bảng 8 ván"}
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
        p_tai = sum(w[k] * ex[k][0] for k in keys)
        top = max(keys, key=lambda k: w[k])

        fast, slow = (3, self.HALF_LIVES[0]), (3, self.HALF_LIVES[1])
        n_eff = (ex[fast][1] + ex[slow][1]) / 2

        side = TAI if p_tai >= 0.5 else XIU
        p_side = max(p_tai, 1 - p_tai)
        a_, b_ = p_tai * n_eff + 1, (1 - p_tai) * n_eff + 1
        if side == TAI:
            post = beta_prob_above(a_, b_, x0=BREAK_EVEN)
        else:
            post = 1 - beta_prob_above(a_, b_, x0=1 - BREAK_EVEN)
        ev = p_side * WIN_PROFIT - (1 - p_side) * LOSE_LOSS
        strat = self.strategy_preds(seq, p_tai, self.dices()) if n else {}
        pick = self.pick_strategy() if n else None
        if pick and strat.get(pick[0]) is None:
            pick = None  # chiến lược đang dẫn không áp dụng được cho ván này

        out = {
            "side": None, "p": p_tai, "post": post, "n_eff": n_eff, "ev": ev,
            "reason": "", "agree": True, "p_tai": p_tai, "strat": strat,
            "ctx": f"{expert_name(top)} {w[top]:.0%}",
            "experts": {k: ex[k][0] for k in keys},
        }
        if pick:
            name, k, pn, _ = pick
            out["ctx"] = f"{self.STRAT_NAMES[name]} · đúng {k}/{pn}"
        else:
            out["ctx"] = "Chưa cách nào vượt ngẫu nhiên"

        if n < cfg["min_sessions"] and not never_skip:
            out["reason"] = f"Cần thêm dữ liệu, mới có {n}/{cfg['min_sessions']} phiên"
            return out

        if never_skip:
            self.skip_streak = 0
            out["side"] = strat[pick[0]] if pick else side
            out["reason"] = "Đánh theo cách đang dẫn đầu" if pick else "Đánh theo tổng hợp"
            return out

        need = cfg["post"] + self.extra_post
        block = None
        if in_blackout():
            out["reason"] = "Khung 0h-5h không áp dụng công thức"
            return out
        if pick is None:
            block = "Chưa cách nào vượt ngẫu nhiên"
        elif strat[pick[0]] != side:
            block = "Cách dẫn đầu và tổng hợp đang lệch"
        elif post < need:
            block = f"Chưa đủ chắc ({post:.0%}, cần {need:.0%})"
        elif ev <= 0:
            block = "Không có lợi về lâu dài"

        if block is None:
            self.skip_streak = 0
            out["side"] = side
            out["reason"] = f"Đủ chắc ({post:.0%}), có thể cược"
            return out

        if self.skip_streak >= self.MAX_SKIP_STREAK:
            self.skip_streak = 0
            out["side"] = side
            out["reason"] = f"Đã bỏ {self.MAX_SKIP_STREAK} phiên liên tiếp, cược theo phía nghiêng ({post:.0%})"
            return out

        self.skip_streak += 1
        self.skips += 1
        out["reason"] = f"{block}, bỏ {self.skip_streak}/{self.MAX_SKIP_STREAK} liên tiếp"
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
def new_state():
    return {
        "engine": CauEngine(), "running": False, "gen": 0,
        "auto": False, "martingale": False, "mart_level": 0,
        "base_bet": 10000, "mode": DEFAULT_MODE, "notify": True,
        "stop_loss": 0, "stop_win": 0, "daily_limit": 0,
        "day": datetime.now().strftime("%Y-%m-%d"), "day_bet": 0,
        "session_id": None, "pending": None, "last_result": None,
        "bet_log": deque(maxlen=300),
        "balance": 0, "profit": 0, "total_win": 0, "total_lose": 0,
        "win_streak": 0, "lose_streak": 0, "total_bet": 0, "nickname": "",
    }


def get_state(cid):
    if cid not in user_states:
        user_states[cid] = new_state()
    return user_states[cid]


def current_bet(st):
    if not st["martingale"] or st["mart_level"] <= 0:
        return st["base_bet"]
    lv = min(st["mart_level"], MAX_MARTINGALE_LEVEL)
    return st["base_bet"] * (MARTINGALE_MULTIPLIER ** lv)


def risk_block(st, amount):
    today = datetime.now().strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"], st["day_bet"] = today, 0
    if st["stop_loss"] and st["profit"] <= -st["stop_loss"]:
        return "Đã chạm mức dừng lỗ"
    if st["stop_win"] and st["profit"] >= st["stop_win"]:
        return "Đã chạm mức dừng lãi"
    if st["daily_limit"] and st["day_bet"] + amount > st["daily_limit"]:
        return "Vượt giới hạn cược ngày"
    if st["balance"] and amount > st["balance"]:
        return "Không đủ số dư"
    return None


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


def prediction_text(sid, info, st):
    mode = MODES[st["mode"]]
    recent = trail(st["engine"].results(), 10) or "—"
    eng = st["engine"]
    head = f"🎯 PHIÊN #{sid}  ·  {mode['label'].upper()}"
    lines = [head, RULE]
    if info["side"] is None:
        lines += ["⏸ BỎ QUA PHIÊN NÀY", f"Lý do   {info['reason']}", RULE]
    else:
        side = info["side"]
        p_side = info["p"] if side == TAI else 1 - info["p"]
        bet = current_bet(st)
        gauge = bar(info["post"], 10)
        lines.append(kv([
            ("ĐÁNH", f"{side_label(side)} {side_icon(side)}"),
            ("CÁCH", info["ctx"] if not info["ctx"].startswith("Chưa") else "Tổng hợp"),
            ("XÁC SUẤT", f"{p_side:.1%}"),
            ("ĐỘ CHẮC", f"{info['post']:.1%} {gauge}"),
            ("TÍN HIỆU", confidence_label(info["post"])),
            ("MẪU", f"{info['n_eff']:.0f} ván hiệu dụng"),
        ]))
        lines.append(RULE)
        x2 = f"gấp {st['mart_level']}/{MAX_MARTINGALE_LEVEL}" if st["martingale"] else "gốc"
        lines.append(kv([
            ("CƯỢC", f"{fmt(bet)} ({x2})"),
            ("THẮNG", f"{signed(bet * WIN_PROFIT)}  x{WIN_PROFIT:g}"),
            ("THUA", f"-{fmt(bet * LOSE_LOSS)}  x{LOSE_LOSS:g}"),
            ("KỲ VỌNG", signed(bet * info["ev"])),
        ]))
        lines.append(RULE)
    lr = st.get("last_result")
    if lr and lr.get("pred") is not None:
        verdict = "ĐÚNG ✅" if lr["pred_ok"] else "SAI ❌"
        extra = ""
        if lr["placed"]:
            extra = f" · cược {'THẮNG' if lr['won'] else 'THUA'} {signed(lr['delta'])}"
        lines.append(f"↩ #{lr['sid']}: đoán {side_label(lr['pred'])} · {verdict}")
        if extra:
            lines.append(f"  {extra.strip(' ·')}")
    k, n = eng.recent_hits()
    if n:
        lines.append(f"📊 Gần đây {k}/{n} đúng ({k / n:.0%})")
    lines.append(f"🧾 {recent}")
    return "\n".join(lines)


def result_text(rec, st):
    eng = st["engine"]
    total = f" · tổng {rec['total']}" if rec.get("total") is not None else ""
    lines = [f"🎲 KẾT QUẢ PHIÊN #{rec['sid']}",
             f"{dice_faces(rec['dice'])}{total} → {side_label(rec['res'])}", RULE]
    rows = []
    if rec["pred"] is None:
        rows.append(("DỰ ĐOÁN", "không có"))
    else:
        rows.append(("DỰ ĐOÁN", f"{side_label(rec['pred'])} {'✅ ĐÚNG' if rec['pred_ok'] else '❌ SAI'}"))
    if rec["placed"]:
        rows.append(("CƯỢC", f"{fmt(rec['bet'])} → {'THẮNG' if rec['won'] else 'THUA'} {signed(rec['delta'])}"))
    else:
        rows.append(("CƯỢC", "không đặt"))
    lines.append(kv(rows))
    lines.append(RULE)
    total_bets = st["total_win"] + st["total_lose"]
    wr = st["total_win"] / total_bets * 100 if total_bets else 0
    lines.append(kv([
        ("LÃI/LỖ", signed(st["profit"])),
        ("THẮNG LIÊN", str(st["win_streak"])),
        ("THUA LIÊN", str(st["lose_streak"])),
        ("TỶ LỆ THẮNG", f"{wr:.1f}% ({st['total_win']}/{total_bets})"),
        ("ĐOÁN ĐÚNG", f"{eng.accuracy():.1f}% ({len(eng.perf)} lần)"),
    ]))
    lines += [RULE, f"🧾 {trail(eng.results(), 10) or '—'}"]
    return "\n".join(lines)


def setup_text(st):
    return kv([
        ("cuoc", f"{fmt(st['base_bet'])}"),
        ("sl", f"{fmt(st['stop_loss'])}  (0 = tắt)"),
        ("sw", f"{fmt(st['stop_win'])}  (0 = tắt)"),
        ("limit", f"{fmt(st['daily_limit'])}  (0 = tắt)"),
        ("mode", f"{st['mode']}  (antoan / canbang / maohiem)"),
        ("thongbao", "bật" if st["notify"] else "tắt"),
    ])


def account_text(st):
    eng = st["engine"]
    total = st["total_win"] + st["total_lose"]
    wr = st["total_win"] / total * 100 if total else 0
    ll = eng.logloss()
    ll_txt = f"{ll:.3f}  (đoán ngẫu nhiên 0.693)" if ll is not None else "chưa đủ dữ liệu"
    x2 = f"bật, cấp {st['mart_level']}/{MAX_MARTINGALE_LEVEL}" if st["martingale"] else "tắt"
    p_face, p_tx = eng.bias_report()
    return "\n".join([
        f"👤 Tài khoản {st['nickname'] or '—'}",
        "",
        kv([
            ("Kết nối", "đang chạy" if st["running"] else "chưa đăng nhập"),
            ("Tự động", "đang bật" if st["auto"] else "đang tắt"),
            ("Gấp đôi", x2),
            ("Chế độ", MODES[st["mode"]]["label"]),
            ("Cược gốc", fmt(st["base_bet"])),
            ("Cược tới", fmt(current_bet(st))),
            ("Số dư", fmt(st["balance"])),
            ("Lãi/lỗ", signed(st["profit"])),
        ]),
        "",
        kv([
            ("Thắng/thua", f"{st['total_win']} / {st['total_lose']}  ({wr:.1f}%)"),
            ("Đoán đúng", f"{eng.accuracy():.1f}%  trên {len(eng.perf)} phiên"),
            ("Đánh giá", danh_gia(*eng.recent_hits())),
            ("ROI kỳ vọng", f"{eng.expected_roi() * 100:+.1f}% mỗi cược"),
            ("Sai số", ll_txt),
            ("Hòa vốn", f"{BREAK_EVEN:.1%}  (trả {WIN_PROFIT:g})"),
            ("Lệch xúc xắc", describe_p(p_face)),
            ("Lệch Tài/Xỉu", describe_p(p_tx)),
            ("Đã nạp", f"{len(eng.recs)} phiên, bỏ qua {eng.skips}"),
        ]),
        "",
        trail(eng.results(), 10) or "—",
    ])


def lichsu_text(rows, n):
    recent = rows[-n:]
    lines = [f"📜 Lịch sử {len(recent)} phiên gần nhất", ""]
    lines.append(f"{'Phiên':<9} {'KQ':<4} {'Đoán':<7} {'Cược':<8} Lãi")
    for r in reversed(recent):
        if r["pred"] is None:
            pred = "—"
        else:
            pred = ("✅" if r["pred_ok"] else "❌") + side_label(r["pred"])
        bet = "—" if not r["placed"] else fmt(r["bet"])
        pl = "—" if not r["placed"] else signed(r["delta"])
        lines.append(f"{str(r['sid']):<9} {side_label(r['res']):<4} {pred:<7} {bet:<8} {pl}")
    scored = [r for r in rows if r["pred"] is not None]
    hit = sum(1 for r in scored if r["pred_ok"])
    placed = [r for r in rows if r["placed"]]
    wins = sum(1 for r in placed if r["won"])
    profit = sum(r["delta"] for r in placed)
    pct = hit / len(scored) * 100 if scored else 0
    lines += ["", kv([
        ("Đoán đúng", f"{hit} / {len(scored)}  ({pct:.1f}%)"),
        ("Đã cược", f"{len(placed)} lần, thắng {wins}, thua {len(placed) - wins}"),
        ("Đánh giá", danh_gia(hit, len(scored))),
        ("Lãi/lỗ", signed(profit)),
    ])]
    return "\n".join(lines)


START_TEXT = """🎰 Auto Cược LC79
🎯 Dự đoán Tài/Xỉu, tự động cược

─── 🔐 Tài khoản ───
🔑 /dangnhap TK MK
   đăng nhập, bắt đầu theo dõi
⏹️ /dung
   ngắt kết nối

─── 🎲 Đặt cược ───
⚡ /tudong bat 10k
   bật tự động, 10k mỗi phiên
🛑 /tudong tat
   tắt tự động
💸 /x2 bat
   thua thì gấp đôi, tối đa 6 lần
↩️ /x2 tat
   tắt gấp đôi

─── ⚙️ Cài đặt ───
📋 /caidat
   xem cấu hình hiện tại
🔧 /caidat cuoc=10k sl=1m
   sw=5m limit=50m
   mode=maohiem thongbao=bat

─── 📊 Theo dõi ───
💰 /thongtin
   số dư, thắng thua, độ chính xác
📜 /lichsu 10
   10 phiên gần nhất
🧪 /kiemthu
   thử lại thuật toán trên lịch sử
📖 /start
   xem lại hướng dẫn này

─── 🧭 Chế độ ───
🟢 antoan   chắc từ 75%
🟡 canbang  chắc từ 55%
🔴 maohiem  đánh mọi phiên
⏭️ antoan, canbang không bỏ quá
   2 phiên liên tiếp
"""


# ============================================================
# NẠP PHIÊN + ĐĂNG NHẬP
# ============================================================
def parse_lines(data):
    if not data or not isinstance(data.get("list"), list):
        return []
    arr = sorted(data["list"], key=lambda x: x["id"])
    out = []
    for it in arr:
        dices = it.get("dices", [0, 0, 0])
        total = it.get("point", sum(dices))
        r = it.get("resultTruyenThong")
        if r in (TAI, XIU):
            out.append({"session": it["id"], "dice": dices, "total": total, "result": r})
    return out


def fetch_history_from_api(limit=MAX_HISTORY):
    for url in (HISTORY_API_URL, API_URL_FALLBACK):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://lc79b.bet"}, timeout=10)
            if r.status_code != 200:
                continue
            arr = parse_lines(r.json())
            if arr:
                return arr[-limit:]
        except Exception:
            continue
    return []


def fetch_latest_results(expect=None, retries=4):
    arr = []
    for _ in range(retries):
        arr = fetch_history_from_api(MAX_HISTORY)
        if arr and (expect is None or arr[-1]["result"] == expect):
            return arr
        time.sleep(1.5)
    return arr


def sync_loop(cid, gen):
    st = get_state(cid)
    while st["running"] and st["gen"] == gen:
        try:
            arr = fetch_history_from_api(MAX_HISTORY)
            if arr:
                with state_lock:
                    added = st["engine"].ingest(arr)
                if added:
                    log.info("[%s] nạp thêm %d phiên, tổng %d", cid, added, len(st["engine"].recs))
                poll_round(cid, arr)
        except Exception:
            log.exception("Lỗi nạp phiên ngầm cho %s", cid)
        for _ in range(SYNC_INTERVAL):
            if not (st["running"] and st["gen"] == gen):
                return
            time.sleep(1)


def md5_hash(t):
    return hashlib.md5(t.encode()).hexdigest()


def login_and_get_token(u, p):
    try:
        r = requests.get(f"https://apifo88daigia.tele68.com/api?c=3&un={u}&pw={md5_hash(p)}&cp=R&cl=R&pf=web&at=", timeout=12).json()
        if not r.get("success"):
            return {"_error": "Sai tài khoản hoặc mật khẩu"}
        sk = r["sessionKey"] + "=" * ((4 - len(r["sessionKey"]) % 4) % 4)
        nn = json.loads(base64.b64decode(sk)).get("nickname")
        r2 = requests.post("https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=",
                           json={"nickName": nn, "accessToken": r["accessToken"]}, timeout=12).json()
        if not r2.get("token"):
            return {"_error": "Không nhận được token"}
        return {"token": r2["token"], "nickname": nn, "money": r2.get("remoteLoginResp", {}).get("money", 0)}
    except Exception as e:
        return {"_error": str(e)}


# ============================================================
# CHỐT PHIÊN, CƯỢC, WEBSOCKET
# ============================================================
def open_round(cid, sid):
    """Mở một ván: dự đoán, gửi thẻ dự đoán và tạo trạng thái chờ cược."""
    st = get_state(cid)
    with state_lock:
        st["session_id"] = sid
        info = st["engine"].predict(st["mode"])
        p_side = info["p"] if info["side"] == TAI else 1 - info["p"]
        text = prediction_text(sid, info, st)
        pend = {"sid": sid, "pred": info["side"], "p_side": p_side, "p_tai": info["p_tai"],
                "experts": info["experts"], "strat": info.get("strat", {}), "placed": False, "sent": False, "bet": 0,
                "base": text, "status": [], "msg_id": None, "done": False}
        st["pending"] = pend
    try:
        sent = bot.send_message(cid, box(text), parse_mode="HTML")
        with state_lock:
            pend["msg_id"] = sent.message_id
    except Exception as e:
        log.warning("Không gửi được dự đoán cho %s: %s", cid, e)
    return pend


def poll_round(cid, arr):
    """Dự phòng khi websocket không gửi sự kiện phiên mới: theo dõi lịch sử API."""
    st = get_state(cid)
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
        settle_round(cid, last["result"], last["dice"], to_settle)
    open_round(cid, sid_last + 1)


def render_pending(pend):
    if not pend["status"]:
        return pend["base"]
    return pend["base"] + "\n\n── Trạng thái ──\n" + "\n".join(pend["status"])


def card_edit(cid, mid, text):
    try:
        bot.edit_message_text(box(text), cid, mid, parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e):
            log.warning("Không sửa được tin %s: %s", mid, e)


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


def settle_round(cid, res, ws_dices, pend):
    st = get_state(cid)
    arr = fetch_latest_results(expect=res)
    with state_lock:
        if arr:
            st["engine"].ingest(arr)
        matched = bool(arr) and arr[-1]["result"] == res
        dice = arr[-1]["dice"] if matched else (ws_dices or [])
        if pend is not None:
            pend["done"] = True
        mid = pend.get("msg_id") if pend else None
        sid = pend["sid"] if pend else st["session_id"]
        pred = pend["pred"] if pend else None
        pred_ok = None if pred is None else (pred == res)
        if pend is not None:
            st["engine"].learn(pend["experts"], pend["p_tai"], res)
            st["engine"].record_shadow(pend.get("strat", {}), res)
        if pred is not None:
            st["engine"].record(pred, pend["p_side"], res)
        placed = bool(pend and pend["placed"])
        bet = pend["bet"] if pend else 0
        won, delta = None, 0
        if placed:
            won = pred == res
            delta = round(bet * WIN_PROFIT) if won else -round(bet * LOSE_LOSS)
            st["profit"] += delta
            if won:
                st["total_win"] += 1
                st["win_streak"] += 1
                st["lose_streak"] = 0
                st["mart_level"] = 0
            else:
                st["total_lose"] += 1
                st["lose_streak"] += 1
                st["win_streak"] = 0
                if st["martingale"] and st["mart_level"] < MAX_MARTINGALE_LEVEL:
                    st["mart_level"] += 1
        rec = {"sid": sid if sid is not None else "?", "pred": pred, "res": res, "dice": dice,
               "total": sum(dice) if dice else None, "pred_ok": pred_ok, "placed": placed,
               "bet": bet, "won": won, "delta": delta}
        st["bet_log"].append(rec)
        st["last_result"] = rec
        text = result_text(rec, st)
    if mid:
        card_edit(cid, mid, text)
    else:
        say(cid, text, force=True)


def handle_ack(cid, data, pend):
    st = get_state(cid)
    if pend is None or pend.get("done"):
        return
    rejected = isinstance(data, dict) and (
        bool(data.get("error")) or data.get("success") is False
        or data.get("status") in (False, 0, "error", "fail", "failed"))
    if rejected:
        line = f"❌ Server từ chối: {short(data)}"
    elif data is None:
        line = "⏳ Chờ server xác nhận..."
    else:
        line = "✅ Server đã nhận cược"
        with state_lock:
            if not pend["placed"]:
                pend["placed"] = True
                st["day_bet"] += pend["bet"]
                st["total_bet"] += pend["bet"]
    pend_status(cid, pend, line)


def start_websocket(cid, token):
    st = get_state(cid)
    old = active_sockets.pop(cid, None)
    if old is not None:
        try:
            old.disconnect()
        except Exception:
            pass

    sio = socketio.Client(reconnection=True, reconnection_attempts=99999, reconnection_delay=3,
                          logger=False, engineio_logger=False)
    active_sockets[cid] = sio

    @sio.event(namespace="/txmd5")
    def connect():
        arr = fetch_history_from_api(MAX_HISTORY)
        if arr:
            with state_lock:
                st["engine"].ingest(arr)
            say(cid, f"Đã kết nối, đã nạp {len(st['engine'].recs)} phiên.\n"
                     f"Gõ /tudong bat 10k nếu muốn bot tự cược.", force=True)
        else:
            say(cid, "Đã kết nối nhưng chưa lấy được lịch sử. Bot sẽ thử lại ngầm.", force=True)

    @sio.on("new-session", namespace="/txmd5")
    def new_sess(d):
        st["ws_seen"] = time.time()
        open_round(cid, d.get("id"))

    @sio.on("tick-update", namespace="/txmd5")
    def tk(d):
        if str(d.get("state", "")).upper() != "BETTING":
            return
        block, amount, pred, pend = None, 0, None, None
        with state_lock:
            pend = st["pending"]
            if not st["auto"] or pend is None or pend["pred"] is None or pend["sent"]:
                return
            amount = current_bet(st)
            block = risk_block(st, amount)
            if block:
                st["auto"] = False
            elif st["engine"].worse_than_chance():
                block = "PAUSE"
            else:
                pend["sent"] = True
                pend["bet"] = amount
                pred = pend["pred"]
        if block == "PAUSE":
            if not pend.get("paused_notice"):
                pend["paused_notice"] = True
                pend_status(cid, pend, "⏸ Tạm dừng cược: mô hình đang kém ngẫu nhiên")
            return
        if block:
            pend_status(cid, pend, f"⛔ Tự tắt: {short(block, 24)}")
            return
        pend_status(cid, pend, f"🚀 Gửi {side_label(pred)} {fmt(amount)}")
        sio.emit("bet", {"type": pred, "amount": amount}, namespace="/txmd5",
                 callback=lambda *args, _p=pend: handle_ack(cid, args[0] if args else None, _p))

    @sio.on("bet-result", namespace="/txmd5")
    def br(d):
        confirmed = False
        with state_lock:
            pend = st["pending"]
            if "postBalance" in d:
                st["balance"] = d["postBalance"]
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

    @sio.on("session-result", namespace="/txmd5")
    def sr(d):
        res = d.get("resultTruyenThong")
        if res not in (TAI, XIU):
            return
        # Lấy đúng thẻ của ván vừa kết thúc tại thời điểm nhận kết quả, để ván mới không bị ghi đè.
        with state_lock:
            pend = st["pending"]
            st["pending"] = None
        threading.Thread(target=settle_round, args=(cid, res, d.get("dices"), pend), daemon=True).start()

    @sio.on("connect_error")
    def connect_error(data):
        say(cid, f"Không kết nối được websocket: {str(data)[:80]}", force=True)

    try:
        sio.connect(WS_URL, socketio_path="txmd5/", namespaces=["/txmd5"],
                    transports=["websocket"], auth={"token": token},
                    headers={"User-Agent": "Mozilla/5.0", "Origin": "https://lc79b.bet"})
        sio.wait()
    except Exception as e:
        say(cid, f"Mất kết nối WebSocket: {e}", force=True)


def stop_user(cid):
    st = get_state(cid)
    st["running"] = False
    st["gen"] += 1
    st["auto"] = False
    st["martingale"] = False
    st["mart_level"] = 0
    sio = active_sockets.pop(cid, None)
    if sio is not None:
        try:
            sio.disconnect()
        except Exception:
            pass


# ============================================================
# LỆNH
# ============================================================
@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    answer(m, START_TEXT)


@bot.message_handler(commands=["dangnhap"])
def cmd_dangnhap(m):
    cid = m.chat.id
    p = m.text.split()
    if len(p) != 3:
        return answer(m, "Nhập theo mẫu:\n/dangnhap TK MK")
    msg = bot.reply_to(m, box("Đang đăng nhập, chờ chút nhé..."), parse_mode="HTML")
    res = login_and_get_token(p[1], p[2])
    if "_error" in res:
        return edit(f"❌ Đăng nhập chưa được.\n{res['_error']}", cid, msg.message_id)

    st = get_state(cid)
    stop_user(cid)
    with state_lock:
        st["balance"] = res["money"]
        st["nickname"] = res["nickname"]
        st["profit"] = 0
        st["running"] = True
        st["login_at"] = time.time()
        st["gen"] += 1
        gen = st["gen"]
    edit("✅ Đăng nhập xong rồi.\n" + kv([
        ("Tài khoản", res["nickname"]),
        ("Số dư", fmt(res["money"])),
        ("Nạp phiên", f"mỗi {SYNC_INTERVAL} giây"),
    ]) + "\n\nBật tự động bằng /tudong bat 10k nếu muốn.", cid, msg.message_id)
    threading.Thread(target=start_websocket, args=(cid, res["token"]), daemon=True).start()
    threading.Thread(target=sync_loop, args=(cid, gen), daemon=True).start()


@bot.message_handler(commands=["dung"])
def cmd_dung(m):
    stop_user(m.chat.id)
    answer(m, "⏹️ Đã ngắt kết nối.\nBot ngừng nạp phiên, tắt tự động và gấp đôi.")


@bot.message_handler(commands=["tudong"])
def cmd_tudong(m):
    cid = m.chat.id
    st = get_state(cid)
    p = m.text.split()
    if not st["running"]:
        return answer(m, "🔒 Bạn chưa đăng nhập.\nGõ /dangnhap TK MK trước nhé.")
    action = norm(p[1]) if len(p) > 1 else ""
    if action not in ("bat", "tat"):
        return answer(m, "Gõ /tudong bat 10k hoặc /tudong tat.")
    if action == "tat":
        st["auto"] = False
        return answer(m, "🛑 Đã tắt tự động.")
    if len(p) > 2:
        try:
            st["base_bet"] = parse_amount(p[2])
        except ValueError:
            return answer(m, "⚠️ Số tiền chưa đúng.\nVí dụ: /tudong bat 10k")
    if st["base_bet"] <= 0:
        return answer(m, "Mức cược phải lớn hơn 0.")
    block = risk_block(st, current_bet(st))
    if block:
        return answer(m, f"⚠️ Chưa bật được: {block}.\nBạn chỉnh /caidat rồi thử lại.")
    st["auto"] = True
    st["mart_level"] = 0
    answer(m, "⚡ Đã bật tự động.\n" + kv([
        ("Mỗi phiên", fmt(st["base_bet"])),
        ("Chế độ", MODES[st["mode"]]["label"]),
    ]))


@bot.message_handler(commands=["x2"])
def cmd_x2(m):
    st = get_state(m.chat.id)
    p = m.text.split()
    action = norm(p[1]) if len(p) > 1 else ""
    if action not in ("bat", "tat"):
        status = f"bật, cấp {st['mart_level']}" if st["martingale"] else "tắt"
        return answer(m, "💸 Gấp đôi khi thua\n" + kv([
            ("Trạng thái", status),
            ("Cược gốc", fmt(st["base_bet"])),
            ("Tối đa", f"{MAX_MARTINGALE_LEVEL} lần"),
        ]) + "\n\nGõ /x2 bat hoặc /x2 tat.")
    st["martingale"] = action == "bat"
    st["mart_level"] = 0
    if st["martingale"]:
        answer(m, f"💸 Đã bật gấp đôi khi thua.\nThua thì nhân {MARTINGALE_MULTIPLIER}, thắng thì về mức gốc, "
                  f"tối đa {MAX_MARTINGALE_LEVEL} lần.")
    else:
        answer(m, "↩️ Đã tắt gấp đôi, về mức cược gốc.")


@bot.message_handler(commands=["caidat"])
def cmd_caidat(m):
    st = get_state(m.chat.id)
    kv_in = parse_kv(m.text)
    if not kv_in:
        return answer(m, "📋 Cài đặt hiện tại\n\n" + setup_text(st))

    updates = {}
    try:
        for k, v in kv_in.items():
            if k in ("cuoc", "bet"):
                amt = parse_amount(v)
                if amt <= 0:
                    raise ValueError("cuoc phải lớn hơn 0")
                updates["base_bet"] = amt
            elif k == "sl":
                updates["stop_loss"] = parse_amount(v)
            elif k == "sw":
                updates["stop_win"] = parse_amount(v)
            elif k == "limit":
                updates["daily_limit"] = parse_amount(v)
            elif k == "mode":
                key = norm(v)
                if key not in MODES:
                    raise ValueError("mode chỉ nhận antoan, canbang hoặc maohiem")
                updates["mode"] = key
            elif k in ("thongbao", "notify"):
                val = norm(v)
                if val not in ("bat", "tat"):
                    raise ValueError("thongbao chỉ nhận bat hoặc tat")
                updates["notify"] = val == "bat"
            else:
                raise ValueError(f"không có mục '{k}'")
    except ValueError as e:
        return answer(m, f"⚠️ Có chỗ chưa đúng: {e}.\nVí dụ: /caidat cuoc=10k mode=canbang")

    st.update(updates)
    answer(m, "✅ Đã lưu.\n\n" + setup_text(st))


@bot.message_handler(commands=["thongtin"])
def cmd_thongtin(m):
    answer(m, account_text(get_state(m.chat.id)))


def backtest(records, mode, base_bet):
    """Chạy lại đúng quy trình trực tuyến trên lịch sử: đoán, cược, rồi mới học kết quả."""
    eng = CauEngine()
    bets = wins = 0
    profit = 0.0
    for sid, res, total, dice in records:
        info = eng.predict(mode)
        if info["side"] is not None:
            bets += 1
            if info["side"] == res:
                wins += 1
                profit += base_bet * WIN_PROFIT
            else:
                profit -= base_bet * LOSE_LOSS
        eng.learn(info["experts"], info["p_tai"], res)
        eng.ingest([{"session": sid, "result": res, "total": total, "dice": dice}])
    return bets, wins, profit


@bot.message_handler(commands=["kiemthu"])
def cmd_kiemthu(m):
    st = get_state(m.chat.id)
    items = sorted(st["engine"].recs.items(), key=lambda kv_: kv_[0])
    records = [(sid, v[0], v[1], v[2]) for sid, v in items]
    if len(records) < 60:
        return answer(m, f"Cần ít nhất 60 phiên để thử.\nHiện có {len(records)} phiên.\n"
                         f"Gõ /dangnhap để nạp thêm lịch sử.")
    n = len(records)
    bet = st["base_bet"]

    def pnl(bets, wins, win_mult):
        return wins * bet * win_mult - (bets - wins) * bet * LOSE_LOSS

    rows_a, rows_b = [], []
    for key, label in (("antoan", "An toàn"), ("canbang", "Cân bằng"), ("maohiem", "Mạo hiểm")):
        bets, wins, _ = backtest(records, key, bet)
        wr = wins / bets * 100 if bets else 0
        rows_a.append((label, f"{bets} cược · đúng {wr:.1f}%"))
        rows_b.append((label, f"{signed(pnl(bets, wins, 1.98))} / {signed(pnl(bets, wins, 0.98))}"))
    tai_wins = sum(1 for r in records if r[1] == TAI)
    rows_a.append(("Luôn TÀI", f"{n} cược · đúng {tai_wins / n * 100:.1f}%"))
    rows_b.append(("Luôn TÀI", f"{signed(pnl(n, tai_wins, 1.98))} / {signed(pnl(n, tai_wins, 0.98))}"))

    lines = [f"🧪 Thử lại trên {n} phiên", f"Mức cược {fmt(bet)}", "", kv(rows_a), "",
             "Lãi/lỗ: trả x1.98 / trả x0.98", kv(rows_b), "",
             "Số sau dấu / là khi sàn trả 0.98.",
             "Đây là số liệu đã qua, không hứa",
             "hẹn kết quả tương lai."]
    answer(m, "\n".join(lines))


@bot.message_handler(commands=["lichsu"])
def cmd_lichsu(m):
    st = get_state(m.chat.id)
    p = m.text.split()
    n = int(p[1]) if len(p) > 1 and p[1].isdigit() else 10
    n = max(1, min(n, 40))
    rows = list(st["bet_log"])
    if not rows:
        return answer(m, "📭 Chưa có lịch sử.\nBạn đăng nhập rồi chờ ít nhất một phiên kết thúc nhé.")
    answer(m, lichsu_text(rows, n))


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    log.info("%s khởi động", BOT_NAME)
    bot.infinity_polling()