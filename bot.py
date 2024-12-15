import logging
import os
import re
import signal
import sys
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, DateTime, ForeignKey, Integer, String, Boolean
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    CallbackContext,
)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize database
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    email = Column(String, unique=True, nullable=False)
    appointments = relationship("Appointment", back_populates="user")

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    appointment_datetime = Column(DateTime, nullable=True)  # None for urgent/suggested
    status = Column(String, default="pending")  # pending, confirmed, canceled
    receipt_file_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    mode = Column(String, nullable=True)  # in_person or phone
    type = Column(String, nullable=True)  # urgent, slots, suggest
    suggested_date = Column(DateTime, nullable=True)  # For suggested appointments
    user = relationship("User", back_populates="appointments")

class AvailableSlot(Base):
    __tablename__ = "available_slots"
    id = Column(Integer, primary_key=True)
    slot_datetime = Column(DateTime, unique=True, nullable=False)

# Create SQLite engine and session
engine = create_engine("sqlite:///database.db", echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PAYPAL_ME_LINK = os.getenv("PAYPAL_ME_LINK")
DEVELOPER_CHAT_ID = int(os.getenv("DEVELOPER_CHAT_ID", "0"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.example.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "user@example.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "password")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not set. Check your .env file.")
if not PAYPAL_ME_LINK:
    raise ValueError("PAYPAL_ME_LINK not set. Check your .env file.")
if DEVELOPER_CHAT_ID == 0:
    raise ValueError("DEVELOPER_CHAT_ID not set correctly. Check your .env file.")

CONSULTATION_PRICE_EUR = 9.00

# Define Conversation States
(
    REGISTER_NAME,
    REGISTER_PHONE,
    REGISTER_EMAIL,
    MAIN_MENU,
    APPOINTMENT_MODE,
    APPOINTMENT_OPTIONS,
    GET_APPOINTMENT_DATE,
    GET_APPOINTMENT_TIME,
    PAYMENT_RECEIVED,
    RESCHEDULE_SELECT_APPOINTMENT,
    RESCHEDULE_NEW_DATE,
    RESCHEDULE_NEW_TIME,
    CANCEL_SELECT_APPOINTMENT,
    MANAGE_SLOTS,
    ADD_SLOT,
    REMOVE_SLOT,
    PROFILE_MENU,
    EDIT_NAME,
    EDIT_PHONE,
    EDIT_EMAIL,
    DEVELOPER_MENU,
    DEV_CANCEL_APPT_MENU,
    CONTACT_ADMIN,
    GET_SUGGESTED_DATE,
    GET_SUGGESTED_TIME,
) = range(24)

# Initialize Scheduler
scheduler = AsyncIOScheduler()

# Email Regex
EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")

# Create Receipts Directory
RECEIPTS_DIR = "receipts"
os.makedirs(RECEIPTS_DIR, exist_ok=True)

# Keyboard Definitions
def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        ["📅 Get Appointment"],
        ["📝 Appointment History"],
        ["🔄 Reschedule Appointment"],
        ["❌ Cancel Appointment"],
        ["👤 View Profile"],
        ["✏️ Edit Profile"],
        ["📞 Contact Us"]
    ]
    if user_id == DEVELOPER_CHAT_ID:
        keyboard.append(["🛠 Developer Menu"])
    keyboard.append(["🔄 Restart"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def appointment_mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["In-Person", "Phone"],
        ["🔙 Back", "❌ Cancel"]
    ], resize_keyboard=True, one_time_keyboard=True)

def appointment_options_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["Urgent", "Available Slots"],
        ["Suggest a Time"],
        ["🔙 Back", "❌ Cancel"]
    ], resize_keyboard=True, one_time_keyboard=True)

def cancel_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True, one_time_keyboard=True)

def back_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["🔙 Back"],
        ["❌ Cancel"]
    ], resize_keyboard=True, one_time_keyboard=True)

def date_selection_keyboard() -> ReplyKeyboardMarkup:
    with Session() as session:
        slots = session.query(AvailableSlot).filter(AvailableSlot.slot_datetime >= datetime.now()).order_by(AvailableSlot.slot_datetime).all()
    unique_dates = sorted(set(slot.slot_datetime.date() for slot in slots))
    keyboard = [[d.strftime("%Y-%m-%d")] for d in unique_dates]
    keyboard.append(["🔙 Back"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def time_selection_keyboard(selected_date: datetime.date) -> ReplyKeyboardMarkup:
    with Session() as session:
        slots = session.query(AvailableSlot).filter(
            AvailableSlot.slot_datetime >= datetime.now(),
            AvailableSlot.slot_datetime.between(
                datetime.combine(selected_date, datetime.min.time()),
                datetime.combine(selected_date, datetime.max.time())
            )
        ).order_by(AvailableSlot.slot_datetime).all()
    available_times = [slot.slot_datetime.time().strftime("%H:%M") for slot in slots]
    keyboard = [[t] for t in available_times]
    keyboard.append(["🔙 Back"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

def manage_slots_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["➕ Add Slot"],
        ["➖ Remove Slot"],
        ["🔙 Back"]
    ], resize_keyboard=True, one_time_keyboard=True)

def developer_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["⚙️ Manage Slots"],
        ["📊 Statistics"],
        ["❌ Dev Cancel Appointment"],
        ["🔙 Back"]
    ], resize_keyboard=True, one_time_keyboard=True)

def developer_action_buttons(appointment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{appointment_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{appointment_id}")
        ]
    ])

# Utility Functions
async def send_and_delete_previous(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None, parse_mode="Markdown"):
    chat_id = update.effective_chat.id
    message = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    if 'last_bot_message_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data['last_bot_message_id'])
        except Exception as e:
            logger.warning(f"Failed to delete message: {e}")
    context.user_data['last_bot_message_id'] = message.message_id

def send_email(to_email: str, subject: str, body: str):
    if not EMAIL_REGEX.match(to_email):
        logger.error(f"Invalid email address: {to_email}")
        return
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = SMTP_USER
    message["To"] = to_email
    message.attach(MIMEText(body, "plain"))
    context = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        logger.info(f"Sent email to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")

async def send_reminder_message(application, chat_id, appointment):
    if appointment.appointment_datetime:
        message = (
            f"🔔 *Reminder*\n\n"
            f"You have an upcoming appointment on {appointment.appointment_datetime.strftime('%Y-%m-%d %H:%M')}.\n"
            "Please ensure you're ready for your consultation."
        )
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="Markdown",
            )
            logger.info(f"Sent reminder to user ID {chat_id} for appointment {appointment.id}")
        except Exception as e:
            logger.error(f"Failed to send reminder: {e}")

async def send_reminder(user_id: int, appointment_id: int, application):
    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id).first()
        if appointment and appointment.appointment_datetime:
            reminder_time = appointment.appointment_datetime - timedelta(hours=1)
            if reminder_time > datetime.utcnow():
                scheduler.add_job(
                    send_reminder_message,
                    DateTrigger(run_date=reminder_time),
                    args=[application, appointment.user.telegram_id, appointment]
                )
                logger.info(f"Scheduled reminder for appointment {appointment_id} at {reminder_time}")

def send_cancellation_email(appointment: Appointment):
    user = appointment.user
    subject = "Your Appointment Cancellation"
    body = (
        f"Hello {user.name},\n\n"
        "We regret to inform you that your upcoming appointment has been canceled.\n"
        "If you have any questions, please contact support.\n\n"
        "Best Regards,\nYour Medical Team"
    )
    send_email(user.email, subject, body)

# Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    with Session() as session:
        existing_user = session.query(User).filter_by(telegram_id=user.id).first()
    if existing_user:
        greeting = f"👋 Welcome back, {existing_user.name}!"
        await send_and_delete_previous(
            update,
            context,
            greeting,
            reply_markup=main_menu_keyboard(existing_user.telegram_id)
        )
        logger.info(f"Existing user {existing_user.name} started the bot.")
    else:
        greeting = "👋 Welcome to the Medical Appointment Booking Bot!\n\nUse the menu below to navigate."
        await send_and_delete_previous(
            update,
            context,
            greeting,
            reply_markup=main_menu_keyboard(0)
        )
        logger.info(f"New user with Telegram ID {user.id} started the bot.")
    return MAIN_MENU

async def register_user_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Check if the user is registered. If not, initiate registration.
    Return True if registration started, False if already registered.
    """
    user_id = update.effective_user.id
    with Session() as session:
        existing_user = session.query(User).filter_by(telegram_id=user_id).first()
    if existing_user:
        return False
    else:
        await send_and_delete_previous(
            update,
            context,
            "It seems you're not registered yet. Let's get you registered.\n\nPlease enter your full name:",
            reply_markup=cancel_menu_keyboard()
        )
        return True

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Please enter a valid name.", reply_markup=cancel_menu_keyboard())
        return REGISTER_NAME
    context.user_data["name"] = name
    await send_and_delete_previous(
        update,
        context,
        "📱 Great! Now, please provide your phone number or ID:",
        reply_markup=cancel_menu_keyboard()
    )
    return REGISTER_PHONE

async def register_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not phone:
        await update.message.reply_text("❌ Please enter a valid phone number or ID.", reply_markup=cancel_menu_keyboard())
        return REGISTER_PHONE
    context.user_data["phone"] = phone
    await send_and_delete_previous(
        update,
        context,
        "✨ Almost done! Please provide your email address:",
        reply_markup=cancel_menu_keyboard()
    )
    return REGISTER_EMAIL

async def register_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    email = update.message.text.strip()
    if not EMAIL_REGEX.match(email):
        await update.message.reply_text("❌ Please enter a valid email address.", reply_markup=cancel_menu_keyboard())
        return REGISTER_EMAIL
    context.user_data["email"] = email
    user_id = update.effective_user.id
    with Session() as session:
        existing_email = session.query(User).filter_by(email=email).first()
        if existing_email:
            await update.message.reply_text(
                "⚠️ This email is already registered. Please use a different email or contact support.",
                reply_markup=cancel_menu_keyboard()
            )
            return REGISTER_EMAIL
        new_user = User(
            telegram_id=user_id,
            name=context.user_data["name"],
            phone=context.user_data["phone"],
            email=email
        )
        try:
            session.add(new_user)
            session.commit()
            logger.info(f"Registered new user: {new_user.name}")
            # Send welcome email
            send_email(
                new_user.email,
                "Welcome to Medical Appointment Booking",
                f"Hello {new_user.name},\n\nThank you for registering with our Medical Appointment Booking Bot.\n\nBest Regards,\nYour Medical Team"
            )
        except IntegrityError:
            session.rollback()
            await update.message.reply_text(
                "❌ An error occurred during registration. Please try again.",
                reply_markup=cancel_menu_keyboard()
            )
            return REGISTER_EMAIL
    # After successful registration, proceed to appointment options if appointment was being booked
    if context.user_data.get("appointment_mode"):
        await send_and_delete_previous(
            update,
            context,
            "✅ Registration successful! Now, please choose what you want to do:",
            reply_markup=appointment_options_keyboard()
        )
        return APPOINTMENT_OPTIONS
    else:
        await send_and_delete_previous(
            update,
            context,
            "✅ Registration successful!",
            reply_markup=main_menu_keyboard(new_user.telegram_id)
        )
        return MAIN_MENU

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_choice = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if user_choice == "📅 Get Appointment":
        if user:
            # Already registered, proceed to appointment mode selection
            await send_and_delete_previous(
                update,
                context,
                "Please choose your appointment mode:",
                reply_markup=appointment_mode_keyboard()
            )
            return APPOINTMENT_MODE
        else:
            # Not registered, initiate appointment mode selection which will trigger registration
            await send_and_delete_previous(
                update,
                context,
                "Please choose your appointment mode:",
                reply_markup=appointment_mode_keyboard()
            )
            return APPOINTMENT_MODE

    elif user_choice == "📝 Appointment History":
        await view_history(update, context)
        return MAIN_MENU

    elif user_choice == "🔄 Reschedule Appointment":
        await select_appointment_to_reschedule(update, context)
        return RESCHEDULE_SELECT_APPOINTMENT

    elif user_choice == "❌ Cancel Appointment":
        await select_appointment_to_cancel(update, context)
        return CANCEL_SELECT_APPOINTMENT

    elif user_choice == "👤 View Profile":
        await view_profile(update, context)
        return MAIN_MENU

    elif user_choice == "✏️ Edit Profile":
        await edit_profile_start(update, context)
        return PROFILE_MENU

    elif user_choice == "📞 Contact Us":
        await update.message.reply_text(
            "✉️ Please type your message for the admin:",
            reply_markup=cancel_menu_keyboard()
        )
        return CONTACT_ADMIN

    elif user_choice == "🔄 Restart":
        return await restart(update, context)

    elif user_choice == "🛠 Developer Menu" and user_id == DEVELOPER_CHAT_ID:
        await send_and_delete_previous(
            update,
            context,
            "🛠 *Developer Menu*\n\nChoose an action:",
            reply_markup=developer_menu_keyboard(),
            parse_mode="Markdown"
        )
        return DEVELOPER_MENU

    else:
        # Invalid option
        if not user:
            await update.message.reply_text(
                "❓ Invalid option. Please restart with /start",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END
        await update.message.reply_text(
            "❓ Invalid option selected. Please choose from the menu.",
            reply_markup=main_menu_keyboard(user.telegram_id)
        )
        return MAIN_MENU

async def appointment_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip().lower()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if choice in ["in-person", "phone"]:
        context.user_data["appointment_mode"] = "in_person" if choice == "in-person" else "phone"
        if user:
            # User is already registered, proceed to appointment options
            await send_and_delete_previous(
                update,
                context,
                "Great! Now, please choose what you want to do:",
                reply_markup=appointment_options_keyboard()
            )
            return APPOINTMENT_OPTIONS
        else:
            # User is not registered, initiate registration
            await send_and_delete_previous(
                update,
                context,
                "It seems you're not registered yet. Let's get you registered.\n\nPlease enter your full name:",
                reply_markup=cancel_menu_keyboard()
            )
            return REGISTER_NAME

    elif choice == "🔙 back":
        # Go back to main menu
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the main menu.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    elif choice == "❌ cancel":
        # Cancel operation and go back to main menu
        await send_and_delete_previous(
            update,
            context,
            "🚫 Operation cancelled.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    else:
        # Invalid choice
        await update.message.reply_text(
            "❌ Invalid choice. Please select In-Person, Phone, Back, or Cancel.",
            reply_markup=appointment_mode_keyboard()
        )
        return APPOINTMENT_MODE

async def appointment_options_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip().lower()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if choice == "urgent":
        context.user_data["appointment_type"] = "urgent"
        with Session() as session:
            new_appointment = Appointment(
                user_id=user.id,
                mode=context.user_data["appointment_mode"],
                type="urgent",
                status="pending"
            )
            session.add(new_appointment)
            session.commit()
            details = (
                f"🚨 *Urgent Appointment Requested*\n\n"
                f"*Name:* {user.name}\n"
                f"*Phone/ID:* {user.phone}\n"
                f"*Email:* {user.email}\n"
                f"*Mode:* {new_appointment.mode.capitalize()}\n"
                f"*Type:* Urgent\n"
                f"*User Telegram ID:* {user.telegram_id}\n"
                f"*Appointment ID:* {new_appointment.id}"
            )
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=details,
                    parse_mode="Markdown",
                    reply_markup=developer_action_buttons(new_appointment.id)
                )
            except Exception as e:
                logger.error(f"Failed to send urgent appointment to developer: {e}")
        payment_message = (
            "💳 Your urgent appointment request has been sent.\n\n"
            f"Please complete the payment of *€{CONSULTATION_PRICE_EUR:.2f}* via [PayPal.me]({PAYPAL_ME_LINK}).\n\n"
            "After completing the payment, please send a screenshot of your PayPal receipt to confirm your appointment."
        )
        await send_and_delete_previous(
            update,
            context,
            payment_message,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return PAYMENT_RECEIVED

    elif choice == "available slots":
        context.user_data["appointment_type"] = "slots"
        await send_and_delete_previous(
            update,
            context,
            "📅 Please choose a date for your appointment:",
            reply_markup=date_selection_keyboard()
        )
        return GET_APPOINTMENT_DATE

    elif choice == "suggest a time":
        context.user_data["appointment_type"] = "suggest"
        await send_and_delete_previous(
            update,
            context,
            "Please suggest a date for your appointment (YYYY-MM-DD):",
            reply_markup=cancel_menu_keyboard()
        )
        return GET_SUGGESTED_DATE

    elif choice == "🔙 back":
        # Go back to appointment mode selection
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to mode selection.",
            reply_markup=appointment_mode_keyboard()
        )
        return APPOINTMENT_MODE

    elif choice == "❌ cancel":
        # Cancel operation and go back to main menu
        await send_and_delete_previous(
            update,
            context,
            "🚫 Operation cancelled.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    else:
        # Invalid choice
        await update.message.reply_text(
            "❌ Invalid choice. Please select Urgent, Available Slots, Suggest a Time, Back, or Cancel.",
            reply_markup=appointment_options_keyboard()
        )
        return APPOINTMENT_OPTIONS

async def get_appointment_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected_date_str = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if selected_date_str == "🔙 Back":
        # Go back to appointment options
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to appointment options.",
            reply_markup=appointment_options_keyboard()
        )
        return APPOINTMENT_OPTIONS

    try:
        selected_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
        if selected_date < datetime.now().date():
            await update.message.reply_text(
                "❌ You cannot select a past date. Please choose a future date.",
                reply_markup=date_selection_keyboard()
            )
            return GET_APPOINTMENT_DATE
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid date format. Please select a date from the options.",
            reply_markup=date_selection_keyboard()
        )
        return GET_APPOINTMENT_DATE

    context.user_data["selected_date"] = selected_date
    available_times = await get_available_slots(selected_date)
    if not available_times:
        await update.message.reply_text(
            "⚠️ No available time slots for the selected date. Please choose another date.",
            reply_markup=date_selection_keyboard()
        )
        return GET_APPOINTMENT_DATE

    await send_and_delete_previous(
        update,
        context,
        "⏰ Please choose a time for your appointment:",
        reply_markup=time_selection_keyboard(selected_date)
    )
    return GET_APPOINTMENT_TIME

async def get_available_slots(selected_date: datetime.date) -> list:
    with Session() as session:
        slots = session.query(AvailableSlot).filter(
            AvailableSlot.slot_datetime >= datetime.now(),
            AvailableSlot.slot_datetime.between(
                datetime.combine(selected_date, datetime.min.time()),
                datetime.combine(selected_date, datetime.max.time())
            )
        ).order_by(AvailableSlot.slot_datetime).all()
    available_times = [slot.slot_datetime.time().strftime("%H:%M") for slot in slots]
    return available_times

async def get_appointment_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected_time_str = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if selected_time_str == "🔙 Back":
        # Go back to date selection
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to date selection.",
            reply_markup=date_selection_keyboard()
        )
        return GET_APPOINTMENT_DATE

    appointment_date = context.user_data.get("selected_date")
    if not appointment_date:
        await update.message.reply_text(
            "❌ No date selected. Please start the appointment process again.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    try:
        appointment_time = datetime.strptime(selected_time_str, "%H:%M").time()
        appointment_datetime = datetime.combine(appointment_date, appointment_time)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time format. Please select a time from the options.",
            reply_markup=time_selection_keyboard(appointment_date)
        )
        return GET_APPOINTMENT_TIME

    with Session() as session:
        slot = session.query(AvailableSlot).filter_by(slot_datetime=appointment_datetime).first()
        if not slot:
            await update.message.reply_text(
                "⚠️ The selected time slot is no longer available. Please choose another time.",
                reply_markup=time_selection_keyboard(appointment_date)
            )
            return GET_APPOINTMENT_TIME

    # Reserve the slot and create the appointment
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            new_appointment = Appointment(
                user_id=user.id,
                appointment_datetime=appointment_datetime,
                status="pending",
                mode=context.user_data.get("appointment_mode"),
                type=context.user_data.get("appointment_type")
            )
            session.add(new_appointment)
            session.delete(slot)
            session.commit()
            logger.info(f"Created new appointment ID {new_appointment.id} for user {user.name}")
            # Notify developer
            details = (
                f"📅 *New Appointment Reserved*\n\n"
                f"*Name:* {user.name}\n"
                f"*Phone/ID:* {user.phone}\n"
                f"*Email:* {user.email}\n"
                f"*Mode:* {new_appointment.mode.capitalize() if new_appointment.mode else 'N/A'}\n"
                f"*Type:* {new_appointment.type.capitalize() if new_appointment.type else 'N/A'}\n"
                f"*Date & Time:* {appointment_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                f"*User Telegram ID:* {user.telegram_id}\n"
                f"*Appointment ID:* {new_appointment.id}"
            )
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=details,
                    parse_mode="Markdown",
                    reply_markup=developer_action_buttons(new_appointment.id)
                )
            except Exception as e:
                logger.error(f"Failed to send appointment details to developer: {e}")
            # Prompt user for payment
            payment_message = (
                f"💳 Your appointment is scheduled for *{appointment_datetime.strftime('%Y-%m-%d %H:%M')}*.\n\n"
                f"Please complete the payment of *€{CONSULTATION_PRICE_EUR:.2f}* via [PayPal.me]({PAYPAL_ME_LINK}).\n\n"
                "After completing the payment, please send a screenshot of your PayPal receipt to confirm your appointment."
            )
            await send_and_delete_previous(
                update,
                context,
                payment_message,
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="Markdown"
            )
            # Schedule reminder
            await send_reminder(user.id, new_appointment.id, context.application)
            return PAYMENT_RECEIVED
        else:
            await update.message.reply_text(
                "❌ User not found. Please register with /start.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END

async def get_suggested_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    suggested_date_str = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if suggested_date_str.lower() == "❌ cancel":
        await send_and_delete_previous(
            update,
            context,
            "🚫 Operation cancelled.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    try:
        suggested_date = datetime.strptime(suggested_date_str, "%Y-%m-%d").date()
        if suggested_date < datetime.now().date():
            await update.message.reply_text(
                "❌ You cannot suggest a past date. Please choose a future date.",
                reply_markup=cancel_menu_keyboard()
            )
            return GET_SUGGESTED_DATE
        context.user_data["suggested_date"] = suggested_date
        await update.message.reply_text(
            "Now please suggest a time (HH:MM):",
            reply_markup=cancel_menu_keyboard()
        )
        return GET_SUGGESTED_TIME
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid date format. Please use YYYY-MM-DD.",
            reply_markup=cancel_menu_keyboard()
        )
        return GET_SUGGESTED_DATE

async def get_suggested_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    suggested_time_str = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if suggested_time_str.lower() == "❌ cancel":
        await send_and_delete_previous(
            update,
            context,
            "🚫 Operation cancelled.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    try:
        suggested_time = datetime.strptime(suggested_time_str, "%H:%M").time()
        suggested_datetime = datetime.combine(context.user_data["suggested_date"], suggested_time)
        if suggested_datetime < datetime.now():
            await update.message.reply_text(
                "❌ You cannot suggest a past date/time. Please choose a future time.",
                reply_markup=cancel_menu_keyboard()
            )
            return GET_SUGGESTED_TIME
        # Save the suggested appointment
        with Session() as session:
            new_appointment = Appointment(
                user_id=user.id,
                mode=context.user_data.get("appointment_mode"),
                type="suggest",
                status="pending",
                suggested_date=suggested_datetime
            )
            session.add(new_appointment)
            session.commit()
            details = (
                f"💡 *Appointment Suggested by User*\n\n"
                f"*Name:* {user.name}\n"
                f"*Phone/ID:* {user.phone}\n"
                f"*Email:* {user.email}\n"
                f"*Mode:* {new_appointment.mode.capitalize() if new_appointment.mode else 'N/A'}\n"
                f"*Type:* Suggest\n"
                f"*Suggested Date & Time:* {suggested_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                f"*User Telegram ID:* {user.telegram_id}\n"
                f"*Appointment ID:* {new_appointment.id}\n\n"
                "Developer, please confirm or cancel."
            )
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=details,
                    parse_mode="Markdown",
                    reply_markup=developer_action_buttons(new_appointment.id)
                )
            except Exception as e:
                logger.error(f"Failed to send suggested appointment to developer: {e}")
        # Prompt user for payment
        payment_message = (
            f"💳 Your suggested appointment has been submitted for approval on *{suggested_datetime.strftime('%Y-%m-%d %H:%M')}*.\n\n"
            f"Please complete the payment of *€{CONSULTATION_PRICE_EUR:.2f}* via [PayPal.me]({PAYPAL_ME_LINK}).\n\n"
            "After completing the payment, please send a screenshot of your PayPal receipt to confirm your appointment."
        )
        await send_and_delete_previous(
            update,
            context,
            payment_message,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return PAYMENT_RECEIVED
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time format. Please use HH:MM.",
            reply_markup=cancel_menu_keyboard()
        )
        return GET_SUGGESTED_TIME

async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if update.message.photo:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = os.path.join(RECEIPTS_DIR, f"receipt_{user_id}_{int(datetime.utcnow().timestamp())}.jpg")
        await file.download_to_drive(file_path)
        try:
            with open(file_path, 'rb') as photo_file:
                await context.bot.send_photo(
                    chat_id=DEVELOPER_CHAT_ID,
                    photo=photo_file,
                    caption=f"📷 *Payment Receipt from {user.name}*\nPlease verify the payment.",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Failed to forward receipt: {e}")
            await update.message.reply_text(
                "❌ Failed to forward your receipt. Please try again."
            )
            return PAYMENT_RECEIVED
        await send_and_delete_previous(
            update,
            context,
            "✅ Your receipt has been received and is under review. The developer will confirm your appointment shortly.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "📷 Please send a screenshot of your PayPal payment receipt as a photo.",
            reply_markup=back_menu_keyboard()
        )
        return PAYMENT_RECEIVED

async def payment_received_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text == "🔙 Back":
        selected_date = context.user_data.get("selected_date")
        if selected_date:
            await send_and_delete_previous(
                update,
                context,
                "🔙 Going back to time selection.",
                reply_markup=time_selection_keyboard(selected_date)
            )
            return GET_APPOINTMENT_TIME
        else:
            user_id = update.effective_user.id
            with Session() as session:
                user = session.query(User).filter_by(telegram_id=user_id).first()
            await send_and_delete_previous(
                update,
                context,
                "🔙 Going back to the main menu.",
                reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
            )
            return MAIN_MENU
    else:
        await update.message.reply_text(
            "❓ Please send your payment receipt as a photo or use the provided buttons.",
            reply_markup=back_menu_keyboard()
        )
        return PAYMENT_RECEIVED

async def view_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    with Session() as session:
        user_record = session.query(User).filter_by(telegram_id=user.id).first()
        if user_record:
            appointments = session.query(Appointment).filter_by(user_id=user_record.id).order_by(Appointment.appointment_datetime.desc(), Appointment.created_at.desc()).all()
            if appointments:
                message = "📝 *Your Appointment History:*\n\n"
                for app in appointments:
                    if app.status == "confirmed":
                        status = "✅ Confirmed"
                    elif app.status == "pending":
                        status = "⏳ Pending"
                    else:
                        status = "❌ Canceled"
                    receipt = (
                        f"\n*Receipt:* [View Receipt]({app.receipt_file_path})"
                        if app.receipt_file_path
                        else ""
                    )
                    datetime_str = app.appointment_datetime.strftime('%Y-%m-%d %H:%M') if app.appointment_datetime else (app.suggested_date.strftime('%Y-%m-%d %H:%M') if app.suggested_date else "N/A")
                    mode_str = app.mode.capitalize() if app.mode else "N/A"
                    type_str = app.type.capitalize() if app.type else "N/A"
                    message += (
                        f"*ID:* {app.id}\n"
                        f"*Date & Time:* {datetime_str}\n"
                        f"*Mode:* {mode_str}\n"
                        f"*Type:* {type_str}\n"
                        f"*Status:* {status}{receipt}\n\n"
                    )
                await send_and_delete_previous(
                    update,
                    context,
                    message,
                    reply_markup=main_menu_keyboard(user_record.telegram_id),
                    parse_mode="Markdown"
                )
            else:
                await send_and_delete_previous(
                    update,
                    context,
                    "📭 You have no appointments.",
                    reply_markup=main_menu_keyboard(user_record.telegram_id)
                )
        else:
            await update.message.reply_text(
                "❌ User not found. Please register with /start.",
                reply_markup=ReplyKeyboardRemove()
            )

async def select_appointment_to_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    with Session() as session:
        user_record = session.query(User).filter_by(telegram_id=user.id).first()
        if user_record:
            appointments = session.query(Appointment).filter_by(user_id=user_record.id, status="confirmed").order_by(Appointment.appointment_datetime.desc()).all()
            # Only show appointments with defined date and time
            valid_appointments = [app for app in appointments if app.appointment_datetime]
            if valid_appointments:
                context.user_data['reschedule_appointments'] = valid_appointments
                keyboard = [[f"ID {app.id} - {app.appointment_datetime.strftime('%Y-%m-%d %H:%M')}"] for app in valid_appointments]
                keyboard.append(["🔙 Back"])
                await send_and_delete_previous(
                    update,
                    context,
                    "🔄 Please select the appointment you want to reschedule:",
                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
                )
                return RESCHEDULE_SELECT_APPOINTMENT
            else:
                await send_and_delete_previous(
                    update,
                    context,
                    "📭 You have no confirmed appointments with a set date/time to reschedule.",
                    reply_markup=main_menu_keyboard(user_record.telegram_id)
                )
                return MAIN_MENU
        else:
            await update.message.reply_text(
                "❌ User not found. Please register with /start.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END

async def reschedule_select_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected = update.message.text.strip()
    if selected == "🔙 Back":
        user_id = update.effective_user.id
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the main menu.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU
    appointment_id_match = re.match(r"ID (\d+) - (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", selected)
    if not appointment_id_match:
        appointments = context.user_data.get('reschedule_appointments', [])
        keyboard = [[f"ID {app.id} - {app.appointment_datetime.strftime('%Y-%m-%d %H:%M')}"] for app in appointments]
        keyboard.append(["🔙 Back"])
        await update.message.reply_text(
            "❌ Invalid selection. Please choose a valid appointment.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return RESCHEDULE_SELECT_APPOINTMENT

    appointment_id = int(appointment_id_match.group(1))
    context.user_data['reschedule_appointment_id'] = appointment_id
    await send_and_delete_previous(
        update,
        context,
        "📅 Please choose a new date for your appointment:",
        reply_markup=date_selection_keyboard()
    )
    return RESCHEDULE_NEW_DATE

async def reschedule_new_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected_date_str = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if selected_date_str == "🔙 Back":
        appointments = context.user_data.get('reschedule_appointments', [])
        keyboard = [[f"ID {app.id} - {app.appointment_datetime.strftime('%Y-%m-%d %H:%M')}"] for app in appointments]
        keyboard.append(["🔙 Back"])
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to appointment selection.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return RESCHEDULE_SELECT_APPOINTMENT

    try:
        selected_date = datetime.strptime(selected_date_str, "%Y-%m-%d").date()
        if selected_date < datetime.now().date():
            await update.message.reply_text(
                "❌ You cannot select a past date. Please choose a future date.",
                reply_markup=date_selection_keyboard()
            )
            return RESCHEDULE_NEW_DATE
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid date format. Please select a date from the options.",
            reply_markup=date_selection_keyboard()
        )
        return RESCHEDULE_NEW_DATE

    context.user_data["reschedule_new_date"] = selected_date
    available_times = await get_available_slots(selected_date)
    if not available_times:
        await update.message.reply_text(
            "⚠️ No available time slots for the selected date. Please choose another date.",
            reply_markup=date_selection_keyboard()
        )
        return RESCHEDULE_NEW_DATE

    await send_and_delete_previous(
        update,
        context,
        "⏰ Please choose a new time for your appointment:",
        reply_markup=time_selection_keyboard(selected_date)
    )
    return RESCHEDULE_NEW_TIME

async def reschedule_new_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected_time_str = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if selected_time_str == "🔙 Back":
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to date selection.",
            reply_markup=date_selection_keyboard()
        )
        return RESCHEDULE_NEW_DATE

    selected_date = context.user_data.get("reschedule_new_date")
    if not selected_date:
        await update.message.reply_text(
            "❌ No date selected. Please start the reschedule process again.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    try:
        selected_time = datetime.strptime(selected_time_str, "%H:%M").time()
        appointment_datetime = datetime.combine(selected_date, selected_time)
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time format. Please select a time from the options.",
            reply_markup=time_selection_keyboard(selected_date)
        )
        return RESCHEDULE_NEW_TIME

    appointment_id = context.user_data.get('reschedule_appointment_id')
    if not appointment_id:
        await update.message.reply_text(
            "❌ No appointment selected for rescheduling. Please start the process again.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id, user_id=user.id).first()
        if not appointment or appointment.status != "confirmed":
            await update.message.reply_text(
                "❌ Appointment not found or not in a reschedulable state.",
                reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
            )
            return MAIN_MENU
        # Restore old slot if exists
        if appointment.appointment_datetime and appointment.appointment_datetime > datetime.now():
            old_slot = AvailableSlot(slot_datetime=appointment.appointment_datetime)
            session.add(old_slot)
        # Update appointment
        appointment.appointment_datetime = appointment_datetime
        appointment.status = "pending"
        appointment.receipt_file_path = None
        # Remove new slot from AvailableSlot
        new_slot = session.query(AvailableSlot).filter_by(slot_datetime=appointment_datetime).first()
        if new_slot:
            session.delete(new_slot)
        session.commit()
        logger.info(f"Rescheduled appointment ID {appointment.id} for user {user.name} to {appointment_datetime}")
        # Notify developer
        details = (
            f"🔄 *Appointment Rescheduled*\n\n"
            f"*Name:* {user.name}\n"
            f"*Phone/ID:* {user.phone}\n"
            f"*Email:* {user.email}\n"
            f"*Mode:* {appointment.mode.capitalize() if appointment.mode else 'N/A'}\n"
            f"*Type:* {appointment.type.capitalize() if appointment.type else 'N/A'}\n"
            f"*New Date & Time:* {appointment_datetime.strftime('%Y-%m-%d %H:%M')}\n"
            f"*User Telegram ID:* {user.telegram_id}\n"
            f"*Appointment ID:* {appointment.id}"
        )
        try:
            await context.bot.send_message(
                chat_id=DEVELOPER_CHAT_ID,
                text=details,
                parse_mode="Markdown",
                reply_markup=developer_action_buttons(appointment.id)
            )
        except Exception as e:
            logger.error(f"Failed to send rescheduled appointment to developer: {e}")
        # Prompt user for payment
        payment_message = (
            f"💳 Your appointment has been rescheduled to *{appointment_datetime.strftime('%Y-%m-%d %H:%M')}*.\n\n"
            f"Please complete the payment of *€{CONSULTATION_PRICE_EUR:.2f}* via [PayPal.me]({PAYPAL_ME_LINK}).\n\n"
            "After completing the payment, please send a screenshot of your PayPal receipt to confirm your appointment."
        )
        await send_and_delete_previous(
            update,
            context,
            payment_message,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        # Schedule reminder
        await send_reminder(user.id, appointment.id, context.application)
        return PAYMENT_RECEIVED

async def select_appointment_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    with Session() as session:
        user_record = session.query(User).filter_by(telegram_id=user.id).first()
        if user_record:
            appointments = session.query(Appointment).filter_by(user_id=user_record.id, status="confirmed").order_by(Appointment.appointment_datetime.desc()).all()
            valid_appointments = [app for app in appointments if app.appointment_datetime]
            if valid_appointments:
                context.user_data['cancel_appointments'] = valid_appointments
                keyboard = [[f"ID {app.id} - {app.appointment_datetime.strftime('%Y-%m-%d %H:%M')}"] for app in valid_appointments]
                keyboard.append(["🔙 Back"])
                await send_and_delete_previous(
                    update,
                    context,
                    "❌ Please select the appointment you want to cancel:",
                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
                )
                return CANCEL_SELECT_APPOINTMENT
            else:
                await send_and_delete_previous(
                    update,
                    context,
                    "📭 You have no confirmed appointments with a set date/time to cancel.",
                    reply_markup=main_menu_keyboard(user_record.telegram_id)
                )
                return MAIN_MENU
        else:
            await update.message.reply_text(
                "❌ User not found. Please register with /start.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END

async def cancel_select_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected = update.message.text.strip()
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()

    if selected == "🔙 Back":
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the main menu.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU

    appointment_id_match = re.match(r"ID (\d+) - (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", selected)
    if not appointment_id_match:
        appointments = context.user_data.get('cancel_appointments', [])
        keyboard = [[f"ID {app.id} - {app.appointment_datetime.strftime('%Y-%m-%d %H:%M')}"] for app in appointments]
        keyboard.append(["🔙 Back"])
        await update.message.reply_text(
            "❌ Invalid selection. Please choose a valid appointment.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return CANCEL_SELECT_APPOINTMENT

    appointment_id = int(appointment_id_match.group(1))

    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id).first()
        if appointment:
            appointment.status = "canceled"
            if appointment.appointment_datetime and appointment.appointment_datetime > datetime.now():
                new_slot = AvailableSlot(slot_datetime=appointment.appointment_datetime)
                session.add(new_slot)
            session.commit()
            logger.info(f"Canceled appointment ID {appointment.id} for user {appointment.user.name}")
            # Notify developer
            cancel_details = (
                f"❌ *Appointment Canceled*\n\n"
                f"*Name:* {appointment.user.name}\n"
                f"*Phone/ID:* {appointment.user.phone}\n"
                f"*Email:* {appointment.user.email}\n"
                f"*Mode:* {appointment.mode.capitalize() if appointment.mode else 'N/A'}\n"
                f"*Type:* {appointment.type.capitalize() if appointment.type else 'N/A'}\n"
                f"*Date & Time:* {appointment.appointment_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                f"*User Telegram ID:* {appointment.user.telegram_id}\n"
                f"*Appointment ID:* {appointment.id}"
            )
            try:
                await context.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=cancel_details,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send cancellation details to developer: {e}")
            # Notify user
            cancellation_message = (
                f"❌ *Your appointment has been canceled.*\n\n"
                f"*Appointment ID:* {appointment.id}\n"
                "If you have any questions, please contact support."
            )
            try:
                await context.bot.send_message(
                    chat_id=appointment.user.telegram_id,
                    text=cancellation_message,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send cancellation to user {appointment.user.name}: {e}")
            await send_and_delete_previous(
                update,
                context,
                f"✅ Appointment {appointment_id} canceled.",
                reply_markup=main_menu_keyboard(appointment.user.telegram_id if user else 0)
            )
            return MAIN_MENU
        else:
            await update.message.reply_text(
                "❌ Appointment not found. Please try again.",
                reply_markup=ReplyKeyboardRemove()
            )
            return MAIN_MENU

async def manage_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = update.message.text.strip()
    if action == "➕ Add Slot":
        await send_and_delete_previous(
            update,
            context,
            "➕ *Add Available Slot*\n\nPlease send the slot date and time in the format `YYYY-MM-DD HH:MM`.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        return ADD_SLOT
    elif action == "➖ Remove Slot":
        with Session() as session:
            slots = session.query(AvailableSlot).order_by(AvailableSlot.slot_datetime).all()
            if slots:
                keyboard = [[slot.slot_datetime.strftime("%Y-%m-%d %H:%M")] for slot in slots]
                keyboard.append(["🔙 Back"])
                await send_and_delete_previous(
                    update,
                    context,
                    "➖ *Remove Available Slot*\n\nPlease select the slot you want to remove:",
                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
                    parse_mode="Markdown"
                )
                return REMOVE_SLOT
            else:
                await send_and_delete_previous(
                    update,
                    context,
                    "⚠️ No available slots to remove.",
                    reply_markup=manage_slots_keyboard(),
                    parse_mode="Markdown"
                )
                return MANAGE_SLOTS
    elif action == "🔙 Back":
        user_id = update.effective_user.id
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the main menu.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "❓ Invalid option selected. Please choose from the menu.",
            reply_markup=manage_slots_keyboard()
        )
        return MANAGE_SLOTS

async def add_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slot_str = update.message.text.strip()
    try:
        slot_datetime = datetime.strptime(slot_str, "%Y-%m-%d %H:%M")
        if slot_datetime < datetime.now():
            await update.message.reply_text(
                "❌ You cannot add a slot in the past. Please enter a future date and time.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ADD_SLOT
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Please use `YYYY-MM-DD HH:MM`.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ADD_SLOT

    with Session() as session:
        existing_slot = session.query(AvailableSlot).filter_by(slot_datetime=slot_datetime).first()
        if existing_slot:
            await update.message.reply_text(
                "⚠️ This slot already exists. Please choose a different time.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ADD_SLOT
        else:
            new_slot = AvailableSlot(slot_datetime=slot_datetime)
            session.add(new_slot)
            session.commit()
            await update.message.reply_text(
                f"✅ Slot added for {slot_datetime.strftime('%Y-%m-%d %H:%M')}.",
                reply_markup=manage_slots_keyboard()
            )
            return MANAGE_SLOTS

async def remove_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    slot_str = update.message.text.strip()
    try:
        slot_datetime = datetime.strptime(slot_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid format. Please select a valid slot.",
        )
        return REMOVE_SLOT

    with Session() as session:
        slot = session.query(AvailableSlot).filter_by(slot_datetime=slot_datetime).first()
        if slot:
            session.delete(slot)
            session.commit()
            await update.message.reply_text(
                f"✅ Slot for {slot_datetime.strftime('%Y-%m-%d %H:%M')} has been removed.",
                reply_markup=manage_slots_keyboard()
            )
            return MANAGE_SLOTS
        else:
            await update.message.reply_text(
                "❌ Slot not found. Please select a valid slot."
            )
            return REMOVE_SLOT

async def contact_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message.text.strip()
    if message.lower() == "❌ cancel":
        user_id = update.effective_user.id
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
        await send_and_delete_previous(
            update,
            context,
            "🚫 Operation cancelled.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU
    user = update.effective_user
    forward_text = (
        f"📩 *New Contact Us Message*\n\n"
        f"From: {user.mention_markdown_v2()}\n"
        f"User ID: {user.id}\n\n"
        f"Message:\n{message}"
    )
    try:
        await context.bot.send_message(
            chat_id=DEVELOPER_CHAT_ID,
            text=forward_text,
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.error(f"Failed to forward contact message: {e}")

    with Session() as session:
        user_record = session.query(User).filter_by(telegram_id=user.id).first()
    await send_and_delete_previous(
        update,
        context,
        "✅ Your message has been sent to the admin. We will get back to you shortly.",
        reply_markup=main_menu_keyboard(user_record.telegram_id if user_record else 0)
    )
    return MAIN_MENU

async def view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            profile_msg = (
                f"👤 *Your Profile*\n\n"
                f"*Name:* {user.name}\n"
                f"*Phone/ID:* {user.phone or 'Not set'}\n"
                f"*Email:* {user.email}"
            )
            await send_and_delete_previous(
                update,
                context,
                profile_msg,
                reply_markup=main_menu_keyboard(user.telegram_id),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ User not found. Please register with /start.",
                reply_markup=ReplyKeyboardRemove()
            )

async def edit_profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        ["Edit Name"],
        ["Edit Phone/ID"],
        ["Edit Email"],
        ["🔙 Back"]
    ]
    await send_and_delete_previous(
        update,
        context,
        "✏️ *Edit Profile*\n\nSelect which detail you want to edit:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
        parse_mode="Markdown"
    )
    return PROFILE_MENU

async def profile_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if choice == "Edit Name":
        await update.message.reply_text("Please enter your new name:", reply_markup=cancel_menu_keyboard())
        return EDIT_NAME
    elif choice == "Edit Phone/ID":
        await update.message.reply_text("Please enter your new phone/ID:", reply_markup=cancel_menu_keyboard())
        return EDIT_PHONE
    elif choice == "Edit Email":
        await update.message.reply_text("Please enter your new email address:", reply_markup=cancel_menu_keyboard())
        return EDIT_EMAIL
    elif choice == "🔙 Back":
        user_id = update.effective_user.id
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the main menu.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU
    else:
        await update.message.reply_text("❌ Invalid choice. Please select an option.", reply_markup=cancel_menu_keyboard())
        return PROFILE_MENU

async def edit_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = update.message.text.strip()
    if not new_name:
        await update.message.reply_text("❌ Please enter a valid name.", reply_markup=cancel_menu_keyboard())
        return EDIT_NAME
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.name = new_name
            session.commit()
            await update.message.reply_text("✅ Name updated successfully!", reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("❌ User not found. Please register with /start.", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    return await return_to_main_menu(update, context)

async def edit_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_phone = update.message.text.strip()
    if not new_phone:
        await update.message.reply_text("❌ Please enter a valid phone/ID.", reply_markup=cancel_menu_keyboard())
        return EDIT_PHONE
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.phone = new_phone
            session.commit()
            await update.message.reply_text("✅ Phone/ID updated successfully!", reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("❌ User not found. Please register with /start.", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    return await return_to_main_menu(update, context)

async def edit_email_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_email = update.message.text.strip()
    if not EMAIL_REGEX.match(new_email):
        await update.message.reply_text("❌ Please enter a valid email address.", reply_markup=cancel_menu_keyboard())
        return EDIT_EMAIL
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if user:
            existing_user = session.query(User).filter(User.email == new_email, User.id != user.id).first()
            if existing_user:
                await update.message.reply_text("⚠️ This email is already in use by another user. Please use a different email.", reply_markup=cancel_menu_keyboard())
                return EDIT_EMAIL
            user.email = new_email
            session.commit()
            await update.message.reply_text("✅ Email updated successfully!", reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("❌ User not found. Please register with /start.", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
    return await return_to_main_menu(update, context)

async def return_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
    await update.message.reply_text(
        "🔄 Returning to main menu...",
        reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
    )
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    with Session() as session:
        user = session.query(User).filter_by(telegram_id=user_id).first()
    await send_and_delete_previous(
        update,
        context,
        "🚫 Operation cancelled.",
        reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
    )
    return MAIN_MENU

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    logger.info("User %s requested to restart the conversation.", user.first_name)
    with Session() as session:
        existing_user = session.query(User).filter_by(telegram_id=user.id).first()
        if existing_user:
            await send_and_delete_previous(
                update,
                context,
                f"🔄 Conversation restarted.\n👋 Welcome back, {existing_user.name}!",
                reply_markup=main_menu_keyboard(existing_user.telegram_id)
            )
            return MAIN_MENU
        else:
            await send_and_delete_previous(
                update,
                context,
                "👋 Welcome to the Medical Appointment Booking Bot!\n\n"
                "Use the menu to navigate. When you're ready to book an appointment, select 'Get Appointment' and we'll assist you.",
                reply_markup=main_menu_keyboard(0)
            )
            return MAIN_MENU

# Developer Menu Handlers
async def developer_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != DEVELOPER_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized to access this menu.")
        return MAIN_MENU

    choice = update.message.text.strip()
    if choice == "⚙️ Manage Slots":
        await send_and_delete_previous(
            update,
            context,
            "⚙️ *Manage Available Time Slots*\n\nChoose an action:",
            reply_markup=manage_slots_keyboard(),
            parse_mode="Markdown"
        )
        return MANAGE_SLOTS
    elif choice == "📊 Statistics":
        await show_statistics(update, context)
        return DEVELOPER_MENU
    elif choice == "❌ Dev Cancel Appointment":
        await dev_select_appointment_to_cancel(update, context)
        return DEV_CANCEL_APPT_MENU
    elif choice == "🔙 Back":
        user_id = update.effective_user.id
        with Session() as session:
            user = session.query(User).filter_by(telegram_id=user_id).first()
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the main menu.",
            reply_markup=main_menu_keyboard(user.telegram_id if user else 0)
        )
        return MAIN_MENU
    else:
        await update.message.reply_text(
            "❓ Invalid option. Please choose from the menu.",
            reply_markup=developer_menu_keyboard()
        )
        return DEVELOPER_MENU

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with Session() as session:
        total_users = session.query(User).count()
        total_appointments = session.query(Appointment).count()
        confirmed_appointments = session.query(Appointment).filter_by(status="confirmed").count()
        pending_appointments = session.query(Appointment).filter_by(status="pending").count()
        canceled_appointments = session.query(Appointment).filter_by(status="canceled").count()

    stats_message = (
        f"📊 *Statistics*\n\n"
        f"👥 *Total Users:* {total_users}\n"
        f"📅 *Total Appointments:* {total_appointments}\n"
        f"✅ *Confirmed:* {confirmed_appointments}\n"
        f"⏳ *Pending:* {pending_appointments}\n"
        f"❌ *Canceled:* {canceled_appointments}"
    )
    await update.message.reply_text(stats_message, parse_mode="Markdown", reply_markup=developer_menu_keyboard())

async def dev_select_appointment_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with Session() as session:
        appointments = session.query(Appointment).filter(Appointment.status.in_(["pending", "confirmed"])).order_by(Appointment.appointment_datetime.asc()).all()
        if appointments:
            context.user_data['dev_cancel_appointments'] = appointments
            keyboard = [[f"ID {app.id} - {(app.appointment_datetime.strftime('%Y-%m-%d %H:%M') if app.appointment_datetime else 'No Time')}"] for app in appointments]
            keyboard.append(["🔙 Back"])
            await send_and_delete_previous(
                update,
                context,
                "❌ *Developer Cancel Appointment*\n\nSelect the appointment to cancel:",
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True),
                parse_mode="Markdown"
            )
            return DEV_CANCEL_APPT_MENU
        else:
            await send_and_delete_previous(
                update,
                context,
                "📭 No appointments available to cancel.",
                reply_markup=developer_menu_keyboard(),
                parse_mode="Markdown"
            )
            return DEVELOPER_MENU

async def dev_cancel_appointment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected = update.message.text.strip()

    if selected == "🔙 Back":
        await send_and_delete_previous(
            update,
            context,
            "🔙 Going back to the Developer Menu.",
            reply_markup=developer_menu_keyboard()
        )
        return DEVELOPER_MENU

    appointment_id_match = re.match(r"ID (\d+) -", selected)
    if not appointment_id_match:
        appointments = context.user_data.get('dev_cancel_appointments', [])
        keyboard = [[f"ID {app.id} - {(app.appointment_datetime.strftime('%Y-%m-%d %H:%M') if app.appointment_datetime else 'No Time')}"] for app in appointments]
        keyboard.append(["🔙 Back"])
        await update.message.reply_text(
            "❌ Invalid selection. Please choose a valid appointment.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return DEV_CANCEL_APPT_MENU

    appointment_id = int(appointment_id_match.group(1))

    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id).first()
        if appointment and appointment.status in ["pending", "confirmed"]:
            if appointment.status == "confirmed" and appointment.appointment_datetime and appointment.appointment_datetime > datetime.now():
                new_slot = AvailableSlot(slot_datetime=appointment.appointment_datetime)
                session.add(new_slot)
            appointment.status = "canceled"
            session.commit()
            send_cancellation_email(appointment)
            user = appointment.user
            datetime_str = appointment.appointment_datetime.strftime('%Y-%m-%d %H:%M') if appointment.appointment_datetime else (appointment.suggested_date.strftime('%Y-%m-%d %H:%M') if appointment.suggested_date else "N/A")

            cancellation_message = (
                f"❌ *Your appointment has been canceled.*\n\n"
                f"*Appointment ID:* {appointment.id}\n"
                "If you have any questions, please contact support."
            )
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=cancellation_message,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send cancellation to user {user.name}: {e}")
            await send_and_delete_previous(
                update,
                context,
                f"✅ Appointment {appointment_id} canceled.",
                reply_markup=developer_menu_keyboard()
            )
            return DEVELOPER_MENU
        else:
            await update.message.reply_text(
                "❌ Appointment not found or already processed.",
                reply_markup=developer_menu_keyboard()
            )
            return DEVELOPER_MENU

async def send_custom_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != DEVELOPER_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Usage: /sendmsg <user_telegram_id> <message>")
            return
        user_telegram_id = int(args[0])
        message_text = ' '.join(args[1:])
        await context.bot.send_message(
            chat_id=user_telegram_id,
            text=message_text,
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"✅ Message sent to user ID {user_telegram_id}.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Invalid command format. Usage: /sendmsg <user_telegram_id> <message>")
    except Exception as e:
        logger.error(f"Failed to send custom message: {e}")
        await update.message.reply_text("❌ Failed to send the message. Please try again.")

async def developer_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("confirm_"):
        appointment_id = int(data.split("_")[1])
        await confirm_appointment(appointment_id, context, query)
    elif data.startswith("cancel_"):
        appointment_id = int(data.split("_")[1])
        await dev_cancel_appointment(appointment_id, context, query)

async def confirm_appointment(appointment_id: int, context: ContextTypes.DEFAULT_TYPE, query: Update.callback_query) -> None:
    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id).first()
        if appointment and appointment.status == "pending":
            appointment.status = "confirmed"
            session.commit()
            user = appointment.user
            datetime_str = appointment.appointment_datetime.strftime('%Y-%m-%d %H:%M') if appointment.appointment_datetime else (appointment.suggested_date.strftime('%Y-%m-%d %H:%M') if appointment.suggested_date else "N/A")

            confirmation_message = (
                f"✅ *Your appointment has been confirmed!*\n\n"
                f"*Date & Time:* {datetime_str}\n"
                "We look forward to seeing you."
            )
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=confirmation_message,
                    parse_mode="Markdown"
                )
                subject = "Your Appointment Confirmation"
                body = (
                    f"Hello {user.name},\n\n"
                    f"Your appointment on {datetime_str} has been confirmed.\n"
                    "We look forward to assisting you.\n\n"
                    "Best Regards,\nYour Medical Team"
                )
                send_email(user.email, subject, body)
            except Exception as e:
                logger.error(f"Failed to send confirmation to user {user.name}: {e}")
            await query.edit_message_text(
                text=f"✅ *Appointment {appointment_id} Confirmed*\n\n"
                     f"*Name:* {user.name}\n"
                     f"*Phone/ID:* {user.phone}\n"
                     f"*Email:* {user.email}\n"
                     f"*Mode:* {appointment.mode.capitalize() if appointment.mode else 'N/A'}\n"
                     f"*Type:* {appointment.type.capitalize() if appointment.type else 'N/A'}\n"
                     f"*Date & Time:* {datetime_str}\n"
                     f"*User Telegram ID:* {user.telegram_id}",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=DEVELOPER_CHAT_ID,
                text=(f"🤝 Appointment {appointment_id} confirmed.\n"
                      f"You can now send the meeting link to the user by using the command:\n"
                      f"`/sendmsg {user.telegram_id} <meeting_link>`"),
                parse_mode="Markdown"
            )
            # Schedule reminder
            await send_reminder(user.id, appointment.id, context.application)
        else:
            await query.message.reply_text("❌ Invalid appointment or already processed.")

async def dev_cancel_appointment(appointment_id: int, context: ContextTypes.DEFAULT_TYPE, query: Update.callback_query) -> None:
    with Session() as session:
        appointment = session.query(Appointment).filter_by(id=appointment_id).first()
        if appointment and appointment.status in ["pending", "confirmed"]:
            if appointment.status == "confirmed" and appointment.appointment_datetime and appointment.appointment_datetime > datetime.now():
                new_slot = AvailableSlot(slot_datetime=appointment.appointment_datetime)
                session.add(new_slot)
            appointment.status = "canceled"
            session.commit()
            user = appointment.user
            datetime_str = appointment.appointment_datetime.strftime('%Y-%m-%d %H:%M') if appointment.appointment_datetime else (appointment.suggested_date.strftime('%Y-%m-%d %H:%M') if appointment.suggested_date else "N/A")

            cancellation_message = (
                f"❌ *Your appointment has been canceled.*\n\n"
                f"*Appointment ID:* {appointment.id}\n"
                "If you have any questions, please contact support."
            )
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=cancellation_message,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send cancellation to user {user.name}: {e}")
            await query.edit_message_text(
                text=f"❌ *Appointment {appointment_id} Canceled*\n\n"
                     f"*Name:* {user.name}\n"
                     f"*Phone/ID:* {user.phone}\n"
                     f"*Email:* {user.email}\n"
                     f"*Mode:* {appointment.mode.capitalize() if appointment.mode else 'N/A'}\n"
                     f"*Type:* {appointment.type.capitalize() if appointment.type else 'N/A'}\n"
                     f"*Date & Time:* {datetime_str}\n"
                     f"*User Telegram ID:* {user.telegram_id}",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("❌ Invalid appointment or already processed.")

# Conversation Handler Setup
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('start', start)],
    states={
        REGISTER_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, register_name),
            CommandHandler('restart', restart)
        ],
        REGISTER_PHONE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, register_phone),
            CommandHandler('restart', restart)
        ],
        REGISTER_EMAIL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, register_email),
            CommandHandler('restart', restart)
        ],
        MAIN_MENU: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler),
            CommandHandler('restart', restart)
        ],
        APPOINTMENT_MODE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, appointment_mode_handler),
            CommandHandler('restart', restart)
        ],
        APPOINTMENT_OPTIONS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, appointment_options_handler),
            CommandHandler('restart', restart)
        ],
        GET_APPOINTMENT_DATE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_appointment_date),
            CommandHandler('restart', restart)
        ],
        GET_APPOINTMENT_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_appointment_time),
            CommandHandler('restart', restart)
        ],
        PAYMENT_RECEIVED: [
            MessageHandler(filters.PHOTO, confirm_payment),
            MessageHandler(filters.TEXT & ~filters.COMMAND, payment_received_text_handler),
            CommandHandler('restart', restart)
        ],
        RESCHEDULE_SELECT_APPOINTMENT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_select_appointment),
            CommandHandler('restart', restart)
        ],
        RESCHEDULE_NEW_DATE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_new_date),
            CommandHandler('restart', restart)
        ],
        RESCHEDULE_NEW_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, reschedule_new_time),
            CommandHandler('restart', restart)
        ],
        CANCEL_SELECT_APPOINTMENT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_select_appointment),
            CommandHandler('restart', restart)
        ],
        MANAGE_SLOTS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, manage_slots),
            CommandHandler('restart', restart)
        ],
        ADD_SLOT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_slot),
            CommandHandler('restart', restart)
        ],
        REMOVE_SLOT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, remove_slot),
            CommandHandler('restart', restart)
        ],
        PROFILE_MENU: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, profile_menu_handler),
            CommandHandler('restart', restart)
        ],
        EDIT_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_name_handler),
            CommandHandler('restart', restart)
        ],
        EDIT_PHONE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_phone_handler),
            CommandHandler('restart', restart)
        ],
        EDIT_EMAIL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, edit_email_handler),
            CommandHandler('restart', restart)
        ],
        DEVELOPER_MENU: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, developer_menu_handler),
            CommandHandler('restart', restart)
        ],
        DEV_CANCEL_APPT_MENU: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, dev_cancel_appointment_handler),
            CommandHandler('restart', restart)
        ],
        CONTACT_ADMIN: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, contact_admin_handler),
            CommandHandler('restart', restart)
        ],
        GET_SUGGESTED_DATE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_suggested_date_handler),
            CommandHandler('restart', restart)
        ],
        GET_SUGGESTED_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_suggested_time_handler),
            CommandHandler('restart', restart)
        ],
    },
    fallbacks=[
        CommandHandler('cancel', cancel),
        CommandHandler('restart', restart)
    ],
    allow_reentry=True
)

# Application Setup
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(conv_handler)
application.add_handler(CallbackQueryHandler(developer_action_handler, pattern=r"^(confirm|cancel)_\d+$"))
application.add_handler(CommandHandler('sendmsg', send_custom_message, filters=filters.User(user_id=DEVELOPER_CHAT_ID)))

# Start Scheduler
scheduler.start()
logger.info("Scheduler started.")

# Shutdown Handler
def shutdown_handler(signum, frame):
    logger.info("Shutting down scheduler and bot...")
    scheduler.shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# Run the Bot
if __name__ == '__main__':
    logger.info("Starting the bot...")
    try:
        application.run_polling()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
        scheduler.shutdown()
