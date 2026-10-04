# created by rajnish sahani
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from pinterest_downloader import PinterestDownloader, PinterestError

load_dotenv()
TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or hashlib.sha256(("pinterest-webhook:" + TOKEN).encode()).hexdigest()


def positive_int(name: str, default: int, maximum: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1 or (maximum and value > maximum):
        raise ValueError(f"{name} must be between 1 and {maximum or 'a positive integer'}")
    return value


PORT = positive_int("PORT", 10000, 65535)
MAX_UPLOAD_MB = positive_int("MAX_UPLOAD_MB", 49, 49)
MAX_TEMP_MB = positive_int("MAX_TEMP_MB", 256)
DOWNLOAD_TIMEOUT_SECONDS = positive_int("DOWNLOAD_TIMEOUT_SECONDS", 420)
ALLOWED_USER_IDS = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".mp4", ".webm", ".mov", ".mkv", ".m4v"}
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("pinterest-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        result = super().format(record)
        for secret in (TOKEN, WEBHOOK_SECRET):
            if secret:
                result = result.replace(secret, "[redacted]")
        return result


for handler in logging.getLogger().handlers:
    handler.setFormatter(RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USER_IDS or bool(update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)


def extract_url(text: str) -> str | None:
    for raw in re.findall(r"https?://[^\s<>]+", text or "", re.IGNORECASE):
        url = raw.rstrip(".,;:!?)]}\"'")
        try:
            return PinterestDownloader.validate_pinterest_url(url)
        except (PinterestError, ValueError):
            continue
    return None


async def drain_output(proc) -> str:
    tail = bytearray()
    while chunk := await proc.stdout.read(4096):
        tail.extend(chunk)
        if len(tail) > 16384:
            del tail[:-16384]
    return tail.decode("utf-8", errors="replace")


async def run_download(url: str, output_dir: Path) -> Path:
    child_env = os.environ.copy()
    for name in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "WEBHOOK_SECRET"):
        child_env.pop(name, None)
    child_env["XDG_CACHE_HOME"] = str(output_dir.parent / "cache")
    child_env["XDG_CONFIG_HOME"] = str(output_dir.parent / "config")
    cmd = [sys.executable, str(Path(__file__).with_name("pinterest_downloader.py")),
           "--worker", url, str(output_dir), str(MAX_TEMP_MB * 1024 * 1024)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=child_env, start_new_session=(os.name == "posix"))
    reader = asyncio.create_task(drain_output(proc))
    deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT_SECONDS
    log.info("Starting Pinterest download")
    try:
        while not reader.done():
            if asyncio.get_running_loop().time() >= deadline:
                raise PinterestError(f"download timed out after {DOWNLOAD_TIMEOUT_SECONDS} seconds; try again later")
            size = 0
            for path in output_dir.parent.rglob("*"):
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass
            if size > MAX_TEMP_MB * 1024 * 1024:
                raise PinterestError("download exceeded the temporary storage limit")
            await asyncio.wait({reader}, timeout=0.25)
        output = await reader
        await proc.wait()
        for line in reversed(output.splitlines()):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if isinstance(result, dict) and result.get("pinbot_result"):
                if result.get("error"):
                    raise PinterestError(result["error"])
                name = result.get("file", "")
                if not isinstance(name, str) or Path(name).name != name:
                    break
                path = output_dir / name
                if proc.returncode == 0 and path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                    return path
                break
        raise PinterestError("the downloader stopped without a complete media file")
    finally:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            proc.kill()
        await proc.wait()
        await reader
        log.info("Downloader finished: exit=%s", proc.returncode)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and is_allowed(update):
        await update.message.reply_text(
            "send me a pinterest pin or pin.it link.\n\n"
            "i’ll download the best available image or video and send the file without re-encoding it.\n\n"
            "created by rajnish sahani"
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and is_allowed(update):
        await update.message.reply_text(
            "paste a link to an individual pinterest pin.\n\n"
            "images and videos are sent as documents, up to 49 mb each. "
            "temporary downloads are deleted after sending or failure.\n\n"
            "/id — show your telegram user id\n"
            "created by rajnish sahani"
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        await update.message.reply_text(f"your telegram user id: {update.effective_user.id}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    if not is_allowed(update):
        await message.reply_text("this is a personal bot; access is restricted.")
        return
    url = extract_url(message.text)
    if not url:
        await message.reply_text("send me a pinterest pin or pin.it link 🙂")
        return
    status = await message.reply_text("downloading the best available quality…")
    async with DOWNLOAD_SEMAPHORE:
        try:
            with tempfile.TemporaryDirectory(prefix="pinbot_") as tmp:
                output_dir = Path(tmp) / "media"
                output_dir.mkdir()
                path = await run_download(url, output_dir)
                if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                    await status.edit_text(f"the file exceeds this bot’s {MAX_UPLOAD_MB} mb telegram upload limit.")
                    return
                await status.edit_text("downloaded. uploading your file…")
                await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
                with path.open("rb") as stream:
                    await message.reply_document(document=stream, filename=path.name,
                        caption="best available quality • pinterest", read_timeout=120,
                        write_timeout=120, connect_timeout=30, pool_timeout=30)
                await status.delete()
        except PinterestError as exc:
            log.warning("Pinterest download failed: %s", exc)
            await status.edit_text(f"couldn’t download this pin: {exc}")
        except Exception:
            log.exception("Unexpected download or upload error")
            await status.edit_text("download or upload failed. try again, or check the server logs.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Telegram handler error", exc_info=context.error)


def main() -> None:
    if not TOKEN:
        raise SystemExit("set BOT_TOKEN in render's environment settings")
    if os.getenv("RENDER") == "true" and not WEBHOOK_URL:
        raise SystemExit("Render needs RENDER_EXTERNAL_URL or WEBHOOK_URL")
    app = Application.builder().token(TOKEN).concurrent_updates(8).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    if WEBHOOK_URL:
        parsed = urlparse(WEBHOOK_URL)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise SystemExit("WEBHOOK_URL must be an HTTPS origin, such as https://your-bot.onrender.com")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
            raise SystemExit("invalid WEBHOOK_SECRET")
        log.info("Starting webhook on port %s", PORT)
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path="telegram",
            webhook_url=f"{WEBHOOK_URL}/telegram", secret_token=WEBHOOK_SECRET,
            allowed_updates=["message"], drop_pending_updates=False)
    else:
        log.info("Starting local polling")
        app.run_polling(allowed_updates=["message"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
