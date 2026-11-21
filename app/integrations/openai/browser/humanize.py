"""Humanized Playwright interaction primitives and persistent signup identities."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import random
import re
import secrets
import sys
import tempfile
import time
from typing import Any

_RNG = random.SystemRandom()

_SLOW_CHARS = frozenset({" ", "@", ".", "-", "_", "/"})

# 64 common English first names x 64 common English surnames = 4096 combinations.
FIRST_NAMES: tuple[str, ...] = (
    "Aaron", "Abigail", "Adam", "Aiden", "Alex", "Alexander", "Alice", "Amelia",
    "Andrew", "Anna", "Anthony", "Audrey", "Austin", "Benjamin", "Brandon", "Caleb",
    "Cameron", "Caroline", "Charles", "Charlotte", "Chloe", "Christian", "Christopher", "Claire",
    "Connor", "Daniel", "David", "Dylan", "Eleanor", "Elena", "Eli", "Elijah",
    "Elizabeth", "Ella", "Emily", "Emma", "Ethan", "Evan", "Evelyn", "Gabriel",
    "Grace", "Hannah", "Harper", "Henry", "Isaac", "Isabella", "Jack", "Jackson",
    "Jacob", "James", "Jonathan", "Jordan", "Joseph", "Joshua", "Julia", "Katherine",
    "Lauren", "Liam", "Lily", "Logan", "Lucas", "Madison", "Mason", "Matthew",
    "Maya", "Megan", "Mia", "Michael", "Natalie", "Nathan", "Noah", "Nora",
    "Oliver", "Olivia", "Owen", "Rachel", "Ryan", "Samuel", "Sarah", "Sophia",
    "Sophie", "Thomas", "Victoria", "William",
)

LAST_NAMES: tuple[str, ...] = (
    "Adams", "Allen", "Anderson", "Bailey", "Baker", "Barnes", "Bell", "Bennett",
    "Brooks", "Brown", "Bryant", "Butler", "Campbell", "Carter", "Clark", "Cole",
    "Coleman", "Collins", "Cook", "Cooper", "Cox", "Crawford", "Davis", "Edwards",
    "Ellis", "Evans", "Fisher", "Flores", "Ford", "Foster", "Gibson", "Graham",
    "Grant", "Gray", "Green", "Griffin", "Hall", "Hamilton", "Harris", "Harrison",
    "Hayes", "Henderson", "Hill", "Howard", "Hughes", "Hunt", "Jackson", "James",
    "Jenkins", "Johnson", "Jones", "Kelly", "Kennedy", "King", "Knight", "Lawrence",
    "Lee", "Lewis", "Long", "Marshall", "Martin", "Mason", "Matthews", "Miller",
    "Mitchell", "Moore", "Morgan", "Morris", "Murphy", "Murray", "Nelson", "Palmer",
    "Parker", "Patterson", "Payne", "Perry", "Peterson", "Phillips", "Porter", "Powell",
    "Price", "Reed", "Reynolds", "Richardson", "Roberts", "Robinson", "Rogers", "Ross",
    "Russell", "Sanders", "Scott", "Shaw", "Simmons", "Simpson", "Smith", "Spencer",
    "Stevens", "Stewart", "Stone", "Sullivan", "Taylor", "Thomas", "Thompson", "Turner",
    "Walker", "Wallace", "Walsh", "Ward", "Warren", "Watson", "Webb", "Wells",
    "West", "White", "Williams", "Wilson", "Wood", "Woods", "Wright", "Young",
)

_IDENTITIES_DIR_NAME = ".signup-identities"
_SELECT_ALL_SHORTCUT = "Meta+a" if sys.platform == "darwin" else "Control+a"


def uniform_float(min_val: float, max_val: float) -> float:
    lo = float(min_val)
    hi = float(max_val)
    if hi < lo:
        hi = lo
    if lo == hi:
        return lo
    return _RNG.uniform(lo, hi)


def human_pause(page: Any, min_sec: float = 0.8, max_sec: float = 2.2) -> float:
    """Pause for a random duration between min_sec and max_sec seconds."""
    lo = max(0.0, float(min_sec))
    hi = max(lo, float(max_sec))
    delay_sec = _RNG.uniform(lo, hi) if hi > lo else lo
    delay_ms = int(round(delay_sec * 1000.0))
    if delay_ms <= 0:
        return 0.0
    if page is not None and hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(delay_ms)
    else:
        time.sleep(delay_ms / 1000.0)
    return delay_ms / 1000.0


def _resolve_locator(page: Any, target: Any):
    if target is None:
        return None
    loc = page.locator(target).first if isinstance(target, str) else target
    if hasattr(loc, "count"):
        try:
            count = loc.count()
        except Exception:  # noqa: BLE001
            return None
        if count <= 0:
            return None
        if count > 1 and hasattr(loc, "first"):
            loc = loc.first
    return loc


def _is_clickable(loc: Any) -> bool:
    try:
        if hasattr(loc, "is_visible") and not loc.is_visible():
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        if hasattr(loc, "is_enabled") and not loc.is_enabled():
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        if hasattr(loc, "get_attribute"):
            if (loc.get_attribute("aria-disabled") or "").strip().lower() == "true":
                return False
    except Exception:  # noqa: BLE001
        pass
    return True


def human_click(page: Any, target: Any, *, timeout: int = 3500) -> bool:
    """Click a visible element at a random 35%..65% offset with human press timing."""
    loc = _resolve_locator(page, target)
    if loc is None or not _is_clickable(loc):
        return False
    try:
        if hasattr(loc, "scroll_into_view_if_needed"):
            loc.scroll_into_view_if_needed(timeout=timeout)
    except Exception:  # noqa: BLE001
        pass

    box = None
    try:
        if hasattr(loc, "bounding_box"):
            box = loc.bounding_box(timeout=timeout)
    except Exception:  # noqa: BLE001
        box = None

    human_pause(page, 0.08, 0.22)
    press_ms = _RNG.uniform(45.0, 115.0)

    if isinstance(box, dict) and float(box.get("width") or 0) > 1.0 and float(box.get("height") or 0) > 1.0:
        rel_x = float(box["width"]) * _RNG.uniform(0.35, 0.65)
        rel_y = float(box["height"]) * _RNG.uniform(0.35, 0.65)
        try:
            loc.click(position={"x": rel_x, "y": rel_y}, delay=press_ms, timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            pass

    try:
        loc.click(delay=press_ms, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_input_value(loc: Any) -> str | None:
    if not hasattr(loc, "input_value"):
        return None
    try:
        return str(loc.input_value(timeout=1000) or "")
    except Exception:  # noqa: BLE001
        return None


def _clear_focused_input(page: Any, loc: Any) -> None:
    """Clear an input using select-all + Backspace (never .fill(''))."""
    try:
        page.keyboard.press(_SELECT_ALL_SHORTCUT)
        human_pause(page, 0.05, 0.13)
        page.keyboard.press("Backspace")
        human_pause(page, 0.08, 0.16)
    except Exception:  # noqa: BLE001
        return

    remaining = _read_input_value(loc)
    if remaining:
        try:
            page.keyboard.press("End")
            for _ in range(min(len(remaining) + 2, 256)):
                page.keyboard.press("Backspace")
                human_pause(page, 0.015, 0.035)
        except Exception:  # noqa: BLE001
            pass


def human_type_text(page: Any, target: Any, text: str, *, clear: bool = True) -> bool:
    """Focus target via human_click and type text char-by-char with uneven cadence."""
    loc = _resolve_locator(page, target)
    if loc is None:
        return False
    if not human_click(page, loc):
        return False
    human_pause(page, 0.12, 0.28)

    wanted = str(text if text is not None else "")
    existing = _read_input_value(loc)
    if clear and existing is not None:
        if existing == wanted and wanted != "":
            human_pause(page, 0.15, 0.32)
            return True
        if existing != "":
            _clear_focused_input(page, loc)

    next_micro_pause = _RNG.randint(8, 12)
    typed_count = 0
    total = len(wanted)
    for idx, ch in enumerate(wanted):
        try:
            page.keyboard.type(ch)
        except Exception:  # noqa: BLE001
            return False
        typed_count += 1
        if idx < total - 1:
            if ch in _SLOW_CHARS:
                human_pause(page, 0.16, 0.36)
            else:
                human_pause(page, 0.045, 0.135)
            if typed_count >= next_micro_pause:
                human_pause(page, 0.24, 0.52)
                next_micro_pause = typed_count + _RNG.randint(8, 12)

    human_pause(page, 0.15, 0.32)
    return True


def _element_is_focused(loc: Any) -> bool:
    try:
        return bool(loc.evaluate("el => document.activeElement === el"))
    except Exception:  # noqa: BLE001
        return True


def human_type_otp(page: Any, code: str, *, single_input: Any = None, boxes: Any = None) -> bool:
    """Type an OTP code in two human beats (3 + remaining) without ever calling .fill()."""
    digits = str(code or "").strip()
    if not digits:
        return False

    has_boxes = False
    box_count = 0
    if boxes is not None and hasattr(boxes, "count"):
        try:
            box_count = boxes.count()
            has_boxes = box_count > 0 and boxes.first.is_visible()
        except Exception:  # noqa: BLE001
            has_boxes = False

    if not has_boxes and single_input is None:
        return False

    # Simulate reading the code from email/SMS before typing.
    human_pause(page, 0.35, 0.80)

    if has_boxes:
        chars = digits[:box_count]
        first_box = boxes.nth(0)
        if not human_click(page, first_box):
            return False
        human_pause(page, 0.10, 0.22)
        for idx, ch in enumerate(chars):
            cell = boxes.nth(idx)
            if idx > 0 and not _element_is_focused(cell):
                if not human_click(page, cell):
                    return False
                human_pause(page, 0.06, 0.14)
            existing = _read_input_value(cell)
            if existing:
                _clear_focused_input(page, cell)
            try:
                page.keyboard.type(ch)
            except Exception:  # noqa: BLE001
                return False
            if idx < len(chars) - 1:
                if len(chars) >= 6 and idx == 2:
                    human_pause(page, 0.18, 0.38)
                else:
                    human_pause(page, 0.08, 0.20)
        human_pause(page, 0.15, 0.30)
        return True

    loc = _resolve_locator(page, single_input)
    if loc is None or not human_click(page, loc):
        return False
    human_pause(page, 0.12, 0.24)
    existing = _read_input_value(loc)
    if existing:
        _clear_focused_input(page, loc)
    for idx, ch in enumerate(digits):
        try:
            page.keyboard.type(ch)
        except Exception:  # noqa: BLE001
            return False
        if idx < len(digits) - 1:
            if len(digits) >= 6 and idx == 2:
                human_pause(page, 0.18, 0.38)
            else:
                human_pause(page, 0.08, 0.20)
    human_pause(page, 0.15, 0.30)
    return True


def human_nudge_mouse(page: Any) -> None:
    """Move the cursor gently to a random in-viewport coordinate during idle dwell."""
    if page is None or not hasattr(page, "mouse"):
        return
    width, height = 1280.0, 720.0
    try:
        vp = getattr(page, "viewport_size", None)
        if isinstance(vp, dict) and vp.get("width") and vp.get("height"):
            width = float(vp["width"])
            height = float(vp["height"])
        else:
            dims = page.evaluate("() => ({w: window.innerWidth || 1280, h: window.innerHeight || 720})")
            if isinstance(dims, dict) and dims.get("w") and dims.get("h"):
                width = float(dims["w"])
                height = float(dims["h"])
    except Exception:  # noqa: BLE001
        pass
    x = width * _RNG.uniform(0.20, 0.80)
    y = height * _RNG.uniform(0.20, 0.75)
    try:
        page.mouse.move(x, y)
    except Exception:  # noqa: BLE001
        pass


def _age_for_birthday(year: int, month: int, day: int, *, today: date | None = None) -> int:
    ref = today or date.today()
    return max(18, ref.year - year - ((month, day) > (ref.month, ref.day)))


def _build_random_identity() -> dict[str, Any]:
    today = date.today()
    first = secrets.choice(FIRST_NAMES)
    last = secrets.choice(LAST_NAMES)
    age = 22 + secrets.randbelow(24)  # 22..45 inclusive
    month = 1 + secrets.randbelow(12)
    day = 1 + secrets.randbelow(28)
    year = today.year - age - ((month, day) > (today.month, today.day))
    return {
        "schema": 1,
        "first_name": first,
        "last_name": last,
        "name": f"{first} {last}",
        "age": age,
        "year": year,
        "month": month,
        "day": day,
        "birthday": f"{year:04d}-{month:02d}-{day:02d}",
    }


def _normalize_identity(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        first = str(raw.get("first_name") or "").strip()
        last = str(raw.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
    if not name:
        return None
    try:
        year = int(raw["year"])
        month = int(raw["month"])
        day = int(raw["day"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (1940 <= year <= date.today().year - 18 and 1 <= month <= 12 and 1 <= day <= 28):
        return None
    parts = name.split(" ", 1)
    first_name = str(raw.get("first_name") or parts[0]).strip()
    last_name = str(raw.get("last_name") or (parts[1] if len(parts) > 1 else "")).strip()
    return {
        "schema": 1,
        "first_name": first_name,
        "last_name": last_name,
        "name": name,
        "age": _age_for_birthday(year, month, day),
        "year": year,
        "month": month,
        "day": day,
        "birthday": f"{year:04d}-{month:02d}-{day:02d}",
    }


def _email_identity_filename(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return ""
    slug = normalized.replace("@", "_at_")
    safe = re.sub(r"[^a-z0-9._+-]", "_", slug).strip(".")
    return safe or "identity"


def signup_identity_for_email(profile_root: Path | str, email: str) -> dict[str, Any]:
    """Return a stable random English name + adult birthday persisted per email."""
    safe_name = _email_identity_filename(email)
    if not safe_name:
        return _build_random_identity()

    identities_dir = Path(profile_root) / _IDENTITIES_DIR_NAME
    path = identities_dir / f"{safe_name}.json"

    if path.is_file():
        try:
            loaded = _normalize_identity(json.loads(path.read_text(encoding="utf-8")))
            if loaded is not None:
                return loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

    identity = _build_random_identity()
    identities_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".signup-id-", suffix=".tmp", dir=identities_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(identity, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            # Another concurrent launch created it first; read the winner if valid.
            try:
                winner = _normalize_identity(json.loads(path.read_text(encoding="utf-8")))
                if winner is not None:
                    return winner
            except (OSError, UnicodeError, json.JSONDecodeError):
                os.replace(temp_path, path)
        except OSError:
            os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    try:
        saved = _normalize_identity(json.loads(path.read_text(encoding="utf-8")))
        if saved is not None:
            return saved
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return identity
