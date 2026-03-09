import asyncio
import csv
import io
import logging
import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8712692200:AAHoIKoV9v0cXV2alCesncNP0gaqDSUlUbA")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))
TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
MAX_IMAGES_PER_BATCH = int(os.getenv("MAX_IMAGES_PER_BATCH", "50"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    amount: Optional[int]
    confidence: float
    reason: str
    raw_text: str
    needs_confirmation: bool = False
    warning: str = ""


@dataclass
class PendingItem:
    """Một ảnh đang chờ xác nhận."""
    index: int          # số thứ tự hiển thị (1-based)
    amount: int         # số tiền phỏng đoán
    confidence: float
    confirmed: bool = False
    corrected: bool = False  # người dùng đã nhập tay


class ChatState:
    def __init__(self) -> None:
        self.amounts: List[int] = []                        # đã xác nhận xong
        self.pending_items: List[PendingItem] = []          # đang chờ xác nhận
        self.pending_manual: Dict[int, str] = {}            # message_id → file_id (ảnh không đọc được)
        self.pending_edit: Optional[int] = None             # index (1-based) đang chờ sửa
        # media group batching
        self.current_group_id: Optional[str] = None
        self.group_task: Optional[object] = None            # asyncio.Task


chat_states: Dict[int, ChatState] = {}

AMOUNT_PATTERN = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|K|đ|d|VND|vnd|vnđ|VNĐ)?")
KEYWORDS = ["giao", "dịch", "thành", "công", "chuyển", "khoản", "total", "amount"]
ACCOUNT_HINT_RE = re.compile(r"(tai\s*khoan|so\s*tk|stk|the/tk|so\s*the|ma\s*giao\s*dich|reference|ref\b|id\b|account|tk\s*nhan|so\s*tk)", re.IGNORECASE)


def get_state(chat_id: int) -> ChatState:
    if chat_id not in chat_states:
        chat_states[chat_id] = ChatState()
    return chat_states[chat_id]


def format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def build_summary_text(amounts: List[int]) -> str:
    """Tạo chuỗi danh sách số tiền + tổng."""
    lines = [f"{idx}. {format_vnd(v)}" for idx, v in enumerate(amounts, start=1)]
    total = sum(amounts)
    lines.append(f"\nTổng: *{format_vnd(total)}*")
    return "\n".join(lines)


async def send_summary(message: Message, state: "ChatState") -> None:
    """Gửi 1 tin nhắn tổng kết toàn bộ danh sách (đã xác nhận + đang chờ)."""
    lines: List[str] = []
    idx = 1

    # Các ảnh đã xác nhận
    for amt in state.amounts:
        lines.append(f"{idx}. {format_vnd(amt)} ✅")
        idx += 1

    # Các ảnh đang chờ xác nhận
    for item in state.pending_items:
        if item.confirmed:
            lines.append(f"{idx}. {format_vnd(item.amount)} ✅ *(đã xác nhận)*")
        else:
            lines.append(f"{idx}. {format_vnd(item.amount)} *(chờ xác nhận)*")
        item.index = idx
        idx += 1

    confirmed_total = sum(state.amounts)
    pending_total = sum(i.amount for i in state.pending_items)
    grand_total = confirmed_total + pending_total

    lines.append("")
    if state.pending_items:
        lines.append(f"💰 *Tổng: {format_vnd(grand_total)}*")
        lines.append(f"_({format_vnd(confirmed_total)} đã xác nhận + {format_vnd(pending_total)} chờ xác nhận)_")
    else:
        lines.append(f"💰 *Tổng: {format_vnd(confirmed_total)}*")

    await message.reply_text(
        "📋 *Danh sách hiện tại:*\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Xuất trang tính", callback_data="action_xuat"),
                InlineKeyboardButton("✏️ Sửa", callback_data="action_sua"),
                InlineKeyboardButton("🔄 Tính mới", callback_data="action_tinh_moi"),
            ]
        ]
    )


def parse_manual_amount(text: str) -> Optional[int]:
    candidates = AMOUNT_PATTERN.findall(text.lower())
    if not candidates:
        return None
    parsed = [normalize_amount(raw_num, suffix) for raw_num, suffix in candidates]
    parsed = [x for x in parsed if x is not None]
    return parsed[0] if parsed else None


def normalize_amount(raw_num: str, suffix: str) -> Optional[int]:
    suffix = (suffix or "").lower().strip()
    clean = raw_num.strip().lower()

    if suffix == "k":
        clean = clean.replace(",", ".")
        try:
            return int(float(clean) * 1000)
        except ValueError:
            return None

    if "." in clean and "," in clean:
        clean = clean.replace(".", "").replace(",", "")
    elif clean.count(".") >= 1 and len(clean.split(".")[-1]) == 3:
        clean = clean.replace(".", "")
    elif clean.count(",") >= 1 and len(clean.split(",")[-1]) == 3:
        clean = clean.replace(",", "")
    else:
        clean = clean.replace(",", ".")

    try:
        if "." in clean:
            val = float(clean)
            if val < 1000:
                return int(val * 1000)
            return int(val)
        return int(clean)
    except ValueError:
        return None


def normalize_text(text: str) -> str:
    lowered = text.lower()
    deaccent = "".join(
        ch for ch in unicodedata.normalize("NFD", lowered) if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"[^a-z0-9\s]", " ", deaccent)


def is_subsequence(pattern: str, text: str) -> bool:
    it = iter(text)
    return all(ch in it for ch in pattern)


def approx_word(target: str, word: str) -> bool:
    if word == target:
        return True
    if SequenceMatcher(None, word, target).ratio() >= 0.62:
        return True
    return is_subsequence(target, word) and len(word) >= max(3, len(target) - 2)


def detect_success_phrase(text: str) -> Dict[str, object]:
    # Support "THANH CONG", "Thanh Cong", with ! or . and OCR missing accents.
    norm = re.sub(r"\s+", " ", normalize_text(text)).strip()
    if not norm:
        return {"found": False, "exact": False}

    if re.search(r"\bthanh\s+cong[!.]?\b", norm):
        return {"found": True, "exact": True}

    words = [w for w in norm.split(" ") if w]
    thanh_positions = [i for i, w in enumerate(words) if approx_word("thanh", w)]
    cong_positions = [i for i, w in enumerate(words) if approx_word("cong", w)]

    for i in thanh_positions:
        for j in cong_positions:
            if 0 <= j - i <= 2:
                return {"found": True, "exact": False}

    merged = "".join(words)
    if is_subsequence("thanh", merged) and is_subsequence("cong", merged):
        return {"found": True, "exact": False}

    return {"found": False, "exact": False}


def detect_currency_suffix_signal(text: str) -> Dict[str, object]:
    if not text:
        return {"found": False, "exact": False}

    norm = re.sub(r"\s+", " ", normalize_text(text)).strip()
    if not norm:
        return {"found": False, "exact": False}

    if re.search(r"\b\d[\d.,]*\s*(vnd|d)\b", norm):
        return {"found": True, "exact": True}

    words = [w for w in norm.split(" ") if w]
    has_currency_token = any(w in {"vnd", "d"} for w in words)
    has_numeric_token = any(any(ch.isdigit() for ch in w) for w in words)
    if has_currency_token and has_numeric_token:
        return {"found": True, "exact": False}

    return {"found": False, "exact": False}


def run_tesseract_tsv(image_path: str, psm: int) -> List[Dict[str, str]]:
    langs = ["vie+eng", "eng"]
    last_err = ""
    for lang in langs:
        cmd = [
            TESSERACT_CMD,
            image_path,
            "stdout",
            "-l",
            lang,
            "--oem",
            "3",
            "--psm",
            str(psm),
            "-c",
            "preserve_interword_spaces=1",
            "tsv",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if proc.returncode == 0 and proc.stdout.strip():
            rows: List[Dict[str, str]] = []
            reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
            for row in reader:
                rows.append(row)
            if rows:
                return rows
        last_err = proc.stderr.strip() or "Tesseract failed"
    raise RuntimeError(last_err)


def _line_key(row: Dict[str, str]) -> Tuple[str, str, str]:
    return (
        row.get("block_num", ""),
        row.get("par_num", ""),
        row.get("line_num", ""),
    )


def extract_best_amount_from_rows(rows: List[Dict[str, str]]) -> ParseResult:
    candidates: List[Dict[str, object]] = []
    avg_conf_values: List[float] = []

    line_tokens: Dict[Tuple[str, str, str], List[str]] = {}
    line_order: List[Tuple[str, str, str]] = []

    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue

        key = _line_key(row)
        if key not in line_tokens:
            line_tokens[key] = []
            line_order.append(key)
        line_tokens[key].append(text)

        conf_raw = (row.get("conf") or "-1").strip()
        try:
            conf = max(0.0, min(1.0, float(conf_raw) / 100.0))
        except ValueError:
            conf = 0.0
        if conf > 0:
            avg_conf_values.append(conf)

    line_texts: Dict[Tuple[str, str, str], str] = {k: " ".join(v) for k, v in line_tokens.items()}
    line_index: Dict[Tuple[str, str, str], int] = {k: i for i, k in enumerate(line_order)}

    success_lines = [k for k, t in line_texts.items() if detect_success_phrase(t)["found"]]
    success_idx = [line_index[k] for k in success_lines]

    all_text = " ".join(line_texts.values())
    full_success = detect_success_phrase(all_text)

    for row in rows:
        token_text = (row.get("text") or "").strip()
        if not token_text:
            continue

        key = _line_key(row)
        line_text = line_texts.get(key, token_text)
        idx = line_index.get(key, 999)

        conf_raw = (row.get("conf") or "-1").strip()
        try:
            conf = max(0.0, min(1.0, float(conf_raw) / 100.0))
        except ValueError:
            conf = 0.0

        matches = AMOUNT_PATTERN.findall(token_text)
        if not matches:
            continue

        for raw_num, suffix in matches:
            amount = normalize_amount(raw_num, suffix)
            if amount is None or amount <= 0:
                continue

            currency_line = detect_currency_suffix_signal(line_text)
            success_line = detect_success_phrase(line_text)

            has_currency = bool(suffix) or bool(currency_line["found"])
            grouped_000 = (("." in raw_num) or ("," in raw_num)) and (amount % 1000 == 0)
            digit_count_ok = 4 <= len(str(amount)) <= 7
            amount_like = has_currency or grouped_000 or digit_count_ok

            digits_raw = re.sub(r"\D", "", raw_num)
            account_like = bool(ACCOUNT_HINT_RE.search(normalize_text(line_text)))
            # Bank account numbers are typically ≥9 digits; transaction IDs ≥8 digits without currency
            if len(digits_raw) >= 9:
                account_like = True
            elif len(digits_raw) >= 8 and not has_currency and not grouped_000:
                account_like = True

            distance_to_success = min((abs(idx - s) for s in success_idx), default=99)
            near_success = distance_to_success <= 2 or bool(success_line["found"] or full_success["found"])

            candidates.append(
                {
                    "amount": amount,
                    "raw_num": raw_num,
                    "suffix": suffix,
                    "conf": conf,
                    "line_text": line_text,
                    "has_currency": has_currency,
                    "currency_exact": bool(suffix) or bool(currency_line["exact"]),
                    "grouped_000": grouped_000,
                    "digit_count_ok": digit_count_ok,
                    "success_found": bool(success_line["found"] or full_success["found"]),
                    "success_exact": bool(success_line["exact"] or full_success["exact"]),
                    "near_success": near_success,
                    "distance_to_success": distance_to_success,
                    "account_like": account_like,
                    "amount_like": amount_like,
                }
            )

    if not candidates:
        return ParseResult(None, 0.0, "Khong nhan dien duoc so tien tu OCR", "")

    # Filter before applying priority to avoid picking account numbers as "largest".
    filtered = [
        c
        for c in candidates
        if c["amount_like"]
        and not c["account_like"]
        and 1000 <= int(c["amount"]) <= 5_000_000
    ]
    pool = filtered if filtered else candidates

    # Priority: prefer candidates with currency suffix + grouped format, near "thanh cong",
    # then within equally-scored candidates pick the largest amount.
    center = max(
        pool,
        key=lambda c: (
            # Tier 1: strong signals (currency suffix + grouped thousands format)
            (1 if c["has_currency"] else 0) + (1 if c["grouped_000"] else 0),
            # Tier 2: proximity to "thanh cong"
            1 if c["near_success"] else 0,
            # Tier 3: success phrase on same line
            1 if c["success_found"] else 0,
            # Tier 4: digit count in expected range
            1 if c["digit_count_ok"] else 0,
            # Tier 5: amount value (largest within same tier)
            int(c["amount"]),
            # Tier 6: OCR confidence
            float(c["conf"]),
        ),
    )

    avg_conf = sum(avg_conf_values) / max(1, len(avg_conf_values))

    signal_count = sum(
        [
            1 if center["has_currency"] else 0,
            1 if center["grouped_000"] else 0,
            1 if center["success_found"] else 0,
            1 if center["digit_count_ok"] else 0,
            1 if center["near_success"] else 0,
        ]
    )

    confidence = (
        0.35 * float(center["conf"])          # OCR token confidence (ảnh mờ/nghiêng thường thấp)
        + 0.10 * avg_conf                      # confidence trung bình toàn ảnh
        + (0.15 if center["currency_exact"] else (0.08 if center["has_currency"] else 0.0))
        + (0.15 if center["grouped_000"] else 0.0)
        + (0.12 if center["success_exact"] else (0.08 if center["success_found"] else 0.0))
        + (0.08 if center["digit_count_ok"] else 0.0)
        + (0.05 if center["near_success"] else 0.0)
    )

    if center["account_like"]:
        confidence -= 0.35
    if not center["near_success"] and not center["success_found"]:
        confidence -= 0.10

    final_conf = max(0.0, min(1.0, confidence))
    amount_value = int(center["amount"])

    warn_parts: List[str] = []
    if center["success_found"] and not center["success_exact"]:
        warn_parts.append("'thanh cong' chi nhan dien gan dung")
    if center["has_currency"] and not center["currency_exact"]:
        warn_parts.append("duoi tien te chi nhan dien gan dung")
    if signal_count <= 2:
        warn_parts.append("it tin hieu xac nhan")

    if final_conf < CONFIDENCE_THRESHOLD:
        return ParseResult(
            amount_value,
            final_conf,
            f"Do tin cay thap ({final_conf:.2%} < {CONFIDENCE_THRESHOLD:.0%})",
            str(center["line_text"]),
            needs_confirmation=True,
            warning="; ".join(warn_parts),
        )

    if warn_parts:
        return ParseResult(
            amount_value,
            final_conf,
            "OK",
            str(center["line_text"]),
            needs_confirmation=True,
            warning="Can xac nhan lai: " + "; ".join(warn_parts),
        )

    return ParseResult(amount_value, final_conf, "OK", str(center["line_text"]))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *Bot nhận diện chuyển khoản đã sẵn sàng!*\n\n"
        "📷 Gửi từ 1–50 ảnh biên lai/chuyển khoản\n"
        "→ Bot sẽ trích số tiền từng ảnh và cộng tổng\n\n"
        "📊 Nhấn *Yêu cầu tính tổng* hoặc gõ /tong để xem danh sách\n"
        "✏️ Nhấn *Sửa* hoặc gõ /sua để sửa số tiền nhận dạng sai\n"
        "🔄 Gõ /reset để xóa phiên hiện tại",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state.amounts.clear()
    state.pending_items.clear()
    state.pending_manual.clear()
    state.pending_edit = None
    context.chat_data.clear()
    await update.message.reply_text("✅ Đã reset phiên hiện tại.")


async def show_sum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        message = update.callback_query.message
        chat_id = update.callback_query.message.chat_id
        await update.callback_query.answer()
    else:
        message = update.message
        chat_id = update.effective_chat.id

    state = get_state(chat_id)
    if not state.amounts and not state.pending_items:
        await message.reply_text("⚠️ Chưa có số tiền nào trong phiên hiện tại.")
        return
    await send_summary(message, state)


async def cmd_sua(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        message = update.callback_query.message
        chat_id = update.callback_query.message.chat_id
        await update.callback_query.answer()
    else:
        message = update.message
        chat_id = update.effective_chat.id

    state = get_state(chat_id)
    total_items = len(state.amounts) + len(state.pending_items)
    if total_items == 0:
        await message.reply_text("⚠️ Chưa có số tiền nào để sửa.")
        return

    await send_summary(message, state)
    await message.reply_text(
        "✏️ Nhập *số thứ tự* và *số tiền* muốn sửa\n"
        "Ví dụ: `2 99k` hoặc `2 99000`",
        parse_mode=ParseMode.MARKDOWN,
    )
    state.pending_edit = -1


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if data == "action_xuat":
        await query.answer()
        await query.message.reply_text(
            "📊 Tính năng xuất trang tính đang được phát triển, vui lòng chờ cập nhật sau."
        )
    elif data == "action_sua":
        await cmd_sua(update, context)
    elif data == "action_tinh_moi":
        await cmd_tinh_moi(update, context)


async def cmd_tinh_moi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        message = update.callback_query.message
        chat_id = update.callback_query.message.chat_id
        await update.callback_query.answer()
    else:
        message = update.message
        chat_id = update.effective_chat.id

    state = get_state(chat_id)
    state.amounts.clear()
    state.pending_items.clear()
    state.pending_manual.clear()
    state.pending_edit = None
    context.chat_data.clear()
    await message.reply_text(
        "🔄 Đã xóa dữ liệu cũ. Hãy gửi ảnh mới để bắt đầu tính tổng mới!",
    )
    query = update.callback_query
    data = query.data

    if data == "action_xuat":
        await query.answer()
        await query.message.reply_text(
            "📊 Tính năng xuất trang tính đang được phát triển, vui lòng chờ cập nhật sau."
        )
    elif data == "action_sua":
        await cmd_sua(update, context)
    elif data == "action_tinh_moi":
        await cmd_tinh_moi(update, context)


async def maybe_capture_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not message.text:
        return False

    state = get_state(update.effective_chat.id)
    text = message.text.strip()

    # ── Trigger tự nhiên: nhập "yêu cầu tính tổng" ───────────────────────────
    norm_text = normalize_text(text)
    if re.search(r"yeu\s*cau\s*tinh\s*tong|tinh\s*tong|xem\s*tong", norm_text):
        if state.amounts or state.pending_items:
            await send_summary(message, state)
        else:
            await message.reply_text("⚠️ Chưa có số tiền nào trong phiên hiện tại.")
        return True

    # ── Flow sửa: đang chờ "số_thứ_tự số_tiền" trong 1 tin nhắn ───────────────
    if state.pending_edit == -1:
        total_items = len(state.amounts) + len(state.pending_items)
        # Parse định dạng: "2 99k" hoặc "2 99000"
        m = re.match(r"^\s*(\d+)\s+(.+)$", text)
        if m:
            try:
                idx = int(m.group(1))
                amount = parse_manual_amount(m.group(2))
                if amount is None:
                    raise ValueError
                if not (1 <= idx <= total_items):
                    await message.reply_text(
                        f"⚠️ Số thứ tự không hợp lệ. Vui lòng nhập từ 1 đến {total_items}.\n"
                        "Ví dụ: `2 99k`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return True
                # Thực hiện sửa
                if idx <= len(state.amounts):
                    old_val = state.amounts[idx - 1]
                    state.amounts[idx - 1] = amount
                else:
                    pi = state.pending_items[idx - len(state.amounts) - 1]
                    old_val = pi.amount
                    pi.amount = amount
                    pi.confirmed = True  # đánh dấu đã được người dùng xác nhận
                state.pending_edit = None
                await message.reply_text(
                    f"✅ Đã sửa mục *{idx}*: {format_vnd(old_val)} → *{format_vnd(amount)}*",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await send_summary(message, state)
            except (ValueError, IndexError):
                await message.reply_text(
                    "⚠️ Định dạng không hợp lệ.\nNhập *số thứ tự* và *số tiền*, ví dụ: `2 99k`",
                    parse_mode=ParseMode.MARKDOWN,
                )
        else:
            await message.reply_text(
                "⚠️ Định dạng không hợp lệ.\nNhập *số thứ tự* và *số tiền*, ví dụ: `2 99k`",
                parse_mode=ParseMode.MARKDOWN,
            )
        return True

    # ── Flow sửa: đang chờ số tiền mới (2 bước cũ — giữ lại phòng trường hợp) ─
    if state.pending_edit is not None and state.pending_edit > 0:
        amount = parse_manual_amount(text)
        if amount is not None:
            idx = state.pending_edit
            if idx <= len(state.amounts):
                # Sửa mục đã confirmed
                old_val = state.amounts[idx - 1]
                state.amounts[idx - 1] = amount
            else:
                # Sửa mục pending → chuyển thành confirmed
                pi = state.pending_items[idx - len(state.amounts) - 1]
                old_val = pi.amount
                state.pending_items.remove(pi)
                state.amounts.insert(idx - 1, amount)
            state.pending_edit = None
            await message.reply_text(
                f"✅ Đã sửa mục *{idx}*: {format_vnd(old_val)} → *{format_vnd(amount)}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            await send_summary(message, state)
        else:
            await message.reply_text(
                "⚠️ Không nhận dạng được số tiền. Nhập lại (ví dụ: `110000` / `110k`):",
                parse_mode=ParseMode.MARKDOWN,
            )
        return True

    # ── Xác nhận hàng loạt pending_items: nhập "ok" / "all" / "tất cả" ────────
    if state.pending_items and re.search(r"\bok\b|all|tat\s*ca|xac\s*nhan\s*tat", norm_text):
        confirmed = [i.amount for i in state.pending_items]
        state.amounts.extend(confirmed)
        state.pending_items.clear()
        await message.reply_text(
            f"✅ Đã xác nhận tất cả {len(confirmed)} mục.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_summary(message, state)
        return True

    # ── Xác nhận từng mục: nhập số thứ tự (vd: "2") ──────────────────────────
    if state.pending_items:
        try:
            idx = int(text)
            confirmed_count = len(state.amounts)
            pending_idx = idx - confirmed_count - 1  # 0-based trong pending_items
            if 0 <= pending_idx < len(state.pending_items):
                item = state.pending_items.pop(pending_idx)
                state.amounts.append(item.amount)
                await message.reply_text(
                    f"✅ Đã xác nhận mục *{idx}*: *{format_vnd(item.amount)}*",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await send_summary(message, state)
                return True
        except ValueError:
            pass

    # ── Nhập tay số tiền cho mục pending đầu tiên ────────────────────────────
    if state.pending_items:
        amount = parse_manual_amount(text)
        if amount is not None:
            item = state.pending_items.pop(0)
            state.amounts.append(amount)
            await message.reply_text(
                f"✅ Đã nhận nhập tay: *{format_vnd(amount)}* (thay cho phỏng đoán {format_vnd(item.amount)})",
                parse_mode=ParseMode.MARKDOWN,
            )
            await send_summary(message, state)
            return True

    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await maybe_capture_manual(update, context):
        return

    await update.message.reply_text(
        "📷 Hãy gửi ảnh giao dịch.\n"
        "Nếu vừa được yêu cầu xác nhận, nhấn `1` để đồng ý hoặc nhập lại số tiền (vd: 90000 / 90k).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(),
    )


async def ocr_photo(image_bytes: bytearray) -> ParseResult:
    """Chạy OCR trên ảnh và trả về ParseResult tốt nhất."""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        best = ParseResult(None, 0.0, "Khong OCR duoc", "")
        for psm in [6, 11, 7]:
            rows = run_tesseract_tsv(tmp_path, psm)
            candidate = extract_best_amount_from_rows(rows)
            if candidate.confidence > best.confidence:
                best = candidate
        return best
    except Exception as ex:
        logger.exception("OCR error: %s", ex)
        return ParseResult(None, 0.0, f"OCR loi: {ex}", "")
    finally:
        try:
            if tmp_path:
                os.remove(tmp_path)
        except Exception:
            pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.photo:
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    group_id = message.media_group_id  # None nếu ảnh gửi đơn lẻ

    # Tải ảnh ngay
    photo = message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = await tg_file.download_as_bytearray()
    result = await ocr_photo(image_bytes)

    # Lưu kết quả vào buffer của group (hoặc xử lý ngay nếu ảnh đơn)
    if "photo_buffer" not in context.chat_data:
        context.chat_data["photo_buffer"] = {}

    buf_key = group_id or f"single_{message.message_id}"
    if buf_key not in context.chat_data["photo_buffer"]:
        context.chat_data["photo_buffer"][buf_key] = []
    context.chat_data["photo_buffer"][buf_key].append((message, result))

    # Nếu đang có task chờ cho group này thì huỷ để reset timer
    task_key = f"task_{buf_key}"
    existing_task = context.chat_data.get(task_key)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    # Delay nhỏ để gom đủ ảnh trong album (Telegram gửi từng ảnh riêng lẻ)
    delay = 1.5 if group_id else 0.0

    async def flush_group():
        if delay > 0:
            await asyncio.sleep(delay)

        items = context.chat_data["photo_buffer"].pop(buf_key, [])
        context.chat_data.pop(task_key, None)
        if not items:
            return

        # Phân loại kết quả — giữ lại (msg, res, file_id) cho pending
        confirmed_this_batch: List[int] = []
        pending_this_batch: List[tuple] = []   # (PendingItem, file_id)
        failed_this_batch: List[tuple] = []

        for msg, res in items:
            photo_file_id = msg.photo[-1].file_id
            if res.amount is not None and res.confidence >= CONFIDENCE_THRESHOLD:
                confirmed_this_batch.append(res.amount)
            elif res.amount is not None:
                item = PendingItem(
                    index=0,  # sẽ gán lại trong send_summary
                    amount=res.amount,
                    confidence=res.confidence,
                )
                pending_this_batch.append((item, photo_file_id))
            else:
                failed_this_batch.append((msg, res))

        # Thêm vào state
        state.amounts.extend(confirmed_this_batch)
        for item, _ in pending_this_batch:
            state.pending_items.append(item)

        # Dùng tin nhắn cuối cùng trong batch để reply
        last_msg = items[-1][0]

        # Báo ảnh không đọc được (nếu có)
        for fail_msg, fail_res in failed_this_batch:
            await fail_msg.reply_text(
                f"❌ Không nhận diện được số tiền trong ảnh này.\nLý do: {fail_res.reason}\n"
                "Vui lòng nhập tay số tiền (vd: 90000 / 90k).",
            )

        # Gửi 1 tin nhắn tổng kết duy nhất
        await send_summary(last_msg, state)

        # Gửi từng ảnh phỏng đoán kèm yêu cầu xác nhận
        # (sau summary để người dùng thấy tổng trước, rồi mới thấy từng ảnh cần xác nhận)
        for item, file_id in pending_this_batch:
            # Tìm lại index thực tế đã gán trong send_summary
            display_idx = item.index
            await last_msg.reply_photo(
                photo=file_id,
                caption=(
                    f"⚠️ *Mục {display_idx}* — Chưa chắc chắn\n"
                    f"Phỏng đoán: *{format_vnd(item.amount)}* (độ tin cậy {item.confidence:.0%})"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )

    task = asyncio.ensure_future(flush_group())
    context.chat_data[task_key] = task


def validate_env() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Thieu BOT_TOKEN trong .env")
    if not os.path.exists(TESSERACT_CMD):
        raise RuntimeError(f"Khong tim thay Tesseract: {TESSERACT_CMD}")


def main() -> None:
    validate_env()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(["sum", "tong"], show_sum))
    app.add_handler(CommandHandler(["sua", "edit"], cmd_sua))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()