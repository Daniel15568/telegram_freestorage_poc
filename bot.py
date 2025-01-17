import os
import time
import hashlib
import logging
import tempfile
import asyncio
import json
import configparser
import zipfile
from datetime import datetime, timedelta
from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.helpers import escape_markdown
import pyzipper
import requests
from requests.exceptions import RequestException
from aiolimiter import AsyncLimiter

# --- DISCLAIMER ---
# This script is for academic and research purposes only.
# The author does not endorse or encourage the use of this script in violation
# of the Terms of Service of any platform, including Telegram.
# Use of this script is at your own risk.

# --- Configuration ---
config = configparser.ConfigParser()
config.read('config/config.ini')

# Telegram Settings
TOKEN = config['Telegram']['token']
CHAT_ID = int(config['Telegram']['chat_id'])
FORWARD_CHAT_ID = int(config['Telegram'].get('forward_chat_id', 0))
ENABLE_FORWARD = config['Telegram'].getboolean('enable_forward', False)

# General Settings
FOLDERS_TO_MONITOR = config['General']['folders_to_monitor'].split(',')
CHECK_INTERVAL = int(config['General']['check_interval'])
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45 MB
LOG_RETENTION_DAYS = int(config['General']['log_retention_days'])
FILE_HISTORY_PATH = 'data/bot_file_history.json'
FILE_SIZE_CACHE_PATH = 'data/file_size_cache.json'
ENABLE_ENCRYPTION = config['General'].getboolean('enable_encryption', False)
ZIP_PASSWORD = config['General'].get('zip_password', '')
ALLOWED_EXTENSIONS = set(config['General'].get('allowed_extensions', '').split(',')) if config['General'].get('allowed_extensions') else set()
ENABLE_CACHE = config['General'].getboolean('enable_cache', True)
COMPRESSION_LEVEL = config['General'].get('compression_level', 'default').lower()
DISABLE_LOGS = config['General'].getboolean('disable_logs', False)

# --- Configuration Validation ---
if ENABLE_ENCRYPTION and COMPRESSION_LEVEL == 'none':
    raise ValueError("Error: Encryption cannot be enabled when compression is set to 'none'.")

# --- Logging Configuration ---
os.makedirs('logs', exist_ok=True)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler = logging.FileHandler('logs/bot_log.txt')
log_handler.setFormatter(log_formatter)
logger = logging.getLogger()

if DISABLE_LOGS:
    logger.setLevel(logging.CRITICAL)
    logger.removeHandler(log_handler)
else:
    logger.setLevel(logging.DEBUG)
    logger.addHandler(log_handler)

# --- Global Variables ---
bot = Bot(token=TOKEN)
file_history = {}
file_counter = 0
file_size_cache = {}
error_messages = {}  # Store error message IDs

# Rate limiters to respect Telegram API limits
message_limiter = AsyncLimiter(30, 1)  # 30 messages per second
media_limiter = AsyncLimiter(20, 60)  # 20 media uploads per minute

# --- Utility Functions ---

def load_data(file_path):
    """Loads JSON data from a file."""
    os.makedirs('data', exist_ok=True)
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"File not found: {file_path}. Creating a new file.")
        return {}
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON from {file_path}. Creating a new file.")
        return {}

def save_data(data, file_path):
    """Saves JSON data to a file."""
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f)
        logger.info(f"Data saved to {file_path}")
    except Exception as e:
        logger.error(f"Error saving data to {file_path}: {str(e)}")

def calculate_md5(file_path):
    """Calculates the MD5 hash of a file."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

async def send_file(file_path, caption, part_number=None, total_parts=None):
    """Sends a file to Telegram, handling rate limits and potential errors."""
    async with media_limiter:
        try:
            escaped_caption = escape_markdown(caption, version=2)
            if part_number is not None and total_parts is not None:
                escaped_caption += f"\n\\(Part {part_number}/{total_parts}\\)"

            with open(file_path, 'rb') as file:
                message = await bot.send_document(chat_id=CHAT_ID, 
                                                  document=InputFile(file),
                                                  caption=escaped_caption, 
                                                  parse_mode=ParseMode.MARKDOWN_V2)
            logger.info(f"File sent successfully: {file_path}")

            if ENABLE_FORWARD:
                await forward_message(message)

            if file_path in error_messages:
                await bot.delete_message(chat_id=CHAT_ID, message_id=error_messages[file_path])
                del error_messages[file_path]

            return True
        except TelegramError as e:
            if hasattr(e, 'response') and e.response.status_code == 429:
                retry_after = int(e.response.headers.get('Retry-After', 1))
                logger.warning(f"Flood control exceeded. Retrying in {retry_after} seconds.")
                await asyncio.sleep(retry_after)
                return await send_file(file_path, caption, part_number, total_parts)
            else:
                logger.error(f"Error sending file {file_path}: {str(e)}")
                error_message = await bot.send_message(chat_id=CHAT_ID, 
                                                       text=f"Error sending file: {file_path}. Check logs.")
                error_messages[file_path] = error_message.message_id
                return False

async def forward_message(message):
    """Forwards a message to the specified chat if enabled."""
    try:
        bot_user = await bot.get_me()
        chat_member = await bot.get_chat_member(chat_id=FORWARD_CHAT_ID, user_id=bot_user.id)
        if chat_member.status == "kicked":
            logger.error(f"Bot kicked from chat: {FORWARD_CHAT_ID}")
        else:
            await bot.forward_message(chat_id=FORWARD_CHAT_ID, from_chat_id=CHAT_ID,
                                      message_id=message.message_id)
            logger.info(f"Message forwarded to {FORWARD_CHAT_ID}")
    except TelegramError as e:
        logger.error(f"Error forwarding message to {FORWARD_CHAT_ID}: {str(e)}")


async def split_and_send_zip(file_path, skip_zip=False):
    """Splits a large file into chunks and sends them as a zipped archive."""
    global file_counter
    try:
        base_name = os.path.basename(file_path)
        original_size = os.path.getsize(file_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = await create_zip_archive(file_path, base_name, temp_dir, skip_zip)

            logger.info(f"Splitting file: {zip_path}")
            chunk_size = MAX_FILE_SIZE
            file_number = 1
            total_parts = os.path.getsize(zip_path) // chunk_size + (1 if os.path.getsize(zip_path) % chunk_size else 0)

            with open(zip_path, 'rb') as zip_file:
                while True:
                    chunk = zip_file.read(chunk_size)
                    if not chunk:
                        break

                    chunk_name = os.path.join(temp_dir, f'{base_name}.{file_number:03d}')
                    with open(chunk_name, 'wb') as chunk_file:
                        chunk_file.write(chunk)

                    logger.info(f"Sending part {file_number}/{total_parts}: {chunk_name}")
                    caption = f"Part {file_number} of {base_name}"
                    success = await send_file(chunk_name, caption, part_number=file_number, total_parts=total_parts)
                    if not success:
                        logger.error(f"Failed to send part {file_number} of {base_name}")
                        return False, 0

                    file_number += 1
                    file_counter += 1

            await send_reassembly_instructions(base_name, file_number)

            if not skip_zip and COMPRESSION_LEVEL != 'none' and not file_path.lower().endswith('.zip'):
                os.remove(zip_path)
                logger.debug(f"Deleted temporary zipped file: {zip_path}")

            return True, original_size
    except Exception as e:
        logger.error(f"Error splitting and sending file {file_path}: {str(e)}")
        await send_error_message(base_name)
        return False, 0

async def create_zip_archive(file_path, base_name, temp_dir, skip_zip):
    """Creates a zip archive of the file, either compressing it or using the original if it's already a zip."""
    if not skip_zip and COMPRESSION_LEVEL != 'none' and not file_path.lower().endswith('.zip'):
        zip_path = os.path.join(temp_dir, f'{base_name}.zip')
        logger.info(f"Compressing file: {file_path}")
        compression = zipfile.ZIP_DEFLATED if COMPRESSION_LEVEL == 'default' else zipfile.ZIP_STORED
        if ENABLE_ENCRYPTION:
            with pyzipper.AESZipFile(zip_path, 'w', compression=compression,
                                     encryption=pyzipper.WZ_AES) as zipf:
                zipf.setpassword(ZIP_PASSWORD.encode())
                zipf.write(file_path, base_name)
        else:
            with zipfile.ZipFile(zip_path, 'w', compression) as zipf:
                zipf.write(file_path, base_name)
        return zip_path
    else:
        return file_path

async def send_reassembly_instructions(base_name, file_number):
    """Sends instructions to the user on how to reassemble the split files."""
    encryption_note = "The ZIP file is encrypted. You'll need the password to extract it." if ENABLE_ENCRYPTION else "The ZIP file is not encrypted."
    instructions = f"""```
To reassemble the file:
1. Download all parts ({file_number - 1} in total)
2. Use one of the following commands:
   # Windows
   copy /b {base_name}.zip.* {base_name}.zip

   # Linux/Mac
   cat {base_name}.zip.* > {base_name}.zip
3. Extract {base_name}.zip

{encryption_note}
```"""
    async with message_limiter:  # Apply rate limit to message sending
        await bot.send_message(chat_id=CHAT_ID, text=instructions, parse_mode=ParseMode.MARKDOWN)
    if FORWARD_CHAT_ID and ENABLE_FORWARD:
        async with message_limiter:
            await bot.send_message(chat_id=FORWARD_CHAT_ID, text=instructions, parse_mode=ParseMode.MARKDOWN)

async def send_error_message(base_name):
    """Sends a generic error message to the user."""
    async with message_limiter:
        await bot.send_message(chat_id=CHAT_ID, text=f"Error processing file: {base_name}. Check logs.")

async def send_event_to_backend(event_type, file_name, file_id, file_hash, file_size, processing_time,
                               upload_speed):
    """Sends an event to the backend server, logging any errors."""
    try:
        backend_url = 'http://localhost:5000/event'
        data = {
            'type': event_type,
            'file': file_name,
            'file_id': file_id,
            'hash': file_hash,
            'file_size': file_size,
            'processing_time': processing_time,
            'upload_speed': upload_speed
        }
        response = requests.post(backend_url, json=data, timeout=5)
        if not response.ok:
            logger.warning(f"Error sending event to backend: {response.text}")
    except RequestException as e:
        logger.warning(f"Unable to connect to backend: {str(e)}")

async def process_file(file_path):
    """Processes a file, compressing, splitting, and sending it to Telegram."""
    global file_counter
    start_time = time.time()
    file_hash = calculate_md5(file_path)
    original_size = os.path.getsize(file_path)
    base_name = os.path.basename(file_path)
    upload_start_time = time.time()

    if any(file_data['hash'] == file_hash for file_data in file_history.values()):
        logger.info(f"File with the same hash already exists: {file_path}")
        return

    if ALLOWED_EXTENSIONS and not any(base_name.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
        logger.info(f"File ignored (extension not allowed): {file_path}")
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        if COMPRESSION_LEVEL == 'none' or (file_path.lower().endswith('.zip') and not ENABLE_ENCRYPTION):
            send_path = file_path
        else:
            send_path = os.path.join(temp_dir, f'{base_name}.zip')
            logger.info(f"Compressing file: {file_path} into {send_path}")
            compression = zipfile.ZIP_DEFLATED if COMPRESSION_LEVEL == 'default' else zipfile.ZIP_STORED
            if ENABLE_ENCRYPTION:
                with pyzipper.AESZipFile(send_path, 'w', compression=compression, encryption=pyzipper.WZ_AES) as zipf:
                    zipf.setpassword(ZIP_PASSWORD.encode())
                    zipf.write(file_path, base_name)
            else:
                with zipfile.ZipFile(send_path, 'w', compression) as zipf:
                    zipf.write(file_path, base_name)

        if file_path not in file_history or file_history[file_path]['hash'] != file_hash:
            logger.info(f"New file detected or file modified: {file_path}")

            if os.path.getsize(send_path) > MAX_FILE_SIZE:
                logger.info(f"File size exceeds {MAX_FILE_SIZE} bytes. Splitting and sending: {send_path}")
                if send_path.lower().endswith('.zip'):
                    success, file_size = await split_and_send_zip(send_path, skip_zip=True)
                else:
                    success, file_size = await split_and_send_zip(send_path)
            else:
                logger.info(f"Sending file: {send_path}")
                encryption_status = "🔒 Encrypted" if ENABLE_ENCRYPTION else "🔓 Not encrypted"
                caption = f"File: {base_name}\n{encryption_status}"
                success = await send_file(send_path, caption)
                file_size = os.path.getsize(send_path)

            if success:
                file_counter += 1
                encryption_algorithm = "AES" if ENABLE_ENCRYPTION else "None"
                processing_time = (time.time() - start_time) * 1000
                upload_speed = file_size / (time.time() - upload_start_time) if (
                            time.time() - upload_start_time) != 0 else 0
                file_history[file_path] = {
                    'hash': file_hash,
                    'last_sent': datetime.now().isoformat(),
                    'send_success': True,
                    'encrypted': ENABLE_ENCRYPTION,
                    'encryption_algorithm': encryption_algorithm,
                    'file_id': file_counter,
                    'file_size': original_size,
                    'processed_size': file_size,
                    'processing_time': processing_time,
                    'upload_speed': upload_speed
                }
                save_data(file_history, FILE_HISTORY_PATH)
                await send_event_to_backend('success', base_name, file_counter, file_hash, original_size,
                                           processing_time, upload_speed)
            else:
                logger.error(f"Failed to send file: {file_path}")
                await send_event_to_backend('failure', base_name, file_counter, file_hash, original_size, 0, 0)

def clean_old_logs():
    """Deletes old log files based on the configured retention period."""
    current_time = datetime.now()
    deletion_time = current_time - timedelta(days=LOG_RETENTION_DAYS)

    for filename in os.listdir('logs'):
        if filename.endswith('.txt') and filename.startswith('bot_log'):
            file_path = os.path.join('logs', filename)
            file_time = datetime.fromtimestamp(os.path.getctime(file_path))
            if file_time < deletion_time:
                os.remove(file_path)
                logger.info(f"Log file deleted for privacy: {filename}")

def build_file_size_cache():
    """Creates a cache of file sizes for faster processing."""
    global file_size_cache
    for folder in FOLDERS_TO_MONITOR:
        for root, _, files in os.walk(folder):
            for file in files:
                file_path = os.path.join(root, file)
                file_size_cache[file_path] = os.path.getsize(file_path)
    save_data(file_size_cache, FILE_SIZE_CACHE_PATH)
    logger.info("File size cache built.")

async def main():
    global file_history, file_counter, file_size_cache
    logger.info("Bot started.")
    print("Bot started. Press Ctrl+C to interrupt.")

    file_history = load_data(FILE_HISTORY_PATH)
    file_counter = max(file_history.values(), key=lambda x: x.get('file_id', 0), default={}).get('file_id', 0)
    if ENABLE_CACHE:
        file_size_cache = load_data(FILE_SIZE_CACHE_PATH)

    try:
        backend_url = 'http://localhost:5000/file_history'
        response = requests.post(backend_url, json=file_history, timeout=5)
        if not response.ok:
            logger.warning(f"Error sending file history to backend: {response.text}")
    except RequestException as e:
        logger.warning(f"Unable to connect to backend: {str(e)}")
        print(f"Error: Unable to connect to backend. Please check if the Flask backend is running.")

    while True:
        try:
            clean_old_logs()

            if ENABLE_CACHE:
                build_file_size_cache()
                # Sort by size, smallest first
                sorted_files = sorted(file_size_cache, key=file_size_cache.get)  
            else:
                sorted_files = []
                for folder in FOLDERS_TO_MONITOR:
                    for root, _, files in os.walk(folder):
                        for file in files:
                            file_path = os.path.join(root, file)
                            sorted_files.append(file_path)
                sorted_files.sort(key=os.path.getsize) # Sort by size, smallest first

            for file_path in sorted_files:
                await process_file(file_path)

            await asyncio.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Bot manually interrupted.")
            print("Bot manually interrupted.")
            break
        except Exception as e:
            logger.error(f"General error: {str(e)}")
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())