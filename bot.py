#!/usr/bin/env python3
"""
Telegram Admin Bot - Gestione membri del gruppo
Rimuove account eliminati e utenti inattivi con anteprima e conferma.
"""

import logging
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from telegram import Update, Chat, ChatMember, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configurazione bot
BOT_TOKEN = "8767403291:AAFQfOkcKWBVk756G7cQPKkG6gccJKLvtlA"
DB_FILE = "group_members.db"

# Costanti
INACTIVITY_PERIODS = {
    "30": 30,
    "60": 60,
    "90": 90,
    "180": 180,
    "365": 365,
}

PENDING_OPERATIONS = {}  # Dizionario per tracciare operazioni in sospeso


class DatabaseManager:
    """Gestisce le operazioni sul database SQLite."""

    def __init__(self, db_file: str = DB_FILE):
        self.db_file = db_file
        self.init_db()

    def init_db(self):
        """Inizializza il database con le tabelle necessarie."""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_messages (
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                message_date TIMESTAMP NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, message_date, group_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_date TIMESTAMP NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, group_id)
            )
        """)

        conn.commit()
        conn.close()

    def record_message(self, user_id: int, username: str, first_name: str, 
                      last_name: str, group_id: int):
        """Registra un messaggio di un utente."""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO user_messages 
                (user_id, username, first_name, last_name, message_date, group_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, username, first_name, last_name, datetime.now(), group_id))

            # Aggiorna o inserisce il membro del gruppo
            cursor.execute("""
                INSERT OR IGNORE INTO group_members
                (user_id, username, first_name, last_name, joined_date, group_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, username, first_name, last_name, datetime.now(), group_id))

            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Errore nel database: {e}")
        finally:
            conn.close()

    def get_inactive_users(self, days: int, group_id: int) -> List[Dict]:
        """Restituisce gli utenti inattivi da più di X giorni."""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cutoff_date = datetime.now() - timedelta(days=days)

        cursor.execute("""
            SELECT DISTINCT gm.user_id, gm.username, gm.first_name, gm.last_name,
                   MAX(um.message_date) as last_message
            FROM group_members gm
            LEFT JOIN user_messages um ON gm.user_id = um.user_id AND gm.group_id = um.group_id
            WHERE gm.group_id = ?
            GROUP BY gm.user_id
            HAVING MAX(um.message_date) IS NULL OR MAX(um.message_date) < ?
            ORDER BY last_message DESC
        """, (group_id, cutoff_date))

        results = []
        for row in cursor.fetchall():
            results.append({
                "user_id": row[0],
                "username": row[1],
                "first_name": row[2],
                "last_name": row[3],
                "last_message": row[4],
            })

        conn.close()
        return results

    def get_all_members(self, group_id: int) -> List[Dict]:
        """Restituisce tutti i membri registrati del gruppo."""
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT user_id, username, first_name, last_name, joined_date
            FROM group_members
            WHERE group_id = ?
            ORDER BY joined_date DESC
        """, (group_id,))

        results = []
        for row in cursor.fetchall():
            results.append({
                "user_id": row[0],
                "username": row[1],
                "first_name": row[2],
                "last_name": row[3],
                "joined_date": row[4],
            })

        conn.close()
        return results


class BotManager:
    """Gestisce la logica principale del bot."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get_deleted_accounts(self, context: ContextTypes.DEFAULT_TYPE, 
                                   group_id: int) -> List[ChatMember]:
        """Identifica gli account eliminati nel gruppo."""
        deleted_accounts = []

        try:
            members = await context.bot.get_chat_members(group_id)
            for member in members:
                if member.user.is_deleted:
                    deleted_accounts.append(member)
        except TelegramError as e:
            logger.error(f"Errore nel recupero dei membri: {e}")

        return deleted_accounts

    async def remove_user(self, context: ContextTypes.DEFAULT_TYPE, 
                         group_id: int, user_id: int) -> bool:
        """Rimuove un utente dal gruppo."""
        try:
            await context.bot.ban_chat_member(group_id, user_id)
            await context.bot.unban_chat_member(group_id, user_id)
            return True
        except TelegramError as e:
            logger.error(f"Errore nella rimozione dell'utente {user_id}: {e}")
            return False

    def format_user_name(self, user: Dict) -> str:
        """Formatta il nome dell'utente."""
        name_parts = []
        if user.get("first_name"):
            name_parts.append(user["first_name"])
        if user.get("last_name"):
            name_parts.append(user["last_name"])

        name = " ".join(name_parts) if name_parts else "Utente sconosciuto"

        if user.get("username"):
            name += f" (@{user['username']})"

        return name


# Istanze globali
db_manager = DatabaseManager()
bot_manager = BotManager(db_manager)


# Handler dei comandi
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - Informazioni sul bot."""
    welcome_text = (
        "🤖 **Benvenuto nel Telegram Admin Bot!**\n\n"
        "Questo bot gestisce i membri del tuo gruppo:\n"
        "• Rimuove account eliminati\n"
        "• Rimuove utenti inattivi\n"
        "• Mostra statistiche\n\n"
        "Usa `/help` per vedere tutti i comandi disponibili."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /help - Lista dei comandi."""
    help_text = (
        "📋 **Comandi disponibili:**\n\n"
        "`/start` - Informazioni sul bot\n"
        "`/help` - Questo messaggio\n"
        "`/stats` - Mostra statistiche del gruppo\n"
        "`/remove_deleted` - Rimuove account eliminati\n"
        "`/remove_inactive` - Rimuove utenti inattivi\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /stats - Mostra statistiche del gruppo."""
    if not update.message.chat.type in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ Questo comando funziona solo nei gruppi."
        )
        return

    group_id = update.message.chat.id
    chat_title = update.message.chat.title or "Gruppo sconosciuto"

    try:
        # Ottieni il numero totale di membri
        chat_members_count = await context.bot.get_chat_member_count(group_id)

        # Ottieni gli account eliminati
        deleted_accounts = await bot_manager.get_deleted_accounts(context, group_id)

        # Ottieni i membri registrati nel database
        all_members = db_manager.get_all_members(group_id)

        # Calcola gli inattivi (ultimi 30 giorni come default)
        inactive_30 = db_manager.get_inactive_users(30, group_id)

        stats_text = (
            f"📊 **Statistiche del gruppo: {chat_title}**\n\n"
            f"👥 **Totale membri:** {chat_members_count}\n"
            f"👻 **Account eliminati:** {len(deleted_accounts)}\n"
            f"📝 **Membri registrati:** {len(all_members)}\n"
            f"😴 **Inattivi (ultimi 30 giorni):** {len(inactive_30)}\n"
        )

        await update.message.reply_text(stats_text, parse_mode="Markdown")

    except TelegramError as e:
        await update.message.reply_text(f"❌ Errore: {e}")
        logger.error(f"Errore nel comando stats: {e}")


async def remove_deleted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /remove_deleted - Rimuove account eliminati con anteprima."""
    if not update.message.chat.type in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ Questo comando funziona solo nei gruppi."
        )
        return

    group_id = update.message.chat.id
    user_id = update.message.from_user.id

    try:
        await update.message.reply_text("🔍 Scansione account eliminati in corso...")

        deleted_accounts = await bot_manager.get_deleted_accounts(context, group_id)

        if not deleted_accounts:
            await update.message.reply_text("✅ Nessun account eliminato trovato!")
            return

        # Prepara l'anteprima
        preview_text = (
            f"👻 **Account eliminati trovati: {len(deleted_accounts)}**\n\n"
            "Questi account verranno rimossi:\n"
        )

        for i, member in enumerate(deleted_accounts[:10], 1):
            preview_text += f"{i}. Account eliminato (ID: {member.user.id})\n"

        if len(deleted_accounts) > 10:
            preview_text += f"\n... e altri {len(deleted_accounts) - 10} account\n"

        preview_text += (
            "\n⚠️ **Confermi la rimozione di questi account?**"
        )

        # Salva l'operazione in sospeso
        operation_key = f"delete_{group_id}_{user_id}"
        PENDING_OPERATIONS[operation_key] = {
            "type": "delete_deleted",
            "group_id": group_id,
            "user_id": user_id,
            "deleted_accounts": deleted_accounts,
        }

        # Crea i bottoni di conferma
        keyboard = [
            [
                InlineKeyboardButton("✅ Conferma", callback_data=f"confirm_{operation_key}"),
                InlineKeyboardButton("❌ Annulla", callback_data=f"cancel_{operation_key}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(preview_text, reply_markup=reply_markup, parse_mode="Markdown")

    except TelegramError as e:
        await update.message.reply_text(f"❌ Errore: {e}")
        logger.error(f"Errore nel comando remove_deleted: {e}")


async def remove_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /remove_inactive - Rimuove utenti inattivi."""
    if not update.message.chat.type in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ Questo comando funziona solo nei gruppi."
        )
        return

    group_id = update.message.chat.id
    user_id = update.message.from_user.id

    # Crea i bottoni per la selezione del periodo
    keyboard = [
        [InlineKeyboardButton("30 giorni", callback_data="period_30")],
        [InlineKeyboardButton("60 giorni", callback_data="period_60")],
        [InlineKeyboardButton("90 giorni", callback_data="period_90")],
        [InlineKeyboardButton("180 giorni", callback_data="period_180")],
        [InlineKeyboardButton("365 giorni", callback_data="period_365")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Salva il contesto dell'operazione
    operation_key = f"inactive_{group_id}_{user_id}"
    PENDING_OPERATIONS[operation_key] = {
        "type": "select_period",
        "group_id": group_id,
        "user_id": user_id,
    }

    await update.message.reply_text(
        "⏰ **Seleziona il periodo di inattività:**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i callback dei bottoni."""
    query = update.callback_query
    await query.answer()

    callback_data = query.data
    group_id = query.message.chat.id
    user_id = query.from_user.id

    try:
        # Gestione conferma/annulla rimozione account eliminati
        if callback_data.startswith("confirm_delete_"):
            operation_key = callback_data.replace("confirm_", "")
            operation = PENDING_OPERATIONS.get(operation_key)

            if not operation:
                await query.edit_message_text("❌ Operazione scaduta.")
                return

            deleted_accounts = operation["deleted_accounts"]
            removed_count = 0
            failed_count = 0

            await query.edit_message_text("⏳ Rimozione in corso...")

            for member in deleted_accounts:
                if await bot_manager.remove_user(context, group_id, member.user.id):
                    removed_count += 1
                else:
                    failed_count += 1

            result_text = (
                f"✅ **Operazione completata!**\n\n"
                f"🗑️ Rimossi: {removed_count}\n"
                f"❌ Errori: {failed_count}\n"
            )

            await query.edit_message_text(result_text, parse_mode="Markdown")
            del PENDING_OPERATIONS[operation_key]

        elif callback_data.startswith("cancel_delete_"):
            operation_key = callback_data.replace("cancel_", "")
            await query.edit_message_text("❌ Operazione annullata.")
            if operation_key in PENDING_OPERATIONS:
                del PENDING_OPERATIONS[operation_key]

        # Gestione selezione periodo inattività
        elif callback_data.startswith("period_"):
            period_str = callback_data.replace("period_", "")
            days = INACTIVITY_PERIODS.get(period_str)

            if not days:
                await query.edit_message_text("❌ Periodo non valido.")
                return

            await query.edit_message_text(
                f"🔍 Ricerca utenti inattivi da {days} giorni..."
            )

            inactive_users = db_manager.get_inactive_users(days, group_id)

            if not inactive_users:
                await query.edit_message_text(
                    f"✅ Nessun utente inattivo da {days} giorni!"
                )
                return

            # Prepara l'anteprima
            preview_text = (
                f"😴 **Utenti inattivi da {days} giorni: {len(inactive_users)}**\n\n"
                "Questi utenti verranno rimossi:\n"
            )

            for i, user in enumerate(inactive_users[:10], 1):
                user_name = bot_manager.format_user_name(user)
                last_msg = user.get("last_message", "Mai")
                preview_text += f"{i}. {user_name}\n"

            if len(inactive_users) > 10:
                preview_text += f"\n... e altri {len(inactive_users) - 10} utenti\n"

            preview_text += "\n⚠️ **Confermi la rimozione?**"

            # Salva l'operazione in sospeso
            operation_key = f"inactive_confirm_{group_id}_{user_id}"
            PENDING_OPERATIONS[operation_key] = {
                "type": "delete_inactive",
                "group_id": group_id,
                "user_id": user_id,
                "inactive_users": inactive_users,
            }

            keyboard = [
                [
                    InlineKeyboardButton("✅ Conferma", callback_data=f"confirm_{operation_key}"),
                    InlineKeyboardButton("❌ Annulla", callback_data=f"cancel_{operation_key}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                preview_text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )

        elif callback_data.startswith("confirm_inactive_confirm_"):
            operation_key = callback_data.replace("confirm_", "")
            operation = PENDING_OPERATIONS.get(operation_key)

            if not operation:
                await query.edit_message_text("❌ Operazione scaduta.")
                return

            inactive_users = operation["inactive_users"]
            removed_count = 0
            failed_count = 0

            await query.edit_message_text("⏳ Rimozione in corso...")

            for user in inactive_users:
                if await bot_manager.remove_user(context, group_id, user["user_id"]):
                    removed_count += 1
                else:
                    failed_count += 1

            result_text = (
                f"✅ **Operazione completata!**\n\n"
                f"🗑️ Rimossi: {removed_count}\n"
                f"❌ Errori: {failed_count}\n"
            )

            await query.edit_message_text(result_text, parse_mode="Markdown")
            del PENDING_OPERATIONS[operation_key]

        elif callback_data.startswith("cancel_inactive_confirm_"):
            operation_key = callback_data.replace("cancel_", "")
            await query.edit_message_text("❌ Operazione annullata.")
            if operation_key in PENDING_OPERATIONS:
                del PENDING_OPERATIONS[operation_key]

    except TelegramError as e:
        await query.edit_message_text(f"❌ Errore: {e}")
        logger.error(f"Errore nel callback: {e}")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i messaggi per tracciare l'attività degli utenti."""
    if update.message.chat.type in ["group", "supergroup"]:
        user = update.message.from_user
        group_id = update.message.chat.id

        db_manager.record_message(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            group_id=group_id,
        )


def main():
    """Funzione principale per avviare il bot."""
    # Crea l'applicazione
    application = Application.builder().token(BOT_TOKEN).build()

    # Registra gli handler dei comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("remove_deleted", remove_deleted))
    application.add_handler(CommandHandler("remove_inactive", remove_inactive))

    # Registra il callback handler per i bottoni
    application.add_handler(CallbackQueryHandler(button_callback))

    # Registra il message handler per tracciare i messaggi
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Avvia il bot
    logger.info("Bot avviato...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
