#!/usr/bin/env python3
"""
Instagram auto-poster.

Reads a schedule (posts.csv, or a published Google Sheet), prepares each post
(converts photos to Instagram-friendly JPEGs, writes missing captions with AI,
draws text graphics when no image is given) and publishes it through the
official Instagram API when its time comes.

Commands:
  python autopost.py check          Test your settings and list upcoming posts
  python autopost.py preview        Prepare ALL posts now (no posting) so you can look at them
  python autopost.py prepare        Prepare posts due within the lead time (used by the scheduler)
  python autopost.py publish        Publish posts that are due (used by the scheduler)
  python autopost.py refresh-token  Print a refreshed Instagram access token
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
MEDIA_DIR = ROOT / "media"
GEN_DIR = ROOT / "generated"
LOG_FILE = ROOT / "posted.json"
CONFIG_FILE = ROOT / "config.yaml"
CSV_FILE = ROOT / "posts.csv"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov"}
TYPES = {"image", "carousel", "reel", "story"}


# --------------------------------------------------------------------------- config

def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    cfg = cfg or {}
    cfg.setdefault("timezone", "America/Toronto")
    cfg.setdefault("lead_hours", 24)
    cfg.setdefault("max_late_hours", 24)
    cfg.setdefault("api_version", "v25.0")
    cfg.setdefault("ai_model", "claude-sonnet-5-5")
    cfg.setdefault("max_hashtags", 5)
    cfg.setdefault("brand_voice", "Friendly, clear and professional.")
    cfg.setdefault("graphic", {})
    g = cfg["graphic"]
    g.setdefault("background", "#1F3A5F")
    g.setdefault("text_color", "#FFFFFF")
    g.setdefault("accent", "#F2B134")
    g.setdefault("footer", "")
    g.setdefault("font", "")
    return cfg


def now_local(cfg) -> datetime:
    return datetime.now(ZoneInfo(cfg["timezone"]))


# --------------------------------------------------------------------------- schedule

@dataclass
class Post:
    id: str
    post_at: datetime
    type: str
    media: list[str] = field(default_factory=list)
    caption: str = ""
    topic: str = ""
    graphic_text: str = ""
    alt_text: str = ""


def parse_time(value: str, tz: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=ZoneInfo(tz))
        except ValueError:
            pass
    raise ValueError(f"Can't read the date/time '{value}'. Use the format 2026-11-20 09:30")


def read_schedule(cfg) -> list[Post]:
    url = os.environ.get("SHEET_CSV_URL", "").strip()
    if url:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
    else:
        text = CSV_FILE.read_text(encoding="utf-8-sig")

    posts, errors, seen = [], [], set()
    for n, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not any(row.values()) or row.get("id", "").startswith("#"):
            continue
        pid = row.get("id", "")
        try:
            if not pid:
                raise ValueError("missing id")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
                raise ValueError("id can only use letters, numbers, - and _")
            if pid in seen:
                raise ValueError(f"id '{pid}' is used twice")
            seen.add(pid)
            ptype = (row.get("type") or "image").lower()
            if ptype not in TYPES:
                raise ValueError(f"type must be one of {', '.join(sorted(TYPES))}")
            media = [m.strip() for m in row.get("media", "").split("|") if m.strip()]
            p = Post(
                id=pid,
                post_at=parse_time(row.get("post_at", ""), cfg["timezone"]),
                type=ptype,
                media=media,
                caption=row.get("caption", ""),
                topic=row.get("topic", ""),
                graphic_text=row.get("graphic_text", ""),
                alt_text=row.get("alt_text", ""),
            )
            validate_post(p)
            posts.append(p)
        except ValueError as e:
            errors.append(f"Row {n} ({pid or 'no id'}): {e}")
    for e in errors:
        print(f"⚠️  {e}")
    return sorted(posts, key=lambda p: p.post_at)


def validate_post(p: Post):
    if p.type == "reel":
        if len(p.media) != 1 or not is_video(p.media[0]):
            raise ValueError("a reel needs exactly one .mp4 or .mov video in the media column")
    if p.type == "carousel":
        if p.media and not 2 <= len(p.media) <= 10:
            raise ValueError("a carousel needs 2-10 files separated by |")
        if not p.media:
            raise ValueError("a carousel needs 2-10 files in the media column")
    if p.type in ("image", "story") and len(p.media) > 1:
        raise ValueError(f"a {p.type} takes one file — use type 'carousel' for several")
    if not p.media and not (p.graphic_text or p.topic):
        raise ValueError("needs media, or graphic_text / topic so a graphic can be made")
    if p.type != "story" and not (p.caption or p.topic or p.graphic_text):
        raise ValueError("needs a caption, or a topic so one can be written")


def is_video(name: str) -> bool:
    return Path(name.split("?")[0]).suffix.lower() in VIDEO_EXT


def is_url(name: str) -> bool:
    return name.lower().startswith(("http://", "https://"))


# --------------------------------------------------------------------------- log

def load_log() -> dict:
    return json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else {}


def save_log(log: dict):
    LOG_FILE.write_text(json.dumps(log, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- AI writing

def ask_claude(cfg, prompt: str, max_tokens: int = 600) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("This post needs AI writing but ANTHROPIC_API_KEY isn't set.")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": cfg["ai_model"],
            "max_tokens": max_tokens,
            "system": (
                "You write Instagram content for one account. Brand voice and guidance:\n"
                f"{cfg['brand_voice']}\n"
                "Reply with only the requested text — no preamble, no quotation marks, no labels."
            ),
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"AI request failed ({r.status_code}): {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json()["content"]).strip().strip('"').strip()


def write_graphic_text(cfg, p: Post) -> str:
    return ask_claude(cfg, (
        f"Write the text for a simple text-only Instagram graphic about: {p.topic}\n"
        "Make it a punchy headline, tip or quote of at most 18 words. No hashtags, no emoji."
    ), 120)


def write_caption(cfg, p: Post, graphic_text: str) -> str:
    bits = [f"Write an Instagram caption about: {p.topic or graphic_text}"]
    if graphic_text:
        bits.append(f"The image shows this text: \"{graphic_text}\"")
    bits.append(
        f"Keep it under 1,200 characters. End with at most {cfg['max_hashtags']} relevant hashtags. "
        "Use line breaks between short paragraphs."
    )
    caption = ask_claude(cfg, "\n".join(bits), 700)
    return limit_hashtags(caption, cfg["max_hashtags"])


def limit_hashtags(caption: str, limit: int) -> str:
    count = 0

    def keep(m):
        nonlocal count
        count += 1
        return m.group(0) if count <= limit else ""

    out = re.sub(r"(?<!\w)#\w+", keep, caption)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", out)).strip()[:2200]


# --------------------------------------------------------------------------- images

def load_font(cfg, size: int):
    candidates = [cfg["graphic"].get("font")] if cfg["graphic"].get("font") else []
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    for path in candidates:
        if path and Path(ROOT / path if not Path(path).is_absolute() else path).exists():
            return ImageFont.truetype(str(ROOT / path if not Path(path).is_absolute() else path), size)
    return ImageFont.load_default(size=size)


def render_graphic(cfg, text: str, out: Path, story: bool = False):
    g = cfg["graphic"]
    W, H = (1080, 1920) if story else (1080, 1350)
    img = Image.new("RGB", (W, H), g["background"])
    d = ImageDraw.Draw(img)
    margin = 110
    box_w = W - 2 * margin
    box_h = H - 2 * margin - (160 if g["footer"] else 60)

    # Find the biggest font size that fits.
    for size in range(110, 30, -4):
        font = load_font(cfg, size)
        chars = max(8, int(box_w / (size * 0.55)))
        lines = textwrap.wrap(text, width=chars)
        while lines and max(d.textlength(l, font=font) for l in lines) > box_w and chars > 8:
            chars -= 1
            lines = textwrap.wrap(text, width=chars)
        line_h = int(size * 1.25)
        if len(lines) * line_h <= box_h and max(d.textlength(l, font=font) for l in lines) <= box_w:
            break

    total_h = len(lines) * line_h
    y = (H - total_h) // 2 - (40 if g["footer"] else 0)
    d.rectangle([margin, y - 60, margin + 120, y - 48], fill=g["accent"])
    for line in lines:
        d.text((margin, y), line, font=font, fill=g["text_color"])
        y += line_h
    if g["footer"]:
        f = load_font(cfg, 40)
        d.text((margin, H - margin - 40), g["footer"], font=f, fill=g["accent"])
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=92)


def to_instagram_jpeg(cfg, src: Path, out: Path, story: bool = False, carousel: bool = False):
    """Convert any photo to a JPEG Instagram will accept, padding odd shapes instead of cropping."""
    try:
        from pillow_heif import register_heif_opener  # optional, for iPhone .heic photos
        register_heif_opener()
    except ImportError:
        pass
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    ratio = w / h
    # Stories are 9:16. Carousel slides all share one shape (4:5) so Instagram doesn't crop them.
    lo, hi = (9 / 16, 9 / 16) if story else (4 / 5, 4 / 5) if carousel else (4 / 5, 1.91)
    if ratio < lo or ratio > hi:
        target = lo if ratio < lo else hi
        if ratio < target:
            new_w, new_h = round(h * target), h
        else:
            new_w, new_h = w, round(w / target)
        canvas = Image.new("RGB", (new_w, new_h), cfg["graphic"]["background"])
        canvas.paste(img, ((new_w - w) // 2, (new_h - h) // 2))
        img = canvas
    if img.width > 1440:
        img = img.resize((1440, round(img.height * 1440 / img.width)), Image.LANCZOS)
    if img.width < 320:
        img = img.resize((320, round(img.height * 320 / img.width)), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=92)


# --------------------------------------------------------------------------- prepare

def prepared_file(p: Post) -> Path:
    return GEN_DIR / p.id / "post.json"


def prepare(cfg, p: Post) -> dict:
    """Build generated/<id>/ with final media + caption. Re-uses it if it already exists,
    so you can edit generated/<id>/post.json (or swap the images) before it goes out."""
    pf = prepared_file(p)
    if pf.exists():
        return json.loads(pf.read_text(encoding="utf-8"))

    folder = GEN_DIR / p.id
    folder.mkdir(parents=True, exist_ok=True)
    story = p.type == "story"
    graphic_text = p.graphic_text
    media_out: list[str] = []

    if p.media:
        for i, m in enumerate(p.media, start=1):
            if is_url(m):
                media_out.append(m)
                continue
            src = MEDIA_DIR / m
            if not src.exists():
                raise FileNotFoundError(f"media/{m} not found")
            if is_video(m):
                dest = folder / f"{i}{src.suffix.lower()}"
                dest.write_bytes(src.read_bytes())
            else:
                dest = folder / f"{i}.jpg"
                to_instagram_jpeg(cfg, src, dest, story=story, carousel=p.type == "carousel")
            media_out.append(dest.relative_to(ROOT).as_posix())
    else:
        if not graphic_text:
            graphic_text = write_graphic_text(cfg, p)
        dest = folder / "1.jpg"
        render_graphic(cfg, graphic_text, dest, story=story)
        media_out.append(dest.relative_to(ROOT).as_posix())

    caption = p.caption
    if not caption and not story:
        caption = write_caption(cfg, p, graphic_text)
    else:
        caption = caption[:2200]

    data = {
        "id": p.id,
        "type": p.type,
        "post_at": p.post_at.isoformat(),
        "media": media_out,
        "caption": caption,
        "alt_text": p.alt_text,
        "graphic_text": graphic_text,
        "prepared_at": now_local(cfg).isoformat(timespec="seconds"),
    }
    pf.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


# --------------------------------------------------------------------------- Instagram API

class Instagram:
    def __init__(self, cfg, token: str):
        self.base = f"https://graph.instagram.com/{cfg['api_version']}"
        self.token = token
        self._user = None

    def _req(self, method, path, **params):
        params["access_token"] = self.token
        if method == "GET":
            r = requests.get(f"{self.base}/{path}", params=params, timeout=60)
        else:
            r = requests.post(f"{self.base}/{path}", data=params, timeout=120)
        try:
            body = r.json()
        except ValueError:
            body = {"error": {"message": r.text[:300]}}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error", {})
            raise RuntimeError(f"Instagram said: {err.get('message', r.text[:300])} "
                               f"(code {err.get('code')}, subcode {err.get('error_subcode')})")
        return body

    def me(self) -> dict:
        if self._user is None:
            self._user = self._req("GET", "me", fields="user_id,username")
        return self._user

    @property
    def uid(self) -> str:
        me = self.me()
        return str(me.get("user_id") or me["id"])

    def publishing_limit(self) -> dict:
        return self._req("GET", f"{self.uid}/content_publishing_limit", fields="quota_usage,config")

    def wait_ready(self, container_id: str, timeout: int = 600):
        start = time.time()
        while True:
            status = self._req("GET", container_id, fields="status_code,status").get("status_code")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                info = self._req("GET", container_id, fields="status")
                raise RuntimeError(f"Instagram couldn't process the media: {info.get('status')}")
            if time.time() - start > timeout:
                raise RuntimeError("Instagram took too long to process the media")
            time.sleep(5 if status == "IN_PROGRESS" else 3)

    def _item(self, url: str, **extra) -> str:
        if is_video(url):
            return self._req("POST", f"{self.uid}/media", video_url=url, **extra)["id"]
        return self._req("POST", f"{self.uid}/media", image_url=url, **extra)["id"]

    def publish(self, post: dict, urls: list[str]) -> dict:
        t, cap, alt = post["type"], post.get("caption", ""), post.get("alt_text", "")
        alt_kw = {"alt_text": alt} if alt else {}
        if t == "image":
            cid = self._item(urls[0], caption=cap, **alt_kw)
        elif t == "reel":
            cid = self._req("POST", f"{self.uid}/media", media_type="REELS", video_url=urls[0],
                            caption=cap, share_to_feed="true")["id"]
        elif t == "story":
            kind = "video_url" if is_video(urls[0]) else "image_url"
            cid = self._req("POST", f"{self.uid}/media", media_type="STORIES", **{kind: urls[0]})["id"]
        elif t == "carousel":
            children = []
            for u in urls:
                extra = {"is_carousel_item": "true"}
                if is_video(u):
                    extra["media_type"] = "VIDEO"
                child = self._item(u, **extra)
                self.wait_ready(child)
                children.append(child)
            cid = self._req("POST", f"{self.uid}/media", media_type="CAROUSEL",
                            children=",".join(children), caption=cap)["id"]
        else:
            raise ValueError(t)
        self.wait_ready(cid)
        media_id = self._req("POST", f"{self.uid}/media_publish", creation_id=cid)["id"]
        try:
            link = self._req("GET", media_id, fields="permalink").get("permalink", "")
        except RuntimeError:
            link = ""
        return {"media_id": media_id, "permalink": link}


def refresh_token(cfg, token: str) -> str:
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=30)
    body = r.json()
    if "access_token" not in body:
        raise RuntimeError(f"Token refresh failed: {body}")
    return body["access_token"]


def public_urls(media: list[str]) -> list[str]:
    base = os.environ.get("MEDIA_BASE_URL", "").strip()
    urls = []
    for m in media:
        if is_url(m):
            urls.append(m)
        elif not base:
            raise RuntimeError("MEDIA_BASE_URL isn't set, so Instagram can't fetch the images.")
        else:
            urls.append(base.rstrip("/") + "/" + m)
    return urls


# --------------------------------------------------------------------------- commands

def cmd_prepare(cfg, everything=False):
    log, now = load_log(), now_local(cfg)
    horizon = now + timedelta(hours=cfg["lead_hours"])
    count = 0
    for p in read_schedule(cfg):
        if p.id in log.get("posted", {}) or prepared_file(p).exists():
            continue
        if everything or p.post_at <= horizon:
            try:
                data = prepare(cfg, p)
                count += 1
                print(f"✅ Prepared {p.id} ({p.type}, {p.post_at:%a %b %d %H:%M})")
                if data["caption"]:
                    print(textwrap.indent(data["caption"][:300] + ("…" if len(data["caption"]) > 300 else ""), "     "))
            except Exception as e:
                print(f"❌ Couldn't prepare {p.id}: {e}")
    print(f"{count} post(s) prepared." if count else "Nothing new to prepare.")


def cmd_publish(cfg):
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit("IG_ACCESS_TOKEN isn't set.")
    ig = Instagram(cfg, token)
    log, now = load_log(), now_local(cfg)
    log.setdefault("posted", {})
    log.setdefault("failed", {})
    failures = 0
    for p in read_schedule(cfg):
        if p.id in log["posted"] or p.post_at > now:
            continue
        if now - p.post_at > timedelta(hours=cfg["max_late_hours"]):
            if p.id not in log["failed"]:
                print(f"⏭️  Skipping {p.id}: it was due {p.post_at:%b %d %H:%M}, more than "
                      f"{cfg['max_late_hours']}h ago. Change its post_at to reschedule.")
                log["failed"][p.id] = {"error": "missed window", "at": now.isoformat(timespec="seconds")}
            continue
        try:
            was_ready = prepared_file(p).exists()
            data = prepare(cfg, p)
            # On GitHub, freshly made files aren't online until they're pushed, so wait a run.
            if not was_ready and os.environ.get("GITHUB_ACTIONS") == "true" \
                    and any(not is_url(m) for m in data["media"]):
                print(f"…{p.id} was prepared just now; it'll go out on the next run once its files are online.")
                continue
            result = ig.publish(data, public_urls(data["media"]))
            log["posted"][p.id] = {**result, "at": now_local(cfg).isoformat(timespec="seconds")}
            log["failed"].pop(p.id, None)
            print(f"📸 Posted {p.id}: {result.get('permalink') or result['media_id']}")
        except Exception as e:
            failures += 1
            log["failed"][p.id] = {"error": str(e)[:500], "at": now.isoformat(timespec="seconds")}
            print(f"❌ {p.id} failed: {e}")
        save_log(log)
    save_log(log)
    if failures:
        sys.exit(1)  # makes the GitHub run show red so you get an email


def cmd_check(cfg):
    ok = True
    print(f"Time zone: {cfg['timezone']} — it's now {now_local(cfg):%a %b %d %H:%M}")
    posts = read_schedule(cfg)
    log = load_log().get("posted", {})
    upcoming = [p for p in posts if p.id not in log]
    print(f"Schedule: {len(posts)} post(s) read, {len(upcoming)} not yet posted")
    for p in upcoming[:10]:
        what = ", ".join(p.media) if p.media else "auto graphic"
        cap = "caption ✓" if p.caption else ("AI caption" if p.type != "story" else "no caption")
        print(f"   {p.post_at:%a %b %d %H:%M}  {p.id:<18} {p.type:<8} {what} · {cap}")
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if token:
        try:
            ig = Instagram(cfg, token)
            print(f"Instagram: connected as @{ig.me().get('username')}")
            lim = ig.publishing_limit().get("data", [{}])[0]
            print(f"   Posts used in last 24h: {lim.get('quota_usage')} of {lim.get('config', {}).get('quota_total')}")
        except Exception as e:
            ok = False
            print(f"Instagram: ❌ {e}")
    else:
        print("Instagram: IG_ACCESS_TOKEN not set (skipped)")
    needs_ai = any(not p.caption and p.type != "story" or (not p.media and not p.graphic_text) for p in upcoming)
    if needs_ai and not os.environ.get("ANTHROPIC_API_KEY"):
        ok = False
        print("AI writing: ❌ some posts need AI captions/graphics but ANTHROPIC_API_KEY isn't set")
    elif needs_ai:
        print("AI writing: key set ✓")
    if not ok:
        sys.exit(1)


def main():
    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "prepare":
        cmd_prepare(cfg)
    elif cmd == "preview":
        cmd_prepare(cfg, everything=True)
        print(f"\nLook in the '{GEN_DIR.name}' folder. Delete a post's folder to regenerate it.")
    elif cmd == "publish":
        cmd_publish(cfg)
    elif cmd == "check":
        cmd_check(cfg)
    elif cmd == "refresh-token":
        print(refresh_token(cfg, os.environ["IG_ACCESS_TOKEN"].strip()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
