import logging
import os
import re
import signal
import sys
from datetime import datetime

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import ssl
from sqlalchemy.orm import joinedload

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler
)
from models import init_db
from models import (
    Base,
    User,
    Doctor,
    Appointment,
    HealthCertificate,
    Specialization,
    engine,
    Session,
    init_db  # <--- add this
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEVELOPER_CHAT_ID = int(os.getenv("DEVELOPER_CHAT_ID", "0"))
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.example.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "user@example.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "password")
PAYPAL_ME_LINK = os.getenv("PAYPAL_ME_LINK", "https://paypal.me/yourlink")

# Validation for essential environment variables
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set.")
if DEVELOPER_CHAT_ID == 0:
    raise ValueError("DEVELOPER_CHAT_ID not set correctly. Please set it to the developer's Telegram chat ID.")

CONSULTATION_PRICE_EUR = 9.00
EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")
RECEIPTS_DIR = "receipts"
os.makedirs(RECEIPTS_DIR, exist_ok=True)

scheduler = AsyncIOScheduler()

# Define Conversation States
(
    MAIN_MENU,
    REGISTER_NAME,
    REGISTER_EMAIL,
    REGISTER_PHONE,
    APPOINTMENT_CHOOSE_SPECIALIZATION,
    APPOINTMENT_CHOOSE_DOCTOR,
    APPOINTMENT_CONTACT_METHOD,
    APPOINTMENT_DESCRIPTION,
    CERTIFICATE_REASON,
    CERTIFICATE_DESCRIPTION,
    EDIT_PROFILE_MENU,
    EDIT_NAME,
    EDIT_PHONE,
    EDIT_EMAIL,
    DEVELOPER_MENU,
    DEV_MANAGE_SPECIALIZATIONS,
    DEV_ADD_SPECIALIZATION,
    DEV_REMOVE_SPECIALIZATION_SELECT,
    CONFIRM_REMOVE_SPEC,
    DEV_ADD_DOCTOR_CHOOSE_SPECIALIZATION,
    DEV_ADD_DOCTOR_NAME,
    DEV_ADD_DOCTOR_AVAILABILITY,
    DEV_REMOVE_DOCTOR_CHOOSE_SPECIALization,  # Note the capitalization mismatch in variable name
    DEV_REMOVE_DOCTOR_SELECT,
    CONFIRM_REMOVE_DOCTOR,
    SEND_MESSAGE_TO_USER,
    SEND_MESSAGE_TO_DEVELOPER,
    PAYMENT_APPOINTMENT_ID,
    PAYMENT_RECEIPT
) = range(29)


##################
# Keyboards
##################

def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        ["📅 گرفتن وقت ملاقات"],
        ["📝 دریافت گواهی سلامت"],
        ["💳 ارسال پرداخت"],
        ["✉️ تماس با ما"],
        ["📜 تاریخچه ملاقات‌ها"],
        ["✏️ ویرایش پروفایل"],
        ["🔄 راه‌اندازی مجدد"]
    ]
    if user_id == DEVELOPER_CHAT_ID:
        keyboard.append(["🛠 منوی توسعه‌دهنده"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def payment_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🔙 بازگشت", "❌ لغو"]], resize_keyboard=True, one_time_keyboard=False)


def cancel_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["❌ لغو"]], resize_keyboard=True, one_time_keyboard=False)


def back_cancel_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🔙 بازگشت", "❌ لغو"]], resize_keyboard=True, one_time_keyboard=False)


def developer_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["🗂 مدیریت تخصص‌ها"],
        ["➕ افزودن پزشک"],
        ["➖ حذف پزشک"],
        ["📊 مشاهده آمار"],
        ["📨 ارسال پیام به کاربر"],
        ["🔙 بازگشت"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def contact_method_keyboard(available_methods=None):
    if available_methods is None:
        available_methods = ["حضوری", "آنلاین", "هر دو"]
    buttons = []
    if "حضوری" in available_methods:
        buttons.append("حضوری")
    if "آنلاین" in available_methods:
        buttons.append("آنلاین")
    if "هر دو" in available_methods:
        buttons.append("هر دو")
    buttons.extend(["🔙 بازگشت", "❌ لغو"])
    return ReplyKeyboardMarkup([[btn] for btn in buttons], resize_keyboard=True, one_time_keyboard=False)


def specialization_keyboard(include_back=True):
    specs = get_specializations()
    if not specs:
        return ReplyKeyboardMarkup([["🔙 بازگشت"]], resize_keyboard=True, one_time_keyboard=False)
    keyboard = [[s] for s in specs]
    if include_back:
        keyboard.append(["🔙 بازگشت"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


##################
# Helper Functions
##################

def get_specializations():
    with Session() as session:
        specs = session.query(Specialization).all()
        return [s.name for s in specs]


def get_doctors_by_specialization(spec_name):
    with Session() as session:
        spec = session.query(Specialization).filter_by(name=spec_name).first()
        if not spec:
            return []
        return session.query(Doctor).filter_by(specialization_id=spec.id).all()


def format_doctor_availability(doctor: Doctor) -> str:
    availability = []
    if doctor.in_person_available:
        availability.append("حضوری")
    if doctor.online_available:
        availability.append("آنلاین")
    return " & ".join(availability) if availability else "در دسترس نیست"


def send_email(to_email: str, subject: str, body: str):
    if not EMAIL_REGEX.match(to_email):
        logger.error(f"ایمیل نامعتبر: {to_email}")
        return
    context_ssl = ssl.create_default_context()
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = SMTP_USER
    message["To"] = to_email
    part = MIMEText(body, "plain")
    message.attach(part)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context_ssl) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls(context=context_ssl)
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        logger.info(f"ایمیل به {to_email} ارسال شد.")
    except Exception as e:
        logger.error(f"خطا در ارسال ایمیل: {e}")


##################
# Handler Functions
##################

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
    if user:
        welcome_text = (
            f"👋 *خوش آمدید، {user.name}!*\n\n"
            "👍 **مزایای استفاده از پلتفرم دکتر لاین:**\n\n"
            "• 🕒 *مشاوره پزشکی ۲۴ ساعته در دسترس*\n"
            "• 📄 *دریافت نسخه پزشکی معتبر در اروپا*\n"
            "• 🚫💼 *عدم نیاز به بیمه برای بهره‌مندی از خدمات*\n"
            "• 🚗🏥 *درخواست ویزیت پزشکی عمومی و تخصصی در منزل بدون انتظار در صف طولانی اورژانس یا نوبت پزشک*\n"
            "• 📝 *امکان درخواست گواهی‌های:* \n"
            "  • Certificato di malattia 🤒\n"
            "  • Certificato dello sport 🏅\n"
            "  • Certificato medico per Patente 🚗📝\n"
            "• 🧘‍♀️ *دسترسی به مشاوره روانشناسی*\n\n"
            "❤️ *علیرغم همکاری با پزشکان فارسی‌زبان مقیم اروپا، دکترلاین سعی دارد تعرفه‌های اقتصادی 💰 را برای تسهیل دسترسی برابر هر قشری به حق سلامت ارائه دهد.*\n\n"

        )
    else:
        welcome_text = (
            "👋 *به Doctor Line خوش آمدید!*\n\n"
            "👍 **مزایای استفاده از پلتفرم پزشکلاین:**\n\n"
            "• 🕒 *مشاوره پزشکی ۲۴ ساعته در دسترس*\n"
            "• 📄 *دریافت نسخه پزشکی معتبر در اروپا*\n"
            "• 🚫💼 *عدم نیاز به بیمه برای بهره‌مندی از خدمات*\n"
            "• 🚗🏥 *درخواست ویزیت پزشکی عمومی و تخصصی در منزل بدون انتظار در صف طولانی اورژانس یا نوبت پزشک*\n"
            "• 📝 *امکان درخواست گواهی‌های:* \n"
            "  • Certificato di malattia 🤒\n"
            "  • Certificato dello sport 🏅\n"
            "  • Certificato medico per Patente 🚗📝\n"
            "• 🧘‍♀️ *دسترسی به مشاوره روانشناسی*\n\n"
            "❤️ *علیرغم همکاری با پزشکان فارسی‌زبان مقیم اروپا، دکترلاین سعی دارد تعرفه‌های اقتصادی 💰 را برای تسهیل دسترسی برابر هر قشری به حق سلامت ارائه دهد.*\n\n"
            "✨ همین حالا وقت خود را دریافت کنید  📲\n"

        )

    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(user_id)
    )
    return MAIN_MENU


##################
# Main Menu Handler
##################

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    logger.info(f"کاربر انتخاب کرد (منوی اصلی): {choice}")
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if choice == "📅 گرفتن وقت ملاقات":
        specs = get_specializations()
        if not specs:
            await update.message.reply_text("❌ *در حال حاضر تخصصی موجود نیست.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU
        await update.message.reply_text("*لطفاً یک تخصص را انتخاب کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=specialization_keyboard())
        return APPOINTMENT_CHOOSE_SPECIALIZATION

    elif choice == "📝 دریافت گواهی سلامت":
        await update.message.reply_text(
            "*لطفاً دلیل دریافت گواهی سلامت را انتخاب کنید:*",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [["عضویت در باشگاه"], ["گواهی رانندگی"], ["سایر"], ["🔙 بازگشت"]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
        )
        return CERTIFICATE_REASON

    elif choice == "📜 تاریخچه ملاقات‌ها":
        with Session() as session_inner:
            if not user:
                await update.message.reply_text(
                    "❌ *ابتدا باید ثبت‌نام کنید.*\nلطفاً وقت ملاقات بگیرید یا درخواست گواهی ارسال کنید.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(user_id))
                return MAIN_MENU
            # Eagerly load 'doctor' relationship using joinedload
            apps = session_inner.query(Appointment).options(joinedload(Appointment.doctor)).filter(
                Appointment.user_id == user.id
            ).order_by(Appointment.created_at.desc()).all()
        if not apps:
            await update.message.reply_text("*📅 شما هیچ وقت ملاقاتی ندارید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
        else:
            msg = "*📝 ملاقات‌های شما:*\n\n"
            for ap in apps:
                # Map status to icons and readable text
                status_icon = {
                    "confirmed": "✅ *تأیید شده*",
                    "pending": "⏳ *در انتظار*",
                    "rejected": "❌ *رد شده*",
                    "canceled": "🚫 *لغو شده*"
                }.get(ap.status, ap.status.capitalize())

                msg += (
                    f"• *شناسه ملاقات:* {ap.id}\n"
                    f"  *پزشک:* {ap.doctor.name} ({format_doctor_availability(ap.doctor)})\n"
                    f"  *روش ارتباط:* {ap.contact_method}\n"
                    f"  *وضعیت:* {status_icon}\n"
                    f"  *تاریخ:* {ap.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
                )
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    elif choice == "✏️ ویرایش پروفایل":
        if not user:
            await update.message.reply_text(
                "❌ *ابتدا باید ثبت‌نام کنید.*\nلطفاً وقت ملاقات بگیرید یا درخواست گواهی ارسال کنید.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU
        kb = [["ویرایش نام"], ["ویرایش تلفن/شناسه"], ["ویرایش ایمیل"], ["🔙 بازگشت"]]
        await update.message.reply_text("*لطفاً جزئیاتی که می‌خواهید ویرایش کنید را انتخاب کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return EDIT_PROFILE_MENU

    elif choice == "💳 ارسال پرداخت":
        if not user:
            await update.message.reply_text(
                "❌ *ابتدا باید ثبت‌نام کنید.*\nلطفاً وقت ملاقات بگیرید یا درخواست گواهی ارسال کنید.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU
        await update.message.reply_text(
            "*🔢 لطفاً شناسه ملاقات خود را وارد کنید:*",
            parse_mode="Markdown",
            reply_markup=payment_menu_keyboard()
        )
        return PAYMENT_APPOINTMENT_ID

    elif choice == "✉️ تماس با ما":
        await update.message.reply_text("*✉️ تماس با ما*\n\nلطفاً پیام خود را در زیر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return SEND_MESSAGE_TO_DEVELOPER

    elif choice == "🔄 راه‌اندازی مجدد":
        return await restart(update, context)

    elif choice == "🛠 منوی توسعه‌دهنده" and user_id == DEVELOPER_CHAT_ID:
        await update.message.reply_text("*🛠 منوی توسعه‌دهنده:*", parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU

    else:
        await update.message.reply_text("❌ *انتخاب نامعتبر.* لطفاً یک گزینه از منو را انتخاب کنید.",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU


##################
# Restart Handler
##################

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
    if user:
        await update.message.reply_text(f"🔄 *ربات راه‌اندازی مجدد شد.*\nخوش آمدید، {user.name}!",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "🔄 *ربات راه‌اندازی مجدد شد.*\nلطفاً برای شروع وقت ملاقات بگیرید یا درخواست گواهی ارسال کنید.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU


##################
# Cancel Handler
##################

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
    await update.message.reply_text("🚫 *عملیات لغو شد.*",
                                    parse_mode="Markdown",
                                    reply_markup=main_menu_keyboard(user.telegram_id if user else user_id))
    return MAIN_MENU


##################
# Appointment Steps
##################

async def appointment_choose_specialization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    spec_name = update.message.text.strip()
    user_id = update.effective_user.id

    if spec_name == "🔙 بازگشت":
        await update.message.reply_text("🔙 *بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    # Validate specialization
    with Session() as session:
        spec = session.query(Specialization).filter_by(name=spec_name).first()
        if not spec:
            await update.message.reply_text("❌ *تخصص نامعتبر.* لطفاً دوباره انتخاب کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=specialization_keyboard())
            return APPOINTMENT_CHOOSE_SPECIALIZATION

    context.user_data['appointment_specialization'] = spec_name
    doctors = get_doctors_by_specialization(spec_name)
    if not doctors:
        await update.message.reply_text("❌ *هیچ پزشکی در این تخصص موجود نیست.*",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    keyboard = []
    for doc in doctors:
        availability = format_doctor_availability(doc)
        keyboard.append([f"{doc.name} ({availability})"])
    keyboard.append(["🔙 بازگشت"])
    await update.message.reply_text("*لطفاً یک پزشک را انتخاب کنید:*",
                                    parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return APPOINTMENT_CHOOSE_DOCTOR


async def appointment_choose_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc_selection = update.message.text.strip()
    user_id = update.effective_user.id

    if doc_selection == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به انتخاب تخصص.*", parse_mode="Markdown",
                                        reply_markup=specialization_keyboard())
        return APPOINTMENT_CHOOSE_SPECIALIZATION

    # Extract doctor name and availability
    match = re.match(r"(.+?) \((.+)\)", doc_selection)
    if not match:
        await update.message.reply_text("❌ *انتخاب نامعتبر.* لطفاً یک پزشک را از لیست انتخاب کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [[f"{doc.name} ({format_doctor_availability(doc)})"] for doc in
                                             get_doctors_by_specialization(
                                                 context.user_data.get('appointment_specialization', ""))] + [
                                                ["🔙 بازگشت"]],
                                            resize_keyboard=True
                                        ))
        return APPOINTMENT_CHOOSE_DOCTOR

    doc_name, availability = match.groups()

    with Session() as session:
        spec = session.query(Specialization).filter_by(
            name=context.user_data.get('appointment_specialization', "")).first()
        doctor = session.query(Doctor).filter_by(name=doc_name, specialization_id=spec.id).first()
        if not doctor:
            await update.message.reply_text("❌ *پزشک پیدا نشد.* لطفاً دوباره انتخاب کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=ReplyKeyboardMarkup(
                                                [[f"{doc.name} ({format_doctor_availability(doc)})"] for doc in
                                                 get_doctors_by_specialization(
                                                     context.user_data.get('appointment_specialization', ""))] + [
                                                    ["🔙 بازگشت"]],
                                                resize_keyboard=True
                                            ))
            return APPOINTMENT_CHOOSE_DOCTOR

    context.user_data['appointment_doctor_id'] = doctor.id
    logger.info(f"کاربر {user_id} پزشک با شناسه: {doctor.id} را انتخاب کرد.")

    # Determine available contact methods based on doctor's availability
    available_methods = []
    if doctor.in_person_available and doctor.online_available:
        available_methods = ["حضوری", "آنلاین", "هر دو"]
    elif doctor.in_person_available:
        available_methods = ["حضوری"]
    elif doctor.online_available:
        available_methods = ["آنلاین"]
    else:
        available_methods = []

    if not available_methods:
        await update.message.reply_text("❌ *پزشک انتخاب‌شده برای هیچ روش ارتباطی در دسترس نیست.*",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    if len(available_methods) == 1:
        # Only one method available; set it automatically
        selected_method = available_methods[0]
        context.user_data['appointment_contact_method'] = selected_method
        logger.info(f"کاربر {user_id} روش ارتباطی را به صورت خودکار تنظیم کرد: {selected_method}")
        await update.message.reply_text(
            """👨‍⚕ **پزشک عمومی**
        - 📞 *مشاوره تلفنی:* ۹٫۸۹€
        - 🏠 *ویزیت حضوری در منزل تورین:* ۲۹€

        🧴 **پزشک متخصص**
        - 📞 *مشاوره تلفنی:* ۱۴٫۵€
        - 🏠 *ویزیت حضوری در منزل:* در حال حاضر فقط تلفنی امکان‌پذیر است.

        🧠 **مشاوره روانشناسی**
        - 🕒 *جلسه ۴۵ دقیقه‌ای:* ۸٫۹۹€
        - 📦 *پک چند جلسه‌ای:* از طریق پشتیبانی در دسترس است.

        📝 **مشکل خود را توضیح دهید:**""",
            parse_mode="Markdown",
            reply_markup=back_cancel_menu_keyboard()
        )
        return APPOINTMENT_DESCRIPTION
    else:
        # Multiple methods available; ask user to choose
        await update.message.reply_text("*لطفاً روش ارتباط را انتخاب کنید:*", parse_mode="Markdown",
                                        reply_markup=contact_method_keyboard(available_methods))
        return APPOINTMENT_CONTACT_METHOD


async def appointment_contact_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    method = update.message.text.strip()
    user_id = update.effective_user.id
    logger.debug(f"کاربر {user_id} روش ارتباطی را انتخاب کرد: {method}")

    if method == "🔙 بازگشت":
        logger.info(f"کاربر {user_id} به انتخاب پزشک بازگشت.")
        spec_name = context.user_data.get('appointment_specialization', "")
        doctors = get_doctors_by_specialization(spec_name)
        keyboard = []
        for doc in doctors:
            availability = format_doctor_availability(doc)
            keyboard.append([f"{doc.name} ({availability})"])
        keyboard.append(["🔙 بازگشت"])
        await update.message.reply_text(
            "*لطفاً یک پزشک را انتخاب کنید:*",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return APPOINTMENT_CHOOSE_DOCTOR

    if method == "❌ لغو":
        return await cancel(update, context)

    valid_methods = ["حضوری", "آنلاین", "هر دو"]
    if method not in valid_methods:
        logger.warning(f"کاربر {user_id} روش ارتباطی نامعتبری را انتخاب کرد: {method}")
        await update.message.reply_text(
            "❌ *روش ارتباطی نامعتبر.* لطفاً از گزینه‌های موجود انتخاب کنید:",
            parse_mode="Markdown",
            reply_markup=contact_method_keyboard(available_methods=valid_methods)
        )
        return APPOINTMENT_CONTACT_METHOD

    context.user_data['appointment_contact_method'] = method
    logger.info(f"کاربر {user_id} روش ارتباطی را تنظیم کرد: {method}")
    await update.message.reply_text(
        """👨‍⚕ **پزشک عمومی**
    - 📞 *مشاوره تلفنی:* ۹٫۸۹€
    - 🏠 *ویزیت حضوری در منزل تورین:* ۲۹€

    🧴 **پزشک متخصص**
    - 📞 *مشاوره تلفنی:* ۱۴٫۵€
    - 🏠 *ویزیت حضوری در منزل:* در حال حاضر فقط تلفنی امکان‌پذیر است.

    🧠 **مشاوره روانشناسی**
    - 🕒 *جلسه ۴۵ دقیقه‌ای:* ۸٫۹۹€
    - 📦 *پک چند جلسه‌ای:* از طریق پشتیبانی در دسترس است.

    📝 **مشکل خود را توضیح دهید:**""",
        parse_mode="Markdown",
        reply_markup=back_cancel_menu_keyboard()
    )
    return APPOINTMENT_DESCRIPTION


async def appointment_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    description = update.message.text.strip()
    user_id = update.effective_user.id
    logger.debug(f"کاربر {user_id} توضیح مشکل را ارائه داد: {description}")

    if description == "🔙 بازگشت":
        logger.info(f"کاربر {user_id} به انتخاب روش ارتباط بازگشت.")
        available_methods = []
        doctor_id = context.user_data.get('appointment_doctor_id')
        with Session() as session:
            doctor = session.query(Doctor).filter_by(id=doctor_id).first()
            if doctor.in_person_available and doctor.online_available:
                available_methods = ["حضوری", "آنلاین", "هر دو"]
            elif doctor.in_person_available:
                available_methods = ["حضوری"]
            elif doctor.online_available:
                available_methods = ["آنلاین"]
        if len(available_methods) == 1:
            selected_method = available_methods[0]
            context.user_data['appointment_contact_method'] = selected_method
            logger.info(f"کاربر {user_id} روش ارتباطی را به صورت خودکار تنظیم کرد: {selected_method}")
            await update.message.reply_text(
                """👨‍⚕ **پزشک عمومی**
            - 📞 *مشاوره تلفنی:* ۹٫۸۹€
            - 🏠 *ویزیت حضوری در منزل تورین:* ۲۹€

            🧴 **پزشک متخصص**
            - 📞 *مشاوره تلفنی:* ۱۴٫۵€
            - 🏠 *ویزیت حضوری در منزل:* در حال حاضر فقط تلفنی امکان‌پذیر است.

            🧠 **مشاوره روانشناسی**
            - 🕒 *جلسه ۴۵ دقیقه‌ای:* ۸٫۹۹€
            - 📦 *پک چند جلسه‌ای:* از طریق پشتیبانی در دسترس است.

            📝 **مشکل خود را توضیح دهید:**""",
                parse_mode="Markdown",
                reply_markup=back_cancel_menu_keyboard()
            )
            return APPOINTMENT_DESCRIPTION
        else:
            await update.message.reply_text("*لطفاً روش ارتباط را انتخاب کنید:*", parse_mode="Markdown",
                                            reply_markup=contact_method_keyboard(available_methods))
            return APPOINTMENT_CONTACT_METHOD

    if description == "❌ لغو":
        return await cancel(update, context)

    if not description:
        logger.warning(f"کاربر {user_id} توضیح خالی ارائه داد.")
        await update.message.reply_text(
            "❌ *توضیح نمی‌تواند خالی باشد.* لطفاً یک توضیح معتبر وارد کنید:",
            parse_mode="Markdown",
            reply_markup=back_cancel_menu_keyboard()
        )
        return APPOINTMENT_DESCRIPTION

    # Store the current appointment details
    context.user_data['appointment_details'] = {
        'description': description,
        'contact_method': context.user_data.get('appointment_contact_method'),
        'doctor_id': context.user_data.get('appointment_doctor_id'),
        'specialization': context.user_data.get('appointment_specialization')
    }

    try:
        with Session() as session:
            # Check if user exists
            user = session.query(User).filter_by(telegram_id=user_id).first()
            if not user:
                await update.message.reply_text(
                    "*🔍 به نظر می‌رسد که شما ثبت‌نام نکرده‌اید.* بیایید ابتدا ثبت‌نام کنیم.\n\n*نام کامل خود را وارد کنید:*",
                    parse_mode="Markdown",
                    reply_markup=cancel_menu_keyboard()
                )
                context.user_data['pending_action'] = 'make_appointment'
                return REGISTER_NAME

            # Check if doctor exists and is available
            doctor = session.query(Doctor).get(context.user_data['appointment_details']['doctor_id'])
            if not doctor:
                logger.error(f"پزشک برای شناسه: {context.user_data['appointment_details']['doctor_id']} پیدا نشد.")
                await update.message.reply_text(
                    "❌ *پزشک انتخاب‌شده دیگر در دسترس نیست.* لطفاً دوباره تلاش کنید.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(user_id)
                )
                return MAIN_MENU

            # Create new appointment
            new_appointment = Appointment(
                user_id=user.id,
                doctor_id=doctor.id,
                appointment_type=context.user_data['appointment_details']['specialization'],
                contact_method=context.user_data['appointment_details']['contact_method'],
                description=description,
                status='pending',
                created_at=datetime.utcnow()
            )

            session.add(new_appointment)
            session.flush()  # Flush to get the appointment ID without committing
            appointment_id = new_appointment.id

            # Prepare notification message
            notification_message = (
                f"📅 *درخواست وقت ملاقات جدید*\n\n"
                f"*کاربر:* {user.name} (شناسه: {user.telegram_id})\n"
                f"*پزشک:* {doctor.name} ({format_doctor_availability(doctor)})\n"
                f"*روش ارتباط:* {new_appointment.contact_method}\n"
                f"*توضیح:* {new_appointment.description}\n\n"
                f"*شناسه ملاقات:* {appointment_id}"
            )

            # Try to notify developer
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=notification_message,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ تأیید", callback_data=f"confirm_appt_{appointment_id}"),
                         InlineKeyboardButton("❌ رد", callback_data=f"reject_appt_{appointment_id}")]
                    ])
                )
                logger.info(f"توسعه‌دهنده در مورد ملاقات {appointment_id} مطلع شد.")

                # Send confirmation email to user
                email_subject = "📅 درخواست وقت ملاقات دریافت شد"
                email_body = (
                    f"سلام {user.name},\n\n"
                    f"از انتخاب *Doctor Line* برای تنظیم وقت ملاقات متشکریم. جزئیات ملاقات شما به شرح زیر است:\n\n"
                    f"• *شناسه ملاقات:* {appointment_id}\n"
                    f"• *پزشک:* {doctor.name}\n"
                    f"• *تخصص:* {new_appointment.appointment_type}\n"
                    f"• *روش ارتباط:* {new_appointment.contact_method}\n"
                    f"• *توضیح:* {new_appointment.description}\n\n"
                    f"*وضعیت:* در انتظار تأیید\n\n"
                    f"پس از تأیید، به شما اطلاع خواهیم داد.\n\n"
                    f"📅 *تاریخ ملاقات:* {new_appointment.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"از انتخاب *Doctor Line* متشکریم. مشتاقانه منتظر کمک به شما هستیم!\n\n"
                    f"با احترام,\n*تیم Doctor Line*"
                )
                send_email(user.email, email_subject, email_body)

                # If notification successful, commit the transaction
                session.commit()

                await update.message.reply_text(
                    "✅ *درخواست وقت ملاقات شما ارسال شد و در انتظار تأیید است.*",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(user_id)
                )

            except Exception as e:
                logger.error(f"عدم موفقیت در اطلاع‌رسانی به توسعه‌دهنده در مورد ملاقات: {e}")
                session.rollback()
                await update.message.reply_text(
                    "❌ *در حال حاضر قادر به پردازش درخواست وقت ملاقات شما نیستیم.* لطفاً بعداً تلاش کنید.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(user_id)
                )

    except Exception as e:
        logger.error(f"خطا در ایجاد ملاقات: {e}")
        await update.message.reply_text(
            "❌ *خطایی در پردازش ملاقات شما رخ داد.* لطفاً دوباره تلاش کنید.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id)
        )

    # Clear appointment data
    context.user_data.pop('appointment_details', None)
    context.user_data.pop('pending_action', None)

    return MAIN_MENU


##################
# Certificate Steps
##################

async def certificate_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reason = update.message.text.strip()
    user_id = update.effective_user.id

    if reason == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    if reason not in ["عضویت در باشگاه", "گواهی رانندگی", "سایر"]:
        await update.message.reply_text("❌ *انتخاب نامعتبر.* لطفاً دلیل مناسبی را انتخاب کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["عضویت در باشگاه"], ["گواهی رانندگی"], ["سایر"], ["🔙 بازگشت"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return CERTIFICATE_REASON

    context.user_data['certificate_reason'] = reason
    await update.message.reply_text(
        """💰 **هزینه صدور گواهی‌ها به شرح ذیل می‌باشد:**\n
    • 🏅 **گواهی ورزش:** ۳۴€\n
    • 📜 **گواهی صدور گواهینامه:** ۳۴.۵€\n
    • 🤒 **گواهی بیماری (Mutua):** ۳۰€\n\n
    🔍 **درخواست صدور گواهی مستلزم ویزیت حضوری است که این خدمات منحصراً در حال حاضر در شهر تورین امکان‌پذیر می‌باشد.** 🏠\n\n
    ✨ **لطفاً نوع گواهی مورد نیاز خود را اعلام کنید:** ✨""",
        parse_mode="Markdown",
        reply_markup=back_cancel_menu_keyboard()
    )
    return CERTIFICATE_DESCRIPTION

async def certificate_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    description = update.message.text.strip()

    user_id = update.effective_user.id
    logger.debug(f"کاربر {user_id} توضیح گواهی سلامت را ارائه داد: {description}")

    if description == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به انتخاب دلیل.*", parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["عضویت در باشگاه"], ["گواهی رانندگی"], ["سایر"], ["🔙 بازگشت"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return CERTIFICATE_REASON

    if description == "❌ لغو":
        return await cancel(update, context)

    if not description:
        logger.warning(f"کاربر {user_id} توضیح گواهی سلامت خالی ارائه داد.")
        await update.message.reply_text("*❌ توضیح نمی‌تواند خالی باشد.* لطفاً یک توضیح معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return CERTIFICATE_DESCRIPTION

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            # Prompt for registration
            await update.message.reply_text(
                "*🔍 به نظر می‌رسد که شما ثبت‌نام نکرده‌اید.* بیایید ابتدا ثبت‌نام کنیم.\n\n*نام کامل خود را وارد کنید:*",
                parse_mode="Markdown",
                reply_markup=cancel_menu_keyboard()
            )
            context.user_data['pending_action'] = 'request_certificate'
            context.user_data['certificate_details'] = {
                'reason': context.user_data.get('certificate_reason'),
                'description': description
            }
            return REGISTER_NAME

        # Proceed to create certificate request
        certificate = HealthCertificate(
            user_id=user.id,
            reason=context.user_data.get('certificate_reason'),
            description=description,
            status='pending',
            created_at=datetime.utcnow()
        )
        session.add(certificate)
        try:
            session.commit()
            logger.info(f"گواهی سلامت {certificate.id} برای کاربر {user_id} ایجاد شد.")
        except IntegrityError as e:
            logger.error(f"خطا در ایجاد گواهی سلامت: {e}")
            session.rollback()
            await update.message.reply_text("❌ *در پردازش درخواست شما خطایی رخ داد.* لطفاً دوباره تلاش کنید.",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU

        # Notify developer
        try:
            await context.bot.send_message(
                chat_id=DEVELOPER_CHAT_ID,
                text=(
                    f"📜 *درخواست گواهی سلامت جدید*\n\n"
                    f"*کاربر:* {user.name} (شناسه:{user.telegram_id})\n"
                    f"*دلیل:* {certificate.reason}\n"
                    f"*توضیح:* {certificate.description}\n\n"
                    f"*شناسه گواهی:* {certificate.id}"
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تایید", callback_data=f"approve_cert_{certificate.id}"),
                     InlineKeyboardButton("❌ رد", callback_data=f"reject_cert_{certificate.id}")]
                ])
            )
            logger.info(f"توسعه‌دهنده در مورد گواهی سلامت {certificate.id} مطلع شد.")
        except Exception as e:
            logger.error(f"خطا در اطلاع‌رسانی به توسعه‌دهنده در مورد گواهی سلامت: {e}")
            await update.message.reply_text("❌ *ناتوان در اطلاع‌رسانی به توسعه‌دهنده.* لطفاً بعداً دوباره تلاش کنید.",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU

    # Send confirmation email to user
    email_subject = "📜 درخواست گواهی سلامت دریافت شد"
    email_body = (
        f"سلام {user.name},\n\n"
        f"از درخواست *گواهی سلامت* در *Doctor Line* متشکریم. جزئیات درخواست شما به شرح زیر است:\n\n"
        f"• *شناسه گواهی:* {certificate.id}\n"
        f"• *دلیل:* {certificate.reason}\n"
        f"• *توضیح:* {certificate.description}\n\n"
        f"*وضعیت:* در انتظار تأیید\n\n"
        f"پس از پردازش، به شما اطلاع خواهیم داد.\n\n"
        f"از انتخاب *Doctor Line* متشکریم. در حمایت از نیازهای سلامت و حرفه‌ای شما هستیم!\n\n"
        f"با احترام,\n*تیم Doctor Line*"
    )
    send_email(user.email, email_subject, email_body)

    await update.message.reply_text("*✅ درخواست گواهی سلامت شما در انتظار تأیید است.*",
                                    parse_mode="Markdown",
                                    reply_markup=main_menu_keyboard(user_id))
    # Clear pending action and details
    context.user_data.pop('pending_action', None)
    context.user_data.pop('certificate_details', None)
    return MAIN_MENU


##################
# Registration Steps
##################

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "*🔑 ثبت‌نام:*\nلطفاً نام کامل خود را وارد کنید:",
        parse_mode="Markdown",
        reply_markup=cancel_menu_keyboard()
    )
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()

    if name == "❌ لغو":
        return await cancel(update, context)

    if not name:
        logger.warning("کاربر نام خالی را در هنگام ثبت‌نام ارائه داد.")
        await update.message.reply_text("*❌ نام نمی‌تواند خالی باشد.* لطفاً نام کامل خود را وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=cancel_menu_keyboard())
        return REGISTER_NAME

    context.user_data['reg_name'] = name
    await update.message.reply_text("*📧 آدرس ایمیل خود را وارد کنید:*",
                                    parse_mode="Markdown",
                                    reply_markup=cancel_menu_keyboard())
    return REGISTER_EMAIL


async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = update.message.text.strip()

    if email == "❌ لغو":
        return await cancel(update, context)

    if not EMAIL_REGEX.match(email):
        logger.warning("کاربر فرمت ایمیل نامعتبری را در هنگام ثبت‌نام ارائه داد.")
        await update.message.reply_text("*❌ فرمت ایمیل نامعتبر است.* لطفاً یک ایمیل معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=cancel_menu_keyboard())
        return REGISTER_EMAIL

    context.user_data['reg_email'] = email
    await update.message.reply_text("*📱 شماره تلفن یا شناسه خود را وارد کنید:*",
                                    parse_mode="Markdown",
                                    reply_markup=cancel_menu_keyboard())
    return REGISTER_PHONE


async def register_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()

    if phone == "❌ لغو":
        return await cancel(update, context)

    if not phone:
        logger.warning("کاربر شماره تلفن/شناسه خالی را در هنگام ثبت‌نام ارائه داد.")
        await update.message.reply_text("*❌ شماره تلفن/شناسه نمی‌تواند خالی باشد.* لطفاً وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=cancel_menu_keyboard())
        return REGISTER_PHONE

    user_id = update.effective_user.id
    name = context.user_data.get('reg_name')
    email = context.user_data.get('reg_email')

    with Session() as session:
        existing_user = session.query(User).filter_by(telegram_id=user_id).first()
        if existing_user:
            existing_user.name = name
            existing_user.email = email
            existing_user.phone = phone
            action = "به‌روزرسانی شد"
            user = existing_user  # **Define 'user' here**
        else:
            new_user = User(
                telegram_id=user_id,
                name=name,
                email=email,
                phone=phone
            )
            session.add(new_user)
            action = "ثبت‌نام شد"
            user = new_user  # **Define 'user' here**

        try:
            session.commit()
            logger.info(f"کاربر {user_id} با موفقیت {action}.")
            await update.message.reply_text(f"✅ *شما با موفقیت {action}.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
        except IntegrityError as e:
            logger.error(f"خطا در ثبت‌نام/به‌روزرسانی کاربر {user_id}: {e}")
            session.rollback()
            await update.message.reply_text("*❌ ثبت‌نام/به‌روزرسانی ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU

        # Send confirmation email to user
        if action == "ثبت‌نام شد":
            email_subject = "👋 خوش آمدید به Doctor Line!"
            email_body = (
                f"سلام {name},\n\n"
                f"به *Doctor Line* خوش آمدید! بسیار خوشحالیم که شما را در جمع خود داریم.\n\n"
                f"جزئیات ثبت‌نام شما به شرح زیر است:\n\n"
                f"• *نام:* {name}\n"
                f"• *ایمیل:* {email}\n"
                f"• *تلفن/شناسه:* {phone}\n\n"
                f"شما اکنون می‌توانید از امکاناتی مانند تنظیم وقت ملاقات، درخواست گواهی سلامت، و مدیریت پروفایل خود از طریق ربات تلگرام ما استفاده کنید.\n\n"
                f"اگر سوالی دارید یا به کمک نیاز دارید، با ما تماس بگیرید.\n\n"
                f"از انتخاب *Doctor Line* متشکریم. مشتاقانه منتظر خدمت به شما هستیم!\n\n"
                f"با احترام,\n*تیم Doctor Line*"
            )
        else:
            email_subject = "🔄 پروفایل با موفقیت به‌روزرسانی شد"
            email_body = (
                f"سلام {name},\n\n"
                f"پروفایل شما با موفقیت به‌روزرسانی شد. جزئیات به‌روزرسانی شده به شرح زیر است:\n\n"
                f"• *نام:* {name}\n"
                f"• *ایمیل:* {email}\n"
                f"• *تلفن/شناسه:* {phone}\n\n"
                f"اگر این تغییر را ایجاد نکرده‌اید یا نگرانی دارید، لطفاً بلافاصله با ما تماس بگیرید.\n\n"
                f"از انتخاب *Doctor Line* متشکریم. همیشه در خدمت شما هستیم!\n\n"
                f"با احترام,\n*تیم Doctor Line*"
            )
        send_email(email, email_subject, email_body)

        # Handle pending actions if any
        pending_action = context.user_data.get('pending_action')
        if pending_action == 'make_appointment':
            # Existing appointment handling logic
            # Ensure 'user' is defined and used correctly here
            # ...
            pass  # Replace with actual logic
        elif pending_action == 'request_certificate':
            certificate_details = context.user_data.get('certificate_details', {})
            reason = certificate_details.get('reason')
            description = certificate_details.get('description')

            if not reason or not description:
                await update.message.reply_text(
                    "*❌ اطلاعات گواهی نامکمل است. لطفاً دوباره تلاش کنید.*",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(user_id)
                )
                return MAIN_MENU

            try:
                # Create new health certificate
                certificate = HealthCertificate(
                    user_id=user.id,  # Now 'user' is defined
                    reason=reason,
                    description=description,
                    status='pending',
                    created_at=datetime.utcnow()
                )
                session.add(certificate)
                session.commit()
                logger.info(f"گواهی سلامت {certificate.id} برای کاربر {user_id} ایجاد شد.")

                # Notify developer
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=(
                        f"📜 *درخواست گواهی سلامت جدید*\n\n"
                        f"*کاربر:* {user.name} (شناسه:{user.telegram_id})\n"
                        f"*دلیل:* {certificate.reason}\n"
                        f"*توضیح:* {certificate.description}\n\n"
                        f"*شناسه گواهی:* {certificate.id}"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ تایید", callback_data=f"approve_cert_{certificate.id}"),
                         InlineKeyboardButton("❌ رد", callback_data=f"reject_cert_{certificate.id}")]
                    ])
                )
                logger.info(f"توسعه‌دهنده در مورد گواهی سلامت {certificate.id} مطلع شد.")

                # Send confirmation email to user
                email_subject = "📜 درخواست گواهی سلامت دریافت شد"
                email_body = (
                    f"سلام {user.name},\n\n"
                    f"از درخواست *گواهی سلامت* در *Doctor Line* متشکریم. جزئیات درخواست شما به شرح زیر است:\n\n"
                    f"• *شناسه گواهی:* {certificate.id}\n"
                    f"• *دلیل:* {certificate.reason}\n"
                    f"• *توضیح:* {certificate.description}\n\n"
                    f"*وضعیت:* در انتظار تأیید\n\n"
                    f"پس از پردازش، به شما اطلاع خواهیم داد.\n\n"
                    f"از انتخاب *Doctor Line* متشکریم. در حمایت از نیازهای سلامت و حرفه‌ای شما هستیم!\n\n"
                    f"با احترام,\n*تیم Doctor Line*"
                )
                send_email(user.email, email_subject, email_body)

                await update.message.reply_text("*✅ درخواست گواهی سلامت شما در انتظار تأیید است.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
            except Exception as e:
                logger.error(f"خطا در ایجاد گواهی سلامت برای کاربر {user_id}: {e}")
                await update.message.reply_text(
                    "*❌ خطایی در پردازش درخواست شما رخ داد.* لطفاً دوباره تلاش کنید.",
                    parse_mode="Markdown",
                    reply_markup=main_menu_keyboard(user_id)
                )

        # Clear pending action and details
        context.user_data.pop('pending_action', None)
        context.user_data.pop('certificate_details', None)

        return MAIN_MENU

    # Clear pending action and details
    context.user_data.pop('pending_action', None)
    context.user_data.pop('certificate_details', None)

    return MAIN_MENU


##################
# Payment Steps
##################

async def payment_appointment_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    appointment_id_text = update.message.text.strip()
    user_id = update.effective_user.id

    if appointment_id_text.lower() == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    if appointment_id_text.lower() == "❌ لغو":
        return await cancel(update, context)

    if not appointment_id_text.isdigit():
        await update.message.reply_text("*❌ شناسه ملاقات نامعتبر است. لطفاً یک شناسه عددی وارد کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=payment_menu_keyboard())
        return PAYMENT_APPOINTMENT_ID

    appointment_id = int(appointment_id_text)

    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id).first()
        if not appointment:
            await update.message.reply_text("*❌ ملاقات پیدا نشد. لطفاً یک شناسه ملاقات معتبر وارد کنید:*",
                                            parse_mode="Markdown",
                                            reply_markup=payment_menu_keyboard())
            return PAYMENT_APPOINTMENT_ID

        # Check if appointment belongs to the user
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user or appointment.user_id != user.id:
            await update.message.reply_text("*❌ شما اجازه ارسال رسید برای این ملاقات را ندارید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU

        # Check if appointment is pending or confirmed
        if appointment.status not in ["pending", "confirmed"]:
            await update.message.reply_text("*❌ این ملاقات برای ارسال پرداخت مجاز نیست.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU

        # Store the appointment ID for receipt submission
        context.user_data['payment_appointment_id'] = appointment_id
        await update.message.reply_text(
            "*📄 لطفاً رسید پرداخت خود را ارسال کنید (JPG/PNG):*",
            parse_mode="Markdown",
            reply_markup=payment_menu_keyboard()
        )
        return PAYMENT_RECEIPT


async def payment_receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.debug("وارد شدن به payment_receipt_handler")
    user_id = update.effective_user.id
    appointment_id = context.user_data.get('payment_appointment_id')
    logger.debug(f"پردازش رسید پرداخت برای شناسه ملاقات: {appointment_id}, شناسه کاربر: {user_id}")

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if not user:
        logger.warning(f"کاربر {user_id} تلاش کرد رسید پرداخت ارسال کند بدون ثبت‌نام.")
        await update.message.reply_text(
            "*❌ ابتدا باید ثبت‌نام کنید با گرفتن وقت ملاقات یا درخواست گواهی.*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU

    if not appointment_id:
        logger.warning(f"شناسه ملاقات برای کاربر {user_id} در هنگام ارسال پرداخت یافت نشد.")
        await update.message.reply_text(
            "*❌ شناسه ملاقات یافت نشد.* لطفاً فرآیند پرداخت را دوباره شروع کنید.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(user_id)
        )
        return MAIN_MENU

    # Initialize variables
    file_path = None
    caption = ""

    if update.message.photo:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        timestamp = int(datetime.utcnow().timestamp())
        file_path = os.path.join(RECEIPTS_DIR, f"receipt_{user_id}_{timestamp}.jpg")
        try:
            await file.download_to_drive(file_path)
            logger.info(f"رسید پرداخت به {file_path} دانلود شد.")
            caption = (
                f"📷 *رسید پرداخت از {user.name} (شناسه: {user.telegram_id})*\n"
                f"*شناسه ملاقات:* {appointment_id}\n\n"
                f"لطفاً پرداخت را تأیید کنید."
            )
        except Exception as e:
            logger.error(f"خطا در دانلود رسید پرداخت از کاربر {user_id}: {e}")
            await update.message.reply_text("*❌ دانلود رسید ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU

    elif update.message.document:
        document = update.message.document
        file = await document.get_file()
        timestamp = int(datetime.utcnow().timestamp())
        file_extension = os.path.splitext(document.file_name)[1].lower()
        if file_extension not in ['.jpg', '.jpeg', '.png']:
            await update.message.reply_text("*❌ نوع فایل پشتیبانی‌شده نیست. لطفاً یک تصویر JPG یا PNG ارسال کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=payment_menu_keyboard())
            return PAYMENT_RECEIPT
        file_path = os.path.join(RECEIPTS_DIR, f"receipt_{user_id}_{timestamp}{file_extension}")
        try:
            await file.download_to_drive(file_path)
            logger.info(f"رسید پرداخت به {file_path} دانلود شد.")
            caption = (
                f"📷 *رسید پرداخت از {user.name} (شناسه: {user.telegram_id})*\n"
                f"*شناسه ملاقات:* {appointment_id}\n\n"
                f"لطفاً پرداخت را تأیید کنید."
            )
        except Exception as e:
            logger.error(f"خطا در دانلود رسید پرداخت از کاربر {user_id}: {e}")
            await update.message.reply_text("*❌ دانلود رسید ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
            return MAIN_MENU
    else:
        text = update.message.text.strip().lower()
        if text in ["🔙 بازگشت", "❌ لغو"]:
            if text == "🔙 بازگشت":
                await update.message.reply_text("*🔙 بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
                return MAIN_MENU
            else:
                return await cancel(update, context)
        else:
            await update.message.reply_text("*❌ لطفاً رسید را به عنوان عکس یا سند (JPG/PNG) ارسال کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=payment_menu_keyboard())
            return PAYMENT_RECEIPT

    try:
        # Send the receipt to the developer
        with open(file_path, 'rb') as receipt_file:
            await context.bot.send_photo(
                chat_id=DEVELOPER_CHAT_ID,
                photo=receipt_file,
                caption=caption,
                parse_mode="Markdown"
            )
        logger.info(
            f"توسعه‌دهنده در مورد رسید پرداخت از کاربر {user_id} برای ملاقات {appointment_id} مطلع شد."
        )
        await update.message.reply_text("*✅ رسید دریافت شد و در حال بررسی است.*",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))

        # Optionally, remove the receipt file after sending
        try:
            os.remove(file_path)
            logger.info(f"فایل رسید حذف شد: {file_path}")
        except Exception as e:
            logger.warning(f"ناتوان در حذف فایل رسید: {file_path}. خطا: {e}")

    except Exception as e:
        logger.error(f"خطا در ارسال رسید پرداخت به توسعه‌دهنده برای کاربر {user_id}: {e}")
        await update.message.reply_text("*❌ ارسال رسید به توسعه‌دهنده ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))

    # Clear payment data
    context.user_data.pop('payment_appointment_id', None)

    return MAIN_MENU


##################
# Developer Menu Handlers
##################

async def developer_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    logger.info(f"توسعه‌دهنده انتخاب کرد: {choice}")

    if choice == "🗂 مدیریت تخصص‌ها":
        kb = [["➕ افزودن تخصص"], ["➖ حذف تخصص"], ["🔙 بازگشت"]]
        await update.message.reply_text("*🗂 مدیریت تخصص‌ها:*", parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return DEV_MANAGE_SPECIALIZATIONS

    elif choice == "➕ افزودن پزشک":
        specs = get_specializations()
        if not specs:
            await update.message.reply_text("❌ *هیچ تخصصی موجود نیست.* لطفاً ابتدا یکی اضافه کنید.",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU
        await update.message.reply_text("*لطفاً تخصص برای پزشک جدید را انتخاب کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=specialization_keyboard())
        return DEV_ADD_DOCTOR_CHOOSE_SPECIALIZATION

    elif choice == "➖ حذف پزشک":
        specs = get_specializations()
        if not specs:
            await update.message.reply_text("❌ *هیچ تخصصی موجود نیست.*",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU
        await update.message.reply_text("*لطفاً تخصصی را که می‌خواهید پزشک را از آن حذف کنید انتخاب کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=specialization_keyboard())
        return DEV_REMOVE_SPECIALIZATION_SELECT

    elif choice == "📊 مشاهده آمار":
        await view_statistics(update, context)
        return DEVELOPER_MENU

    elif choice == "📨 ارسال پیام به کاربر":
        await update.message.reply_text(
            "*📨 ارسال پیام به کاربر*\n\nلطفاً شناسه تلگرام کاربر و پیام خود را با فاصله وارد کنید:",
            parse_mode="Markdown",
            reply_markup=cancel_menu_keyboard())
        return SEND_MESSAGE_TO_USER

    elif choice == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(DEVELOPER_CHAT_ID))
        return MAIN_MENU

    else:
        await update.message.reply_text("❌ *انتخاب نامعتبر.* لطفاً یک گزینه از منوی توسعه‌دهنده را انتخاب کنید.",
                                        parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU


##################
# Developer: Manage Specializations
##################

async def dev_manage_specializations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()

    if choice == "➕ افزودن تخصص":
        await update.message.reply_text("*🆕 افزودن تخصص جدید*\n\nنام تخصص جدید را وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return DEV_ADD_SPECIALIZATION

    elif choice == "➖ حذف تخصص":
        specs = get_specializations()
        if not specs:
            await update.message.reply_text("*❌ تخصصی برای حذف وجود ندارد.*",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU
        kb = [[s] for s in specs]
        kb.append(["🔙 بازگشت"])
        await update.message.reply_text("*لطفاً تخصصی را که می‌خواهید حذف کنید انتخاب کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return DEV_REMOVE_SPECIALIZATION_SELECT

    elif choice == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی توسعه‌دهنده.*", parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU

    elif choice == "❌ لغو":
        return await cancel(update, context)

    else:
        await update.message.reply_text("❌ *انتخاب نامعتبر.* لطفاً یک گزینه از منوی مدیریت تخصص‌ها را انتخاب کنید.",
                                        parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEV_MANAGE_SPECIALIZATIONS


async def dev_add_specialization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    spec_name = update.message.text.strip()

    if spec_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به مدیریت تخصص‌ها.*", parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU

    if spec_name == "❌ لغو":
        return await cancel(update, context)

    if not spec_name:
        await update.message.reply_text("*❌ نام تخصص نمی‌تواند خالی باشد.* لطفاً یک نام معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return DEV_ADD_SPECIALIZATION

    with Session() as session:
        existing_spec = session.query(Specialization).filter_by(name=spec_name).first()
        if existing_spec:
            await update.message.reply_text("*❌ تخصص قبلاً وجود دارد.* لطفاً یک نام متفاوت وارد کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=back_cancel_menu_keyboard())
            return DEV_ADD_SPECIALIZATION
        new_spec = Specialization(name=spec_name)
        session.add(new_spec)
        try:
            session.commit()
            logger.info(f"تخصص '{spec_name}' توسط توسعه‌دهنده اضافه شد.")
        except IntegrityError as e:
            logger.error(f"خطا در افزودن تخصص '{spec_name}': {e}")
            session.rollback()
            await update.message.reply_text("*❌ افزودن تخصص ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEV_MANAGE_SPECIALIZATIONS

    await update.message.reply_text(f"✅ *تخصص '{spec_name}' با موفقیت اضافه شد.*",
                                    parse_mode="Markdown",
                                    reply_markup=developer_menu_keyboard())
    return DEVELOPER_MENU


async def dev_remove_specialization_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    spec_name = update.message.text.strip()

    if spec_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی توسعه‌دهنده.*", parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU

    with Session() as session:
        spec = session.query(Specialization).filter_by(name=spec_name).first()
        if not spec:
            await update.message.reply_text("*❌ تخصص پیدا نشد.*",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU
        # Store specialization ID in context
        context.user_data['remove_specialization_id'] = spec.id

        # Ask for confirmation to remove. **We WILL remove it even if appointments are active.**
        await update.message.reply_text(
            f"⚠️ *آیا مطمئنید که می‌خواهید تخصص '{spec_name}' را حذف کنید؟*\n\n"
            f"همه پزشکان مرتبط و ملاقات‌هایشان (حتی اگر فعال باشند) لغو و حذف خواهند شد.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["بله", "خیر"]], resize_keyboard=True)
        )
    return CONFIRM_REMOVE_SPEC


async def confirm_remove_spec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    confirmation = update.message.text.strip()

    if confirmation == "بله":
        spec_id = context.user_data.get('remove_specialization_id')
        with Session() as session:
            spec = session.query(Specialization).filter_by(id=spec_id).first()
            if not spec:
                await update.message.reply_text(
                    "❌ تخصص پیدا نشد.",
                    parse_mode="Markdown",
                    reply_markup=developer_menu_keyboard()
                )
                return DEVELOPER_MENU

            # ----------------------------------------------------------------
            # 1) For each doctor in the specialization, delete ALL appointments
            # ----------------------------------------------------------------
            for doctor in spec.doctors:
                appointments = session.query(Appointment).filter_by(doctor_id=doctor.id).all()

                for appt in appointments:
                    # If you'd like to notify users, do so here:
                    if appt.status in ["pending", "confirmed"]:
                        try:
                            await context.bot.send_message(
                                chat_id=appt.user.telegram_id,
                                text=(
                                    f"⚠️ *ملاقات لغو شد*\n\n"
                                    f"ملاقات شما (شناسه: {appt.id}) با دکتر {doctor.name} "
                                    f"به دلیل حذف تخصص '{spec.name}' حذف شده است."
                                ),
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logger.error(f"خطا در اطلاع‌رسانی به کاربر {appt.user.telegram_id}: {e}")

                    # Physically remove the appointment from DB
                    session.delete(appt)

                # 2) Delete the Doctor
                session.delete(doctor)

            # 3) Finally, delete the Specialization
            session.delete(spec)

            try:
                session.commit()
                logger.info(f"تخصص '{spec.name}' و پزشکان و ملاقات‌های مرتبط حذف شدند.")
                await update.message.reply_text(
                    f"✅ تخصص '{spec.name}' و تمامی پزشکان و ملاقات‌های مرتبط حذف شدند.",
                    parse_mode="Markdown",
                    reply_markup=developer_menu_keyboard()
                )
            except IntegrityError as e:
                session.rollback()
                logger.error(f"خطا در حذف تخصص '{spec.name}': {e}")
                await update.message.reply_text(
                    "❌ حذف تخصص ناموفق بود. لطفاً دوباره تلاش کنید.",
                    parse_mode="Markdown",
                    reply_markup=developer_menu_keyboard()
                )

        # Cleanup context
        context.user_data.pop('remove_specialization_id', None)
        return DEVELOPER_MENU

    elif confirmation == "خیر":
        await update.message.reply_text(
            "❌ عملیات لغو شد.",
            parse_mode="Markdown",
            reply_markup=developer_menu_keyboard()
        )
        context.user_data.pop('remove_specialization_id', None)
        return DEVELOPER_MENU

    else:
        await update.message.reply_text(
            "لطفاً 'بله' یا 'خیر' را انتخاب کنید.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["بله", "خیر"]], resize_keyboard=True)
        )
        return CONFIRM_REMOVE_SPEC


##################
# Developer: Add Doctor
##################

async def dev_add_doctor_choose_specialization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    spec_name = update.message.text.strip()

    if spec_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی توسعه‌دهنده.*", parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU

    with Session() as session:
        spec = session.query(Specialization).filter_by(name=spec_name).first()
        if not spec:
            await update.message.reply_text("*❌ تخصص پیدا نشد.* لطفاً دوباره انتخاب کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=specialization_keyboard())
            return DEV_ADD_DOCTOR_CHOOSE_SPECIALIZATION
    context.user_data['add_doctor_specialization_id'] = spec.id
    await update.message.reply_text("*🆕 نام پزشک را وارد کنید:*",
                                    parse_mode="Markdown",
                                    reply_markup=back_cancel_menu_keyboard())
    return DEV_ADD_DOCTOR_NAME


async def dev_add_doctor_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc_name = update.message.text.strip()

    if doc_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به انتخاب تخصص.*", parse_mode="Markdown",
                                        reply_markup=specialization_keyboard())
        return DEV_ADD_DOCTOR_CHOOSE_SPECIALIZATION

    if doc_name == "❌ لغو":
        return await cancel(update, context)

    if not doc_name:
        await update.message.reply_text("*❌ نام پزشک نمی‌تواند خالی باشد.* لطفاً یک نام معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return DEV_ADD_DOCTOR_NAME

    with Session() as session:
        spec_id = context.user_data.get('add_doctor_specialization_id')
        existing_doc = session.query(Doctor).filter_by(name=doc_name, specialization_id=spec_id).first()
        if existing_doc:
            await update.message.reply_text(
                "*❌ پزشک در این تخصص قبلاً وجود دارد.* لطفاً یک نام متفاوت وارد کنید:",
                parse_mode="Markdown",
                reply_markup=back_cancel_menu_keyboard())
            return DEV_ADD_DOCTOR_NAME

    context.user_data['add_doctor_name'] = doc_name
    # Ask for availability
    await update.message.reply_text("*🕒 دسترسی پزشک را انتخاب کنید:*", parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardMarkup(
                                        [["حضوری"], ["آنلاین"], ["هر دو"], ["🔙 بازگشت", "❌ لغو"]],
                                        resize_keyboard=True,
                                        one_time_keyboard=True
                                    ))
    return DEV_ADD_DOCTOR_AVAILABILITY


async def dev_add_doctor_availability(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    availability_choice = update.message.text.strip()

    if availability_choice == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به وارد کردن نام پزشک.*", parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return DEV_ADD_DOCTOR_NAME

    if availability_choice == "❌ لغو":
        return await cancel(update, context)

    valid_choices = ["حضوری", "آنلاین", "هر دو"]
    if availability_choice not in valid_choices:
        await update.message.reply_text("*❌ انتخاب نامعتبر.* لطفاً از گزینه‌های موجود انتخاب کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["حضوری"], ["آنلاین"], ["هر دو"], ["🔙 بازگشت", "❌ لغو"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return DEV_ADD_DOCTOR_AVAILABILITY

    spec_id = context.user_data.get('add_doctor_specialization_id')
    doc_name = context.user_data.get('add_doctor_name')

    with Session() as session:
        if availability_choice == "هر دو":
            # Create a single doctor with both availabilities
            doctor = Doctor(
                name=doc_name,
                specialization_id=spec_id,
                in_person_available=True,
                online_available=True
            )
            session.add(doctor)
            success_message = f"✅ *پزشک '{doc_name}' با دسترسی حضوری و آنلاین اضافه شد.*"
            logger.info(f"توسعه‌دهنده پزشک '{doc_name}' را با دسترسی حضوری و آنلاین اضافه کرد.")
        elif availability_choice == "حضوری":
            doctor = Doctor(
                name=doc_name,
                specialization_id=spec_id,
                in_person_available=True,
                online_available=False
            )
            session.add(doctor)
            success_message = f"✅ *پزشک '{doc_name}' با دسترسی حضوری اضافه شد.*"
            logger.info(f"توسعه‌دهنده پزشک '{doc_name}' را با دسترسی حضوری اضافه کرد.")
        elif availability_choice == "آنلاین":
            doctor = Doctor(
                name=doc_name,
                specialization_id=spec_id,
                in_person_available=False,
                online_available=True
            )
            session.add(doctor)
            success_message = f"✅ *پزشک '{doc_name}' با دسترسی آنلاین اضافه شد.*"
            logger.info(f"توسعه‌دهنده پزشک '{doc_name}' را با دسترسی آنلاین اضافه کرد.")

        try:
            session.commit()
            await update.message.reply_text(success_message,
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
        except IntegrityError as e:
            logger.error(f"خطا در افزودن پزشک '{doc_name}': {e}")
            session.rollback()
            await update.message.reply_text("*❌ افزودن پزشک ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU

    # Clear temporary data
    context.user_data.pop('add_doctor_specialization_id', None)
    context.user_data.pop('add_doctor_name', None)
    return DEVELOPER_MENU


##################
# Developer: Remove Doctor
##################

async def dev_remove_doctor_choose_specialization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    spec_name = update.message.text.strip()

    if spec_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی توسعه‌دهنده.*", parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        return DEVELOPER_MENU

    with Session() as session:
        spec = session.query(Specialization).filter_by(name=spec_name).first()
        if not spec:
            await update.message.reply_text("*❌ تخصص پیدا نشد.* لطفاً دوباره انتخاب کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=specialization_keyboard())
            return DEV_REMOVE_SPECIALIZATION_SELECT
        doctors = session.query(Doctor).filter_by(specialization_id=spec.id).all()
        if not doctors:
            await update.message.reply_text("*❌ هیچ پزشکی در این تخصص موجود نیست.*",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU

    context.user_data['remove_doctor_specialization_id'] = spec.id
    keyboard = [[doc.name] for doc in doctors]
    keyboard.append(["🔙 بازگشت"])
    await update.message.reply_text("*لطفاً پزشکی را که می‌خواهید حذف کنید انتخاب کنید:*",
                                    parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return DEV_REMOVE_DOCTOR_SELECT


async def DEV_REMOVE_DOCTOR_SELECT(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc_name = update.message.text.strip()

    if doc_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به انتخاب تخصص.*", parse_mode="Markdown",
                                        reply_markup=specialization_keyboard())
        return DEV_REMOVE_SPECIALIZATION_SELECT

    with Session() as session:
        spec_id = context.user_data.get('remove_doctor_specialization_id')
        doctor = session.query(Doctor).filter_by(name=doc_name, specialization_id=spec_id).first()
        if not doctor:
            await update.message.reply_text("*❌ پزشک پیدا نشد.* لطفاً دوباره انتخاب کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=developer_menu_keyboard())
            return DEVELOPER_MENU
        # Optionally, confirm deletion
        await update.message.reply_text(f"⚠️ *آیا مطمئنید که می‌خواهید پزشک '{doctor.name}' را حذف کنید؟*\n\n"
                                        f"تمام ملاقات‌های مرتبط لغو خواهند شد.",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup([["بله", "خیر"]], resize_keyboard=True))
        context.user_data['remove_doctor_id'] = doctor.id
    return CONFIRM_REMOVE_DOCTOR


async def confirm_remove_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    confirmation = update.message.text.strip()

    if confirmation == "بله":
        doctor_id = context.user_data.get('remove_doctor_id')
        with Session() as session:
            doctor = session.query(Doctor).filter_by(id=doctor_id).first()
            if doctor:
                # Handle appointments before deleting the doctor
                appointments = session.query(Appointment).filter_by(doctor_id=doctor.id).all()
                for appt in appointments:
                    if appt.status in ['pending', 'confirmed']:
                        # Notify the user about the cancellation
                        try:
                            await context.bot.send_message(
                                chat_id=appt.user.telegram_id,
                                text=(
                                    f"⚠️ *ملاقات لغو شد*\n\n"
                                    f"ملاقات شما (شناسه: {appt.id}) با *دکتر {doctor.name}* به دلیل حذف پزشک از سیستم لغو شده است.\n\n"
                                    f"لطفاً برای تنظیم مجدد یا انتخاب پزشک دیگر با ما تماس بگیرید."
                                ),
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logger.error(f"خطا در اطلاع‌رسانی به کاربر {appt.user.telegram_id} در مورد لغو ملاقات: {e}")
                    # Set appointment status to 'canceled'
                    appt.status = 'canceled'
                    logger.info(f"ملاقات {appt.id} مرتبط با پزشک {doctor.id} به 'لغو شده' تغییر وضعیت داد.")

                session.delete(doctor)
                try:
                    session.commit()
                    logger.info(f"پزشک '{doctor.name}' توسط توسعه‌دهنده حذف شد.")
                    await update.message.reply_text(
                        f"✅ *پزشک '{doctor.name}' حذف شد.*\nتمام ملاقات‌های مرتبط لغو شده‌اند.",
                        parse_mode="Markdown",
                        reply_markup=developer_menu_keyboard())
                except IntegrityError as e:
                    logger.error(f"خطا در حذف پزشک '{doctor.name}': {e}")
                    session.rollback()
                    await update.message.reply_text("*❌ حذف پزشک ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                                    parse_mode="Markdown",
                                                    reply_markup=developer_menu_keyboard())
            else:
                await update.message.reply_text("*❌ پزشک پیدا نشد.*",
                                                parse_mode="Markdown",
                                                reply_markup=developer_menu_keyboard())
        context.user_data.pop('remove_doctor_id', None)
        return DEVELOPER_MENU

    elif confirmation == "خیر":
        await update.message.reply_text("*❌ عملیات لغو شد.*",
                                        parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
        context.user_data.pop('remove_doctor_id', None)
        return DEVELOPER_MENU

    else:
        await update.message.reply_text("*لطفاً 'بله' یا 'خیر' را انتخاب کنید.*",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup([["بله", "خیر"]], resize_keyboard=True))
        return CONFIRM_REMOVE_DOCTOR


##################
# Developer: View Statistics
##################

async def view_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with Session() as session:
        total_users = session.query(User).count()
        total_appointments = session.query(Appointment).count()
        pending_appointments = session.query(Appointment).filter_by(status='pending').count()
        confirmed_appointments = session.query(Appointment).filter_by(status='confirmed').count()
        canceled_appointments = session.query(Appointment).filter_by(status='canceled').count()
        rejected_appointments = session.query(Appointment).filter_by(status='rejected').count()

        total_certificates = session.query(HealthCertificate).count()
        pending_certificates = session.query(HealthCertificate).filter_by(status='pending').count()
        approved_certificates = session.query(HealthCertificate).filter_by(status='approved').count()
        rejected_certificates = session.query(HealthCertificate).filter_by(status='rejected').count()

    msg = (
        f"*📊 آمار*\n\n"
        f"👥 *کاربران:* {total_users}\n\n"
        f"📅 *ملاقات‌ها:* {total_appointments}\n"
        f"• *در انتظار:* {pending_appointments}\n"
        f"• *تأیید شده:* {confirmed_appointments}\n"
        f"• *لغو شده:* {canceled_appointments}\n"
        f"• *رد شده:* {rejected_appointments}\n\n"
        f"📜 *گواهی‌های سلامت:* {total_certificates}\n"
        f"• *در انتظار:* {pending_certificates}\n"
        f"• *تأیید شده:* {approved_certificates}\n"
        f"• *رد شده:* {rejected_certificates}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=developer_menu_keyboard())


##################
# Developer: Send Message to User
##################

async def send_message_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_input = update.message.text.strip()
    parts = user_input.split(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("*❌ فرمت نامعتبر.*\n*نحوه استفاده:* `<شناسه کاربر> <پیام>`",
                                        parse_mode="Markdown")
        return DEVELOPER_MENU

    try:
        target_user_id = int(parts[0])
        message = parts[1]
    except ValueError:
        await update.message.reply_text("*❌ شناسه کاربر باید یک عدد باشد.*",
                                        parse_mode="Markdown")
        return DEVELOPER_MENU

    try:
        await context.bot.send_message(chat_id=target_user_id, text=message)
        logger.info(f"توسعه‌دهنده پیام به کاربر {target_user_id} ارسال کرد: {message}")
        await update.message.reply_text("*✅ پیام با موفقیت ارسال شد.*",
                                        parse_mode="Markdown",
                                        reply_markup=developer_menu_keyboard())
    except Exception as e:
        logger.error(f"خطا در ارسال پیام به کاربر {target_user_id}: {e}")
        await update.message.reply_text(
            "*❌ ارسال پیام ناموفق بود. اطمینان حاصل کنید که شناسه کاربر صحیح است و کاربر ربات را شروع کرده باشد.*",
            parse_mode="Markdown",
            reply_markup=developer_menu_keyboard())

    return DEVELOPER_MENU


##################
# Developer: Contact User
##################

async def send_message_to_developer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message.text.strip()

    if message == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(update.effective_user.id))
        return MAIN_MENU

    if message == "❌ لغو":
        return await cancel(update, context)

    if not message:
        await update.message.reply_text("*❌ پیام نمی‌تواند خالی باشد.* لطفاً یک پیام معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return SEND_MESSAGE_TO_DEVELOPER

    # Forward the message to the developer
    try:
        await context.bot.send_message(
            chat_id=DEVELOPER_CHAT_ID,
            text=f"✉️ *پیام از کاربر (شناسه: {update.effective_user.id}):*\n\n{message}",
            parse_mode="Markdown"
        )
        logger.info(f"کاربر {update.effective_user.id} پیام به توسعه‌دهنده ارسال کرد.")
        await update.message.reply_text("*✅ پیام شما به ما ارسال شد. به زودی پاسخ خواهیم داد.*",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(update.effective_user.id))
    except Exception as e:
        logger.error(f"خطا در ارسال پیام به توسعه‌دهنده: {e}")
        await update.message.reply_text("*❌ ارسال پیام به توسعه‌دهنده ناموفق بود. لطفاً بعداً تلاش کنید.*",
                                        parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(update.effective_user.id))

    return MAIN_MENU


##################
# Developer Inline Actions
##################

async def developer_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("confirm_appt_"):
        appt_id = int(data.split("_")[2])
        await confirm_appointment(update, context, appt_id)
    elif data.startswith("reject_appt_"):
        appt_id = int(data.split("_")[2])
        await reject_appointment(update, context, appt_id)
    elif data.startswith("approve_cert_"):
        cert_id = int(data.split("_")[2])
        await approve_certificate(update, context, cert_id)
    elif data.startswith("reject_cert_"):
        cert_id = int(data.split("_")[2])
        await reject_certificate(update, context, cert_id)


async def confirm_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    query = update.callback_query
    logger.debug(f"توسعه‌دهنده در حال تأیید ملاقات شناسه: {appt_id}")

    with Session() as session:
        appt = session.query(Appointment).filter_by(id=appt_id).first()
        if appt and appt.status == 'pending':
            appt.status = 'confirmed'
            try:
                session.commit()
                logger.info(f"ملاقات {appt_id} تأیید شد.")
            except IntegrityError as e:
                logger.error(f"خطا در تأیید ملاقات {appt_id}: {e}")
                session.rollback()
                await query.edit_message_text("*❌ تأیید ملاقات ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                              parse_mode="Markdown")
                return
            user = appt.user
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f"✅ *ملاقات شما (شناسه: {appt_id}) تأیید شد.*\n\n"
                        f"• *پزشک:* {appt.doctor.name}\n"
                        f"• *تخصص:* {appt.appointment_type}\n"
                        f"• *روش ارتباط:* {appt.contact_method}\n\n"
                        f"از انتخاب *Doctor Line* متشکریم!"
                    ),
                    parse_mode="Markdown"
                )
                logger.info(f"کاربر {user.telegram_id} در مورد تأیید ملاقات مطلع شد.")
            except Exception as e:
                logger.error(f"خطا در اطلاع‌رسانی به کاربر {user.telegram_id} در مورد تأیید ملاقات: {e}")

            # Send confirmation email to user
            email_subject = "✅ ملاقات تأیید شد"
            email_body = (
                f"سلام {user.name},\n\n"
                f"خبر خوب! درخواست ملاقات شما (شناسه: {appt_id}) با *دکتر {appt.doctor.name}* تأیید شد.\n\n"
                f"• *تخصص:* {appt.appointment_type}\n"
                f"• *روش ارتباط:* {appt.contact_method}\n\n"
                f"منتظر کمک به شما هستیم.\n\n"
                f"📅 *تاریخ ملاقات:* {appt.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"از انتخاب *Doctor Line* متشکریم.\n\n"
                f"با احترام,\n*تیم Doctor Line*"
            )
            send_email(user.email, email_subject, email_body)

            await query.edit_message_text(
                text=f"✅ *ملاقات {appt_id} تأیید شد.*\n*کاربر:* {user.name}\n*پزشک:* {appt.doctor.name}",
                parse_mode="Markdown"
            )
        else:
            logger.warning(f"ملاقات {appt_id} نامعتبر یا قبلاً پردازش شده است.")
            await query.edit_message_text("*❌ شناسه ملاقات نامعتبر یا قبلاً پردازش شده است.*",
                                          parse_mode="Markdown")


async def reject_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE, appt_id: int):
    query = update.callback_query
    logger.debug(f"توسعه‌دهنده در حال رد ملاقات شناسه: {appt_id}")

    with Session() as session:
        appt = session.query(Appointment).filter_by(id=appt_id).first()
        if appt and appt.status == 'pending':
            appt.status = 'rejected'
            try:
                session.commit()
                logger.info(f"ملاقات {appt_id} رد شد.")
            except IntegrityError as e:
                logger.error(f"خطا در رد ملاقات {appt_id}: {e}")
                session.rollback()
                await query.edit_message_text("*❌ رد ملاقات ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                              parse_mode="Markdown")
                return
            user = appt.user
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ *ملاقات شما (شناسه: {appt_id}) رد شد.*"
                )
                logger.info(f"کاربر {user.telegram_id} در مورد رد ملاقات مطلع شد.")
            except Exception as e:
                logger.error(f"خطا در اطلاع‌رسانی به کاربر {user.telegram_id} در مورد رد ملاقات: {e}")

            # Send rejection email to user
            email_subject = "❌ ملاقات رد شد"
            email_body = (
                f"سلام {user.name},\n\n"
                f"با تاسف اعلام می‌کنیم که درخواست ملاقات شما (شناسه: {appt_id}) "
                f"با *دکتر {appt.doctor.name}* رد شده است.\n\n"
                f"اگر فکر می‌کنید این اشتباه است یا می‌خواهید مجدداً تنظیم کنید، لطفاً با ما تماس بگیرید.\n\n"
                f"از درک شما متشکریم.\n\n"
                f"با احترام,\n*تیم Doctor Line*"
            )
            send_email(user.email, email_subject, email_body)

            await query.edit_message_text(
                text=f"❌ *ملاقات {appt_id} رد شد.*\n*کاربر:* {user.name}",
                parse_mode="Markdown"
            )
        else:
            logger.warning(f"ملاقات {appt_id} نامعتبر یا قبلاً پردازش شده است.")
            await query.edit_message_text("*❌ شناسه ملاقات نامعتبر یا قبلاً پردازش شده است.*",
                                          parse_mode="Markdown")


async def approve_certificate(update: Update, context: ContextTypes.DEFAULT_TYPE, cert_id: int):
    query = update.callback_query
    logger.debug(f"توسعه‌دهنده در حال تأیید گواهی سلامت شناسه: {cert_id}")

    with Session() as session:
        cert = session.query(HealthCertificate).filter_by(id=cert_id).first()
        if cert and cert.status == 'pending':
            cert.status = 'approved'
            try:
                session.commit()
                logger.info(f"گواهی سلامت {cert_id} تأیید شد.")
            except IntegrityError as e:
                logger.error(f"خطا در تأیید گواهی سلامت {cert_id}: {e}")
                session.rollback()
                await query.edit_message_text("*❌ تأیید گواهی سلامت ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                              parse_mode="Markdown")
                return
            user = cert.user
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f"✅ *درخواست گواهی سلامت شما (شناسه: {cert_id}) تأیید شد.*\n\n"
                        f"از انتخاب *Doctor Line* متشکریم!"
                    ),
                    parse_mode="Markdown"
                )
                logger.info(f"کاربر {user.telegram_id} در مورد تأیید گواهی سلامت مطلع شد.")
            except Exception as e:
                logger.error(f"خطا در اطلاع‌رسانی به کاربر {user.telegram_id} در مورد تأیید گواهی سلامت: {e}")

            # Send approval email to user
            email_subject = "✅ گواهی سلامت تأیید شد"
            email_body = (
                f"سلام {user.name},\n\n"
                f"تبریک! درخواست گواهی سلامت شما (شناسه: {cert_id}) تأیید شد.\n\n"
                f"• *دلیل:* {cert.reason}\n"
                f"• *توضیح:* {cert.description}\n\n"
                f"شما اکنون می‌توانید با هرگونه نیازمندی لازم ادامه دهید.\n\n"
                f"از انتخاب *Doctor Line* متشکریم.\n\n"
                f"با احترام,\n*تیم Doctor Line*"
            )
            send_email(user.email, email_subject, email_body)

            await query.edit_message_text(
                text=f"✅ *گواهی سلامت {cert_id} تأیید شد.*\n*کاربر:* {user.name}",
                parse_mode="Markdown"
            )
        else:
            logger.warning(f"گواهی سلامت {cert_id} نامعتبر یا قبلاً پردازش شده است.")
            await query.edit_message_text("*❌ شناسه گواهی سلامت نامعتبر یا قبلاً پردازش شده است.*",
                                          parse_mode="Markdown")


async def reject_certificate(update: Update, context: ContextTypes.DEFAULT_TYPE, cert_id: int):
    query = update.callback_query
    logger.debug(f"توسعه‌دهنده در حال رد گواهی سلامت شناسه: {cert_id}")

    with Session() as session:
        cert = session.query(HealthCertificate).filter_by(id=cert_id).first()
        if cert and cert.status == 'pending':
            cert.status = 'rejected'
            try:
                session.commit()
                logger.info(f"گواهی سلامت {cert_id} رد شد.")
            except IntegrityError as e:
                logger.error(f"خطا در رد گواهی سلامت {cert_id}: {e}")
                session.rollback()
                await query.edit_message_text("*❌ رد گواهی سلامت ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                              parse_mode="Markdown")
                return
            user = cert.user
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ *درخواست گواهی سلامت شما (شناسه: {cert_id}) رد شد.*"
                )
                logger.info(f"کاربر {user.telegram_id} در مورد رد گواهی سلامت مطلع شد.")
            except Exception as e:
                logger.error(f"خطا در اطلاع‌رسانی به کاربر {user.telegram_id} در مورد رد گواهی سلامت: {e}")

            # Send rejection email to user
            email_subject = "❌ گواهی سلامت رد شد"
            email_body = (
                f"سلام {user.name},\n\n"
                f"با تاسف اعلام می‌کنیم که درخواست گواهی سلامت شما (شناسه: {cert_id}) "
                f"رد شده است.\n\n"
                f"اگر فکر می‌کنید این اشتباه است یا می‌خواهید دوباره درخواست دهید، لطفاً با ما تماس بگیرید.\n\n"
                f"از درک شما متشکریم.\n\n"
                f"با احترام,\n*تیم Doctor Line*"
            )
            send_email(user.email, email_subject, email_body)

            await query.edit_message_text(
                text=f"❌ *گواهی سلامت {cert_id} رد شد.*\n*کاربر:* {user.name}",
                parse_mode="Markdown"
            )
        else:
            logger.warning(f"گواهی سلامت {cert_id} نامعتبر یا قبلاً پردازش شده است.")
            await query.edit_message_text("*❌ شناسه گواهی سلامت نامعتبر یا قبلاً پردازش شده است.*",
                                          parse_mode="Markdown")


##################
# Profile Editing Handlers
##################

async def edit_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if choice == "ویرایش نام":
        await update.message.reply_text("*📝 نام جدید خود را وارد کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return EDIT_NAME
    elif choice == "ویرایش تلفن/شناسه":
        await update.message.reply_text("*📱 شماره تلفن یا شناسه جدید خود را وارد کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return EDIT_PHONE
    elif choice == "ویرایش ایمیل":
        await update.message.reply_text("*📧 آدرس ایمیل جدید خود را وارد کنید:*",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return EDIT_EMAIL
    elif choice == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی اصلی.*", parse_mode="Markdown",
                                        reply_markup=main_menu_keyboard(user_id))
        return MAIN_MENU
    elif choice == "❌ لغو":
        return await cancel(update, context)
    else:
        await update.message.reply_text("*❌ انتخاب نامعتبر است.* لطفاً یک گزینه از منوی ویرایش پروفایل را انتخاب کنید.",
                                        parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["ویرایش نام"], ["ویرایش تلفن/شناسه"], ["ویرایش ایمیل"], ["🔙 بازگشت"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return EDIT_PROFILE_MENU


async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = update.message.text.strip()
    user_id = update.effective_user.id

    if new_name == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی ویرایش پروفایل.*", parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["ویرایش نام"], ["ویرایش تلفن/شناسه"], ["ویرایش ایمیل"], ["🔙 بازگشت"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return EDIT_PROFILE_MENU

    if new_name == "❌ لغو":
        return await cancel(update, context)

    if not new_name:
        await update.message.reply_text("*❌ نام نمی‌تواند خالی باشد.* لطفاً یک نام معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return EDIT_NAME

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.name = new_name
            try:
                session.commit()
                logger.info(f"کاربر {user_id} نام خود را به '{new_name}' به‌روزرسانی کرد.")
                await update.message.reply_text("*✅ نام با موفقیت به‌روزرسانی شد.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
            except IntegrityError as e:
                logger.error(f"خطا در به‌روزرسانی نام کاربر {user_id}: {e}")
                session.rollback()
                await update.message.reply_text("*❌ به‌روزرسانی نام ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
        else:
            await update.message.reply_text("*❌ کاربر پیدا نشد. لطفاً ابتدا ثبت‌نام کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
    return MAIN_MENU


async def edit_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_phone = update.message.text.strip()
    user_id = update.effective_user.id

    if new_phone == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی ویرایش پروفایل.*", parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["ویرایش نام"], ["ویرایش تلفن/شناسه"], ["ویرایش ایمیل"], ["🔙 بازگشت"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return EDIT_PROFILE_MENU

    if new_phone == "❌ لغو":
        return await cancel(update, context)

    if not new_phone:
        await update.message.reply_text("*❌ شماره تلفن/شناسه نمی‌تواند خالی باشد.* لطفاً یک شماره معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return EDIT_PHONE

    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.phone = new_phone
            try:
                session.commit()
                logger.info(f"کاربر {user_id} شماره تلفن/شناسه خود را به '{new_phone}' به‌روزرسانی کرد.")
                await update.message.reply_text("*✅ شماره تلفن/شناسه با موفقیت به‌روزرسانی شد.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
            except IntegrityError as e:
                logger.error(f"خطا در به‌روزرسانی شماره تلفن/شناسه کاربر {user_id}: {e}")
                session.rollback()
                await update.message.reply_text("*❌ به‌روزرسانی شماره تلفن/شناسه ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
        else:
            await update.message.reply_text("*❌ کاربر پیدا نشد. لطفاً ابتدا ثبت‌نام کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
    return MAIN_MENU


async def edit_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_email = update.message.text.strip()
    user_id = update.effective_user.id

    if new_email == "🔙 بازگشت":
        await update.message.reply_text("*🔙 بازگشت به منوی ویرایش پروفایل.*", parse_mode="Markdown",
                                        reply_markup=ReplyKeyboardMarkup(
                                            [["ویرایش نام"], ["ویرایش تلفن/شناسه"], ["ویرایش ایمیل"], ["🔙 بازگشت"]],
                                            resize_keyboard=True,
                                            one_time_keyboard=True
                                        ))
        return EDIT_PROFILE_MENU

    if new_email == "❌ لغو":
        return await cancel(update, context)

    if not EMAIL_REGEX.match(new_email):
        logger.warning(f"کاربر {user_id} فرمت ایمیل نامعتبری را در هنگام ویرایش پروفایل ارائه داد: {new_email}")
        await update.message.reply_text("*❌ فرمت ایمیل نامعتبر است.* لطفاً یک ایمیل معتبر وارد کنید:",
                                        parse_mode="Markdown",
                                        reply_markup=back_cancel_menu_keyboard())
        return EDIT_EMAIL

    with Session() as session:
        existing_user = session.query(User).filter(User.email == new_email, User.telegram_id != user_id).first()
        if existing_user:
            await update.message.reply_text("*❌ این ایمیل قبلاً استفاده شده است.* لطفاً یک ایمیل متفاوت وارد کنید:",
                                            parse_mode="Markdown",
                                            reply_markup=back_cancel_menu_keyboard())
            return EDIT_EMAIL
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.email = new_email
            try:
                session.commit()
                logger.info(f"کاربر {user_id} ایمیل خود را به '{new_email}' به‌روزرسانی کرد.")
                await update.message.reply_text("*✅ ایمیل با موفقیت به‌روزرسانی شد.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
            except IntegrityError as e:
                logger.error(f"خطا در به‌روزرسانی ایمیل کاربر {user_id}: {e}")
                session.rollback()
                await update.message.reply_text("*❌ به‌روزرسانی ایمیل ناموفق بود. لطفاً دوباره تلاش کنید.*",
                                                parse_mode="Markdown",
                                                reply_markup=main_menu_keyboard(user_id))
        else:
            await update.message.reply_text("*❌ کاربر پیدا نشد. لطفاً ابتدا ثبت‌نام کنید.*",
                                            parse_mode="Markdown",
                                            reply_markup=main_menu_keyboard(user_id))
    return MAIN_MENU


##################
# Conversation Handler
##################

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('start', start)],
    states={
        REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
        REGISTER_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_email)],
        REGISTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_phone)],

        MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)],

        APPOINTMENT_CHOOSE_SPECIALIZATION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, appointment_choose_specialization)],
        APPOINTMENT_CHOOSE_DOCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, appointment_choose_doctor)],
        APPOINTMENT_CONTACT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, appointment_contact_method)],
        APPOINTMENT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, appointment_description)],

        CERTIFICATE_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, certificate_reason)],
        CERTIFICATE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, certificate_description)],

        EDIT_PROFILE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_profile_menu)],
        EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name)],
        EDIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_phone)],
        EDIT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_email)],

        DEVELOPER_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, developer_menu_handler)],
        DEV_MANAGE_SPECIALIZATIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, dev_manage_specializations)],
        DEV_ADD_SPECIALIZATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, dev_add_specialization)],
        DEV_REMOVE_SPECIALIZATION_SELECT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, dev_remove_specialization_select)],
        CONFIRM_REMOVE_SPEC: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_remove_spec)],

        DEV_ADD_DOCTOR_CHOOSE_SPECIALIZATION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, dev_add_doctor_choose_specialization)],
        DEV_ADD_DOCTOR_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, dev_add_doctor_name)],
        DEV_ADD_DOCTOR_AVAILABILITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, dev_add_doctor_availability)],

        DEV_REMOVE_DOCTOR_CHOOSE_SPECIALization: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, dev_remove_doctor_choose_specialization)],
        DEV_REMOVE_DOCTOR_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, DEV_REMOVE_DOCTOR_SELECT)],
        CONFIRM_REMOVE_DOCTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_remove_doctor)],

        SEND_MESSAGE_TO_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_message_to_user)],
        SEND_MESSAGE_TO_DEVELOPER: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_message_to_developer)],

        PAYMENT_APPOINTMENT_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, payment_appointment_id_handler)
        ],
        PAYMENT_RECEIPT: [
            MessageHandler(filters.PHOTO, payment_receipt_handler),
            MessageHandler(filters.TEXT & ~filters.COMMAND, payment_receipt_handler)  # Handle "Back" and "Cancel"
        ]
    },
    fallbacks=[
        CommandHandler('cancel', cancel),
        CommandHandler('restart', restart)
    ],
    allow_reentry=True
)


##################
# Application Setup
##################

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(conv_handler)
application.add_handler(
    CallbackQueryHandler(developer_action_handler,
                         pattern=r"^(confirm_appt_|reject_appt_|approve_cert_|reject_cert_)\d+$")
)
application.add_handler(CommandHandler('sendmsg', send_message_to_user, filters=filters.User(DEVELOPER_CHAT_ID)))


##################
# Temporary Handlers for Verification
##################

# Temporary command to get developer's chat ID
async def get_developer_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"شناسه چت توسعه‌دهنده: {chat_id}")
    await update.message.reply_text(f"📢 *شناسه چت شما:* `{chat_id}`",
                                    parse_mode="Markdown")


# Temporary command to send test receipt to developer
async def send_test_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != DEVELOPER_CHAT_ID:
        await update.message.reply_text("*❌ شما مجاز به استفاده از این دستور نیستید.*",
                                        parse_mode="Markdown")
        return
    try:
        test_photo_path = "test_receipt.jpg"  # Ensure this file exists in your project directory
        if not os.path.exists(test_photo_path):
            await update.message.reply_text(
                "*❌ فایل رسید تست پیدا نشد.* لطفاً اطمینان حاصل کنید که 'test_receipt.jpg' در دایرکتوری ربات موجود است.",
                parse_mode="Markdown")
            return
        with open(test_photo_path, 'rb') as photo_file:
            await context.bot.send_photo(
                chat_id=DEVELOPER_CHAT_ID,
                photo=photo_file,
                caption="📷 *رسید تست*",
                parse_mode="Markdown"
            )
        await update.message.reply_text("*✅ رسید تست به توسعه‌دهنده ارسال شد.*",
                                        parse_mode="Markdown")
    except Exception as e:
        logger.error(f"خطا در ارسال رسید تست: {e}")
        await update.message.reply_text("*❌ ارسال رسید تست ناموفق بود.*",
                                        parse_mode="Markdown")


# Add temporary handlers (Remove these after verification)
application.add_handler(CommandHandler('getdevid', get_developer_id, filters=filters.User(DEVELOPER_CHAT_ID)))
application.add_handler(CommandHandler('sendtestreceipt', send_test_receipt, filters=filters.User(DEVELOPER_CHAT_ID)))


##################
# Scheduler (Optional)
##################

scheduler.start()
logger.info("زمان‌بندی شروع شد.")


##################
# Shutdown Handler
##################

def shutdown_handler(signum, frame):
    logger.info("در حال خاموش‌سازی...")
    scheduler.shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

##################
# Main Execution
##################

if __name__ == '__main__':
    # Make sure tables exist before starting the bot
    init_db()  # <-- Call init_db() here

    logger.info("شروع ربات...")
    try:
        application.run_polling()
    except KeyboardInterrupt:
        logger.info("ربات توسط KeyboardInterrupt متوقف شد.")
        scheduler.shutdown()
