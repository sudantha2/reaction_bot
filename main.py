
import os
import logging
import threading
import time
import asyncio
from typing import Dict, List, Optional
from urllib.parse import urlparse
import requests
from flask import Flask, request, jsonify
from pymongo import MongoClient
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, TelegramError
from keep_alive import start_keep_alive

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global variables for instance management
_instance_lock = threading.Lock()
_running_instances = set()
_system_running = False

# Default emoji list (will be replaced by custom emojis from database)
DEFAULT_EMOJIS = ['❤️‍🔥', '🥰', '⚡']

class BotManager:
    def __init__(self):
        self.mongo_client = None
        self.db = None
        self.bots_collection = None
        self.emoji_collection = None
        self.custom_posts_collection = None
        self.running_bots: Dict[str, dict] = {}
        self.emoji_counters: Dict[str, int] = {}
        self.current_emojis = DEFAULT_EMOJIS.copy()
        self.bot_emoji_assignment: Dict[str, str] = {}
        self.pending_custom_posts: Dict[int, dict] = {}  # user_id -> {chat_id, message_id, post_url}

    def init_database(self):
        """Initialize MongoDB connection"""
        try:
            mongo_uri = os.getenv('MONGO_REACT_DATA')
            if not mongo_uri:
                raise ValueError("MONGO_REACT_DATA environment variable not set")

            self.mongo_client = MongoClient(mongo_uri)
            self.db = self.mongo_client['telegram_bots']
            self.bots_collection = self.db['bots']
            self.emoji_collection = self.db['emojis']
            self.custom_posts_collection = self.db['custom_posts']
            logger.info("Connected to MongoDB successfully")

            # Load current emojis from database
            self.load_emojis_from_database()

            # Test connection
            self.mongo_client.admin.command('ping')

        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    def get_next_available_port(self) -> int:
        """Get the next available port starting from 5000"""
        existing_ports = set()
        for bot in self.bots_collection.find({}, {"port": 1}):
            if "port" in bot:
                existing_ports.add(bot["port"])

        port = 5000
        while port in existing_ports:
            port += 1
        return port

    def add_bot_to_database(self, name: str, token: str) -> dict:
        """Add a new bot to the database"""
        try:
            port = self.get_next_available_port()

            # Get the last bot to update its next_url
            last_bot = self.bots_collection.find_one(sort=[("port", -1)])

            new_bot = {
                "name": name,
                "token": token,
                "next_url": "",
                "port": port
            }

            # Insert the new bot
            result = self.bots_collection.insert_one(new_bot)
            new_bot["_id"] = result.inserted_id

            # Update the previous bot's next_url if exists
            if last_bot:
                signal_url = f"http://0.0.0.0:{port}/signal"
                self.bots_collection.update_one(
                    {"_id": last_bot["_id"]},
                    {"$set": {"next_url": signal_url}}
                )
                logger.info(f"Updated bot {last_bot['name']} next_url to {signal_url}")

            logger.info(f"Added bot {name} with port {port}")
            return new_bot

        except Exception as e:
            logger.error(f"Failed to add bot to database: {e}")
            raise

    def get_all_bots(self) -> List[dict]:
        """Get all bots from database"""
        try:
            return list(self.bots_collection.find())
        except Exception as e:
            logger.error(f"Failed to get bots from database: {e}")
            return []

    def send_signal_to_next_bot(self, bot_name: str):
        """Send HTTP POST signal to the next bot"""
        try:
            bot = self.bots_collection.find_one({"name": bot_name})
            if not bot or not bot.get("next_url"):
                logger.info(f"No next bot configured for {bot_name}")
                return

            next_url = bot["next_url"]
            response = requests.post(
                next_url,
                json={"signal": "react", "from": bot_name},
                timeout=5
            )

            if response.status_code == 200:
                logger.info(f"Signal sent successfully from {bot_name} to {next_url}")
            else:
                logger.error(f"Failed to send signal from {bot_name}: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error sending signal from {bot_name}: {e}")
        except Exception as e:
            logger.error(f"Error sending signal from {bot_name}: {e}")

    def load_emojis_from_database(self):
        """Load emoji packs from database"""
        try:
            # Load all emoji packs
            emoji_packs = list(self.emoji_collection.find({"pack_name": {"$exists": True}}))
            if not emoji_packs:
                # Initialize with default pack
                self.save_emoji_pack("emoji1", DEFAULT_EMOJIS)
                self.current_emojis = DEFAULT_EMOJIS.copy()
                logger.info(f"Initialized database with default emoji pack: {DEFAULT_EMOJIS}")
            else:
                # Use first pack as default
                self.current_emojis = emoji_packs[0].get("emojis", DEFAULT_EMOJIS)
                logger.info(f"Loaded emoji packs from database, using first pack: {self.current_emojis}")
        except Exception as e:
            logger.error(f"Error loading emoji packs from database: {e}")
            self.current_emojis = DEFAULT_EMOJIS.copy()

    def save_emoji_pack(self, pack_name: str, emojis: List[str]):
        """Save emoji pack to database"""
        try:
            self.emoji_collection.update_one(
                {"pack_name": pack_name},
                {"$set": {"pack_name": pack_name, "emojis": emojis, "created_at": time.time()}},
                upsert=True
            )
            logger.info(f"Saved emoji pack '{pack_name}' to database: {emojis}")
        except Exception as e:
            logger.error(f"Error saving emoji pack '{pack_name}' to database: {e}")

    def get_all_emoji_packs(self) -> List[dict]:
        """Get all emoji packs from database"""
        try:
            return list(self.emoji_collection.find({"pack_name": {"$exists": True}}).sort("pack_name", 1))
        except Exception as e:
            logger.error(f"Error getting emoji packs from database: {e}")
            return []

    def assign_emoji_pack_to_bot(self, bot_name: str) -> List[str]:
        """Assign a random emoji pack to a bot at startup"""
        try:
            import random
            all_packs = self.get_all_emoji_packs()
            
            if not all_packs:
                logger.info(f"No emoji packs found, using default for bot {bot_name}")
                return DEFAULT_EMOJIS.copy()
            
            # Use bot name as seed for consistent pack selection per bot
            random.seed(hash(bot_name) % (2**32))
            selected_pack = random.choice(all_packs)
            pack_emojis = selected_pack.get("emojis", DEFAULT_EMOJIS)
            pack_name = selected_pack.get("pack_name", "unknown")
            
            logger.info(f"Bot {bot_name} assigned emoji pack '{pack_name}' with {len(pack_emojis)} emojis: {pack_emojis}")
            return pack_emojis
            
        except Exception as e:
            logger.error(f"Error assigning emoji pack to bot {bot_name}: {e}")
            return DEFAULT_EMOJIS.copy()

    def get_random_emoji_from_bot_pack(self, bot_name: str, message_id: str) -> str:
        """Get a random emoji from the bot's assigned pack"""
        try:
            import random
            
            # Get bot's assigned pack
            bot_pack = self.bot_emoji_assignment.get(bot_name, DEFAULT_EMOJIS)
            
            if not bot_pack:
                return DEFAULT_EMOJIS[0] if DEFAULT_EMOJIS else "❤️"
            
            # Use message_id as seed for consistent randomness per message
            random.seed(hash(f"{bot_name}_{message_id}") % (2**32))
            selected_emoji = random.choice(bot_pack)
            
            logger.info(f"Bot {bot_name} selected emoji: {selected_emoji} from assigned pack")
            return selected_emoji
            
        except Exception as e:
            logger.error(f"Error getting random emoji for bot {bot_name}: {e}")
            return DEFAULT_EMOJIS[0] if DEFAULT_EMOJIS else "❤️"

    def get_random_pack_for_message(self, message_id: str) -> List[str]:
        """Select one random emoji pack for a specific message"""
        try:
            import random
            all_packs = self.get_all_emoji_packs()
            
            if not all_packs:
                logger.info(f"No emoji packs found, using default for message {message_id}")
                return DEFAULT_EMOJIS.copy()
            
            # Use message_id as seed for consistent pack selection per message
            random.seed(hash(f"msg_{message_id}") % (2**32))
            selected_pack = random.choice(all_packs)
            pack_emojis = selected_pack.get("emojis", DEFAULT_EMOJIS)
            pack_name = selected_pack.get("pack_name", "unknown")
            
            logger.info(f"Message {message_id} assigned emoji pack '{pack_name}' with {len(pack_emojis)} emojis: {pack_emojis}")
            return pack_emojis
            
        except Exception as e:
            logger.error(f"Error selecting pack for message {message_id}: {e}")
            return DEFAULT_EMOJIS.copy()

    def assign_emoji_to_bot(self, bot_name: str, message_id: str) -> str:
        """Assign a random emoji to a bot for a message from the selected pack for this message"""
        try:
            import random
            
            # Get the emoji pack selected for this specific message
            message_pack = self.get_random_pack_for_message(message_id)
            
            if not message_pack:
                return DEFAULT_EMOJIS[0] if DEFAULT_EMOJIS else "❤️"
            
            # Use bot_name + message_id as seed for consistent emoji selection per bot per message
            random.seed(hash(f"{bot_name}_{message_id}") % (2**32))
            selected_emoji = random.choice(message_pack)
            
            logger.info(f"Bot {bot_name} assigned emoji: {selected_emoji} from message pack")
            return selected_emoji

        except Exception as e:
            logger.error(f"Error assigning emoji to bot {bot_name}: {e}")
            return DEFAULT_EMOJIS[0] if DEFAULT_EMOJIS else "❤️"

    def create_flask_app(self, bot_name: str, port: int) -> Flask:
        """Create Flask app for signal handling"""
        app = Flask(f'bot_{bot_name}')

        @app.route('/')
        def home():
            return jsonify({
                "bot_name": bot_name,
                "port": port,
                "status": "running",
                "endpoints": {
                    "signal": f"/signal",
                    "health": f"/health"
                }
            })

        @app.route('/signal', methods=['POST'])
        def handle_signal():
            try:
                data = request.get_json()
                logger.info(f"Bot {bot_name} received signal: {data}")

                # Process the signal and continue chain
                threading.Thread(
                    target=self.send_signal_to_next_bot,
                    args=(bot_name,),
                    daemon=True
                ).start()

                return jsonify({"status": "success", "bot": bot_name})
            except Exception as e:
                logger.error(f"Error handling signal in {bot_name}: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500

        @app.route('/health', methods=['GET'])
        def health_check():
            return jsonify({"status": "healthy", "bot": bot_name, "port": port})

        return app

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, bot_name: str):
        """Handle /start command"""
        help_text = f"""
🤖 *{bot_name} - Telegram Reaction Bot*

This bot is part of a reaction chain system. Here's how it works:

📝 *Commands:*
• `/start` - Show this help message
• `/clone <BOT_TOKEN>` - Add a new bot to the chain (Main bot only)
• `/clone_list` - Show all cloned bots and their status (Main bot only)
• `/unclone @bot_username` - Remove a bot from the chain (Main bot only)
• `/emoji1 [em1,em2,em3]` - Set emoji pack 1 (Main bot only)
• `/emoji2 [em1,em2,em3]` - Set emoji pack 2 (Main bot only)
• `/emoji3 [em1,em2,em3]` - Set emoji pack 3 (Main bot only)
• `/emoji_list` - Show all emoji packs (Main bot only)
• `/del_emoji1` - Delete emoji pack 1 with confirmation (Main bot only)
• `/del_emoji2` - Delete emoji pack 2 with confirmation (Main bot only)
• `/custom <post_link>` - Set custom reactions for specific post (Main bot only)

🔄 *How it works:*
• In channels/groups: Each bot adds ONE random emoji reaction per message
• In private chats: Bots reply with their assigned emoji
• Each bot is assigned ONE random emoji pack at startup
• Bots use ONLY emojis from their assigned pack
• Each message gets random emoji from bot's assigned pack

⚡ *Chain System:*
• Bots are connected in a chain via HTTP signals
• Each reaction triggers the next bot
• All bots run on different ports for communication

💾 *Data:*
• Bot configurations stored in MongoDB
• Multiple emoji packs saved in database
• Automatic port assignment starting from 5000

🏷️ *Usage:*
• Add me to channels/groups to auto-react to all messages
• Use `/emoji1 [🥰,❤️‍🔥,⚡]` to set emoji pack 1
• Use `/emoji2 [🔥,💯,⭐]` to set emoji pack 2
• Bots will randomly select from all configured packs
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def emoji_pack_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, pack_number: int):
        """Handle /emoji1, /emoji2, etc. commands to set emoji packs"""
        pack_name = f"emoji{pack_number}"
        logger.info(f"Emoji pack command received: {pack_name} from user {update.effective_user.id}")

        if not context.args:
            # Show current pack or instructions
            all_packs = self.get_all_emoji_packs()
            current_pack = next((pack for pack in all_packs if pack["pack_name"] == pack_name), None)
            
            if current_pack:
                current_emojis_str = ",".join(current_pack["emojis"])
                await update.message.reply_text(
                    f"🎭 *{pack_name.upper()} Current Emojis:* {current_emojis_str}\n\n"
                    f"📝 *Usage:*\n"
                    f"`/{pack_name} [emoji1,emoji2,emoji3,...]`\n\n"
                    f"📋 *Example:*\n"
                    f"`/{pack_name} [🥰,❤️‍🔥,⚡,🔥,💯]`\n\n"
                    f"ℹ️ *Note:* Use square brackets and separate emojis with commas",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"🎭 *{pack_name.upper()} Pack Not Set*\n\n"
                    f"📝 *Usage:*\n"
                    f"`/{pack_name} [emoji1,emoji2,emoji3,...]`\n\n"
                    f"📋 *Example:*\n"
                    f"`/{pack_name} [🥰,❤️‍🔥,⚡,🔥,💯]`\n\n"
                    f"ℹ️ *Note:* Use square brackets and separate emojis with commas",
                    parse_mode='Markdown'
                )
            return

        try:
            # Join all arguments to handle spaces
            emoji_input = " ".join(context.args)

            # Check if input is in correct format [emoji1,emoji2,emoji3]
            if not (emoji_input.startswith('[') and emoji_input.endswith(']')):
                await update.message.reply_text(
                    "❌ *Invalid format!*\n"
                    f"Please use: `/{pack_name} [emoji1,emoji2,emoji3]`\n"
                    f"Example: `/{pack_name} [🥰,❤️‍🔥,⚡]`",
                    parse_mode='Markdown'
                )
                return

            # Extract emojis from brackets
            emoji_string = emoji_input[1:-1]  # Remove brackets
            emoji_list = [emoji.strip() for emoji in emoji_string.split(',')]

            # Filter out empty emojis
            emoji_list = [emoji for emoji in emoji_list if emoji]

            if not emoji_list:
                await update.message.reply_text("❌ No valid emojis found!")
                return

            if len(emoji_list) > 20:
                await update.message.reply_text("❌ Too many emojis! Maximum 20 emojis allowed.")
                return

            # Save emoji pack to database
            self.save_emoji_pack(pack_name, emoji_list)

            # Get total packs and emojis
            all_packs = self.get_all_emoji_packs()
            total_emojis = sum(len(pack.get("emojis", [])) for pack in all_packs)

            await update.message.reply_text(
                f"✅ *{pack_name.upper()} Pack updated successfully!*\n\n"
                f"🎭 *Pack emojis:* {', '.join(emoji_list)}\n"
                f"📊 *Pack size:* {len(emoji_list)} emojis\n"
                f"📦 *Total packs:* {len(all_packs)}\n"
                f"🎯 *Total emojis:* {total_emojis}\n"
                f"🤖 *Active bots:* {len(_running_instances)}\n\n"
                f"ℹ️ Each bot uses ONE randomly assigned emoji pack!\n"
                f"🔄 Restart bots to reassign packs if needed.",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error in emoji pack command {pack_name}: {e}")
            await update.message.reply_text(
                f"❌ Error updating emoji pack {pack_name}: {str(e)}\n"
                "Please check the format and try again."
            )

    async def emoji_list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /emoji_list command to show all emoji packs"""
        logger.info(f"Emoji list command received from user {update.effective_user.id}")

        try:
            all_packs = self.get_all_emoji_packs()

            if not all_packs:
                await update.message.reply_text(
                    "❌ No emoji packs found!\n\n"
                    "💡 Use `/emoji1 [🥰,❤️‍🔥,⚡]` to create your first pack."
                )
                return

            # Build the response message
            message_lines = ["🎭 *Emoji Packs List*\n"]

            total_emojis = 0
            for pack in all_packs:
                pack_name = pack.get("pack_name", "unknown")
                pack_emojis = pack.get("emojis", [])
                emoji_count = len(pack_emojis)
                total_emojis += emoji_count
                
                # Limit display to first 10 emojis to avoid message length issues
                display_emojis = pack_emojis[:10]
                emoji_display = ", ".join(display_emojis)
                if len(pack_emojis) > 10:
                    emoji_display += f"... (+{len(pack_emojis) - 10} more)"

                message_lines.append(
                    f"*📦 {pack_name.upper()}*\n"
                    f"   🎯 Count: {emoji_count} emojis\n"
                    f"   🎭 Emojis: {emoji_display}\n"
                )

            # Add summary
            message_lines.append(
                f"\n📈 *Summary:*\n"
                f"• Total Packs: {len(all_packs)}\n"
                f"• Total Emojis: {total_emojis}\n"
                f"• Active Bots: {len(_running_instances)}\n\n"
                f"💡 *Usage:* Use `/emoji1`, `/emoji2`, etc. to set packs\n"
                f"🗑️ *Delete:* Use `/del_emoji1`, `/del_emoji2`, etc. to delete packs\n"
                f"🎲 *Reaction Mode:* Random selection from all packs"
            )

            # Join all lines and send
            full_message = "\n".join(message_lines)

            # Split message if too long (Telegram limit is 4096 characters)
            if len(full_message) > 4000:
                # Send in chunks
                chunks = []
                current_chunk = "🎭 *Emoji Packs List*\n\n"

                for line in message_lines[1:]:  # Skip the header
                    if len(current_chunk + line) > 3800:
                        chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += line

                if current_chunk:
                    chunks.append(current_chunk)

                for chunk in chunks:
                    await update.message.reply_text(chunk, parse_mode='Markdown')
            else:
                await update.message.reply_text(full_message, parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Error in emoji_list command: {e}")
            await update.message.reply_text(
                f"❌ Error retrieving emoji packs: {str(e)}\n"
                "Please check the logs for more details."
            )

    async def delete_emoji_pack_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE, pack_number: int):
        """Handle /del_emoji1, /del_emoji2, etc. commands to delete emoji packs with confirmation"""
        pack_name = f"emoji{pack_number}"
        logger.info(f"Delete emoji pack command received: {pack_name} from user {update.effective_user.id}")

        try:
            # Check if pack exists
            all_packs = self.get_all_emoji_packs()
            target_pack = next((pack for pack in all_packs if pack["pack_name"] == pack_name), None)
            
            if not target_pack:
                await update.message.reply_text(
                    f"❌ *{pack_name.upper()} Pack Not Found*\n\n"
                    f"📋 This emoji pack doesn't exist or is already deleted.\n"
                    f"💡 Use `/emoji_list` to see all available packs.",
                    parse_mode='Markdown'
                )
                return

            pack_emojis = target_pack.get("emojis", [])
            emoji_display = ", ".join(pack_emojis[:10])
            if len(pack_emojis) > 10:
                emoji_display += f"... (+{len(pack_emojis) - 10} more)"

            # Create confirmation buttons
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes, Delete", callback_data=f"delete_pack_{pack_name}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_delete_{pack_name}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"⚠️ *Delete Confirmation*\n\n"
                f"🗑️ **Are you sure you want to delete {pack_name.upper()}?**\n\n"
                f"📦 *Pack Details:*\n"
                f"• Name: {pack_name.upper()}\n"
                f"• Emojis: {len(pack_emojis)} total\n"
                f"• Content: {emoji_display}\n\n"
                f"⚠️ **This action cannot be undone!**\n"
                f"🤖 All bots using this pack will be reassigned new packs.",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Error in delete emoji pack command {pack_name}: {e}")
            await update.message.reply_text(
                f"❌ Error processing delete command for {pack_name}: {str(e)}\n"
                "Please check the logs for more details."
            )

    async def handle_delete_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle confirmation button clicks for deleting emoji packs"""
        query = update.callback_query
        await query.answer()

        try:
            callback_data = query.data
            user_id = update.effective_user.id
            
            logger.info(f"Delete confirmation callback received: {callback_data} from user {user_id}")

            if callback_data.startswith("delete_pack_"):
                pack_name = callback_data.replace("delete_pack_", "")
                
                # Check if pack still exists
                all_packs = self.get_all_emoji_packs()
                target_pack = next((pack for pack in all_packs if pack["pack_name"] == pack_name), None)
                
                if not target_pack:
                    await query.edit_message_text(
                        f"❌ *Pack Already Deleted*\n\n"
                        f"📋 {pack_name.upper()} was already deleted or doesn't exist.\n"
                        f"💡 Use `/emoji_list` to see current packs.",
                        parse_mode='Markdown'
                    )
                    return

                # Delete the pack from database
                result = self.emoji_collection.delete_one({"pack_name": pack_name})
                
                if result.deleted_count > 0:
                    # Get updated stats
                    remaining_packs = self.get_all_emoji_packs()
                    total_emojis = sum(len(pack.get("emojis", [])) for pack in remaining_packs)
                    
                    # Reassign emoji packs to all bots
                    reassigned_bots = []
                    for bot_name in self.running_bots.keys():
                        new_pack = self.assign_emoji_pack_to_bot(bot_name)
                        self.bot_emoji_assignment[bot_name] = new_pack
                        reassigned_bots.append(bot_name)

                    await query.edit_message_text(
                        f"✅ *Pack Deleted Successfully!*\n\n"
                        f"🗑️ **Deleted:** {pack_name.upper()}\n"
                        f"📊 **Updated Stats:**\n"
                        f"• Remaining Packs: {len(remaining_packs)}\n"
                        f"• Total Emojis: {total_emojis}\n"
                        f"• Reassigned Bots: {len(reassigned_bots)}\n\n"
                        f"🔄 All bots have been reassigned new emoji packs!\n"
                        f"💡 Use `/emoji_list` to see remaining packs.",
                        parse_mode='Markdown'
                    )
                    
                    logger.info(f"Successfully deleted emoji pack {pack_name} and reassigned {len(reassigned_bots)} bots")
                else:
                    await query.edit_message_text(
                        f"❌ *Delete Failed*\n\n"
                        f"🔄 Could not delete {pack_name.upper()}.\n"
                        f"📋 The pack may have been already deleted.\n"
                        f"💡 Use `/emoji_list` to check current packs.",
                        parse_mode='Markdown'
                    )

            elif callback_data.startswith("cancel_delete_"):
                pack_name = callback_data.replace("cancel_delete_", "")
                
                await query.edit_message_text(
                    f"✅ *Delete Cancelled*\n\n"
                    f"🛡️ {pack_name.upper()} pack has been preserved.\n"
                    f"📋 No changes were made to your emoji packs.\n"
                    f"💡 Use `/emoji_list` to see all your packs.",
                    parse_mode='Markdown'
                )
                
                logger.info(f"Delete cancelled for emoji pack {pack_name} by user {user_id}")

        except Exception as e:
            logger.error(f"Error in delete confirmation handler: {e}")
            await query.edit_message_text(
                f"❌ Error processing confirmation: {str(e)}\n"
                "Please try again or check the logs for more details."
            )

    async def clone_list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clone_list command to show all cloned bots"""
        logger.info(f"Clone list command received from user {update.effective_user.id}")

        try:
            # Get all bots from database
            all_bots = self.get_all_bots()

            if not all_bots:
                await update.message.reply_text("❌ No bots found in the system.")
                return

            # Build the response message
            message_lines = ["🤖 *Bot Clone List*\n"]

            active_tokens = list(_running_instances)

            for i, bot in enumerate(all_bots, 1):
                bot_name = bot.get('name', 'Unknown')
                bot_token = bot.get('token', '')
                bot_port = bot.get('port', 'N/A')
                next_url = bot.get('next_url', 'None')

                # Check if bot is currently running
                is_running = bot_token in active_tokens
                status = "🟢 Running" if is_running else "🔴 Stopped"

                # Get bot username from Telegram API (with error handling)
                bot_username = "Unknown"
                try:
                    if bot_token:
                        test_url = f"https://api.telegram.org/bot{bot_token}/getMe"
                        response = requests.get(test_url, timeout=5)
                        if response.status_code == 200:
                            bot_info = response.json()
                            if bot_info.get('ok'):
                                username = bot_info.get('result', {}).get('username', 'unknown')
                                # Escape underscores for Markdown formatting
                                escaped_username = username.replace('_', '\\_')
                                bot_username = f"@{escaped_username}"
                except:
                    bot_username = "Error getting username"

                # Format next bot info
                next_bot_info = "None"
                if next_url and next_url != "":
                    try:
                        # Extract port from next_url
                        port_from_url = next_url.split(':')[-1].split('/')[0]
                        next_bot_info = f"Port {port_from_url}"
                    except:
                        next_bot_info = "Configured"

                # Get bot's assigned emoji pack
                bot_pack_info = "Not assigned"
                if bot_name in self.bot_emoji_assignment:
                    pack_emojis = self.bot_emoji_assignment[bot_name][:5]  # Show first 5 emojis
                    bot_pack_info = f"{', '.join(pack_emojis)}{'...' if len(self.bot_emoji_assignment[bot_name]) > 5 else ''}"

                message_lines.append(
                    f"*{i}. {bot_name}*\n"
                    f"   🤖 Username: {bot_username}\n"
                    f"   🔌 Port: {bot_port}\n"
                    f"   📊 Status: {status}\n"
                    f"   🎭 Emoji Pack: {bot_pack_info}\n"
                    f"   🔗 Next Bot: {next_bot_info}\n"
                    f"   🎯 Token: {bot_token[:15]}...\n"
                )

            # Add summary
            running_count = len([bot for bot in all_bots if bot.get('token') in active_tokens])
            # Get emoji pack summary
            all_emoji_packs = self.get_all_emoji_packs()
            total_emojis = sum(len(pack.get("emojis", [])) for pack in all_emoji_packs)
            
            message_lines.append(
                f"\n📈 *Summary:*\n"
                f"• Total Bots: {len(all_bots)}\n"
                f"• Running: {running_count}\n"
                f"• Stopped: {len(all_bots) - running_count}\n"
                f"• Emoji Packs: {len(all_emoji_packs)}\n"
                f"• Total Emojis: {total_emojis}"
            )

            # Join all lines and send
            full_message = "\n".join(message_lines)

            # Split message if too long (Telegram limit is 4096 characters)
            if len(full_message) > 4000:
                # Send in chunks
                chunks = []
                current_chunk = "🤖 *Bot Clone List*\n\n"

                for line in message_lines[1:]:  # Skip the header
                    if len(current_chunk + line) > 3800:
                        chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += line

                if current_chunk:
                    chunks.append(current_chunk)

                for chunk in chunks:
                    await update.message.reply_text(chunk, parse_mode='Markdown', disable_web_page_preview=True)
            else:
                await update.message.reply_text(full_message, parse_mode='Markdown', disable_web_page_preview=True)

        except Exception as e:
            logger.error(f"Error in clone_list command: {e}")
            await update.message.reply_text(
                f"❌ Error retrieving bot list: {str(e)}\n"
                "Please check the logs for more details."
            )

    def parse_post_link(self, post_url: str) -> Optional[dict]:
        """Parse a Telegram post link and extract chat_id and message_id"""
        try:
            # Handle different Telegram URL formats
            # https://t.me/channel_name/123
            # https://t.me/c/123456789/123
            # https://telegram.me/channel_name/123

            if 't.me/' in post_url or 'telegram.me/' in post_url:
                # Extract the path part
                if 't.me/' in post_url:
                    path = post_url.split('t.me/')[-1]
                else:
                    path = post_url.split('telegram.me/')[-1]

                parts = path.split('/')

                if len(parts) >= 2:
                    channel_part = parts[0]
                    message_id = parts[1]

                    # Handle private channel format: c/123456789/123
                    if channel_part == 'c' and len(parts) >= 3:
                        chat_id = f"-100{parts[1]}"
                        message_id = parts[2]
                    else:
                        # Public channel format: @channel_name/123
                        chat_id = f"@{channel_part}" if not channel_part.startswith('@') else channel_part

                    return {
                        'chat_id': chat_id,
                        'message_id': int(message_id),
                        'original_url': post_url
                    }

            return None
        except Exception as e:
            logger.error(f"Error parsing post link: {e}")
            return None

    async def remove_bot_reactions(self, chat_id: str, message_id: int):
        """Remove all reactions from bots on a specific message"""
        removed_count = 0

        for bot_name, bot_info in self.running_bots.items():
            try:
                application = bot_info.get('application')
                if application and application.bot:
                    # Try to remove reactions by setting empty reaction list
                    await application.bot.set_message_reaction(
                        chat_id=chat_id,
                        message_id=message_id,
                        reaction=[],
                        is_big=False
                    )
                    removed_count += 1
                    logger.info(f"Removed reactions from {bot_name} on message {message_id}")
            except Exception as e:
                logger.error(f"Error removing reactions from {bot_name}: {e}")

        return removed_count

    async def custom_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /custom command for setting custom reactions on specific posts"""
        logger.info(f"Custom command received from user {update.effective_user.id}")

        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a post link:\n"
                "`/custom https://t.me/channel_name/123`\n\n"
                "📋 Supported formats:\n"
                "• `https://t.me/channel_name/123`\n"
                "• `https://t.me/c/123456789/123`\n"
                "• `https://telegram.me/channel_name/123`\n\n"
                "💡 This will remove existing bot reactions and let you set custom ones.",
                parse_mode='Markdown'
            )
            return

        try:
            post_url = context.args[0].strip()
            user_id = update.effective_user.id

            # Parse the post link
            post_info = self.parse_post_link(post_url)
            if not post_info:
                await update.message.reply_text(
                    "❌ Invalid post link format!\n"
                    "Please use a valid Telegram post link like:\n"
                    "`https://t.me/channel_name/123`"
                )
                return

            chat_id = post_info['chat_id']
            message_id = post_info['message_id']

            await update.message.reply_text(
                f"🔍 Processing post link...\n"
                f"📍 Chat: {chat_id}\n"
                f"📄 Message ID: {message_id}\n"
                f"🔄 Removing existing bot reactions..."
            )

            # Remove existing bot reactions
            removed_count = await self.remove_bot_reactions(chat_id, message_id)

            # Store pending custom post info
            self.pending_custom_posts[user_id] = {
                'chat_id': chat_id,
                'message_id': message_id,
                'post_url': post_url
            }

            await update.message.reply_text(
                f"✅ Removed {removed_count} bot reactions from the post!\n\n"
                f"🎭 Now send me the custom emojis you want to use:\n"
                f"Format: `[emoji1,emoji2,emoji3,...]`\n"
                f"Example: `[🔥,💯,⚡,🚀,❤️]`\n\n"
                f"💡 These emojis will be used ONLY for this specific post.",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"Error in custom command: {e}")
            await update.message.reply_text(
                f"❌ Error processing custom command: {str(e)}\n"
                "Please check the post link and try again."
            )

    async def handle_custom_emoji_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle emoji input for custom posts. Returns True if handled, False otherwise"""
        user_id = update.effective_user.id

        # Check if user has a pending custom post
        if user_id not in self.pending_custom_posts:
            return False

        message_text = update.message.text

        # Check if message contains emoji list format
        if not (message_text.startswith('[') and message_text.endswith(']')):
            return False

        try:
            # Extract emojis from brackets
            emoji_string = message_text[1:-1]  # Remove brackets
            emoji_list = [emoji.strip() for emoji in emoji_string.split(',')]
            emoji_list = [emoji for emoji in emoji_list if emoji]  # Filter empty

            if not emoji_list:
                await update.message.reply_text("❌ No valid emojis found!")
                return True

            if len(emoji_list) > 20:
                await update.message.reply_text("❌ Too many emojis! Maximum 20 emojis allowed.")
                return True

            # Get pending post info
            post_info = self.pending_custom_posts[user_id]
            chat_id = post_info['chat_id']
            message_id = post_info['message_id']
            post_url = post_info['post_url']

            # Save to database
            custom_post_doc = {
                'chat_id': chat_id,
                'message_id': message_id,
                'post_url': post_url,
                'custom_emojis': emoji_list,
                'created_by': user_id,
                'created_at': time.time()
            }

            # Remove existing entry if exists
            self.custom_posts_collection.delete_many({
                'chat_id': chat_id,
                'message_id': message_id
            })

            # Insert new custom post
            self.custom_posts_collection.insert_one(custom_post_doc)

            # Apply custom reactions to the post
            applied_count = await self.apply_custom_reactions(chat_id, message_id, emoji_list)

            # Clean up pending post
            del self.pending_custom_posts[user_id]

            await update.message.reply_text(
                f"✅ Custom reactions applied successfully!\n"
                f"🎭 Emojis: {', '.join(emoji_list)}\n"
                f"🤖 Applied by {applied_count} bots\n"
                f"📍 Post: {post_url}\n\n"
                f"💡 These bots will now use only these emojis for this specific post!"
            )

            return True

        except Exception as e:
            logger.error(f"Error handling custom emoji input: {e}")
            await update.message.reply_text(
                f"❌ Error processing emojis: {str(e)}\n"
                "Please check the format and try again."
            )
            return True

    async def apply_custom_reactions(self, chat_id: str, message_id: int, emoji_list: List[str]) -> int:
        """Apply custom reactions to a specific post using available bots"""
        applied_count = 0

        # Get available bots
        running_bot_names = list(self.running_bots.keys())

        for i, bot_name in enumerate(running_bot_names):
            try:
                bot_info = self.running_bots[bot_name]
                application = bot_info.get('application')

                if application and application.bot:
                    # Assign emoji based on bot index
                    emoji_index = i % len(emoji_list)
                    assigned_emoji = emoji_list[emoji_index]

                    from telegram import ReactionTypeEmoji

                    await application.bot.set_message_reaction(
                        chat_id=chat_id,
                        message_id=message_id,
                        reaction=[ReactionTypeEmoji(emoji=assigned_emoji)],
                        is_big=False
                    )

                    applied_count += 1
                    logger.info(f"Applied custom reaction {assigned_emoji} from {bot_name}")

                    # Small delay between reactions
                    await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"Error applying custom reaction from {bot_name}: {e}")

        return applied_count

    def get_custom_post_emojis(self, chat_id: str, message_id: int) -> Optional[List[str]]:
        """Get custom emojis for a specific post"""
        try:
            custom_post = self.custom_posts_collection.find_one({
                'chat_id': str(chat_id),
                'message_id': int(message_id)
            })

            if custom_post:
                return custom_post.get('custom_emojis', [])

            return None
        except Exception as e:
            logger.error(f"Error getting custom post emojis: {e}")
            return None

    async def unclone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /unclone command to remove a bot from the system"""
        logger.info(f"Unclone command received from user {update.effective_user.id}")

        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a bot username to unclone:\n"
                "`/unclone @bot_username`\n\n"
                "📋 Example:\n"
                "`/unclone @MyTestBot`\n\n"
                "💡 Use `/clone_list` to see all available bots",
                parse_mode='Markdown'
            )
            return

        try:
            target_username = context.args[0].strip()

            # Remove @ if present
            if target_username.startswith('@'):
                target_username = target_username[1:]

            # Find bot by username
            target_bot = None
            all_bots = self.get_all_bots()

            await update.message.reply_text("🔍 Searching for bot...")

            for bot in all_bots:
                try:
                    bot_token = bot.get('token', '')
                    if bot_token:
                        test_url = f"https://api.telegram.org/bot{bot_token}/getMe"
                        response = requests.get(test_url, timeout=5)
                        if response.status_code == 200:
                            bot_info = response.json()
                            if bot_info.get('ok'):
                                bot_username = bot_info.get('result', {}).get('username', '')
                                if bot_username.lower() == target_username.lower():
                                    target_bot = bot
                                    break
                except:
                    continue

            if not target_bot:
                await update.message.reply_text(
                    f"❌ Bot @{target_username} not found in the system!\n"
                    "💡 Use `/clone_list` to see all available bots"
                )
                return

            bot_name = target_bot['name']
            bot_token = target_bot['token']
            bot_port = target_bot['port']

            # Check if it's the main bot
            main_token = os.getenv('TOKEN')
            if bot_token == main_token:
                await update.message.reply_text(
                    "❌ Cannot unclone the main bot!\n"
                    "The main bot is required for system operation."
                )
                return

            await update.message.reply_text(
                f"⚠️ Found bot: {bot_name} (@{target_username})\n"
                f"🔌 Port: {bot_port}\n"
                f"🔄 Removing from system..."
            )

            # Stop the bot instance
            with _instance_lock:
                if bot_token in _running_instances:
                    _running_instances.discard(bot_token)
                    logger.info(f"Removed {bot_name} from running instances")

            # Remove bot from running_bots dict
            if bot_name in self.running_bots:
                del self.running_bots[bot_name]

            # Get the bot that was pointing to this bot
            previous_bot = self.bots_collection.find_one({"next_url": f"http://0.0.0.0:{bot_port}/signal"})

            # Get the bot this bot was pointing to
            next_url = target_bot.get('next_url', '')

            # Update the chain connection
            if previous_bot:
                # Update previous bot to point to the next bot in chain
                self.bots_collection.update_one(
                    {"_id": previous_bot["_id"]},
                    {"$set": {"next_url": next_url}}
                )
                logger.info(f"Updated {previous_bot['name']} to point to: {next_url}")

            # Remove bot from database
            self.bots_collection.delete_one({"_id": target_bot["_id"]})
            logger.info(f"Removed bot {bot_name} from database")

            # Get updated bot count
            remaining_bots = self.get_all_bots()
            running_count = len([bot for bot in remaining_bots if bot.get('token') in _running_instances])

            await update.message.reply_text(
                f"✅ Bot uncloned successfully!\n"
                f"📝 Removed: {bot_name} (@{target_username})\n"
                f"🔌 Freed port: {bot_port}\n"
                f"🔗 Chain reconnected successfully\n"
                f"📊 Remaining bots: {len(remaining_bots)}\n"
                f"🏃 Running bots: {running_count}\n\n"
                f"💡 The bot chain has been automatically reconstructed!"
            )

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error in unclone command: {e}")
            await update.message.reply_text(
                "❌ Network error while processing request. Please try again."
            )
        except Exception as e:
            logger.error(f"Error in unclone command: {e}")
            await update.message.reply_text(
                f"❌ Error uncloning bot: {str(e)}\n"
                "Please check the logs for more details."
            )

    async def clone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clone command"""
        logger.info(f"Clone command received from user {update.effective_user.id}")

        if not context.args:
            await update.message.reply_text(
                "❌ Please provide a bot token:\n"
                "`/clone YOUR_BOT_TOKEN`\n\n"
                "📋 Example:\n"
                "`/clone 1234567890:AAFbfZhf-abcdefghijk123456789`",
                parse_mode='Markdown'
            )
            return

        try:
            new_token = context.args[0].strip()

            # Validate token format (basic validation)
            if not new_token or len(new_token) < 40 or ':' not in new_token:
                await update.message.reply_text(
                    "❌ Invalid bot token format!\n"
                    "Bot tokens should look like: `1234567890:AAFbfZhf...`",
                    parse_mode='Markdown'
                )
                return

            # Check if token already exists in running instances
            with _instance_lock:
                if new_token in _running_instances:
                    await update.message.reply_text("❌ This bot token is already running!")
                    return

            # Check if bot already exists in database
            existing_bot = self.bots_collection.find_one({"token": new_token})
            if existing_bot:
                await update.message.reply_text(
                    f"❌ This bot token is already in the database!\n"
                    f"Bot name: {existing_bot['name']}\n"
                    f"Port: {existing_bot['port']}"
                )
                return

            # Test the token by making a simple API call
            await update.message.reply_text("🔍 Validating bot token...")

            test_url = f"https://api.telegram.org/bot{new_token}/getMe"
            response = requests.get(test_url, timeout=10)

            if response.status_code != 200:
                await update.message.reply_text(
                    "❌ Invalid bot token! The token doesn't work with Telegram API."
                )
                return

            bot_info = response.json()
            if not bot_info.get('ok'):
                await update.message.reply_text(
                    "❌ Invalid bot token! API returned an error."
                )
                return

            bot_count = self.bots_collection.count_documents({})
            bot_name = f"clone_bot_{bot_count}"
            bot_username = bot_info.get('result', {}).get('username', 'unknown')

            await update.message.reply_text(
                f"✅ Token validated!\n"
                f"🤖 Bot: @{bot_username}\n"
                f"🔄 Adding to system as {bot_name}..."
            )

            # Add bot to database
            new_bot = self.add_bot_to_database(bot_name, new_token)

            # Start the new bot in a separate thread
            thread = threading.Thread(
                target=self.start_single_bot_sync,
                args=(new_bot,),
                daemon=True
            )
            thread.start()

            # Give it time to start
            await asyncio.sleep(5)

            await update.message.reply_text(
                f"✅ Bot cloned successfully!\n"
                f"📝 Name: {bot_name}\n"
                f"🤖 Username: @{bot_username}\n"
                f"🔌 Port: {new_bot['port']}\n"
                f"🔗 Signal URL: http://0.0.0.0:{new_bot['port']}/signal\n"
                f"🏃 Total bots running: {len(_running_instances)}\n\n"
                f"🎉 Your new bot is ready to receive messages!"
            )

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error in clone command: {e}")
            await update.message.reply_text(
                "❌ Network error while validating token. Please try again."
            )
        except Exception as e:
            logger.error(f"Error in clone command: {e}")
            await update.message.reply_text(
                f"❌ Error cloning bot: {str(e)}\n"
                "Please check the logs for more details."
            )

    async def message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE, bot_name: str):
        """Handle regular messages with emoji reactions"""
        try:
            # Get the message from update (could be message, edited_message, channel_post, etc.)
            message = (update.message or 
                      update.edited_message or 
                      update.channel_post or 
                      update.edited_channel_post)

            if not message:
                logger.warning(f"Bot {bot_name}: No message found in update")
                return

            # Check if message is in a channel or group
            chat_type = update.effective_chat.type
            is_channel_or_group = chat_type in ['channel', 'group', 'supergroup']

            logger.info(f"Bot {bot_name} processing message in {chat_type} chat")

            # Check if this is a custom post with specific emojis
            custom_emojis = self.get_custom_post_emojis(str(message.chat_id), message.message_id)

            if custom_emojis:
                # Use custom emojis for this specific post
                logger.info(f"Using custom emojis for post {message.message_id}: {custom_emojis}")

                # Get bot index for emoji assignment
                all_bots = self.get_all_bots()
                bot_index = next((i for i, bot in enumerate(all_bots) if bot.get("name") == bot_name), 0)
                emoji_index = bot_index % len(custom_emojis)
                assigned_emoji = custom_emojis[emoji_index]

            else:
                # Use random emoji assignment from all packs
                assigned_emoji = self.assign_emoji_to_bot(bot_name, str(message.message_id))

            if is_channel_or_group:
                logger.info(f"Bot {bot_name} attempting to add reaction: {assigned_emoji}")

                try:
                    # Import ReactionTypeEmoji for proper emoji reactions
                    from telegram import ReactionTypeEmoji

                    # Use the correct method for setting reactions with proper emoji format
                    await context.bot.set_message_reaction(
                        chat_id=message.chat_id,
                        message_id=message.message_id,
                        reaction=[ReactionTypeEmoji(emoji=assigned_emoji)],
                        is_big=False
                    )
                    logger.info(f"Bot {bot_name} successfully added reaction {assigned_emoji} to message in {chat_type}")

                except BadRequest as e:
                    logger.error(f"BadRequest adding reaction {assigned_emoji}: {e}")
                    # Don't send fallback text messages - only log the error

                except AttributeError as e:
                    logger.warning(f"Reaction method not available: {e}")
                    # Don't send fallback text messages - only log the error

                except Exception as e:
                    logger.error(f"Error adding reaction {assigned_emoji}: {e}")

            else:
                # In private chats: Handle custom emoji input or add reactions
                # First check if this is custom emoji input
                handled_custom = await self.handle_custom_emoji_input(update, context)
                if handled_custom:
                    return  # Don't continue with regular reaction logic

                # Regular private chat reactions
                try:
                    from telegram import ReactionTypeEmoji

                    await context.bot.set_message_reaction(
                        chat_id=message.chat_id,
                        message_id=message.message_id,
                        reaction=[ReactionTypeEmoji(emoji=assigned_emoji)],
                        is_big=False
                    )
                    logger.info(f"Bot {bot_name} added reaction {assigned_emoji} in private chat")
                except Exception as e:
                    logger.error(f"Failed to add reaction in private chat: {e}")

            # Signal next bot in chain
            threading.Thread(
                target=self.send_signal_to_next_bot,
                args=(bot_name,),
                daemon=True
            ).start()

        except Exception as e:
            logger.error(f"Error in message handler for {bot_name}: {e}")

    def start_single_bot_sync(self, bot_config: dict):
        """Start a single bot with its Telegram polling and Flask server"""
        bot_name = bot_config["name"]
        token = bot_config["token"]
        port = bot_config["port"]

        # Prevent duplicate token usage
        with _instance_lock:
            if token in _running_instances:
                logger.warning(f"Bot with token {token[:10]}... is already running, skipping")
                return
            _running_instances.add(token)

        try:
            logger.info(f"Starting bot {bot_name} on port {port}")

            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Create Telegram application with specific settings
            application = (Application.builder()
                          .token(token)
                          .concurrent_updates(True)
                          .build())

            # Add handlers
            application.add_handler(CommandHandler(
                "start", 
                lambda update, context: self.start_command(update, context, bot_name)
            ))

            # Add clone and emoji commands to the first bot (lowest port number) or main bot
            is_main_bot = (bot_name == "main_bot" or 
                          bot_config.get("port", 999999) == 5000 or
                          self.bots_collection.count_documents({"port": {"$lt": bot_config.get("port", 999999)}}) == 0)

            if is_main_bot:
                application.add_handler(CommandHandler("clone", self.clone_command))
                
                # Add multiple emoji pack handlers (emoji1 through emoji10)
                for i in range(1, 11):
                    application.add_handler(CommandHandler(
                        f"emoji{i}", 
                        lambda update, context, num=i: self.emoji_pack_command(update, context, num)
                    ))
                
                application.add_handler(CommandHandler("emoji_list", self.emoji_list_command))
                application.add_handler(CommandHandler("clone_list", self.clone_list_command))
                application.add_handler(CommandHandler("unclone", self.unclone_command))
                application.add_handler(CommandHandler("custom", self.custom_command))
                
                # Add delete emoji pack handlers (del_emoji1 through del_emoji10)
                for i in range(1, 11):
                    application.add_handler(CommandHandler(
                        f"del_emoji{i}", 
                        lambda update, context, num=i: self.delete_emoji_pack_command(update, context, num)
                    ))
                
                # Add callback query handler for confirmation buttons
                from telegram.ext import CallbackQueryHandler
                application.add_handler(CallbackQueryHandler(self.handle_delete_confirmation))
                
                logger.info(f"Added clone, emoji packs (1-10), emoji_list, clone_list, unclone, custom, and delete emoji commands to bot {bot_name}")

            # Handle all types of messages (private, group, channel posts)
            application.add_handler(MessageHandler(
                filters.ALL & ~filters.COMMAND,
                lambda update, context: self.message_handler(update, context, bot_name)
            ))

            # Create Flask app for signals
            flask_app = self.create_flask_app(bot_name, port)

            # Assign emoji pack to this bot
            bot_emoji_pack = self.assign_emoji_pack_to_bot(bot_name)
            self.bot_emoji_assignment[bot_name] = bot_emoji_pack

            # Store bot info
            self.running_bots[bot_name] = {
                "application": application,
                "flask_app": flask_app,
                "port": port,
                "config": bot_config,
                "emoji_pack": bot_emoji_pack
            }

            # Start Flask server in thread
            def run_flask():
                try:
                    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
                except Exception as e:
                    logger.error(f"Flask server error for {bot_name}: {e}")

            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()
            time.sleep(2)  # Give Flask time to start

            # Start Telegram polling
            async def run_bot():
                try:
                    await application.initialize()
                    await application.start()

                    # Start polling with error handling
                    await application.updater.start_polling(
                        drop_pending_updates=True,
                        allowed_updates=Update.ALL_TYPES
                    )

                    logger.info(f"Bot {bot_name} started successfully")

                    # Keep running until stopped
                    while token in _running_instances:
                        await asyncio.sleep(1)

                except Exception as e:
                    logger.error(f"Bot {bot_name} polling error: {e}")
                finally:
                    try:
                        await application.stop()
                        await application.shutdown()
                    except:
                        pass

            loop.run_until_complete(run_bot())

        except Exception as e:
            logger.error(f"Failed to start bot {bot_name}: {e}")
        finally:
            # Clean up on exit
            with _instance_lock:
                _running_instances.discard(token)
            logger.info(f"Bot {bot_name} stopped")

    def start_all_bots(self):
        """Start all bots from database"""
        global _system_running

        if _system_running:
            logger.warning("Bot system is already running!")
            return

        _system_running = True

        try:
            self.init_database()

            # Get main bot token from environment
            main_token = os.getenv('TOKEN')
            if not main_token:
                raise ValueError("Main bot TOKEN not found in environment variables")

            # Check if main bot exists in database, if not add it
            main_bot = self.bots_collection.find_one({"token": main_token})
            if not main_bot:
                logger.info("Main bot not found in database, adding it...")
                main_bot = self.add_bot_to_database("main_bot", main_token)
            else:
                logger.info(f"Found existing main bot: {main_bot['name']}")

            # Get all bots from database
            all_bots = self.get_all_bots()

            if not all_bots:
                logger.error("No bots found in database")
                return

            # Start each bot in its own thread
            threads = []
            for bot_config in all_bots:
                thread = threading.Thread(
                    target=self.start_single_bot_sync,
                    args=(bot_config,),
                    daemon=True
                )
                thread.start()
                threads.append(thread)
                time.sleep(2)  # Stagger bot starts to avoid conflicts

            logger.info(f"All {len(all_bots)} bots started successfully")

            # Keep main thread alive
            try:
                while True:
                    time.sleep(60)
                    logger.info(f"System running with {len(_running_instances)} active bots")
            except KeyboardInterrupt:
                logger.info("Bot system stopped by user")
                _system_running = False

        except Exception as e:
            logger.error(f"Error starting bots: {e}")
            _system_running = False
            raise

def main():
    """Main function to start the bot system"""
    logger.info("Starting Telegram Reaction Bot System...")

    # Start keep alive service
    start_keep_alive()

    bot_manager = BotManager()

    try:
        # Run the bot system
        bot_manager.start_all_bots()
    except KeyboardInterrupt:
        logger.info("Bot system stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

if __name__ == "__main__":
    main()
