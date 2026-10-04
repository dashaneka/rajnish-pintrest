from __future__ import annotations

import html
import json
import logging
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:  # lets parser-only tests/imports work before dependencies are installed
    YoutubeDL = None  # type: ignore[assignment]

    class DownloadError(Exception):
        pass

log = logging.getLogger(__name__)

PINTEREST_HOST_RE = re.compile(
    r"(^|\.)(pinterest\.(com|fr|de|ch|jp|cl|ca|it|co\.uk|nz|ru|com\.au|at|pt|co\.kr|es|com\.mx|dk|ph|th|com\.uy|co|nl|info|kr|ie|vn|com\.vn|ec|mx|in|pe|co\.at|hu|co\.in|co\.nz|id|com\.ec|com\.py|tw|be|uk|com\.bo|com\.pe)|pin\.it)$",
    re.IGNORECASE,
)

MEDIA_HOST_RE = re.compile(r"(^|\.)(pinimg\.com|pinterestusercontent\.com)$", re.IGNORECASE)
VIDEO_EXT_RE = re.compile(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|\?)", re.IGNORECASE)
IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|avif)(?:$|\?)", re.IGNORECASE)
PIN_ID_RE = re.compile(r"/pin/(?:[\w-]+--)?(?P<id>\d+)")
URL_RE = re.compile(r'https?://[^"\'<>\\\s]+')


class PinterestError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    url: str
    kind: str  # image | video
    width: int = 0
    height: int = 0
    source: str = "page"

    @property
    def score(self) -> tuple[int, int, int]:
        area = max(self.width, 0) * max(self.height, 0)
        original_bonus = 1 if "/originals/" in self.url else 0
        video_bonus = 1 if self.kind == "video" else 0
        return (area, original_bonus, video_bonus)


class PinterestDownloader:
    def __init__(self, timeout: float = 20.0, max_download_bytes: int = 256 * 1024 * 1024):
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        }

    @staticmethod
    def validate_pinterest_url(raw_url: str) -> str:
        url = raw_url.strip().strip("<>[](){}\"'")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise PinterestError("please send a full pinterest link starting with http:// or https://")
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise PinterestError("invalid pinterest URL port") from exc
        if (not PINTEREST_HOST_RE.search(host) or parsed.username or parsed.password
                or port not in (None, 80, 443)):
            raise PinterestError("that doesn't look like a pinterest link")
        return url

    def _client(self) -> httpx.Client:
        return httpx.Client(headers=self.headers, timeout=self.timeout,
                            verify=ssl.create_default_context(), follow_redirects=False)

    def _get_page(self, url: str) -> tuple[str, str]:
        # Validate every redirect before contacting its destination.
        with self._client() as client:
            for _ in range(10):
                self.validate_pinterest_url(url)
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 16 * 1024 * 1024:
                            raise PinterestError("pinterest page exceeded the size limit")
                    return body.decode("utf-8", errors="replace"), str(response.url)
        raise PinterestError("too many pinterest redirects")

    def resolve_url(self, raw_url: str) -> str:
        url = self.validate_pinterest_url(raw_url)
        try:
            _, final_url = self._get_page(url)
        except httpx.HTTPError as exc:
            raise PinterestError(f"couldn't open that pinterest link: {exc}") from exc

        host = (urlparse(final_url).hostname or "").lower().rstrip(".")
        if not PINTEREST_HOST_RE.search(host):
            raise PinterestError("the pinterest link redirected somewhere unexpected")
        return final_url

    def download(self, raw_url: str, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        resolved = self.resolve_url(raw_url)
        match = PIN_ID_RE.search(urlparse(resolved).path)
        if not match:
            raise PinterestError("please send a link to an individual pinterest pin")
        pin_id = match.group("id")
        # Drop share/tracking paths and queries after the short link has resolved.
        resolved = f"https://www.pinterest.com/pin/{pin_id}/"

        # 1) Prefer yt-dlp for videos because it knows how to choose the best
        # format/HLS variant and merge audio when necessary.
        try:
            video = self._download_video_with_ytdlp(resolved, out_dir)
            if video:
                return video
        except (DownloadError, PinterestError, OSError) as exc:
            log.info("yt-dlp pinterest path failed; trying page fallback: %s", exc)

        # 2) Pinterest image pins are not reliably supported by yt-dlp. Parse
        # Pinterest's own HTML/embedded JSON, then fetch the best media URL.
        page_html, canonical = self._fetch_page(resolved)
        candidates = self._extract_candidates(page_html, pin_id)
        if not candidates:
            raise PinterestError("i couldn't find downloadable media in that pin")

        videos = [c for c in candidates if c.kind == "video"]
        if videos:
            best_video = max(videos, key=lambda c: c.score)
            return self._download_direct_video(best_video.url, out_dir)

        images = [c for c in candidates if c.kind == "image"]
        if not images:
            raise PinterestError("i found the pin, but not a downloadable image or video")

        best_image = self._pick_best_image(images)
        return self._download_image(best_image.url, out_dir, canonical)

    def _download_video_with_ytdlp(self, url: str, out_dir: Path) -> Path | None:
        if YoutubeDL is None:
            return None
        before = {p.resolve() for p in out_dir.iterdir() if p.is_file()}
        opts = {
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": str(out_dir / "pinterest_%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_headers": self.headers,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": self.timeout,
            "cachedir": False,
            "compat_opts": {"no-certifi"},
            "extractor_retries": 1,
            "concurrent_fragment_downloads": 4,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None

        created = [p for p in out_dir.iterdir() if p.is_file() and p.resolve() not in before]
        if not created:
            return None
        # Ignore temp/metadata files if any.
        media = [p for p in created if p.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv", ".m4v"}]
        return max(media, key=lambda p: p.stat().st_size) if media else None

    def _fetch_page(self, url: str) -> tuple[str, str]:
        try:
            page, final_url = self._get_page(url)
            host = (urlparse(final_url).hostname or "").lower().rstrip(".")
            if not PINTEREST_HOST_RE.search(host):
                raise PinterestError("pinterest redirected to an unexpected site")
            return page, final_url
        except httpx.HTTPError as exc:
            raise PinterestError(f"pinterest blocked or failed the request: {exc}") from exc

    def _extract_candidates(self, page_html: str, pin_id: str | None = None) -> list[Candidate]:
        soup = BeautifulSoup(page_html, "html.parser")
        found: dict[str, Candidate] = {}

        def add(url: str, kind: str | None = None, width: Any = 0, height: Any = 0, source: str = "page"):
            if not isinstance(url, str):
                return
            url = html.unescape(url).replace("\\u002F", "/").replace("\\/", "/")
            if not url.startswith("https://"):
                return
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if not MEDIA_HOST_RE.search(host):
                return
            inferred = kind
            if inferred is None:
                if VIDEO_EXT_RE.search(url):
                    inferred = "video"
                elif IMAGE_EXT_RE.search(url):
                    inferred = "image"
                else:
                    return
            try:
                w = int(width or 0)
            except (TypeError, ValueError):
                w = 0
            try:
                h = int(height or 0)
            except (TypeError, ValueError):
                h = 0
            candidate = Candidate(url=url, kind=inferred, width=w, height=h, source=source)
            prev = found.get(url)
            if prev is None or candidate.score > prev.score:
                found[url] = candidate

        # Open Graph / Twitter metadata is a reliable fallback for standard pins.
        meta_map = {
            "og:video": "video",
            "og:video:url": "video",
            "og:video:secure_url": "video",
            "twitter:player:stream": "video",
            "og:image": "image",
            "og:image:url": "image",
            "og:image:secure_url": "image",
            "twitter:image": "image",
        }
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            content = meta.get("content")
            if prop in meta_map and content:
                add(content, meta_map[prop], source="meta")

        # JSON-LD often contains the original PNG while og:image names a resized
        # JPEG of the same picture. Accept only the exact image hash from the
        # requested page's metadata, never arbitrary recommendation media.
        image_hashes = {Path(urlparse(c.url).path).stem for c in found.values()
                        if c.kind == "image" and re.fullmatch(r"[a-fA-F0-9]{32}", Path(urlparse(c.url).path).stem)}

        def add_matching_image(url, kind=None, width=0, height=0, source="json"):
            if (kind in (None, "image") and isinstance(url, str)
                    and Path(urlparse(url).path).stem in image_hashes):
                add(url, "image", width, height, source)

        # Pinterest places useful media descriptors in application/json script tags.
        for script in soup.find_all("script"):
            raw = script.string or script.get_text(strip=False)
            if not raw:
                continue
            stype = (script.get("type") or "").lower()
            if "json" in stype or script.get("id") in {"__PWS_DATA__", "__PWS_INITIAL_PROPS__", "__NEXT_DATA__"}:
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if pin_id:
                    for pin in self._find_pin_objects(obj, pin_id):
                        for key in ("images", "videos", "video_list", "story_pin_data"):
                            if key in pin:
                                self._walk_json(pin[key], add)
                self._walk_json(obj, add_matching_image)
                # Without a pin identity, only page metadata is trusted.

        # Add a potential Pinterest originals URL for resized images. We only
        # keep it if it is actually reachable when selecting the winner.
        for candidate in list(found.values()):
            if candidate.kind != "image":
                continue
            upgraded = self._to_originals(candidate.url)
            if upgraded and upgraded not in found:
                found[upgraded] = Candidate(
                    url=upgraded,
                    kind="image",
                    width=candidate.width,
                    height=candidate.height,
                    source="originals-guess",
                )

        return list(found.values())

    def _find_pin_objects(self, obj: Any, pin_id: str):
        if isinstance(obj, dict):
            if str(obj.get("id", "")) == pin_id:
                yield obj
            for key, value in obj.items():
                if str(key) == pin_id and isinstance(value, dict):
                    yield value
                yield from self._find_pin_objects(value, pin_id)
        elif isinstance(obj, list):
            for value in obj:
                yield from self._find_pin_objects(value, pin_id)

    def _walk_json(self, obj: Any, add) -> None:
        if isinstance(obj, dict):
            width = obj.get("width") or obj.get("w") or 0
            height = obj.get("height") or obj.get("h") or 0
            for key, value in obj.items():
                k = str(key).lower()
                if isinstance(value, str):
                    kind = None
                    if "video" in k or k in {"stream", "src"}:
                        kind = "video" if VIDEO_EXT_RE.search(value) else None
                    elif "image" in k or "thumbnail" in k or k in {"url", "src"}:
                        kind = "image" if IMAGE_EXT_RE.search(value) else None
                    if kind or VIDEO_EXT_RE.search(value) or IMAGE_EXT_RE.search(value):
                        add(value, kind=kind, width=width, height=height, source="json")
                else:
                    self._walk_json(value, add)
        elif isinstance(obj, list):
            for item in obj:
                self._walk_json(item, add)
        elif isinstance(obj, str):
            if VIDEO_EXT_RE.search(obj) or IMAGE_EXT_RE.search(obj):
                add(obj, source="json-string")

    @staticmethod
    def _to_originals(url: str) -> str | None:
        # Common Pinterest image paths: /236x/, /474x/, /564x/, /736x/,
        # /1200x/, /originals/. Original bytes, when available, live under
        # /originals/ with the same hash path.
        return re.sub(r"/(?:\d+x|\d+x\d+|cropx[^/]+)/", "/originals/", url, count=1)

    def _pick_best_image(self, images: Iterable[Candidate]) -> Candidate:
        ranked = sorted(images, key=lambda c: c.score, reverse=True)
        with self._client() as client:
            for candidate in ranked:
                if candidate.source != "originals-guess":
                    return candidate
                try:
                    r = client.head(candidate.url)
                    ctype = r.headers.get("content-type", "")
                    if r.status_code < 400 and ctype.startswith("image/"):
                        return candidate
                except httpx.HTTPError:
                    pass
        # If every guessed original failed, fall back to a real page candidate.
        for candidate in ranked:
            if candidate.source != "originals-guess":
                return candidate
        raise PinterestError("no reachable image candidate was found")

    def _download_image(self, url: str, out_dir: Path, canonical: str) -> Path:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
            suffix = ".jpg"
        pin_id_match = PIN_ID_RE.search(urlparse(canonical).path)
        pin_id = pin_id_match.group("id") if pin_id_match else "image"
        target = out_dir / f"pinterest_{pin_id}{suffix}"

        try:
            with self._client() as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    ctype = response.headers.get("content-type", "")
                    if not ctype.startswith("image/"):
                        raise PinterestError("pinterest returned something other than an image")
                    with target.open("wb") as fh:
                        for chunk in response.iter_bytes(1024 * 256):
                            fh.write(chunk)
                            if fh.tell() > self.max_download_bytes:
                                raise PinterestError("image exceeded the temporary storage limit")
        except httpx.HTTPError as exc:
            raise PinterestError(f"couldn't download the pinterest image: {exc}") from exc
        return target

    def _download_direct_video(self, url: str, out_dir: Path) -> Path:
        if YoutubeDL is None:
            raise PinterestError("yt-dlp is not installed; install requirements.txt first")
        before = {p.resolve() for p in out_dir.iterdir() if p.is_file()}
        opts = {
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": str(out_dir / "pinterest_video.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "http_headers": self.headers,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": self.timeout,
            "cachedir": False,
            "compat_opts": {"no-certifi"},
            "extractor_retries": 1,
            "concurrent_fragment_downloads": 4,
        }
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadError as exc:
            raise PinterestError("i found the video, but couldn't download its best stream") from exc

        created = [p for p in out_dir.iterdir() if p.is_file() and p.resolve() not in before]
        media = [p for p in created if p.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv", ".m4v"}]
        if not media:
            raise PinterestError("the video download finished without a usable file")
        return max(media, key=lambda p: p.stat().st_size)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 5 or sys.argv[1] != "--worker":
        raise SystemExit("usage: pinterest_downloader.py --worker URL OUTPUT_DIR MAX_BYTES")
    try:
        path = PinterestDownloader(max_download_bytes=int(sys.argv[4])).download(sys.argv[2], Path(sys.argv[3]))
        print(json.dumps({"pinbot_result": True, "file": path.name}), flush=True)
    except Exception as exc:
        # Do not expose raw network URLs or credentials in Telegram replies.
        detail = str(exc).lower()
        if "certificate" in detail or "ssl" in detail:
            message = "a secure connection to pinterest could not be established"
        elif "429" in detail:
            message = "pinterest is rate-limiting this server; retry later"
        elif "403" in detail or "login" in detail:
            message = "pinterest blocked this server or requires login"
        elif "timeout" in detail or "timed out" in detail:
            message = "pinterest did not respond in time; retry later"
        elif isinstance(exc, PinterestError) and not ("http" in detail or "request" in detail):
            message = str(exc)
        else:
            message = "the pin is unavailable or pinterest could not return its media"
        print(json.dumps({"pinbot_result": True, "error": message}), flush=True)
        raise SystemExit(1)
