import os, sys, time, json, random, string, hmac, hashlib, sqlite3, asyncio, logging
import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup,
    KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
BINANCE_PAY_API_KEY = os.getenv("BINANCE_PAY_API_KEY", "").strip()
BINANCE_PAY_SECRET_KEY = os.getenv("BINANCE_PAY_SECRET_KEY", "").strip()
BINANCE_PAY_BASE_URL = os.getenv("BINANCE_PAY_BASE_URL", "https://bapi.binance.com").rstrip("/")
CURRENCY = os.getenv("CURRENCY", "USDT")
PAYMENT_POLL_INTERVAL = int(os.getenv("PAYMENT_POLL_INTERVAL", "20"))
DB_PATH = os.getenv("DB_PATH", "cloud_shop.sqlite")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", stream=sys.stdout)
logger = logging.getLogger("CloudShopBot")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance REAL DEFAULT 0.0, total_spent REAL DEFAULT 0.0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, icon TEXT DEFAULT '☁️')")
    c.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '', price REAL NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS stock (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, account_data TEXT NOT NULL, is_sold INTEGER DEFAULT 0, sold_to INTEGER, sold_at TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS invoices (merchant_trade_no TEXT PRIMARY KEY, prepay_id TEXT, user_id INTEGER NOT NULL, amount REAL NOT NULL, currency TEXT DEFAULT 'USDT', status TEXT DEFAULT 'PENDING', invoice_type TEXT DEFAULT 'TOPUP', product_id INTEGER, checkout_url TEXT, qrcode_url TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL, amount REAL NOT NULL, delivered_data TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

def get_or_create_user(user_id: int, username: str = "", first_name: str = ""):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user_id, username or "", first_name or ""))
        conn.commit()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
    res = dict(row)
    conn.close()
    return res

def update_user_balance(user_id: int, delta: float) -> float:
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
    conn.commit()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    new_bal = c.fetchone()["balance"]
    conn.close()
    return new_bal

def get_categories():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM categories ORDER BY id ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_products_by_cat(cat_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT p.*, COUNT(s.id) as stock_count FROM products p LEFT JOIN stock s ON p.id = s.product_id AND s.is_sold = 0 WHERE p.category_id = ? GROUP BY p.id ORDER BY p.id ASC", (cat_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_product(prod_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT p.*, COUNT(s.id) as stock_count FROM products p LEFT JOIN stock s ON p.id = s.product_id AND s.is_sold = 0 WHERE p.id = ? GROUP BY p.id", (prod_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_goods_summary():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM categories ORDER BY id ASC")
    cats = [dict(r) for r in c.fetchall()]
    res = []
    for cat in cats:
        c.execute("SELECT p.id, p.name, p.price, COUNT(s.id) as stock_qty FROM products p LEFT JOIN stock s ON p.id = s.product_id AND s.is_sold = 0 WHERE p.category_id = ? GROUP BY p.id ORDER BY p.id ASC", (cat["id"],))
        res.append({"category": cat, "products": [dict(p) for p in c.fetchall()]})
    conn.close()
    return res

def dispense_stock_item(product_id: int, user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("BEGIN IMMEDIATE")
    c.execute("SELECT id, account_data FROM stock WHERE product_id = ? AND is_sold = 0 LIMIT 1", (product_id,))
    item = c.fetchone()
    if not item:
        conn.rollback()
        conn.close()
        return None
    item_id, data = item["id"], item["account_data"]
    c.execute("UPDATE stock SET is_sold = 1, sold_to = ?, sold_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id, item_id))
    c.execute("SELECT price FROM products WHERE id = ?", (product_id,))
    price = c.fetchone()["price"]
    c.execute("INSERT INTO orders (user_id, product_id, amount, delivered_data) VALUES (?, ?, ?, ?)", (user_id, product_id, price, data))
    c.execute("UPDATE users SET total_spent = total_spent + ? WHERE user_id = ?", (price, user_id))
    conn.commit()
    conn.close()
    return {"id": item_id, "account_data": data}

def add_stock(prod_id: int, lines):
    conn = get_db()
    c = conn.cursor()
    items = [l.strip() for l in lines if l.strip()]
    for item in items:
        c.execute("INSERT INTO stock (product_id, account_data, is_sold) VALUES (?, ?, 0)", (prod_id, item))
    conn.commit()
    conn.close()
    return len(items)

def create_db_invoice(trade_no, prepay_id, user_id, amount, inv_type="TOPUP", prod_id=None, checkout_url="", qr_url=""):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO invoices (merchant_trade_no, prepay_id, user_id, amount, currency, status, invoice_type, product_id, checkout_url, qrcode_url) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)", (trade_no, prepay_id, user_id, amount, CURRENCY, inv_type, prod_id, checkout_url, qr_url))
    conn.commit()
    conn.close()

def get_db_invoice(trade_no):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM invoices WHERE merchant_trade_no = ?", (trade_no,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_pending_invoices():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM invoices WHERE status = 'PENDING'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def set_invoice_status(trade_no, status):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE invoices SET status = ? WHERE merchant_trade_no = ?", (status, trade_no))
    conn.commit()
    conn.close()

class BinancePay:
    @staticmethod
    def is_configured() -> bool:
        return bool(BINANCE_PAY_API_KEY and BINANCE_PAY_SECRET_KEY)

    @staticmethod
    def _sig(ts: int, nonce: str, body: str) -> str:
        payload = f"{ts}\n{nonce}\n{body}\n"
        return hmac.new(BINANCE_PAY_SECRET_KEY.encode(), payload.encode(), hashlib.sha512).hexdigest().upper()

    @classmethod
    async def create_order(cls, trade_no: str, amount: float, title: str):
        if not cls.is_configured():
            return {
                "success": True,
                "is_simulation": True,
                "merchantTradeNo": trade_no,
                "prepayId": f"SIM_{trade_no}",
                "checkoutUrl": f"https://pay.binance.com/checkout/demo_{trade_no}",
                "qrcodeLink": f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=demo_{trade_no}"
            }
        url = f"{BINANCE_PAY_BASE_URL}/binancepay/openapi/v2/order"
        body_str = json.dumps({
            "env": {"terminalType": "WEB"},
            "merchantTradeNo": trade_no,
            "orderAmount": f"{amount:.2f}",
            "currency": CURRENCY,
            "goods": {"goodsType": "02", "goodsCategory": "6000", "referenceGoodsId": "CLOUD_ACC", "goodsName": title[:250], "goodsDetail": "Cloud panel"}
        })
        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
        headers = {"Content-Type": "application/json", "BinancePay-Timestamp": str(ts), "BinancePay-Nonce": nonce, "BinancePay-Certificate-SN": BINANCE_PAY_API_KEY, "BinancePay-Signature": cls._sig(ts, nonce, body_str)}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, data=body_str) as r:
                data = await r.json()
                if r.status == 200 and data.get("status") == "SUCCESS":
                    d = data.get("data", {})
                    return {"success": True, "merchantTradeNo": trade_no, "prepayId": d.get("prepayId"), "checkoutUrl": d.get("checkoutUrl"), "qrcodeLink": d.get("qrcodeLink")}
                return {"success": False, "error": data.get("errorMessage") or str(data)}

    @classmethod
    async def query_order(cls, trade_no: str):
        if not cls.is_configured():
            return {"success": True, "status": "PAID", "merchantTradeNo": trade_no}
        url = f"{BINANCE_PAY_BASE_URL}/binancepay/openapi/v2/order/query"
        body_str = json.dumps({"merchantTradeNo": trade_no})
        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
        headers = {"Content-Type": "application/json", "BinancePay-Timestamp": str(ts), "BinancePay-Nonce": nonce, "BinancePay-Certificate-SN": BINANCE_PAY_API_KEY, "BinancePay-Signature": cls._sig(ts, nonce, body_str)}
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers, data=body_str) as r:
                data = await r.json()
                if r.status == 200 and data.get("status") == "SUCCESS":
                    return {"success": True, "status": data.get("data", {}).get("status")}
                return {"success": False, "error": data.get("errorMessage")}

def kb_main(is_admin=False):
    rows = [
        [KeyboardButton(text="🍱 Availability Of Goods")],
        [KeyboardButton(text="🎁 Buy"), KeyboardButton(text="👤 Profile"), KeyboardButton(text="💰 Top Up")]
    ]
    rows.append([KeyboardButton(text="⚙️ Panel Admin" if is_admin else "⚙️ General Functions")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def kb_invoice(url, trade_no):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Pagar en Binance Pay", url=url)],
        [InlineKeyboardButton(text="🔄 Verificar Pago Ahora", callback_data=f"verify_{trade_no}")],
        [InlineKeyboardButton(text="❌ Cancelar Orden", callback_data=f"cancel_{trade_no}")]
    ])

def seed_catalog():
    if get_categories():
        return
    conn = get_db()
    c = conn.cursor()
    sample_data = [
        ("Amazon (AWS)", "🟧", [("300vCpu Ohio+Canada+Singapre+Germany | 2FA + API Keys", 80.0, 1), ("300vCpu Ohio With 50 RPM Ai Kiro", 230.0, 1), ("512vCpu Ohio | 10KRPM [Error 002]", 200.0, 1)]),
        ("Amazon (Aws) OLD", "☁️", [("Aws500v FrankFurt ( 1/2 Month Old) | 10RPM Ai", 150.0, 1)]),
        ("Digital Ocean", "🌀", [("3Drop | Card Made | 2fa Enable", 3.0, 21), ("10Drop | Card Made | 2fa Enable", 4.0, 19)]),
        ("Azure", "🅰️", [("FT200$ | Outlook", 12.5, 7), ("Pay As(Upgrade) | Domain", 22.0, 7)]),
        ("Vultr", "🔷", [("Vultr 200$ | 250$ | 300$", 6.0, 10), ("Vultr 205$ | 255$ | 305$", 9.0, 2)]),
        ("OVH", "☑️", [("Ovh 34vCpu", 8.5, 2)]),
        ("Oracle FT 300$", "⭕", [("Tokyo", 12.0, 1), ("Frankfurt", 12.0, 1), ("Batam", 12.0, 1)]),
        ("Oracle Pay As", "⭕", [("Phoenix", 55.0, 3)]),
        ("G_Suit", "🇬", [("Non Profit 300", 180.0, 1)])
    ]
    for cat_name, icon, prods in sample_data:
        c.execute("INSERT INTO categories (name, icon) VALUES (?, ?)", (cat_name, icon))
        cat_id = c.lastrowid
        for p_name, price, qty in prods:
            c.execute("INSERT INTO products (category_id, name, price) VALUES (?, ?, ?)", (cat_id, p_name, price))
            p_id = c.lastrowid
            for i in range(qty):
                c.execute("INSERT INTO stock (product_id, account_data, is_sold) VALUES (?, ?, 0)", (p_id, f"demo_{cat_name.lower().replace(' ','_')}_{i+1}@provider.com:Pass1234!:KEY:TOKEN"))
    conn.commit()
    conn.close()

router = Router()

class Form(StatesGroup):
    custom_topup = State()
    admin_stock_lines = State()

@router.message(CommandStart())
async def start_handler(m: Message):
    uid = m.from_user.id
    get_or_create_user(uid, m.from_user.username, m.from_user.first_name)
    await m.answer(
        "👋 <b>¡Bienvenido a la Tienda Automatizada de Paneles Cloud!</b>\n\n"
        "🚀 Cuentas y paneles cloud con entrega 100% automática tras pagar en Binance Pay (USDT).\n\n"
        "Usa los botones del menú para comenzar:",
        parse_mode="HTML",
        reply_markup=kb_main(uid in ADMIN_IDS)
    )

@router.message(F.text == "🍱 Availability Of Goods")
async def availability_handler(m: Message):
    goods = get_goods_summary()
    lines, total = [], 0
    for g in goods:
        cat, prods = g["category"], g["products"]
        if not prods:
            continue
        lines.append(f"➖➖➖ {cat['icon']} <b>{cat['name']}</b> ➖➖➖")
        for p in prods:
            total += p["stock_qty"]
            lines.append(f"{p['name']} {p['price']:.1f}$ <b>{p['stock_qty']}pcs</b>")
        lines.append("")
    lines.append(f"📦 <b>Total de unidades en tienda:</b> {total} pcs")
    await m.answer("\n".join(lines), parse_mode="HTML")

@router.message(F.text == "👤 Profile")
async def profile_handler(m: Message):
    u = get_or_create_user(m.from_user.id)
    await m.answer(f"👤 <b>Perfil de Usuario</b>\n\n🆔 <b>ID:</b> <code>{u['user_id']}</code>\n💰 <b>Saldo:</b> <code>${u['balance']:.2f} USDT</code>\n🛍️ <b>Total Compras:</b> <code>${u['total_spent']:.2f} USDT</code>", parse_mode="HTML")

@router.message(F.text == "💰 Top Up")
async def topup_handler(m: Message):
    u = get_or_create_user(m.from_user.id)
    btns = [
        [InlineKeyboardButton(text="💵 $5 USDT", callback_data="amt_5"), InlineKeyboardButton(text="💵 $10 USDT", callback_data="amt_10")],
        [InlineKeyboardButton(text="💵 $20 USDT", callback_data="amt_20"), InlineKeyboardButton(text="💵 $50 USDT", callback_data="amt_50")],
        [InlineKeyboardButton(text="✏️ Otro Monto", callback_data="amt_custom")]
    ]
    await m.answer(f"💳 <b>Recarga de Saldo Binance Pay</b>\nSaldo actual: ${u['balance']:.2f} USDT\n\nElige un monto:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("amt_"))
async def process_topup_btn(cb: CallbackQuery, state: FSMContext):
    val = cb.data.replace("amt_", "")
    if val == "custom":
        await state.set_state(Form.custom_topup)
        await cb.message.edit_text("✏️ Escribe el monto en USDT que deseas recargar (mínimo $1.00):")
        await cb.answer()
        return
    await make_topup_invoice(cb.message, cb.from_user.id, float(val))
    await cb.answer()

@router.message(Form.custom_topup)
async def process_custom_topup(m: Message, state: FSMContext):
    try:
        amt = float(m.text.replace(",", ".").strip())
        if amt < 1.0:
            await m.answer("El monto mínimo es $1.00 USDT.")
            return
        await state.clear()
        await make_topup_invoice(m, m.from_user.id, amt)
    except ValueError:
        await m.answer("Escribe un número válido (ej. 15.5):")

async def make_topup_invoice(target_msg, uid, amount):
    trade_no = f"TOP{uid}{int(time.time())}"
    res = await BinancePay.create_order(trade_no, amount, f"Recarga de Saldo - Usuario {uid}")
    if not res.get("success"):
        await target_msg.answer(f"❌ Error al crear orden en Binance: {res.get('error')}")
        return
    create_db_invoice(trade_no, res.get("prepayId"), uid, amount, "TOPUP", checkout_url=res.get("checkoutUrl"), qr_url=res.get("qrcodeLink"))
    text = f"🧾 <b>Orden de Recarga Creada</b>\n\n• <b>Monto:</b> <code>${amount:.2f} USDT</code>\n• <b>Trade No:</b> <code>{trade_no}</code>\n\nCompleta el pago en Binance. Al confirmarse, el saldo se acreditará <b>automáticamente</b>."
    await target_msg.answer(text, parse_mode="HTML", reply_markup=kb_invoice(res.get("checkoutUrl"), trade_no))

@router.callback_query(F.data.startswith("verify_"))
async def verify_payment_btn(cb: CallbackQuery):
    trade_no = cb.data.replace("verify_", "")
    inv = get_db_invoice(trade_no)
    if not inv or inv["status"] == "PAID":
        await cb.answer("Esta orden ya fue procesada.", show_alert=True)
        return
    query = await BinancePay.query_order(trade_no)
    if query.get("status") == "PAID":
        set_invoice_status(trade_no, "PAID")
        uid, amt = inv["user_id"], inv["amount"]
        if inv["invoice_type"] == "TOPUP":
            new_b = update_user_balance(uid, amt)
            await cb.message.edit_text(f"✅ <b>¡Pago recibido!</b>\nSe acreditaron +${amt:.2f} USDT.\n💰 Saldo actual: ${new_b:.2f} USDT", parse_mode="HTML")
        else:
            await deliver_item(cb.bot, uid, inv["product_id"])
            await cb.message.edit_text("✅ <b>¡Pago recibido!</b>\nCredenciales entregadas en el chat.", parse_mode="HTML")
        await cb.answer("¡Pago confirmado!", show_alert=True)
    else:
        await cb.answer("⏳ Pago aún no detectado en Binance.", show_alert=True)

@router.callback_query(F.data.startswith("cancel_"))
async def cancel_inv_btn(cb: CallbackQuery):
    set_invoice_status(cb.data.replace("cancel_", ""), "CANCELED")
    await cb.message.edit_text("❌ Orden cancelada.")
    await cb.answer("Cancelado")

@router.message(F.text == "🎁 Buy")
async def buy_handler(m: Message):
    cats = get_categories()
    btns = [[InlineKeyboardButton(text=f"{c['icon']} {c['name']}", callback_data=f"c_{c['id']}")] for c in cats]
    await m.answer("🛒 <b>Selecciona una categoría:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("c_"))
async def cat_prods_handler(cb: CallbackQuery):
    cid = int(cb.data.replace("c_", ""))
    prods = get_products_by_cat(cid)
    btns = [[InlineKeyboardButton(text=f"{p['name']} | ${p['price']:.1f} ({p['stock_count']} pcs)", callback_data=f"p_{p['id']}")] for p in prods]
    btns.append([InlineKeyboardButton(text="🔙 Categorías", callback_data="back_cats")])
    await cb.message.edit_text("Selecciona el panel cloud:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data == "back_cats")
async def back_cats_handler(cb: CallbackQuery):
    cats = get_categories()
    btns = [[InlineKeyboardButton(text=f"{c['icon']} {c['name']}", callback_data=f"c_{c['id']}")] for c in cats]
    await cb.message.edit_text("🛒 <b>Selecciona una categoría:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("p_"))
async def prod_detail_handler(cb: CallbackQuery):
    pid = int(cb.data.replace("p_", ""))
    p, u = get_product(pid), get_or_create_user(cb.from_user.id)
    stk = p["stock_count"]
    text = f"📦 <b>{p['name']}</b>\n\n💵 <b>Precio:</b> ${p['price']:.2f} USDT\n📊 <b>Stock:</b> {stk} pcs\n💳 <b>Tu Saldo:</b> ${u['balance']:.2f} USDT"
    btns = []
    if stk > 0:
        if u["balance"] >= p["price"]:
            btns.append([InlineKeyboardButton(text=f"✅ Comprar con Saldo (${p['price']:.2f})", callback_data=f"paybal_{pid}")])
        btns.append([InlineKeyboardButton(text=f"⚡ Pagar con Binance Pay (${p['price']:.2f})", callback_data=f"paydirect_{pid}")])
    else:
        btns.append([InlineKeyboardButton(text="❌ Agotado", callback_data="none")])
    btns.append([InlineKeyboardButton(text="🔙 Volver", callback_data=f"c_{p['category_id']}")])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("paybal_"))
async def pay_balance_handler(cb: CallbackQuery):
    pid = int(cb.data.replace("paybal_", ""))
    p, u = get_product(pid), get_or_create_user(cb.from_user.id)
    if u["balance"] < p["price"]:
        await cb.answer("Saldo insuficiente.", show_alert=True)
        return
    disp = dispense_stock_item(pid, cb.from_user.id)
    if not disp:
        await cb.answer("Agotado.", show_alert=True)
        return
    update_user_balance(cb.from_user.id, -p["price"])
    await cb.message.edit_text(f"✅ ¡Compra exitosa! Producto: <b>{p['name']}</b>", parse_mode="HTML")
    await cb.bot.send_message(cb.from_user.id, f"🎉 <b>ENTREGA DE PANEL CLOUD</b>\n📦 <b>Producto:</b> {p['name']}\n\n🔑 <b>Credenciales:</b>\n<code>{disp['account_data']}</code>", parse_mode="HTML")
    await cb.answer("¡Compra exitosa!", show_alert=True)

@router.callback_query(F.data.startswith("paydirect_"))
async def pay_direct_handler(cb: CallbackQuery):
    pid = int(cb.data.replace("paydirect_", ""))
    p = get_product(pid)
    if p["stock_count"] <= 0:
        await cb.answer("Producto agotado.", show_alert=True)
        return
    trade_no = f"BUY{cb.from_user.id}{pid}{int(time.time())}"
    res = await BinancePay.create_order(trade_no, p["price"], f"Compra: {p['name']}")
    create_db_invoice(trade_no, res.get("prepayId"), cb.from_user.id, p["price"], "BUY", pid, res.get("checkoutUrl"), res.get("qrcodeLink"))
    await cb.message.edit_text(f"⚡ <b>Pago Directo Binance Pay</b>\n\n• <b>Producto:</b> {p['name']}\n• <b>Total:</b> ${p['price']:.2f} USDT", parse_mode="HTML", reply_markup=kb_invoice(res.get("checkoutUrl"), trade_no))
    await cb.answer()

async def deliver_item(bot: Bot, uid: int, pid: int):
    p = get_product(pid)
    disp = dispense_stock_item(pid, uid)
    if not disp:
        update_user_balance(uid, p["price"])
        await bot.send_message(uid, f"⚠️ Stock agotado. Se acreditó ${p['price']} USDT a tu balance.", parse_mode="HTML")
        return
    await bot.send_message(uid, f"🎉 <b>PAGO DE BINANCE CONFIRMADO</b>\n📦 <b>Producto:</b> {p['name']}\n\n🔑 <b>Credenciales:</b>\n<code>{disp['account_data']}</code>", parse_mode="HTML")

@router.message(F.text == "⚙️ Panel Admin")
@router.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer(f"⛔ Tu ID ({m.from_user.id}) no está en ADMIN_IDS dentro de .env")
        return
    btns = [[InlineKeyboardButton(text="📥 Cargar Stock Masivo", callback_data="adm_stk")]]
    await m.answer("⚙️ <b>Panel de Control Administrador</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data == "adm_stk")
async def adm_stk_cats(cb: CallbackQuery):
    cats = get_categories()
    btns = [[InlineKeyboardButton(text=f"{c['icon']} {c['name']}", callback_data=f"astk_{c['id']}")] for c in cats]
    await cb.message.edit_text("Selecciona la categoría:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("astk_"))
async def adm_stk_prods(cb: CallbackQuery):
    cid = int(cb.data.replace("astk_", ""))
    prods = get_products_by_cat(cid)
    btns = [[InlineKeyboardButton(text=f"{p['name']} ({p['stock_count']} pcs)", callback_data=f"selp_{p['id']}")] for p in prods]
    await cb.message.edit_text("Selecciona el producto:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("selp_"))
async def adm_prompt_lines(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.replace("selp_", ""))
    await state.update_data(prod_id=pid)
    await state.set_state(Form.admin_stock_lines)
    await cb.message.edit_text("📥 Pega las cuentas una por línea:")
    await cb.answer()

@router.message(Form.admin_stock_lines)
async def adm_save_lines(m: Message, state: FSMContext):
    data = await state.get_data()
    added = add_stock(data["prod_id"], m.text.strip().split("\n"))
    await state.clear()
    await m.answer(f"✅ ¡{added} cuentas añadidas al stock!")

async def payment_monitor(bot: Bot):
    while True:
        try:
            for inv in get_pending_invoices():
                tno = inv["merchant_trade_no"]
                res = await BinancePay.query_order(tno)
                if res.get("status") == "PAID":
                    set_invoice_status(tno, "PAID")
                    if inv["invoice_type"] == "TOPUP":
                        new_b = update_user_balance(inv["user_id"], inv["amount"])
                        await bot.send_message(inv["user_id"], f"✅ <b>¡Pago recibido!</b> +${inv['amount']:.2f} USDT\n💰 Saldo: ${new_b:.2f} USDT", parse_mode="HTML")
                    else:
                        await deliver_item(bot, inv["user_id"], inv["product_id"])
        except Exception as e:
            logger.error(f"Error monitor: {e}")
        await asyncio.sleep(PAYMENT_POLL_INTERVAL)

async def main():
    init_db()
    seed_catalog()
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN está vacío en .env")
        return
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(payment_monitor(bot))
    logger.info("✅ Bot en línea y escuchando mensajes en Telegram...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())