import os
import json
import sqlite3
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional
import aiohttp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '3600'))  # Default: 1 hour
DATA_FILE = os.getenv('DATA_FILE', 'watched_games.json')
HISTORY_FILE = os.getenv('HISTORY_FILE', 'price_history.db')

# Supported currencies
CURRENCIES = ['us', 'gb', 'eu', 'ru', 'br', 'au', 'jp', 'in', 'ca', 'cn']


class SteamPriceMonitor:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.watched_games: Dict = {}
        self.load_data()
        self.init_history_db()

    def init_history_db(self):
        """Initialize the SQLite database for price-change history"""
        try:
            self.history_conn = sqlite3.connect(HISTORY_FILE)
            self.history_conn.execute("PRAGMA journal_mode=WAL")
            self.history_conn.execute("PRAGMA synchronous=NORMAL")
            self.history_conn.execute("""
                CREATE TABLE IF NOT EXISTS price_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    chat_id TEXT,
                    app_id TEXT NOT NULL,
                    game_name TEXT,
                    currency TEXT,
                    prev_price REAL,
                    new_price REAL NOT NULL,
                    prev_discount REAL,
                    new_discount REAL NOT NULL,
                    change_type TEXT NOT NULL
                )
            """)
            self.history_conn.commit()
            logger.info(f"Price history database initialized: {HISTORY_FILE}")
        except Exception as e:
            logger.error(f"Error initializing history database: {e}")
            self.history_conn = None

    def log_price_change(self, chat_id, app_id, game_name, currency,
                         prev_price, new_price, prev_discount, new_discount,
                         change_type):
        """Record a single price-change event in the history database"""
        if self.history_conn is None:
            logger.warning("History DB unavailable; skipping price log")
            return False
        try:
            self.history_conn.execute(
                """INSERT INTO price_changes
                   (timestamp, chat_id, app_id, game_name, currency,
                    prev_price, new_price, prev_discount, new_discount, change_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(),
                    str(chat_id),
                    str(app_id),
                    game_name,
                    str(currency),
                    prev_price,
                    new_price,
                    prev_discount,
                    new_discount,
                    change_type,
                )
            )
            self.history_conn.commit()
            logger.info(f"Logged {change_type} for {game_name} (App {app_id})")
            return True
        except Exception as e:
            logger.error(f"Error logging price change: {e}")
            return False

    def query_price_history(self, app_id, currency=None, limit=50):
        """Query logged price changes for a game, newest first"""
        if self.history_conn is None:
            return []
        try:
            query = "SELECT * FROM price_changes WHERE app_id = ?"
            params = [str(app_id)]
            if currency:
                query += " AND currency = ?"
                params.append(str(currency).lower())
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            cursor = self.history_conn.execute(query, params)
            rows = cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            logger.error(f"Error querying history: {e}")
            return []

    def load_data(self):
        """Load watched games from file"""
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r') as f:
                    self.watched_games = json.load(f)
                logger.info(f"Loaded {len(self.watched_games)} watched games")
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            self.watched_games = {}

    def save_data(self):
        """Save watched games to file"""
        try:
            with open(DATA_FILE, 'w') as f:
                json.dump(self.watched_games, f, indent=2)
            logger.info("Data saved successfully")
        except Exception as e:
            logger.error(f"Error saving data: {e}")

    async def init_session(self):
        """Initialize aiohttp session"""
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def close_session(self):
        """Close aiohttp session"""
        if self.session:
            await self.session.close()

    async def get_game_details(self, app_id: str, country: str = 'us') -> Optional[Dict]:
        """Fetch game details from Steam API - WITHOUT filters to get full data"""
        await self.init_session()
        
        url = f"https://store.steampowered.com/api/appdetails"
        params = {
            'appids': app_id,
            'cc': country,
            'l': 'english'
        }
        
        try:
            async with self.session.get(url, params=params, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Steam API returns data with app_id as key
                    app_id_str = str(app_id)
                    if app_id_str in data and data[app_id_str].get('success'):
                        game_data = data[app_id_str]['data']
                        game_name = game_data.get('name', 'Unknown Game')
                        logger.info(f"Successfully fetched data for app {app_id}: {game_name}")
                        return game_data
                    else:
                        logger.warning(f"API returned success=false for {app_id}")
                        if app_id_str in data:
                            logger.debug(f"Response: {data[app_id_str]}")
                        return None
                else:
                    logger.error(f"HTTP {response.status} for app {app_id}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching game details for {app_id}")
            return None
        except Exception as e:
            logger.error(f"Error fetching game details for {app_id}: {e}")
            return None

    def add_watch(self, chat_id: str, app_id: str, currency: str, game_name: str = None):
        """Add a game to watch list"""
        key = f"{chat_id}_{app_id}_{currency}"
        
        # Get user's default apprise URLs
        user_key = f"user_{chat_id}"
        apprise_urls = self.watched_games.get(user_key, {}).get('default_apprise_urls', [])
        
        if key not in self.watched_games:
            self.watched_games[key] = {
                'chat_id': chat_id,
                'app_id': app_id,
                'currency': currency,
                'last_price': None,
                'last_discount': None,
                'game_name': game_name,
                'apprise_urls': apprise_urls,
                'added_at': datetime.now().isoformat()
            }
            self.save_data()
            return True
        return False

    def remove_watch(self, chat_id: str, app_id: str, currency: str):
        """Remove a game from watch list"""
        key = f"{chat_id}_{app_id}_{currency}"
        if key in self.watched_games:
            del self.watched_games[key]
            self.save_data()
            return True
        return False

    def get_user_watches(self, chat_id: str) -> List[Dict]:
        """Get all watched games for a user"""
        return [
            data for key, data in self.watched_games.items()
            if data.get('chat_id') == chat_id and not key.startswith('user_')
        ]

    def set_user_apprise(self, chat_id: str, urls: List[str]):
        """Set default Apprise URLs for a user"""
        user_key = f"user_{chat_id}"
        self.watched_games[user_key] = {
            'chat_id': chat_id,
            'default_apprise_urls': urls,
            'updated_at': datetime.now().isoformat()
        }
        self.save_data()

    def get_user_apprise(self, chat_id: str) -> List[str]:
        """Get user's default Apprise URLs"""
        user_key = f"user_{chat_id}"
        return self.watched_games.get(user_key, {}).get('default_apprise_urls', [])

    def clear_user_apprise(self, chat_id: str):
        """Clear user's default Apprise URLs"""
        user_key = f"user_{chat_id}"
        if user_key in self.watched_games:
            del self.watched_games[user_key]
            self.save_data()
            return True
        return False

    async def check_price_changes(self, application: Application):
        """Check for price changes for all watched games"""
        logger.info("Checking price changes...")
        
        for key, watch_data in list(self.watched_games.items()):
            # Skip user configuration entries
            if key.startswith('user_'):
                continue
            
            # Validate watch_data has required keys
            if 'app_id' not in watch_data or 'currency' not in watch_data:
                logger.error(f"Invalid watch data for key {key}: missing required fields")
                continue
                
            try:
                game_data = await self.get_game_details(
                    watch_data['app_id'],
                    watch_data['currency']
                )
                
                if not game_data:
                    logger.warning(f"Could not fetch data for app {watch_data['app_id']}")
                    continue

                # ALWAYS update game name from API response
                game_name = game_data.get('name', 'Unknown Game')
                if watch_data.get('game_name') != game_name:
                    watch_data['game_name'] = game_name
                    logger.info(f"Updated game name to: {game_name}")
                    self.save_data()
                
                # Check if game has price info
                price_overview = game_data.get('price_overview')
                
                if not price_overview:
                    # Game might be free or unavailable
                    logger.info(f"No price info for {watch_data['game_name']} (might be free)")
                    continue

                current_price = price_overview.get('final', 0) / 100  # Convert cents to currency
                current_discount = price_overview.get('discount_percent', 0)
                currency_symbol = price_overview.get('currency', watch_data['currency'].upper())
                
                logger.info(f"{watch_data['game_name']}: {currency_symbol} {current_price:.2f} ({current_discount}% off)")
                
                # Check for changes
                price_changed = (watch_data['last_price'] is not None and 
                               watch_data['last_price'] != current_price)
                discount_changed = (watch_data['last_discount'] is not None and 
                                  watch_data['last_discount'] != current_discount)
                
                if price_changed or discount_changed or watch_data['last_price'] is None:
                    # Prepare notification message
                    message = self.format_notification(
                        watch_data['game_name'],
                        watch_data['app_id'],
                        current_price,
                        current_discount,
                        watch_data['last_price'],
                        watch_data['last_discount'],
                        currency_symbol
                    )
                    
                    # Send notification to Telegram
                    try:
                        await application.bot.send_message(
                            chat_id=watch_data['chat_id'],
                            text=message,
                            parse_mode='HTML',
                            disable_web_page_preview=True
                        )
                        logger.info(f"Sent Telegram notification for {watch_data['game_name']}")
                    except Exception as e:
                        logger.error(f"Error sending Telegram message: {e}")
                    
                    # Send to Apprise endpoints if configured
                    apprise_urls = watch_data.get('apprise_urls', [])
                    if apprise_urls:
                        logger.info(f"Sending Apprise notifications to {len(apprise_urls)} endpoint(s)")
                        await self.send_apprise_notifications(
                            apprise_urls,
                            watch_data['game_name'],
                            message
                        )
                    
                    # Determine change type and log to history database
                    prev_price = watch_data['last_price']
                    prev_discount = watch_data['last_discount']
                    if prev_price is None and prev_discount is None:
                        change_type = 'initial'
                    elif current_price != (prev_price or 0) and current_discount != (prev_discount or 0):
                        change_type = 'price_and_discount_change'
                    elif current_price != (prev_price or 0):
                        change_type = 'price_change'
                    elif (prev_discount or 0) == 0 and current_discount > 0:
                        change_type = 'discount_start'
                    elif current_discount == 0 and (prev_discount or 0) > 0:
                        change_type = 'discount_end'
                    elif current_discount != (prev_discount or 0):
                        change_type = 'discount_change'
                    else:
                        change_type = 'change'
                    
                    self.log_price_change(
                        watch_data['chat_id'],
                        watch_data['app_id'],
                        watch_data['game_name'],
                        watch_data['currency'],
                        prev_price,
                        current_price,
                        prev_discount,
                        current_discount,
                        change_type
                    )
                    
                    # Update stored values
                    watch_data['last_price'] = current_price
                    watch_data['last_discount'] = current_discount
                    self.save_data()
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Error checking game {watch_data.get('app_id')}: {e}")

    def format_notification(self, name, app_id, current_price, current_discount, 
                          last_price, last_discount, currency):
        """Format price change notification"""
        message = f"🎮 <b>{name}</b>\n"
        message += f"Steam App ID: {app_id}\n\n"
        
        if last_price is None:
            message += f"💰 Current Price: {currency} {current_price:.2f}\n"
            if current_discount > 0:
                message += f"🔥 Discount: {current_discount}% OFF\n"
            message += "\n✅ Now monitoring this game!"
        else:
            if current_price != last_price:
                change = "📉 PRICE DROP" if current_price < last_price else "📈 PRICE INCREASE"
                message += f"{change}\n"
                message += f"Old: {currency} {last_price:.2f}\n"
                message += f"New: {currency} {current_price:.2f}\n"
                diff = abs(current_price - last_price)
                message += f"Change: {currency} {diff:.2f}\n\n"
            
            if current_discount != last_discount:
                if current_discount > 0 and (last_discount == 0 or last_discount is None):
                    message += f"🔥 NEW DISCOUNT: {current_discount}% OFF!\n"
                elif current_discount > last_discount:
                    message += f"🔥 BIGGER DISCOUNT: {current_discount}% OFF (was {last_discount}%)\n"
                elif current_discount == 0:
                    message += f"⚠️ Discount ended (was {last_discount}%)\n"
                else:
                    message += f"📊 Discount: {current_discount}% OFF\n"
        
        message += f"\n🔗 <a href='https://store.steampowered.com/app/{app_id}'>View on Steam</a>"
        return message

    async def send_apprise_notifications(self, urls: List[str], title: str, message: str):
        """Send notifications via Apprise endpoints"""
        if not urls:
            return
            
        try:
            import apprise
            apobj = apprise.Apprise()
            
            for url in urls:
                apobj.add(url)
            
            # Strip HTML tags for plain text notifications
            import re
            plain_message = re.sub('<[^<]+?>', '', message)
            
            # Send notification
            success = apobj.notify(
                body=plain_message,
                title=f"Steam Price Alert: {title}"
            )
            
            if success:
                logger.info(f"Successfully sent Apprise notifications to {len(urls)} endpoint(s)")
            else:
                logger.error(f"Failed to send Apprise notifications")
                
        except ImportError:
            logger.warning("Apprise not installed. Install with: pip install apprise")
        except Exception as e:
            logger.error(f"Error sending Apprise notification: {e}")


# Global monitor instance
monitor = SteamPriceMonitor()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_message = """
🎮 <b>Steam Price Monitor Bot</b>

I'll help you track Steam game prices and notify you when they change!

<b>Commands:</b>
/watch - Add a game to watch
/list - Show your watched games
/remove - Remove a game from watch list
/history - View logged price-change history
/apprise - Manage notification endpoints
/help - Show this help message

<b>Usage:</b>
Use /watch followed by the Steam App ID and currency code.
Example: <code>/watch 570 us</code>

You can find the App ID in the Steam store URL.
Supported currencies: us, gb, eu, ru, br, au, jp, in, ca, cn

<b>Apprise Notifications:</b>
Configure additional notification endpoints with /apprise
Supported: Discord, Slack, Email, Pushover, and more!
"""
    await update.message.reply_text(welcome_message, parse_mode='HTML')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    await start_command(update, context)


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /watch command"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: /watch <app_id> <currency>\n"
            "Example: /watch 570 us\n\n"
            f"Supported currencies: {', '.join(CURRENCIES)}\n\n"
            "💡 Tip: Set default notification endpoints with /apprise"
        )
        return
    
    app_id = context.args[0]
    currency = context.args[1].lower()
    
    if currency not in CURRENCIES:
        await update.message.reply_text(
            f"❌ Invalid currency. Supported: {', '.join(CURRENCIES)}"
        )
        return
    
    chat_id = str(update.effective_chat.id)
    
    # Check if game exists
    status_msg = await update.message.reply_text("⏳ Checking game details...")
    
    game_data = await monitor.get_game_details(app_id, currency)
    
    if not game_data:
        await status_msg.edit_text(
            f"❌ Could not find game with App ID {app_id}. "
            "Please check the ID and try again."
        )
        return
    
    game_name = game_data.get('name', 'Unknown Game')
    logger.info(f"Adding watch for game: {game_name} (App ID: {app_id})")
    
    # Add to watch list with game name
    added = monitor.add_watch(chat_id, app_id, currency, game_name)
    
    if added:
        user_apprise = monitor.get_user_apprise(chat_id)
        apprise_msg = ""
        if user_apprise:
            apprise_msg = f"\n📢 Notifications: Telegram + {len(user_apprise)} other endpoint(s)"
        
        await status_msg.edit_text(
            f"✅ Now watching <b>{game_name}</b>\n"
            f"App ID: {app_id}\n"
            f"Currency: {currency.upper()}{apprise_msg}\n\n"
            "You'll receive notifications when the price changes!",
            parse_mode='HTML'
        )
        # Trigger immediate check for this game
        await monitor.check_price_changes(context.application)
    else:
        await status_msg.edit_text(
            f"ℹ️ You're already watching <b>{game_name}</b> in {currency.upper()}",
            parse_mode='HTML'
        )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command"""
    chat_id = str(update.effective_chat.id)
    watches = monitor.get_user_watches(chat_id)
    
    if not watches:
        await update.message.reply_text(
            "📭 You're not watching any games yet.\n"
            "Use /watch to add games!"
        )
        return
    
    message = "📋 <b>Your Watched Games:</b>\n\n"
    
    for watch in watches:
        name = watch.get('game_name', 'Loading...')
        app_id = watch['app_id']
        currency = watch['currency'].upper()
        last_price = watch.get('last_price')
        discount = watch.get('last_discount', 0)
        
        message += f"🎮 <b>{name}</b>\n"
        message += f"   App ID: {app_id} | Currency: {currency}\n"
        
        if last_price is not None:
            message += f"   Last Price: {currency} {last_price:.2f}"
            if discount > 0:
                message += f" ({discount}% OFF)"
            message += "\n"
        
        message += "\n"
    
    await update.message.reply_text(message, parse_mode='HTML')


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command to view logged price changes"""
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Usage: /history <app_id> [currency]\n"
            "Example: /history 570 us\n\n"
            "Shows the logged price-change history for a game."
        )
        return

    app_id = context.args[0]
    currency = context.args[1].lower() if len(context.args) > 1 else None

    records = monitor.query_price_history(app_id, currency=currency)

    if not records:
        await update.message.reply_text(
            f"📭 No price history found for App ID {app_id}"
            f"{' (' + currency.upper() + ')' if currency else ''}."
        )
        return

    game_name = records[0].get('game_name', 'Unknown Game')
    currency_label = (records[0].get('currency') or 'us').upper()

    message = f"📊 <b>{game_name}</b> — Price History\n"
    message += f"App ID: {app_id}"
    if currency:
        message += f" | Currency: {currency_label}"
    message += f"\n({len(records)} change(s))\n\n"

    for rec in records:
        timestamp = rec['timestamp']
        change_type = rec.get('change_type', 'change')
        prev_price = rec.get('prev_price')
        new_price = rec.get('new_price')
        prev_discount = rec.get('prev_discount')
        new_discount = rec.get('new_discount')

        date_part = timestamp[:16].replace('T', ' ')  # YYYY-MM-DD HH:MM

        if change_type == 'price_change':
            icon = "📉" if new_price < prev_price else "📈"
            title = "Price change"
        elif change_type == 'price_and_discount_change':
            icon = "🔄"
            title = "Price + discount change"
        elif change_type == 'discount_start':
            icon = "🔥"
            title = "Discount started"
        elif change_type == 'discount_end':
            icon = "⚠️"
            title = "Discount ended"
        elif change_type == 'discount_change':
            icon = "📊"
            title = "Discount changed"
        else:
            icon = "✅"
            title = "Now monitoring"

        message += f"{icon} <b>{title}</b> — {date_part}\n"
        if change_type == 'initial':
            message += f"   Price: {currency_label} {new_price:.2f}"
            if new_discount > 0:
                message += f" ({new_discount}% OFF)"
            message += "\n"
        else:
            if prev_price is not None and new_price != prev_price:
                message += f"   Price: {currency_label} {prev_price:.2f} → {currency_label} {new_price:.2f}\n"
            if new_discount != (prev_discount or 0):
                if new_discount > (prev_discount or 0):
                    message += f"   Discount: {prev_discount}% → {new_discount}% OFF\n"
                else:
                    message += f"   Discount: {prev_discount}% → {new_discount}% OFF\n"
            elif new_discount > 0:
                message += f"   Discount: {new_discount}% OFF\n"
        message += "\n"

    # Trim if too long for one message
    if len(message) > 4000:
        message = message[:3900] + "\n… (truncated)"

    await update.message.reply_text(message, parse_mode='HTML')


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove command"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: /remove <app_id> <currency>\n"
            "Example: /remove 570 us"
        )
        return
    
    app_id = context.args[0]
    currency = context.args[1].lower()
    chat_id = str(update.effective_chat.id)
    
    removed = monitor.remove_watch(chat_id, app_id, currency)
    
    if removed:
        await update.message.reply_text(
            f"✅ Stopped watching App ID {app_id} ({currency.upper()})"
        )
    else:
        await update.message.reply_text(
            f"❌ You're not watching App ID {app_id} ({currency.upper()})"
        )


async def apprise_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /apprise command to manage notification endpoints"""
    chat_id = str(update.effective_chat.id)
    
    # If no subcommand, show current config
    if not context.args:
        urls = monitor.get_user_apprise(chat_id)
        if urls:
            message = "📢 <b>Your Notification Endpoints:</b>\n\n"
            for i, url in enumerate(urls, 1):
                masked_url = mask_url(url)
                message += f"{i}. <code>{masked_url}</code>\n"
            message += "\n<b>Commands:</b>\n"
            message += "• /apprise add &lt;url&gt; - Add endpoint\n"
            message += "• /apprise remove &lt;number&gt; - Remove endpoint\n"
            message += "• /apprise clear - Remove all endpoints\n"
            message += "• /apprise test - Test notifications"
        else:
            message = (
                "📢 <b>Apprise Notification Endpoints</b>\n\n"
                "You haven't configured any additional notification endpoints yet.\n\n"
                "<b>Supported Services:</b>\n"
                "• Discord, Slack, Telegram\n"
                "• Email (SMTP, Gmail, Outlook)\n"
                "• Pushover, Pushbullet\n"
                "• And many more!\n\n"
                "<b>Commands:</b>\n"
                "• /apprise add &lt;url&gt; - Add endpoint\n"
                "• /apprise test - Test notifications\n\n"
                "📖 Visit https://github.com/caronc/apprise for URL formats"
            )
        await update.message.reply_text(message, parse_mode='HTML')
        return
    
    subcommand = context.args[0].lower()
    
    # ADD subcommand
    if subcommand == 'add':
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Usage: /apprise add &lt;url&gt;\n"
                "Example: /apprise add discord://webhook_id/webhook_token\n\n"
                "📖 Visit https://github.com/caronc/apprise for URL formats",
                parse_mode='HTML'
            )
            return
        
        url = context.args[1]
        urls = monitor.get_user_apprise(chat_id)
        
        if url in urls:
            await update.message.reply_text("ℹ️ This endpoint is already configured")
            return
        
        urls.append(url)
        monitor.set_user_apprise(chat_id, urls)
        
        masked_url = mask_url(url)
        await update.message.reply_text(
            f"✅ Added notification endpoint:\n<code>{masked_url}</code>\n\n"
            "This will be used for all your watched games!",
            parse_mode='HTML'
        )
        return
    
    # REMOVE subcommand
    if subcommand == 'remove':
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Usage: /apprise remove &lt;number&gt;\n"
                "Use /apprise to see endpoint numbers",
                parse_mode='HTML'
            )
            return
        
        try:
            index = int(context.args[1]) - 1
            urls = monitor.get_user_apprise(chat_id)
            
            if not urls:
                await update.message.reply_text("ℹ️ You don't have any endpoints configured")
                return
            
            if 0 <= index < len(urls):
                removed_url = urls.pop(index)
                monitor.set_user_apprise(chat_id, urls)
                masked_url = mask_url(removed_url)
                await update.message.reply_text(
                    f"✅ Removed endpoint:\n<code>{masked_url}</code>",
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    f"❌ Invalid endpoint number. You have {len(urls)} endpoint(s).\n"
                    "Use /apprise to see your endpoints"
                )
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid number")
        return
    
    # CLEAR subcommand
    if subcommand == 'clear':
        urls = monitor.get_user_apprise(chat_id)
        if urls:
            monitor.clear_user_apprise(chat_id)
            await update.message.reply_text(
                f"✅ Cleared all {len(urls)} notification endpoint(s)"
            )
        else:
            await update.message.reply_text("ℹ️ You don't have any endpoints configured")
        return
    
    # TEST subcommand
    if subcommand == 'test':
        urls = monitor.get_user_apprise(chat_id)
        if not urls:
            await update.message.reply_text(
                "ℹ️ No endpoints configured. Add one with:\n"
                "/apprise add &lt;url&gt;",
                parse_mode='HTML'
            )
            return
        
        await update.message.reply_text("🔔 Sending test notification...")
        
        test_message = (
            "🎮 Steam Price Monitor Bot\n\n"
            "This is a test notification!\n"
            "Your Apprise endpoints are configured correctly."
        )
        
        await monitor.send_apprise_notifications(
            urls,
            "Test Notification",
            test_message
        )
        
        await update.message.reply_text(
            f"✅ Test notification sent to {len(urls)} endpoint(s)!\n"
            "Check your notification services."
        )
        return
    
    # Unknown subcommand - provide help
    await update.message.reply_text(
        "❌ Unknown command. Available commands:\n\n"
        "• /apprise - Show endpoints\n"
        "• /apprise add &lt;url&gt; - Add endpoint\n"
        "• /apprise remove &lt;number&gt; - Remove endpoint\n"
        "• /apprise clear - Clear all\n"
        "• /apprise test - Test notifications",
        parse_mode='HTML'
    )


def mask_url(url: str) -> str:
    """Mask sensitive parts of URLs for display"""
    import re
    
    # Discord webhook
    url = re.sub(r'(discord://\d+/)(.+)', r'\1****', url)
    # Generic pattern for tokens after last slash
    if '://' in url and url.count('/') > 2:
        parts = url.rsplit('/', 1)
        if len(parts[1]) > 10:
            url = f"{parts[0]}/****"
    
    return url


async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job to check price changes"""
    await monitor.check_price_changes(context.application)


async def post_init(application: Application):
    """Initialize after bot starts"""
    # Schedule periodic price checks
    job_queue = application.job_queue
    if job_queue is not None:
        job_queue.run_repeating(
            price_check_job,
            interval=CHECK_INTERVAL,
            first=10  # First check after 10 seconds
        )
        logger.info(f"Price check job scheduled every {CHECK_INTERVAL} seconds")
    else:
        logger.error("JobQueue not available. Please install: pip install 'python-telegram-bot[job-queue]'")
        raise RuntimeError("JobQueue is required for this bot to function")


async def post_shutdown(application: Application):
    """Cleanup on shutdown"""
    await monitor.close_session()
    logger.info("Bot shutdown complete")


def main():
    """Start the bot"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Error: TELEGRAM_BOT_TOKEN not found in environment variables")
        logger.error("Please create a .env file with your bot token")
        print("\n❌ Error: Missing TELEGRAM_BOT_TOKEN")
        print("Please create a .env file with the following content:")
        print("\nTELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("CHECK_INTERVAL=3600  # Optional: Check interval in seconds (default: 3600)")
        print("DATA_FILE=watched_games.json  # Optional: Data file path (default: watched_games.json)")
        return
    
    try:
        # Create application
        application = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
        
        # Add handlers
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("watch", watch_command))
        application.add_handler(CommandHandler("list", list_command))
        application.add_handler(CommandHandler("remove", remove_command))
        application.add_handler(CommandHandler("history", history_command))
        application.add_handler(CommandHandler("apprise", apprise_command))
        
        # Start bot
        logger.info("Bot starting...")
        logger.info(f"Check interval: {CHECK_INTERVAL} seconds")
        logger.info(f"Data file: {DATA_FILE}")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except RuntimeError as e:
        logger.error(str(e))
        print("\n❌ Error: JobQueue not available")
        print("Please install the job-queue extra:")
        print("\npip install 'python-telegram-bot[job-queue]'")
        print("\nOr install all required dependencies:")
        print("pip install 'python-telegram-bot[job-queue]' aiohttp apprise python-dotenv")


if __name__ == '__main__':
    main()
