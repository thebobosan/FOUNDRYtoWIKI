#!/usr/bin/env python3
"""
Foundry PF2e to MediaWiki - Full Exporter
==========================================
Reads PF2e characters from LevelDB, reconstructs stats via math engine,
enriches items from system compendium packs, resolves Foundry icon URLs,
and pushes formatted pages to MediaWiki.

Usage:
    python full-exporter.py                          # Preview all PCs
    python full-exporter.py --push                   # Push all PCs to wiki
    python full-exporter.py --char "Name"            # Preview one character
    python full-exporter.py --char "Name" --push     # Push one character
    python full-exporter.py --char "Name" --debug    # Dump raw system data
"""

import plyvel
import json
import mwclient
import sys
import os
import re
import html
import shutil
import tempfile
import math
import bisect
from pathlib import Path
from datetime import datetime, timedelta
from html.parser import HTMLParser

# ════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════

WORLD_PATH   = "/var/www/java/foundry.atkennedy.com/foundrydata/Data/worlds/temporary-title/"
FOUNDRY_DATA = "/var/www/java/foundry.atkennedy.com/foundrydata/Data"
FOUNDRY_URL  = "https://foundry.atkennedy.com"

WIKI_URL      = "wiki.atkennedy.com"
WIKI_USER     = "Oracle"
WIKI_PASSWORD = os.environ.get("WIKI_PASSWORD")

COMPENDIUM_PACKS = [
    "equipment", "spells", "feats", "actions",
    "ancestries", "backgrounds", "classes", "heritages",
    "class-features", "ancestry-features", "deities",
]

PROFICIENCY_MAP = {
    0: {"name": "Untrained", "color": "#708090"},
    1: {"name": "Trained",   "color": "#2e8b57"},
    2: {"name": "Expert",    "color": "#4682b4"},
    3: {"name": "Master",    "color": "#8a2be2"},
    4: {"name": "Legendary", "color": "#d4af37"},
}

DEFAULT_ICONS = {
    "weapon":     "systems/pf2e/icons/default-icons/weapon.svg",
    "armor":      "systems/pf2e/icons/default-icons/armor.svg",
    "shield":     "systems/pf2e/icons/default-icons/shield.svg",
    "equipment":  "systems/pf2e/icons/default-icons/equipment.svg",
    "consumable": "systems/pf2e/icons/default-icons/consumable.svg",
    "backpack":   "systems/pf2e/icons/default-icons/backpack.svg",
    "treasure":   "systems/pf2e/icons/default-icons/treasure.svg",
    "feat":       "systems/pf2e/icons/default-icons/feat.svg",
    "action":     "systems/pf2e/icons/default-icons/action.svg",
    "spell":      "systems/pf2e/icons/default-icons/spell.svg",
    "lore":       "systems/pf2e/icons/default-icons/lore.svg",
    "character":  "systems/pf2e/icons/default-icons/mystery-man.svg",
}
GENERIC_ICON_FRAGMENTS = ("default-icons", "mystery-man", "iconics")

# ════════════════════════════════════════════════════════════════════════════
# Utility helpers
# ════════════════════════════════════════════════════════════════════════════

def icon_url(img: str, item_type: str = "", allow_iconics: bool = False) -> str:
    """
    allow_iconics: when True, an "iconics" path (Paizo's bundled iconic-hero
    art, e.g. Ezren) is treated as a real portrait rather than a generic
    placeholder. Only actor portraits should pass this — for items,
    "iconics" in the path is still a placeholder signal.
    """
    path = img or ""
    fragments = GENERIC_ICON_FRAGMENTS
    if allow_iconics:
        fragments = tuple(f for f in fragments if f != "iconics")
    is_generic = not path or any(f in path for f in fragments)
    if is_generic:
        path = DEFAULT_ICONS.get(item_type, "")
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{FOUNDRY_URL}/{path.lstrip('/')}"


def wiki_escape(name: str) -> str:
    """
    Escape MediaWiki markup metacharacters in player-editable text (item,
    character, spell, feat names, etc.) before interpolating it into
    wikitext. A literal '|' breaks table rows, '[[' / ']]' breaks or
    redirects links, and '{{' / '}}' can trigger template transclusion —
    all of which are otherwise reachable since these names come straight
    from freely player-editable Foundry data.
    """
    if not isinstance(name, str):
        return name
    return (name.replace("{{", "&#123;&#123;")
                .replace("}}", "&#125;&#125;")
                .replace("[[", "&#91;&#91;")
                .replace("]]", "&#93;&#93;")
                .replace("|", "&#124;"))


def wiki_img(url: str, size: int = 20, alt: str = "") -> str:
    """
    Render an external image using MediaWiki's <html> extension tag.
    Requires $wgRawHtml = true in LocalSettings.php.
    """
    if not url:
        return ""
    alt_attr = html.escape(alt, quote=True) if alt else ""
    url_attr = html.escape(url, quote=True)
    return (f'<html><img src="{url_attr}" width="{size}" height="{size}" '
            f'alt="{alt_attr}" style="vertical-align:middle;" /></html>')


def item_stat_line(item: dict) -> str:
    """
    Build a short crunch summary (damage, AC bonus, traits, price, etc.)
    for an item, for use alongside its flavor description in a tooltip.
    The Foundry description text is pure flavor prose — none of a
    weapon's damage die, an armor's AC bonus, or a shield's Hardness/HP
    live in it, so it has to be assembled separately from system fields.
    """
    s     = item.get("system") or {}
    itype = item.get("type")
    parts = []

    if itype == "weapon":
        dmg = s.get("damage") or {}
        dice, die, dtype = dmg.get("dice"), dmg.get("die"), dmg.get("damageType")
        if dice and die:
            dmg_str = f"{dice}{die}"
            if dtype:
                dmg_str += f" {dtype}"
            parts.append(dmg_str)
        cat, grp = s.get("category"), s.get("group")
        if cat or grp:
            parts.append(" ".join(str(x).title() for x in (cat, grp) if x))
        rng = s.get("range")
        if rng:
            parts.append(f"Range {rng} ft.")
        reload_v = (s.get("reload") or {}).get("value")
        if reload_v:
            parts.append(f"Reload {reload_v}")

    elif itype == "armor":
        ac = s.get("acBonus")
        if ac is not None:
            parts.append(f"AC {fmt_mod(ac)}")
        cat = s.get("category")
        if cat:
            parts.append(str(cat).title())
        dex_cap = s.get("dexCap")
        if dex_cap is not None:
            parts.append(f"Dex Cap +{dex_cap}")
        str_req = s.get("strength")
        if str_req is not None:
            parts.append(f"Str {str_req}")
        cp = s.get("checkPenalty")
        if cp:
            parts.append(f"Check {cp}")
        sp = s.get("speedPenalty")
        if sp:
            parts.append(f"Speed {sp} ft.")

    elif itype == "shield":
        ac = s.get("acBonus")
        if ac is not None:
            parts.append(f"AC {fmt_mod(ac)}")
        hardness = s.get("hardness")
        if hardness is not None:
            parts.append(f"Hardness {hardness}")
        hp = s.get("hp") or {}
        if isinstance(hp, dict) and hp.get("max"):
            parts.append(f"HP {hp.get('value', 0)}/{hp['max']}")

    elif itype == "consumable":
        cat = s.get("category")
        if cat:
            parts.append(str(cat).title())

    traits = ((s.get("traits") or {}).get("value") or [])
    if traits:
        parts.append("Traits: " + ", ".join(str(t).title() for t in traits))

    price_node = (s.get("price") or {}).get("value") or {}
    if isinstance(price_node, dict):
        price_str = ", ".join(f"{price_node[c]} {c}" for c in ("pp", "gp", "sp", "cp")
                              if price_node.get(c))
        if price_str:
            parts.append(price_str)

    return " • ".join(parts)


def wiki_tooltip(name: str, desc_plain: str = "", stat_line: str = "", max_len: int = 600) -> str:
    """
    Render an item name as inline HTML with a native browser tooltip (the
    'title' attribute) showing its stats and description, instead of a
    MediaWiki [[link]] to a per-item page that doesn't exist. Requires
    $wgRawHtml = true, same as wiki_img.

    desc_plain must already be plain text (e.g. via strip_html) — a title
    attribute renders as literal text, so wikitext/HTML markup would show
    up as raw '''/[[ ]]/<tags> in the tooltip rather than being rendered.
    """
    name_esc = html.escape(name, quote=True)
    text = "\n".join(t for t in (stat_line.strip(), desc_plain.strip()) if t)
    if not text:
        return f'<html><span title="{name_esc}">{name_esc}</span></html>'
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    desc_esc = html.escape(text, quote=True)
    return f'<html><span title="{desc_esc}" style="cursor:help; border-bottom:1px dotted;">{name_esc}</span></html>'


# Matches @UUID[...]{Label} — keep the label
_UUID_RE = re.compile(r'@UUID\[[^\]]*\]\{([^}]+)\}')
# Matches bare @UUID[...] with no label — drop
_UUID_BARE_RE = re.compile(r'@UUID\[[^\]]*\]')
# Matches Foundry inline roll expressions — drop entirely
# Non-greedy with re.DOTALL handles nested brackets like [[/r 2d8[healing] #label]]
_INLINE_ROLL_RE = re.compile(r'\[\[/.*?\]\]', re.DOTALL)

# Matches @Damage[...]/@Check[...]/@Template[...] enrichers, with an optional
# trailing {Label}. The bracket body allows one level of nested [...] (e.g.
# "(1d8+3)[slashing]", "(4[splash])[force]") since PF2e never nests deeper.
_ENRICHER_TAG_RE = re.compile(
    r'@(Damage|Check|Template)\[((?:[^\[\]]|\[[^\[\]]*\])*)\](?:\{([^}]*)\})?'
)


def _format_damage_tag(content: str) -> str:
    """'(1d8+3)[slashing]|options:area-damage' → '1d8+3 slashing damage'."""
    m = re.match(r'^(.*)\[([^\[\]]+)\](?:\|.*)?$', content)
    if not m:
        return content.strip()
    formula = m.group(1).strip()
    if formula.startswith('(') and formula.endswith(')'):
        formula = formula[1:-1]
    types = [t.strip() for t in m.group(2).split(',') if t.strip()]
    if "healing" in types:
        other = [t for t in types if t != "healing"]
        suffix = (" ".join(other) + " " if other else "") + "healing"
    else:
        suffix = (" ".join(types) + " damage") if types else "damage"
    return f"{formula} {suffix}".strip()


def _format_check_tag(content: str) -> str:
    """'fortitude|dc:20|basic' → 'basic DC 20 Fortitude'."""
    parts = [p.strip() for p in content.split('|')]
    slug  = parts[0] if parts else ""
    dc, basic = None, False
    for p in parts[1:]:
        if p.startswith('dc:'):
            dc = p[3:]
        elif p == 'basic':
            basic = True
    name   = "flat check" if slug == "flat" else slug.capitalize()
    prefix = "basic " if basic else ""
    if dc:
        return f"{prefix}DC {dc} {name}"
    return name if slug == "flat" else f"{name} check"


def _format_template_tag(content: str) -> str:
    """'burst|distance:40' → '40-foot burst'."""
    parts    = [p.strip() for p in content.split('|')]
    shape    = parts[0] if parts else ""
    distance = next((p[len('distance:'):] for p in parts[1:]
                      if p.startswith('distance:')), None)
    return f"{distance}-foot {shape}" if distance else shape


def _resolve_enricher_tag(m: re.Match) -> str:
    tag, content, label = m.group(1), m.group(2), m.group(3)
    if label:
        return label
    if tag == "Damage":
        return _format_damage_tag(content)
    if tag == "Check":
        return _format_check_tag(content)
    if tag == "Template":
        return _format_template_tag(content)
    return content


def clean_foundry_text(text: str) -> str:
    """
    Remove Foundry-specific markup:
      @UUID[...]{Label}    → Label
      @UUID[...]           → (removed)
      @Damage[...]{Label}  → Label
      @Damage[2d6[fire]]   → '2d6 fire damage'
      @Check[fortitude|dc:20]  → 'DC 20 Fortitude'
      @Template[burst|distance:40]  → '40-foot burst'
      [[/r 2d8 ...]]       → (removed)
    """
    text = _UUID_RE.sub(r'\1', text)
    text = _UUID_BARE_RE.sub('', text)
    text = _ENRICHER_TAG_RE.sub(_resolve_enricher_tag, text)
    text = _INLINE_ROLL_RE.sub('', text)
    return text


def html_to_wikitext(text) -> str:
    """
    Convert Foundry HTML description text to MediaWiki wikitext.
    """
    if text is None:
        return ""
    if isinstance(text, list):
        text = "\n\n".join(str(i) for i in text if i)
    elif not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return ""

    text = clean_foundry_text(text)

    class _Converter(HTMLParser):
        def __init__(self):
            super().__init__()
            self.out        = []
            self.list_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag == "p":
                if self.out:
                    self.out.append("\n\n")
            elif tag == "br":
                self.out.append("<br />\n")
            elif tag in ("ul", "ol"):
                self.list_depth += 1
            elif tag == "li":
                self.out.append("\n" + "*" * self.list_depth + " ")
            elif tag in ("strong", "b"):
                self.out.append("'''")
            elif tag in ("em", "i"):
                self.out.append("''")
            elif tag == "h1":
                self.out.append("\n== ")
            elif tag == "h2":
                self.out.append("\n=== ")
            elif tag == "h3":
                self.out.append("\n==== ")
            elif tag == "h4":
                self.out.append("\n===== ")

        def handle_endtag(self, tag):
            if tag == "p":
                self.out.append("\n\n")
            elif tag in ("ul", "ol"):
                self.list_depth = max(0, self.list_depth - 1)
                if self.list_depth == 0:
                    self.out.append("\n")
            elif tag in ("strong", "b"):
                self.out.append("'''")
            elif tag in ("em", "i"):
                self.out.append("''")
            elif tag == "h1":
                self.out.append(" ==\n")
            elif tag == "h2":
                self.out.append(" ===\n")
            elif tag == "h3":
                self.out.append(" ====\n")
            elif tag == "h4":
                self.out.append(" =====\n")

        def handle_data(self, d):
            self.out.append(d)

        def result(self):
            out = "".join(self.out).strip()
            out = re.sub(r'\n{3,}', '\n\n', out)
            out = re.sub(r'\n[ \t]+\n', '\n\n', out)
            return out

    c = _Converter()
    c.feed(text)
    return c.result()


def strip_html(text) -> str:
    """
    Strip all HTML and return plain text. Use for infobox values and
    other plain-text fields where wikitext markup is not wanted.
    """
    if text is None:
        return ""
    if isinstance(text, list):
        text = "\n".join(str(i) for i in text if i)
    elif not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return ""

    text = clean_foundry_text(text)

    class _S(HTMLParser):
        def __init__(self):
            super().__init__()
            self.out = []
        def handle_data(self, d):
            self.out.append(d)
        def handle_starttag(self, tag, _):
            if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "h5", "tr"):
                self.out.append("\n")
            elif tag in ("td", "th"):
                self.out.append(" ")
        def result(self):
            return re.sub(r'\n{3,}', '\n\n', "".join(self.out)).strip()

    s = _S()
    s.feed(text)
    return s.result()


def fmt_mod(val) -> str:
    try:
        v = int(val)
        return f"+{v}" if v >= 0 else str(v)
    except (ValueError, TypeError):
        return "+0"


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _int(val, default: int = 0) -> int:
    """Safe int cast."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def collapsible(title: str, content: str, collapsed: bool = True) -> str:
    """
    Wrap content in a MediaWiki mw-collapsible div.
    Requires no extensions — mw-collapsible is built into MediaWiki core.
    """
    state = " mw-collapsed" if collapsed else ""
    return (
        f'<div class="toccolours mw-collapsible{state}">\n'
        f"<b>{html.escape(title)}</b>\n"
        f'<div class="mw-collapsible-content">\n'
        f"{content}\n"
        f"</div>\n"
        f"</div>\n"
    )


def make_site() -> mwclient.Site:
    """Create and return an authenticated MediaWiki site connection."""
    if not WIKI_PASSWORD:
        raise RuntimeError(
            "WIKI_PASSWORD environment variable is not set — refusing to "
            "connect without it. Preview/--no-push runs don't need it."
        )
    site = mwclient.Site(WIKI_URL, path="/")
    site.login(WIKI_USER, WIKI_PASSWORD)
    return site


# ════════════════════════════════════════════════════════════════════════════
# Compendium index
# ════════════════════════════════════════════════════════════════════════════

_COMP_CACHE_PATH = Path("session_snapshots/compendium_cache.json")

def build_compendium_index(data_root: str) -> dict:
    index = {}
    if not data_root:
        print("  ⚠  No data root — skipping compendium enrichment.")
        return index

    packs_root = Path(data_root) / "systems" / "pf2e" / "packs"
    print(f"  Compendium path: {packs_root}")

    if not packs_root.exists():
        print(f"  ⚠  Path does not exist.")
        return index

    # Determine the newest mtime across all tracked pack directories
    max_mtime = 0.0
    for pack_name in COMPENDIUM_PACKS:
        pack_path = packs_root / pack_name
        if pack_path.exists():
            for f in pack_path.rglob("*"):
                try:
                    max_mtime = max(max_mtime, f.stat().st_mtime)
                except OSError:
                    pass

    # Load cache if it exists and packs haven't changed since it was written
    if _COMP_CACHE_PATH.exists():
        try:
            cached = json.loads(_COMP_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("mtime", 0) >= max_mtime:
                print(f"  Compendium index loaded from cache "
                      f"({len(cached['index'])} entries).")
                return cached["index"]
        except Exception:
            pass  # stale or corrupt cache — fall through to rebuild

    available = sorted(p.name for p in packs_root.iterdir() if p.is_dir())
    print(f"  Available packs ({len(available)}): {', '.join(available[:20])}"
          + (" …" if len(available) > 20 else ""))

    total = 0
    for pack_name in COMPENDIUM_PACKS:
        pack_path = packs_root / pack_name
        if not pack_path.exists():
            print(f"  ⚠  Pack not found: {pack_name}")
            continue
        tmp = tempfile.mkdtemp()
        tmp_pack = Path(tmp) / pack_name
        try:
            shutil.copytree(str(pack_path), str(tmp_pack))
            db = plyvel.DB(str(tmp_pack))
            pack_count = 0
            err_count  = 0
            with db:
                for raw_key, raw_val in db:
                    k = raw_key.decode("utf-8", errors="replace")
                    parts = k.split("!")
                    if len(parts) not in (2, 3):
                        continue
                    try:
                        entry = json.loads(raw_val)
                        name  = entry.get("name", "")
                        if not name:
                            continue
                        sys_data  = entry.get("system", {})
                        desc_raw  = sys_data.get("description", {})
                        desc      = desc_raw.get("value", "") if isinstance(desc_raw, dict) else ""
                        traits_r  = sys_data.get("traits", {})
                        traits    = traits_r.get("value", []) if isinstance(traits_r, dict) else []
                        slug      = sys_data.get("slug", "")
                        record    = {"img": entry.get("img", ""), "desc": desc,
                                     "traits": traits, "slug": slug, "pack": pack_name}
                        index[norm_name(name)] = record
                        if slug:
                            index[norm_name(slug)] = record
                        pack_count += 1
                        total += 1
                    except Exception:
                        err_count += 1
            warn = f" ({err_count} parse errors)" if err_count else ""
            print(f"    ✓ {pack_name}: {pack_count} entries{warn}")
        except Exception as e:
            print(f"  ⚠  Could not read pack '{pack_name}': {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"  Compendium index: {total} entries.")

    # Save cache for next run
    try:
        _COMP_CACHE_PATH.parent.mkdir(exist_ok=True)
        _COMP_CACHE_PATH.write_text(
            json.dumps({"mtime": max_mtime, "index": index}),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"  ⚠  Could not save compendium cache: {e}")

    return index


def enrich_item(item: dict, compendium: dict) -> dict:
    if not compendium:
        return item
    comp = compendium.get(norm_name(item.get("name", "")), {})
    if not comp:
        return item

    enriched = dict(item)
    actor_img = item.get("img", "")
    if not actor_img or any(f in actor_img for f in GENERIC_ICON_FRAGMENTS):
        if comp.get("img") and not any(f in comp["img"] for f in GENERIC_ICON_FRAGMENTS):
            enriched["img"] = comp["img"]

    sys_data  = dict(item.get("system", {}))
    desc_node = sys_data.get("description", {})
    actor_desc = desc_node.get("value", "") if isinstance(desc_node, dict) else ""
    if not actor_desc.strip() and comp.get("desc"):
        desc_node = dict(desc_node) if isinstance(desc_node, dict) else {}
        desc_node["value"] = comp["desc"]
        sys_data["description"] = desc_node
        enriched["system"] = sys_data

    traits_node  = sys_data.get("traits", {})
    actor_traits = traits_node.get("value", []) if isinstance(traits_node, dict) else []
    if not actor_traits and comp.get("traits"):
        traits_node = dict(traits_node) if isinstance(traits_node, dict) else {}
        traits_node["value"] = list(comp["traits"])
        sys_data["traits"] = traits_node
        enriched["system"] = sys_data

    return enriched


# ════════════════════════════════════════════════════════════════════════════
# Full exporter class
# ════════════════════════════════════════════════════════════════════════════

def _read_boost_slot(slot):
    """
    Return the ability slug(s) that a PF2e boost/flaw slot actually grants.

    Slot shapes:
      "str"                                 → "str"   (plain fixed)
      {"value": "con"}                      → "con"   (dict fixed, single)
      {"value": ["con"]}                    → "con"   (forced boost, one option — nothing to choose)
      {"value": ["int","str"], "selected":"str"} → "str"  (player chose STR)
      {"value": ["str","dex","con",...6..], "selected":"wis"} → "wis" (free pick)
      {"value": ["str","dex","con",...6..], "selected":null}  → None  (not yet chosen)
    """
    if isinstance(slot, str):
        return slot
    if not isinstance(slot, dict):
        return None
    # "selected" is the player's actual choice; trust it when present and non-null
    selected = slot.get("selected")
    if selected and isinstance(selected, str):
        return selected
    val = slot.get("value")
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        # A single-element list is a forced boost — there was only one
        # possible ability, so Foundry never records a "selected" key.
        # A multi-element list with no "selected" is a genuine unresolved
        # free-pick choice and must be skipped.
        return val[0] if len(val) == 1 else None
    return None


class FullExporter:

    SKILL_ABILITY = {
        "acrobatics": "dex", "arcana": "int", "athletics": "str", "crafting": "int",
        "deception": "cha", "diplomacy": "cha", "intimidation": "cha", "medicine": "wis",
        "nature": "wis", "occultism": "int", "performance": "cha", "religion": "wis",
        "society": "int", "stealth": "dex", "survival": "wis", "thievery": "dex",
    }

    def __init__(self, world_path: str, data_root: str = FOUNDRY_DATA):
        self.world_path = Path(world_path)
        original = self.world_path / "data" / "actors"
        if not original.exists():
            raise FileNotFoundError(f"Actor database not found at {original}")

        self.temp_dir  = tempfile.mkdtemp()
        actors_copy    = Path(self.temp_dir) / "actors"
        shutil.copytree(str(original), str(actors_copy))
        self.actors_db = plyvel.DB(str(actors_copy))

        print("Building compendium index…")
        self.compendium = build_compendium_index(data_root)

        # Combat record (last kill / last downed-by) is built lazily on first
        # access via combat_record(), since it requires reading the messages DB
        # and resolving token→actor names. Cached after first build.
        self._combat_record = None
        self._raw_msgs_cache = None
        self._campaign_stats_cache = None

    # ── Wiki page name tracking (renames) ───────────────────────────────────

    _PAGE_NAMES_PATH = Path("session_snapshots") / "wiki_page_names.json"

    def _load_page_names(self) -> dict:
        """
        {"Characters": {actor_id: last-pushed page name}, "NPCs": {...}}

        Wiki pages are titled after the actor's *current* name (e.g.
        "Characters/Ashes"), but Foundry actor ids are stable across renames
        (e.g. a player renaming "Yuki's Character" to "Ashes"). Without this
        map, a rename silently creates a second page under the new name and
        leaves the old one orphaned forever — this lets push_to_wiki detect
        the rename and move the existing page instead.
        """
        if not self._PAGE_NAMES_PATH.exists():
            return {}
        try:
            return json.loads(self._PAGE_NAMES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_page_names(self, page_names: dict):
        self._PAGE_NAMES_PATH.parent.mkdir(exist_ok=True)
        self._PAGE_NAMES_PATH.write_text(json.dumps(page_names, indent=2), encoding="utf-8")

    # ── Combat record (last kill / last knocked unconscious) ───────────────

    def combat_record(self) -> dict:
        """
        Build per-actor combat record by replaying the chat message log.

        Returns dict: actor_id → {
            "last_kill":      {"name": victim, "ts": ms} or None,
            "last_downed_by": {"name": attacker, "ts": ms} or None,
        }

        Merges two event sources, sorted chronologically so the most recent
        event wins per actor:
          - PC downing events (see _build_downing_events) — a PC actually
            going to the Dying/Unconscious condition, attributed to the
            attacker's last hit before the condition card.
          - NPC kill events (see _build_npc_kill_events) — an NPC token's
            ActorDelta HP reaching <= 0, attributed to the last hit recorded
            against that token anywhere in the chat log.
        """
        if self._combat_record is not None:
            return self._combat_record

        _, type_by_actor = self._actor_maps()

        record: dict = {}
        events = self._build_downing_events() + self._build_npc_kill_events()
        events.sort(key=lambda e: e["ts"])

        for ev in events:   # events are chronological (ascending ts)
            atk_id, atk_name = ev["attacker_id"], ev["attacker_name"]
            vic_id, vic_name = ev["victim_id"],   ev["victim_name"]
            ts               = ev["ts"]

            # Victim: record who downed them (most recent wins — last assignment).
            # NPC kill events key victim_id by token id, not actor id (see
            # _build_npc_kill_events), so skip the write when it isn't a known
            # actor id — otherwise combat_record() ends up with phantom
            # "actor" entries keyed by token id.
            if vic_id and vic_id in type_by_actor:
                rec = record.setdefault(vic_id, {"last_kill": None, "last_downed_by": None})
                if atk_name and atk_id != vic_id:
                    rec["last_downed_by"] = {"name": atk_name, "ts": ts}
                else:
                    # self-inflicted / persistent / sourceless
                    rec["last_downed_by"] = {"name": ev.get("source_label") or "—", "ts": ts}

            # Attacker: record their kill (any non-self victim counts)
            if atk_id and atk_id != vic_id and vic_name:
                rec = record.setdefault(atk_id, {"last_kill": None, "last_downed_by": None})
                rec["last_kill"] = {"name": vic_name, "ts": ts}

        self._combat_record = record
        return record

    def _build_downing_events(self) -> list:
        """
        Build a chronological list of downing events from the chat message log.

        Strategy
        ────────
        The pf2e system sends two kinds of relevant messages:
          A. appliedDamage messages — structured flags with origin.actor (attacker)
             and uuid (victim). The 'value' field is the NEW remaining HP (not
             damage dealt). In practice the killing blow often bypasses the
             Apply Damage button so value never reaches 0 in the log.
          B. Dying/unconscious condition cards — plain HTML messages with no
             pf2e flags, but speaker.actor = the victim and content containing
             the condition name.

        We detect knockouts via (B), then walk backward through (A) to find
        the most recent hit on that victim — that hit's origin.actor is the
        attacker. No time window needed: "most recent hit before going down"
        is the correct attribution regardless of gap.
        """
        messages_path = self.world_path / "data" / "messages"
        if not messages_path.exists():
            print("  ⚠  No messages database — combat record unavailable.")
            return []

        name_by_actor, _ = self._actor_maps()
        raw_msgs         = self._load_raw_messages()

        # ── Pass 1: build hit history per victim ─────────────────────────────
        # Two attribution sources:
        #   A. appliedDamage — structured, when Apply Damage button is used.
        #   B. context.target — on ALL roll messages (attack + damage rolls),
        #      captures hits even when damage is applied manually to HP.
        hit_history: dict[str, list] = {}

        _DYING_RE = re.compile(r'dying|unconscious', re.IGNORECASE)

        for m in raw_msgs:
            pf     = (m.get("flags") or {}).get("pf2e") or {}
            ts     = m.get("timestamp", 0)
            msg_id = m.get("_id", "")

            # Source A: appliedDamage (button-applied damage)
            applied = pf.get("appliedDamage")
            if isinstance(applied, dict) and not applied.get("isHealing"):
                victim_id = self._uuid_to_actor_id(applied.get("uuid", ""))
                if not victim_id:
                    victim_id = (m.get("speaker") or {}).get("actor", "")
                origin  = pf.get("origin") or {}
                atk_id  = self._uuid_to_actor_id(origin.get("actor", "") if isinstance(origin, dict) else "")
                if victim_id and atk_id and atk_id != victim_id:
                    hit_history.setdefault(victim_id, []).append(
                        (ts, atk_id, name_by_actor.get(atk_id, ""), msg_id)
                    )

            # Source B: context.target on attack/damage roll messages.
            # Captures all attacks regardless of how damage is applied.
            # Restricted to actual attack/damage rolls (matching
            # _build_npc_kill_events) — context.target also shows up on
            # saving throws and skill checks rolled against a target, which
            # deal no damage and shouldn't count as a "hit" for downing
            # attribution. isHealing check kept too, to avoid crediting a
            # healer as the one who downed a PC.
            ctx = pf.get("context") or {}
            if (isinstance(ctx, dict) and not ctx.get("isHealing")
                    and ctx.get("type") in ("attack-roll", "damage-roll")):
                target = ctx.get("target") or pf.get("target") or {}
                if isinstance(target, dict):
                    raw_target = target.get("actor", "")
                    victim_id  = self._uuid_to_actor_id(raw_target) or raw_target
                    origin     = pf.get("origin") or {}
                    atk_id     = (self._uuid_to_actor_id(origin.get("actor", "") if isinstance(origin, dict) else "")
                                  or (m.get("speaker") or {}).get("actor", ""))
                    if victim_id and atk_id and atk_id != victim_id:
                        hit_history.setdefault(victim_id, []).append(
                            (ts, atk_id, name_by_actor.get(atk_id, ""), msg_id)
                        )

        # Deduplicate by (msg_id, atk_id) where a message id is available —
        # this collapses the same physical hit when both Source A and B
        # detect it on the same message, without also collapsing two
        # distinct simultaneous hits from the same attacker (e.g. two-weapon
        # fighting) that happen to share a millisecond timestamp but come
        # from different messages. Falls back to (ts, atk_id) if no message
        # id is present.
        for vid in hit_history:
            seen, deduped = set(), []
            for entry in hit_history[vid]:
                ts, atk_id, name, msg_id = entry
                key = (msg_id, atk_id) if msg_id else (ts, atk_id)
                if key not in seen:
                    seen.add(key); deduped.append((ts, atk_id, name))
            hit_history[vid] = sorted(deduped, key=lambda e: e[0])

        # ── Pass 2: find downing signals; attribute via damage history ─────────
        # Two independent signal sources, since not every downing gets a
        # public broadcast card (e.g. it may happen off a roll with no
        # subsequent condition announcement):
        #   A. Dying/Unconscious condition cards — plain HTML, no pf2e flags,
        #      carry the condition name in a <span class="name"> tag.
        #   B. Any pf2e-flagged message's context.options, which snapshot the
        #      roller's active conditions at roll time (e.g. a self-heal
        #      rolled while still Dying/Unconscious). This catches downings
        #      that never produced a condition card at all.
        down_signals = []   # (ts, victim_id)
        for m in raw_msgs:
            pf = (m.get("flags") or {}).get("pf2e") or {}
            ts = m.get("timestamp", 0)

            if not pf:
                content = m.get("content", "")
                # Match against applied condition *names* only, not the full
                # HTML blob — condition tooltips carry full rules text (e.g.
                # Wounded's tooltip mentions "unconscious" in prose), which
                # false-positives a raw content search.
                condition_names = re.findall(r'<span class="name">(.*?)</span>', content)
                if any(_DYING_RE.search(n) for n in condition_names):
                    victim_id = (m.get("speaker") or {}).get("actor", "")
                    if victim_id:
                        down_signals.append((ts, victim_id))
            else:
                ctx  = pf.get("context") or {}
                opts = ctx.get("options") or []
                if any(o.startswith("self:condition:dying") or o.startswith("self:condition:unconscious")
                       for o in opts):
                    victim_id = ctx.get("actor") or (m.get("speaker") or {}).get("actor", "")
                    if victim_id:
                        down_signals.append((ts, victim_id))

        events = []
        for ts, victim_id in down_signals:
            victim_name = name_by_actor.get(victim_id, "")

            # Find most recent hit on this victim BEFORE this timestamp.
            # Sort by (timestamp, insertion index) so ties resolve deterministically.
            history   = hit_history.get(victim_id, [])
            preceding = sorted(
                ((e, i) for i, e in enumerate(history) if e[0] <= ts),
                key=lambda x: (x[0][0], x[1])
            )
            if preceding:
                _, atk_id, atk_name = preceding[-1][0]
            else:
                atk_id, atk_name = "", ""

            events.append({
                "attacker_id":   atk_id,
                "attacker_name": atk_name,
                "victim_id":     victim_id,
                "victim_name":   victim_name,
                "source_label":  "",
                "ts":            ts,
            })

        events.sort(key=lambda e: e["ts"])
        print(f"  Combat record: {len(events)} downing event(s) found "
              f"(from {len(hit_history)} actors with hit history).")
        return events

    @staticmethod
    def _uuid_to_actor_id(uuid: str) -> str:
        """
        Extract the actor id from a Foundry document UUID.
        "Actor.abc"                              → "abc"
        "Scene.x.Token.y.Actor.abc"              → "abc"
        Returns "" if no Actor segment present.
        """
        if not uuid or "Actor." not in uuid:
            return ""
        # Take the segment immediately after the last "Actor."
        tail = uuid.rsplit("Actor.", 1)[1]
        return tail.split(".")[0] if tail else ""

    @staticmethod
    def _uuid_to_token_id(uuid: str) -> str:
        """
        Extract the token id from a Foundry document UUID.
        "Scene.x.Token.y.Actor.abc" → "y"
        Returns "" if no Token segment present.
        """
        if not uuid or "Token." not in uuid:
            return ""
        tail = uuid.split("Token.", 1)[1]
        return tail.split(".")[0] if tail else ""

    def _actor_maps(self) -> tuple:
        """Build actor id → name and actor id → type maps from the actors DB."""
        name_by_actor: dict[str, str] = {}
        type_by_actor: dict[str, str] = {}
        with self.actors_db.iterator() as it:
            for key, raw in it:
                if not key.startswith(b"!actors!"):
                    continue
                try:
                    a = json.loads(raw)
                except Exception:
                    continue
                if isinstance(a, dict) and a.get("_id"):
                    name_by_actor[a["_id"]] = a.get("name", "Unknown")
                    type_by_actor[a["_id"]] = a.get("type", "")
        return name_by_actor, type_by_actor

    def _load_raw_messages(self) -> list:
        """
        Copy the messages LevelDB to a temp dir and load all documents,
        chronologically. Cached after first call — combat_record() and
        SessionExporter's combat-stats both need the full message log, and
        without caching each would independently re-copy and re-parse the
        entire LevelDB from scratch.
        """
        if self._raw_msgs_cache is not None:
            return self._raw_msgs_cache

        messages_path = self.world_path / "data" / "messages"
        if not messages_path.exists():
            return []

        tmp        = tempfile.mkdtemp()
        tmp_path   = Path(tmp) / "messages"
        raw_msgs   = []
        msg_errors = 0
        try:
            shutil.copytree(str(messages_path), str(tmp_path))
            db = plyvel.DB(str(tmp_path))
            with db:
                for k, v in db:
                    parts = k.decode("utf-8", "replace").split("!")
                    if len(parts) not in (2, 3):
                        continue
                    try:
                        m = json.loads(v)
                        if isinstance(m, dict):
                            raw_msgs.append(m)
                    except Exception:
                        msg_errors += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if msg_errors:
            print(f"  ⚠  {msg_errors} chat messages failed to parse and were skipped")

        raw_msgs.sort(key=lambda m: m.get("timestamp", 0))
        self._raw_msgs_cache = raw_msgs
        return raw_msgs

    def _build_npc_kill_events(self) -> list:
        """
        Build a list of NPC-kill events using the per-token ActorDelta HP
        override in the scenes DB as ground truth for "is this NPC dead".

        Chat messages alone can't detect most NPC deaths: NPCs don't get a
        Dying/Unconscious condition card (that's a PC-only mechanic), and the
        killing blow is often applied by dragging the token's HP bar to 0
        directly rather than via the chat "Apply Damage" button — which
        leaves no trace in the message log. But every unlinked token has its
        own ActorDelta document overriding HP for that specific instance
        (independent of the shared base Actor, which GMs frequently reuse/
        reset as a stat-block template across encounters), so a token's
        current delta HP <= 0 is a reliable "this instance died" signal.

        Attribution: the last hit recorded against that specific token id
        anywhere in the chat log (via context.target.token or
        appliedDamage.uuid), since token ids are unique per encounter
        instance — unlike actor ids, which multiple simultaneous copies of
        the same monster type (e.g. 5 Cave Scorpion tokens) can share.
        """
        scenes_path = self.world_path / "data" / "scenes"
        if not scenes_path.exists():
            return []

        name_by_actor, type_by_actor = self._actor_maps()

        tmp      = tempfile.mkdtemp()
        tmp_path = Path(tmp) / "scenes"
        base_tokens: dict[str, dict] = {}   # token_id → {name, actorId}
        deltas:      dict[str, dict] = {}   # token_id → {hp, type}
        try:
            shutil.copytree(str(scenes_path), str(tmp_path))
            db = plyvel.DB(str(tmp_path))
            with db:
                for k, v in db:
                    ks = k.decode("utf-8", "replace")
                    try:
                        if ks.startswith("!scenes.tokens!"):
                            token_id = ks.split("!")[-1].split(".")[-1]
                            doc = json.loads(v)
                            if isinstance(doc, dict):
                                base_tokens[token_id] = {
                                    "name":    doc.get("name"),
                                    "actorId": doc.get("actorId"),
                                }
                        elif ks.startswith("!scenes.tokens.delta!"):
                            token_id = ks.split("!")[-1].split(".")[1]
                            doc = json.loads(v)
                            if isinstance(doc, dict):
                                hp = ((doc.get("system") or {}).get("attributes") or {}).get("hp")
                                deltas[token_id] = {
                                    "hp":   hp.get("value") if isinstance(hp, dict) else None,
                                    "type": doc.get("type"),
                                }
                    except Exception:
                        continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        dead_npc_tokens = []
        for tid, base in base_tokens.items():
            actor_id = base.get("actorId")
            delta    = deltas.get(tid, {})
            dtype    = delta.get("type") or type_by_actor.get(actor_id, "")
            if dtype != "npc":
                continue
            hp = delta.get("hp")
            if hp is not None and hp <= 0:
                dead_npc_tokens.append((tid, base))

        if not dead_npc_tokens:
            return []

        raw_msgs = self._load_raw_messages()

        events = []
        for tid, base in dead_npc_tokens:
            actor_id     = base.get("actorId")
            display_name = base.get("name") or name_by_actor.get(actor_id, "Unknown")

            best = None
            for m in raw_msgs:
                pf      = (m.get("flags") or {}).get("pf2e") or {}
                ctx     = pf.get("context") or {}
                target  = ctx.get("target") or pf.get("target") or {}
                applied = pf.get("appliedDamage") or {}
                origin  = pf.get("origin") or {}
                atk_id  = self._uuid_to_actor_id(origin.get("actor", "") if isinstance(origin, dict) else "")
                if not atk_id or atk_id == actor_id:
                    continue

                ts = m.get("timestamp", 0)
                if (isinstance(target, dict) and ctx.get("type") in ("attack-roll", "damage-roll")
                        and self._uuid_to_token_id(target.get("token", "")) == tid):
                    if best is None or ts > best[0]:
                        best = (ts, atk_id)
                elif (isinstance(applied, dict) and not applied.get("isHealing")
                      and self._uuid_to_token_id(applied.get("uuid", "")) == tid):
                    if best is None or ts > best[0]:
                        best = (ts, atk_id)

            if best is None:
                continue
            ts, atk_id = best
            events.append({
                "attacker_id":   atk_id,
                "attacker_name": name_by_actor.get(atk_id, ""),
                "victim_id":     tid,
                "victim_name":   display_name,
                "source_label":  "",
                "ts":            ts,
            })

        print(f"  Combat record: {len(events)} NPC kill(s) found "
              f"(from {len(dead_npc_tokens)} dead NPC token(s)).")
        return sorted(events, key=lambda e: e["ts"])

    # ── Campaign-wide cumulative stats ──────────────────────────────────────

    # A single downing produces multiple raw signals: the condition card AND
    # every subsequent roll made while still Dying/Unconscious (recovery
    # checks, received-healing rolls) all carry the condition in
    # context.options. Signals for the same victim within this gap are
    # collapsed into one "episode" so 'times downed' counts downings, not
    # dying-state messages.
    DOWNING_EPISODE_GAP_MS = 10 * 60 * 1000

    def campaign_stats(self) -> dict:
        """
        Cumulative per-PC combat statistics across the entire message log
        (no session window). Returns:

            {
              "per_pc": { actor_id: {
                  "name", "kills", "downed", "dealt", "taken", "healed",
                  "nemesis":  most frequent downer name or "",
                  "fav_prey": most frequently slain victim name or "",
              }, ... },                      # only PCs with any activity
              "kill_log":    [kill events, chronological],   # PC attackers only
              "downing_log": [downing episodes, chronological],  # PC victims only
            }

        Sources: _build_npc_kill_events (kills), _build_downing_events
        collapsed into episodes (times downed), and appliedDamage chat flags
        (damage dealt/taken and healing given). Healing given only counts
        button-applied healing with an origin actor, so it can undercount;
        damage numbers share the known appliedDamage limitation that
        manual HP-bar edits leave no message to sum.
        """
        if getattr(self, "_campaign_stats_cache", None) is not None:
            return self._campaign_stats_cache

        name_by_actor, type_by_actor = self._actor_maps()
        pc_ids = {aid for aid, t in type_by_actor.items() if t == "character"}

        kill_log = [ev for ev in self._build_npc_kill_events()
                    if ev["attacker_id"] in pc_ids]

        # Collapse raw downing signals into per-victim episodes.
        events_by_victim: dict[str, list] = {}
        for ev in self._build_downing_events():
            if ev["victim_id"] in pc_ids:
                events_by_victim.setdefault(ev["victim_id"], []).append(ev)
        downing_log = []
        for evs in events_by_victim.values():
            evs.sort(key=lambda e: e["ts"])
            prev_ts = None
            for ev in evs:
                if prev_ts is None or ev["ts"] - prev_ts > self.DOWNING_EPISODE_GAP_MS:
                    downing_log.append(ev)
                prev_ts = ev["ts"]
        downing_log.sort(key=lambda e: e["ts"])

        dealt: dict[str, int] = {}
        taken: dict[str, int] = {}
        healed: dict[str, int] = {}
        for m in self._load_raw_messages():
            pf      = (m.get("flags") or {}).get("pf2e") or {}
            applied = pf.get("appliedDamage")
            if not isinstance(applied, dict):
                continue
            amount = sum(abs(_int(u.get("value", 0))) for u in (applied.get("updates") or [])
                         if u.get("path") == "system.attributes.hp.value")
            if amount <= 0:
                continue
            victim_id = (self._uuid_to_actor_id(applied.get("uuid", ""))
                         or (m.get("speaker") or {}).get("actor", ""))
            origin = pf.get("origin") or {}
            atk_id = self._uuid_to_actor_id(origin.get("actor", "") if isinstance(origin, dict) else "")
            if applied.get("isHealing"):
                # Self-healing excluded: "Healing Given" measures support play.
                if atk_id in pc_ids and atk_id != victim_id:
                    healed[atk_id] = healed.get(atk_id, 0) + amount
            else:
                if victim_id in pc_ids:
                    taken[victim_id] = taken.get(victim_id, 0) + amount
                if atk_id in pc_ids and atk_id != victim_id:
                    dealt[atk_id] = dealt.get(atk_id, 0) + amount

        def _most_common(names: list) -> str:
            counts: dict[str, int] = {}
            for n in names:
                if n:
                    counts[n] = counts.get(n, 0) + 1
            return max(counts, key=lambda n: (counts[n], n)) if counts else ""

        per_pc = {}
        for aid in pc_ids:
            my_kills = [e for e in kill_log    if e["attacker_id"] == aid]
            my_downs = [e for e in downing_log if e["victim_id"]   == aid]
            rec = {
                "name":     name_by_actor.get(aid, "Unknown"),
                "kills":    len(my_kills),
                "downed":   len(my_downs),
                "dealt":    dealt.get(aid, 0),
                "taken":    taken.get(aid, 0),
                "healed":   healed.get(aid, 0),
                "nemesis":  _most_common([e["attacker_name"] for e in my_downs]),
                "fav_prey": _most_common([e["victim_name"]   for e in my_kills]),
            }
            # Leave out PCs with zero campaign activity (test actors, retired
            # sheets) — an all-zero leaderboard row is noise, not data.
            if any(rec[k] for k in ("kills", "downed", "dealt", "taken", "healed")):
                per_pc[aid] = rec

        self._campaign_stats_cache = {
            "per_pc":      per_pc,
            "kill_log":    kill_log,
            "downing_log": downing_log,
        }
        return self._campaign_stats_cache

    # ── LevelDB helpers ───────────────────────────────────────────────────

    def _get_actor_items(self, actor_id: str) -> list:
        prefix = f"!actors.items!{actor_id}.".encode()
        items  = []
        with self.actors_db.iterator() as it:
            for key, value in it:
                if key.startswith(prefix):
                    try:
                        item = json.loads(value)
                        if isinstance(item, dict):
                            items.append(item)
                    except Exception as e:
                        print(f"  ⚠  Failed to parse item {key!r}: {e}")
        return items

    def get_all_characters(self) -> list:
        chars = []
        with self.actors_db.iterator() as it:
            for _, value in it:
                try:
                    actor = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(actor, dict) and actor.get("type") == "character":
                    chars.append(self._parse_character(actor))
        return chars

    def get_all_npcs(self) -> list:
        npcs = []
        with self.actors_db.iterator() as it:
            for _, value in it:
                try:
                    actor = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(actor, dict) and actor.get("type") == "npc":
                    npcs.append(self._parse_npc(actor))
        return npcs

    def get_party_actors(self) -> list:
        """
        PF2e 'party' actors — the shared party stash/inventory. Returns
        lightweight dicts shaped like get_all_characters() output
        ({id, name, items}) so callers can treat them the same way for
        inventory-diffing purposes.
        """
        parties = []
        with self.actors_db.iterator() as it:
            for _, value in it:
                try:
                    actor = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(actor, dict) and actor.get("type") == "party":
                    actor_id = actor.get("_id")
                    raw_items = self._get_actor_items(actor_id)
                    items = [enrich_item(i, self.compendium) for i in raw_items]
                    parties.append({"id": actor_id, "name": actor.get("name", "Party Stash"), "items": items})
        return parties

    def debug_dump(self, name: str):
        """Dump raw system fields for a named actor, including full item data."""
        with self.actors_db.iterator() as it:
            for _, value in it:
                try:
                    actor = json.loads(value)
                except Exception:
                    continue
                if not isinstance(actor, dict):
                    continue
                if actor.get("type") not in ("character", "npc"):
                    continue
                if actor.get("name", "").lower() != name.lower():
                    continue

                system = actor.get("system", {})
                keys_to_dump = [
                    "abilities", "saves", "skills",
                    ("attributes", "ac"),
                    ("attributes", "perception"),
                    ("attributes", "hp"),
                    ("attributes", "speed"),
                    "proficiencies",
                    "currency",
                    ("build", "attributes"),
                ]
                print(f"\n{'='*60}")
                print(f"DEBUG DUMP: {actor.get('name')} (type={actor.get('type')})")
                print('='*60)
                for key in keys_to_dump:
                    if isinstance(key, tuple):
                        val = system
                        label = ".".join(key)
                        for k in key:
                            val = val.get(k, {}) if isinstance(val, dict) else {}
                    else:
                        val = system.get(key, {})
                        label = key
                    print(f"\nsystem.{label}:")
                    print(json.dumps(val, indent=2, default=str)[:2000])

                items = self._get_actor_items(actor.get("_id", ""))
                print(f"\nTotal items: {len(items)}")

                for itype in ("class", "ancestry", "background", "heritage"):
                    found = [i for i in items if i.get("type") == itype]
                    for item in found:
                        isys = item.get("system") or {}
                        print(f"\n── {itype.upper()} item: {item.get('name','?')} ──")
                        for field in ("boosts","flaws","keyAbility","savingThrows",
                                      "defenses","perception","trainedSkills",
                                      "trainedLore","hp","rules"):
                            fval = isys.get(field)
                            if fval is not None:
                                dumped = json.dumps(fval, indent=2, default=str)
                                if field == "rules" and len(dumped) > 1500:
                                    dumped = dumped[:1500] + "\n  … (truncated)"
                                print(f"  .{field}: {dumped}")

                treasure_items = [i for i in items if i.get("type") == "treasure"]
                if treasure_items:
                    print(f"\n── TREASURE items ({len(treasure_items)}) ──")
                    for item in treasure_items[:12]:
                        isys = item.get("system") or {}
                        print(f"  {item.get('name','?')!r}")
                        for field in ("quantity", "denomination", "stackGroup",
                                      "price", "value", "weight", "bulk"):
                            fval = isys.get(field)
                            if fval is not None:
                                print(f"    .{field}: {json.dumps(fval, default=str)}")
                    if len(treasure_items) > 12:
                        print(f"  … and {len(treasure_items)-12} more")
                return

        print(f"✗  Actor '{name}' not found.")

    # ── Ability score extraction ───────────────────────────────────────────

    def _get_ability_mod(self, system: dict, slug: str,
                          items: list = None) -> int:
        """
        Extract ability modifier. Priority:
          1. system.abilities.<slug>.mod   — pre-computed modifier
          2. system.abilities.<slug>.value — score ≥ 18 (treat as score)
          3. system.abilities.<slug>        — plain int (legacy)
          4. Rebuild from boosts across build.attributes + ancestry/class items
        """
        abilities = system.get("abilities")
        # abilities can be None (null in JSON) — treat as absent
        if isinstance(abilities, dict):
            node = abilities.get(slug)
            if isinstance(node, dict):
                if node.get("mod") is not None:
                    return _int(node["mod"])
                # Only treat value as a score if it looks like one (>= 3 means
                # a score of 3 which gives mod -3; actual scores start at 1+)
                # Use heuristic: if value > 6 it's almost certainly a score not a mod
                if node.get("value") is not None:
                    v = _int(node["value"])
                    if v > 6:
                        return (v - 10) // 2
                    # Small value — ambiguous; fall through to boost rebuild
            elif isinstance(node, (int, float)):
                return _int(node)

        return self._ability_from_boosts(system, slug, items)

    def _ability_from_boosts(self, system: dict, slug: str,
                              items: list = None) -> int:
        """
        Reconstruct ability modifier from PF2e Remaster build data.

        system.build.attributes.boosts only contains the FREE PICK tiers
        (level 1, 5, 10, 15, 20). Ancestry, background, and class boosts
        live on the respective ITEMS — we must read both sources.
        """
        score = 10

        # ── Source 1: free-pick tiers + flaws in build.attributes ─────────
        build = system.get("build") or {}
        attrs = build.get("attributes") if isinstance(build, dict) else None
        if isinstance(attrs, dict):
            for flaw_list in (attrs.get("flaws") or {}).values():
                if isinstance(flaw_list, list) and slug in flaw_list:
                    score -= 2

            for boost_list in (attrs.get("boosts") or {}).values():
                if isinstance(boost_list, list) and slug in boost_list:
                    score = score + 2 if score < 18 else score + 1

            # Apex item boost
            apex = attrs.get("apex")
            if isinstance(apex, str) and apex == slug:
                score = score + 2 if score < 18 else score + 1

        # ── Source 2: ancestry / background / class / heritage items ──────
        # ancestry/background: system.boosts[], system.flaws[]
        # class:               system.keyAbility.value (str or list)
        # heritage:            system.boost (str) or system.boosts (list)
        for item in (items or []):
            itype = item.get("type", "")
            isys  = item.get("system") or {}

            if itype in ("ancestry", "background"):
                boosts_raw = isys.get("boosts") or []
                boost_iter = boosts_raw.values() if isinstance(boosts_raw, dict) else boosts_raw
                for slot in boost_iter:
                    chosen = _read_boost_slot(slot)
                    if chosen == slug or (isinstance(chosen, list) and slug in chosen):
                        score = score + 2 if score < 18 else score + 1
                flaws_raw = isys.get("flaws") or []
                flaw_iter = flaws_raw.values() if isinstance(flaws_raw, dict) else flaws_raw
                for slot in flaw_iter:
                    chosen = _read_boost_slot(slot)
                    if chosen == slug or (isinstance(chosen, list) and slug in chosen):
                        score -= 2

            elif itype == "class":
                ka = isys.get("keyAbility") or {}
                if isinstance(ka, dict):
                    ka_val      = ka.get("value", [])
                    ka_selected = ka.get("selected")
                elif isinstance(ka, str):
                    ka_val, ka_selected = [ka], None
                else:
                    ka_val, ka_selected = (ka or []), None
                if isinstance(ka_val, str):
                    ka_val = [ka_val]

                # Foundry writes a "selected" key-ability field on both the
                # class item and system.details.keyability, but for a
                # single-option class (no real UI choice was ever offered)
                # it's left at an unreliable schema default rather than
                # cleared or unset — observed as a uniform "str" across
                # nearly every actor in the fixture regardless of class.
                # Trust an explicit selection only when it's genuinely one
                # of the class's real options; a single-option class's one
                # option is always correct regardless of what "selected"
                # (or the details.keyability default) says.
                if len(ka_val) == 1:
                    chosen = ka_val[0]
                elif not ka_val:
                    chosen = ka_selected
                elif ka_selected in ka_val:
                    chosen = ka_selected
                else:
                    detail_key = self._get_detail_field(system.get("details"), "keyability")
                    chosen = detail_key if detail_key in ka_val else None

                if chosen == slug:
                    score = score + 2 if score < 18 else score + 1

            elif itype == "heritage":
                b = isys.get("boost") or isys.get("boosts")
                if isinstance(b, str) and b == slug:
                    score = score + 2 if score < 18 else score + 1
                elif isinstance(b, list) and slug in b:
                    score = score + 2 if score < 18 else score + 1

        return (score - 10) // 2

    # ── Item helpers ──────────────────────────────────────────────────────

    _COIN_SLUGS   = {"gold-pieces", "silver-pieces", "copper-pieces", "platinum-pieces"}
    _COIN_NAME_RE = re.compile(r'^(platinum|gold|silver|copper)\s+pieces$', re.IGNORECASE)

    @staticmethod
    def _is_coin_item(item: dict) -> bool:
        """
        True when a treasure item represents actual currency, not just any
        treasure priced at exactly 1 coin of a single denomination (a 1 gp
        gem would otherwise false-positive on that price pattern alone).
        """
        if item.get("type") != "treasure":
            return False
        isys = item.get("system") or {}
        slug = isys.get("slug") or ""
        if isys.get("stackGroup") == "coins" or slug in FullExporter._COIN_SLUGS:
            return True
        # No reliable slug/stackGroup (e.g. a homebrew coin item) — fall back
        # to the price-pattern heuristic, but only for something that also
        # looks like currency by name, so real 1-coin-priced treasure isn't
        # swept in.
        if not FullExporter._COIN_NAME_RE.match((item.get("name") or "").strip()):
            return False
        price = isys.get("price") or {}
        if not isinstance(price, dict):
            return False
        if _int(price.get("per", 1)) != 1:
            return False
        pval = price.get("value") or {}
        if not isinstance(pval, dict):
            return False
        coin_denoms = {k for k in ("pp", "gp", "sp", "cp") if pval.get(k)}
        return len(coin_denoms) == 1 and all(_int(pval[d]) == 1 for d in coin_denoms)

    # ── Proficiency rank helpers ──────────────────────────────────────────

    @staticmethod
    def _rank_from_node(node) -> int:
        """
        Extract a proficiency rank (0-4) from a node.

        IMPORTANT: only an explicit 'rank' key is a rank. A bare 'value' on a
        skill/save node is the TOTAL MODIFIER, not a rank, so we must not fall
        back to it — doing so misreads e.g. {"value": 8} as rank 8.
        A plain int node IS treated as a rank (class items store ranks this way).
        """
        if isinstance(node, dict):
            r = node.get("rank", 0)
            if isinstance(r, dict):
                r = r.get("value", 0)
            rank = _int(r)
        elif isinstance(node, (int, float)):
            rank = _int(node)
        else:
            rank = 0
        # Clamp to the valid PF2e proficiency range
        return max(0, min(4, rank))

    def _rules_ranks(self, items: list, level: int) -> dict:
        """
        Collect all proficiency rank grants from every item on the actor.

        Two sources per item:
          1. ActiveEffectLike rules  — heritage, class-feature, feat, etc.
          2. system.trainedSkills.value — flat list of skill slugs trained by
             backgrounds and classes that don't use rules for skill grants.

        Returns dict mapping short keys → effective rank int, e.g.:
          "saves.fortitude" → 2, "skills.athletics" → 1, "perception" → 1
        """
        _SKILL_SLUGS = (
            "acrobatics","arcana","athletics","crafting","deception",
            "diplomacy","intimidation","medicine","nature","occultism",
            "performance","religion","society","stealth","survival","thievery",
        )
        PATH_MAP = {
            "system.saves.fortitude.rank":              "saves.fortitude",
            "system.saves.reflex.rank":                 "saves.reflex",
            "system.saves.will.rank":                   "saves.will",
            "system.attributes.perception.rank":        "perception",
            "system.proficiencies.defenses.unarmored.rank": "defenses.unarmored",
            "system.proficiencies.defenses.light.rank":     "defenses.light",
            "system.proficiencies.defenses.medium.rank":    "defenses.medium",
            "system.proficiencies.defenses.heavy.rank":     "defenses.heavy",
            **{f"system.skills.{s}.rank": f"skills.{s}" for s in _SKILL_SLUGS},
        }
        ranks: dict[str, int] = {}

        # Scan every item — backgrounds and ancestries need to be included
        # because they hold skill training data that class-features do not.
        RULE_TYPES = ("class", "class-feature", "ancestry-feature", "ancestry",
                      "background", "feat", "heritage")

        for item in items:
            itype = item.get("type", "")
            isys  = item.get("system") or {}

            # ── Source A: ActiveEffectLike rules ──────────────────────────
            if itype in RULE_TYPES:
                item_rules = isys.get("rules") or []
                for rule in item_rules:
                    if not isinstance(rule, dict):
                        continue
                    if rule.get("key") != "ActiveEffectLike":
                        continue
                    mode = rule.get("mode", "")
                    if mode not in ("override", "upgrade"):
                        continue
                    path = self._resolve_rule_path(rule.get("path", ""), item, item_rules)
                    if path not in PATH_MAP:
                        continue
                    value = self._eval_rule_value(rule.get("value", 0), level)
                    if value <= 0:
                        continue
                    if not self._rule_applies(rule, level):
                        continue
                    key = PATH_MAP[path]
                    if mode == "override":
                        ranks[key] = value
                    else:
                        ranks[key] = max(ranks.get(key, 0), value)

            # ── Source B: system.trainedSkills.value ──────────────────────
            # Backgrounds and classes often list trained skills as a flat
            # array rather than individual rules.
            if itype in ("class", "background", "ancestry", "heritage",
                         "class-feature", "ancestry-feature"):
                trained = isys.get("trainedSkills") or {}
                skill_list = (trained.get("value", [])
                              if isinstance(trained, dict) else
                              (trained if isinstance(trained, list) else []))
                for s in skill_list:
                    if isinstance(s, str) and s in _SKILL_SLUGS:
                        ranks[f"skills.{s}"] = max(ranks.get(f"skills.{s}", 0), 1)

        return ranks

    _CHOICESET_TEMPLATE_RE = re.compile(r"\{item\|flags\.pf2e\.rulesSelections\.(\w+)\}")

    @classmethod
    def _resolve_rule_path(cls, path: str, item: dict, item_rules: list) -> str:
        """
        Resolve a templated ActiveEffectLike path like
        'system.skills.{item|flags.pf2e.rulesSelections.skill}.rank' against a
        player's choice. Foundry normally records the choice at
        flags.pf2e.rulesSelections.<key>; when that's absent (observed on at
        least one real character), fall back to the sibling ChoiceSet rule's
        own 'selection' field, matched by its 'flag' key.
        """
        m = cls._CHOICESET_TEMPLATE_RE.search(path)
        if not m:
            return path
        flag_key = m.group(1)
        selections = (((item.get("flags") or {}).get("pf2e") or {}).get("rulesSelections") or {})
        choice = selections.get(flag_key)
        if choice is None:
            for rule in item_rules:
                if (isinstance(rule, dict) and rule.get("key") == "ChoiceSet"
                        and rule.get("flag") == flag_key):
                    choice = rule.get("selection")
                    break
        if not isinstance(choice, str) or not choice:
            return path
        return cls._CHOICESET_TEMPLATE_RE.sub(choice, path)

    _TERNARY_GTE_RE = re.compile(
        r"^ternary\(gte\(@actor\.level,\s*(\d+)\),\s*(\d+),\s*(\d+)\)$"
    )

    @classmethod
    def _eval_rule_value(cls, value, level: int) -> int:
        """
        Evaluate an ActiveEffectLike rule value, which is usually a plain int
        but can be a Foundry roll-data expression string, e.g.
        'ternary(gte(@actor.level,5),2,1)' (Skilled Human: Trained until
        level 5, Expert after). Unrecognized expressions fall back to 0.
        """
        if isinstance(value, str):
            m = cls._TERNARY_GTE_RE.match(value.strip())
            if m:
                threshold, if_true, if_false = (int(m.group(i)) for i in (1, 2, 3))
                return if_true if level >= threshold else if_false
        return _int(value, 0)

    @staticmethod
    def _rule_applies(rule: dict, level: int) -> bool:
        """
        Evaluate whether a rule's level predicate passes for the character's level.
        Handles the most common PF2e predicate formats; unknown formats → True.
        """
        # Some rules have a direct top-level "level" field
        rule_level = rule.get("level")
        if rule_level is not None:
            try:
                return level >= int(rule_level)
            except (TypeError, ValueError):
                pass

        predicate = rule.get("predicate")
        if predicate is None:
            return True  # no predicate → always applies

        if isinstance(predicate, str):
            m = re.match(r"self:level:(\d+)", predicate)
            return level >= int(m.group(1)) if m else True

        if isinstance(predicate, list):
            for cond in predicate:
                if isinstance(cond, str):
                    m = re.match(r"self:level:(\d+)", cond)
                    if m and level < int(m.group(1)):
                        return False
                elif isinstance(cond, dict):
                    for op, operands in cond.items():
                        if not isinstance(operands, list) or len(operands) != 2:
                            continue
                        lhs, rhs = operands
                        if lhs != "self:level":
                            continue
                        try:
                            rhs_int = int(rhs)
                        except (TypeError, ValueError):
                            continue
                        if op == "gte" and level < rhs_int:
                            return False
                        elif op == "lte" and level > rhs_int:
                            return False
                        elif op == "eq" and level != rhs_int:
                            return False
            return True

        return True  # unknown predicate format → assume applies

    @staticmethod
    def _predicate_facts_true(predicate, facts: set) -> bool:
        """
        Evaluate an "item:equipped" / "armor:base:X" / "armor:category:X"
        style predicate (as used by ItemAlteration rules) against a set of
        known-true fact strings. Handles string atoms, {"or"/"and"/"not": ...}
        combinators, and top-level lists (implicit AND). Unrecognized dict
        shapes (e.g. numeric gte/lte comparisons unrelated to these facts)
        conservatively evaluate to False rather than risk a false-positive
        stat bonus.
        """
        if predicate is None:
            return True
        if isinstance(predicate, str):
            return predicate in facts
        if isinstance(predicate, dict):
            if "or" in predicate:
                return any(FullExporter._predicate_facts_true(p, facts) for p in predicate["or"])
            if "and" in predicate:
                return all(FullExporter._predicate_facts_true(p, facts) for p in predicate["and"])
            if "not" in predicate:
                return not FullExporter._predicate_facts_true(predicate["not"], facts)
            return False
        if isinstance(predicate, list):
            return all(FullExporter._predicate_facts_true(p, facts) for p in predicate)
        return False

    def _armor_alteration(self, equipped_armor: dict, items: list, prop: str) -> int:
        """
        Net add/subtract delta that other equipped items' ItemAlteration
        rules (e.g. the Armored Skirt's "+1 ac-bonus while wearing chain
        mail/breastplate/etc") apply to the worn armor's `prop`. These rules
        live on the *other* item, not the armor's own data, so `_calc_ac`
        can't see them just by reading the armor item. "override" mode is
        rare for these properties and not handled here.
        """
        s        = equipped_armor.get("system") or {}
        base_raw = s.get("baseItem")
        base     = base_raw.get("value") if isinstance(base_raw, dict) else base_raw
        cat_raw  = s.get("category", "unarmored")
        cat      = cat_raw.get("value", "unarmored") if isinstance(cat_raw, dict) else (cat_raw or "unarmored")

        facts = {"item:equipped"}
        if base:
            facts.add(f"armor:base:{base}")
        if cat:
            facts.add(f"armor:category:{cat}")

        delta = 0
        for item in items:
            if item is equipped_armor or not self._is_equipped(item):
                continue
            for rule in (item.get("system") or {}).get("rules") or []:
                if not isinstance(rule, dict) or rule.get("key") != "ItemAlteration":
                    continue
                if rule.get("itemType") != "armor" or rule.get("property") != prop:
                    continue
                if not self._predicate_facts_true(rule.get("predicate"), facts):
                    continue
                value = _int(rule.get("value", 0))
                mode  = rule.get("mode")
                if mode == "add":
                    delta += value
                elif mode == "subtract":
                    delta -= value
        return delta

    def _save_rank(self, system: dict, items: list, slug: str,
                   rules_ranks: dict = None) -> int:
        # 1. Pre-computed rank stored on the actor (present in some Foundry versions)
        saves = system.get("saves") or {}
        node  = saves.get(slug, {})
        rank  = self._rank_from_node(node)

        # 2. Rules-based rank (ActiveEffectLike rules on class/class-feature items)
        #    Merge with stored rank — level-gated upgrades (e.g. Expert Fortitude at
        #    level 7) may be stored as rules while the actor node still shows the
        #    initial Trained rank.
        rr = rules_ranks or {}
        rr_key = f"saves.{slug}"
        rank = max(rank, rr.get(rr_key, 0))
        if rank:
            return rank

        # 3. Static fields on the class item (initial rank before upgrades)
        for item in items:
            if item.get("type") == "class":
                s = item.get("system") or {}
                if not isinstance(s, dict):
                    continue
                for path in (
                    (s.get("savingThrows") or {}).get(slug),
                    s.get(slug),
                ):
                    if path is not None:
                        r = self._rank_from_node(path)
                        if r:
                            return r
        return 0

    def _perception_rank(self, system: dict, items: list = None,
                          rules_ranks: dict = None) -> int:
        # 1. Pre-computed rank on the actor (two possible storage paths)
        perc = system.get("perception") or {}
        rank = self._rank_from_node(perc)
        perc2 = (system.get("attributes") or {}).get("perception") or {}
        rank = max(rank, self._rank_from_node(perc2))

        # 2. Merge with rules-based rank so level-gated upgrades are not silenced
        #    by a stored base rank.
        rr = rules_ranks or {}
        rank = max(rank, rr.get("perception", 0))
        if rank:
            return rank
        # 3. Static field on the class item: system.perception (int or {value:N})
        for item in (items or []):
            if item.get("type") == "class":
                s = item.get("system") or {}
                if not isinstance(s, dict):
                    continue
                for path in (s.get("perception"), (s.get("proficiencies") or {}).get("perception")):
                    if path is not None:
                        r = self._rank_from_node(path)
                        if r:
                            return r
        return 1  # all classes get at least Trained perception — safe default

    def _armor_prof_rank(self, system: dict, items: list, category: str,
                          rules_ranks: dict = None) -> int:
        # 1. Pre-computed on actor
        defenses = (system.get("proficiencies") or {}).get("defenses") or {}
        if isinstance(defenses, dict) and category in defenses:
            r = self._rank_from_node(defenses[category])
            if r:
                return r
        # 2. Rules-based rank
        rr = rules_ranks or {}
        if rr.get(f"defenses.{category}", 0):
            return rr[f"defenses.{category}"]

        for item in items:
            if item.get("type") == "class":
                s = item.get("system") or {}
                if not isinstance(s, dict):
                    continue
                # PF2e stores armor proficiency under several paths across versions:
                #   system.defenses.<cat>              (v5+)
                #   system.proficiencies.defenses.<cat> (some intermediate versions)
                #   system.<cat>                        (legacy)
                for path in (
                    (s.get("defenses") or {}).get(category),
                    (s.get("proficiencies") or {}).get("defenses", {}).get(category),
                    s.get(category),
                ):
                    if path is not None:
                        r = self._rank_from_node(path)
                        if r:
                            return r
                # Name-based fallback: old Foundry data may lack structured armor fields.
                # Only reached when all structured lookups fail.
                cname = item.get("name", "").lower()
                if any(c in cname for c in ("fighter", "champion", "ranger", "barbarian",
                                            "investigator", "swashbuckler", "magus",
                                            "gunslinger", "inventor")):
                    if category in ("unarmored", "light", "medium", "heavy"):
                        return 1
                # All classes get unarmored trained
                if category == "unarmored":
                    return 1
        return 1 if category == "unarmored" else 0

    # ── Math engine ───────────────────────────────────────────────────────

    @staticmethod
    def _is_equipped(item: dict) -> bool:
        eq = item.get("system", {}).get("equipped", {})
        if isinstance(eq, dict):
            return (eq.get("inSlot") is True
                    or eq.get("value") is True
                    or eq.get("carryType") == "worn")
        return bool(eq)

    def _calc_ac(self, system: dict, items: list, level: int, dex_mod: int,
                  rules_ranks: dict = None) -> int:
        precomp = (system.get("attributes") or {}).get("ac") or {}
        if isinstance(precomp, dict) and precomp.get("value") is not None:
            return _int(precomp["value"])

        equipped = next((i for i in items
                         if i.get("type") == "armor" and self._is_equipped(i)), None)
        if equipped:
            s       = equipped.get("system") or {}
            ac_val  = _int(s.get("acBonus", 0)) + self._armor_alteration(equipped, items, "ac-bonus")
            cat_raw = s.get("category", "unarmored")
            cat     = cat_raw.get("value", "unarmored") if isinstance(cat_raw, dict) else (cat_raw or "unarmored")
            potency = _int((s.get("potencyRune") or {}).get("value", 0)
                           if isinstance(s.get("potencyRune"), dict) else s.get("potencyRune", 0))
            dex_cap_raw = s.get("dexCap")
            dex_cap = _int(dex_cap_raw.get("value", 99)
                           if isinstance(dex_cap_raw, dict) else (dex_cap_raw if dex_cap_raw is not None else 99))
            dex_cap += self._armor_alteration(equipped, items, "dex-cap")
        else:
            ac_val, cat, potency, dex_cap = 0, "unarmored", 0, 99

        rank        = self._armor_prof_rank(system, items, cat, rules_ranks)
        applied_dex = min(dex_mod, dex_cap)
        prof_bonus  = (level + rank * 2) if rank > 0 else 0
        return 10 + ac_val + potency + prof_bonus + applied_dex

    def _calc_save(self, system: dict, items: list, slug: str,
                   ability_mod: int, level: int,
                   rules_ranks: dict = None) -> tuple[int, int]:
        rank = self._save_rank(system, items, slug, rules_ranks)

        saves = system.get("saves") or {}
        node  = saves.get(slug) or {}
        # If the rules-derived rank exceeds what the stored node itself implies
        # (e.g. a level-gated "Expert Fortitude at level 3" upgrade), the
        # stored 'value' predates that upgrade — discard it and recompute.
        if (isinstance(node, dict) and node.get("value") is not None
                and rank <= self._rank_from_node(node)):
            return _int(node["value"]), rank

        prof_bonus = (level + rank * 2) if rank > 0 else 0
        resilient  = 0
        for item in items:
            if item.get("type") == "armor" and self._is_equipped(item):
                res = (item.get("system") or {}).get("resilientRune", 0)
                resilient = _int(res.get("value", 0) if isinstance(res, dict) else res)
                break
        return prof_bonus + ability_mod + resilient, rank

    def _calc_perception(self, system: dict, level: int, wis_mod: int,
                          items: list = None, rules_ranks: dict = None) -> tuple[int, int]:
        rank = self._perception_rank(system, items, rules_ranks)

        # If the rules-derived rank exceeds what the stored node itself implies,
        # the stored 'value' predates that upgrade — discard it and recompute.
        perc = system.get("perception") or {}
        if (isinstance(perc, dict) and perc.get("value") is not None
                and rank <= self._rank_from_node(perc)):
            return _int(perc["value"]), rank
        perc2 = (system.get("attributes") or {}).get("perception") or {}
        if (isinstance(perc2, dict) and perc2.get("value") is not None
                and rank <= self._rank_from_node(perc2)):
            return _int(perc2["value"]), rank

        prof_bonus = (level + rank * 2) if rank > 0 else 0
        return prof_bonus + wis_mod, rank

    def _calc_skill(self, skill_data: dict, ability_mod: int, level: int) -> tuple[int, int]:
        if not isinstance(skill_data, dict):
            return ability_mod, 0

        rank = self._rank_from_node(skill_data)

        if skill_data.get("value") is not None:
            return _int(skill_data["value"]), rank

        prof_bonus = (level + rank * 2) if rank > 0 else 0
        return prof_bonus + ability_mod, rank

    def _calc_hp_max(self, items: list, level: int, con_mod: int, hp_val: int) -> int:
        """
        Reconstruct max HP from first principles.

        Foundry never persists system.attributes.hp.max for PC actors — it's
        derived client-side and not written back to the LevelDB document — so
        it must be rebuilt the same way ability scores/ranks are: from the
        ancestry/class items plus conMod.

        Formula: ancestryHP + level * (classHP + conMod), plus any flat HP
        rule bonuses expressed as a literal int, or as the "@actor.level"
        formula (special-cased since it's the Toughness feat's formula and
        one of the most commonly taken PF2e feats). Other roll-formula
        strings are skipped — evaluating arbitrary Foundry formulas is out
        of scope.
        """
        ancestry_hp = 0
        class_hp    = 0
        for item in items:
            itype = item.get("type")
            if itype == "ancestry":
                ancestry_hp = _int((item.get("system") or {}).get("hp", 0))
            elif itype == "class":
                class_hp = _int((item.get("system") or {}).get("hp", 0))

        _PHYSICAL = {"weapon","armor","shield","consumable","ammo",
                     "equipment","treasure","backpack","kit"}

        bonus = 0
        for item in items:
            if item.get("type") in _PHYSICAL and not self._is_equipped(item):
                continue
            for rule in (item.get("system") or {}).get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                if rule.get("key") not in ("FlatModifier", "ItemAlteration"):
                    continue
                selector = rule.get("selector") or rule.get("property")
                selectors = selector if isinstance(selector, list) else [selector]
                if not any(s in ("hp", "hp-max") for s in selectors):
                    continue
                if not self._rule_applies(rule, level):
                    continue
                value = rule.get("value")
                if isinstance(value, (int, float)):
                    bonus += _int(value)
                elif value == "@actor.level":
                    # Common single-variable formula (e.g. the Toughness feat);
                    # other formula strings remain out of scope.
                    bonus += level

        base = ancestry_hp + level * (class_hp + con_mod) + bonus
        return max(base, hp_val)

    # ── Character parsing ─────────────────────────────────────────────────

    def _get_detail_field(self, details: dict, key: str) -> str:
        if not isinstance(details, dict):
            return ""
        field = details.get(key, "")
        return field.get("value", "") if isinstance(field, dict) else (field or "")

    def _parse_character(self, actor: dict) -> dict:
        system   = actor.get("system") or {}
        details  = system.get("details") or {}
        attrs    = system.get("attributes") or {}
        bio      = details.get("biography") or {}

        hp_data  = attrs.get("hp") or {}
        hp_val   = _int(hp_data.get("value", 0) if isinstance(hp_data, dict) else 0)
        hp_temp  = _int(hp_data.get("temp", 0) if isinstance(hp_data, dict) else 0)

        actor_id  = actor.get("_id")
        raw_items = self._get_actor_items(actor_id)
        items     = [enrich_item(i, self.compendium) for i in raw_items]

        lvl_f = details.get("level", 1)
        level = _int(lvl_f.get("value", 1) if isinstance(lvl_f, dict) else (lvl_f or 1))

        abilities = {ab: self._get_ability_mod(system, ab, items)
                     for ab in ("str", "dex", "con", "int", "wis", "cha")}

        # hp.max is never persisted by Foundry for PC actors — it's derived
        # client-side. Prefer a stored value if one ever shows up, otherwise
        # reconstruct from ancestry/class/conMod like other derived stats.
        stored_max = hp_data.get("max") if isinstance(hp_data, dict) else None
        hp_max = (_int(stored_max) if stored_max is not None
                  else self._calc_hp_max(items, level, abilities["con"], hp_val))

        # Compute rules-based ranks once — reads ActiveEffectLike rules on
        # class/class-feature items with level predicates (e.g. Expert Fort at lvl 3)
        rules_ranks              = self._rules_ranks(items, level)

        ac                       = self._calc_ac(system, items, level, abilities["dex"], rules_ranks)
        fort_total, fort_rank    = self._calc_save(system, items, "fortitude", abilities["con"], level, rules_ranks)
        ref_total,  ref_rank     = self._calc_save(system, items, "reflex",    abilities["dex"], level, rules_ranks)
        will_total, will_rank    = self._calc_save(system, items, "will",      abilities["wis"], level, rules_ranks)
        perc_total, perc_rank    = self._calc_perception(system, level, abilities["wis"], items, rules_ranks)

        speed_node  = attrs.get("speed") or {}
        if isinstance(speed_node, dict):
            v = speed_node.get("value")
            speed_val = _int(v if v is not None else speed_node.get("total", 25))
        else:
            speed_val = _int(speed_node) if speed_node is not None else 25
        other_speeds = [{"type": sp["type"].title(), "value": _int(sp["value"])}
                        for sp in (speed_node.get("otherSpeeds", [])
                                   if isinstance(speed_node, dict) else [])
                        if isinstance(sp, dict) and sp.get("type") and sp.get("value")]

        size_raw  = (system.get("traits") or {}).get("size") or {}
        size_val  = size_raw.get("value", "med") if isinstance(size_raw, dict) else (size_raw or "med")
        SIZE_LABELS = {"tiny":"Tiny","sm":"Small","med":"Medium",
                       "lg":"Large","huge":"Huge","grg":"Gargantuan"}
        size_label = SIZE_LABELS.get(str(size_val).lower(), str(size_val).title())

        senses = []
        perc_senses = ((system.get("perception") or {}).get("senses")
                       or (attrs.get("perception") or {}).get("senses", []))
        if not perc_senses:
            senses_raw  = (system.get("traits") or {}).get("senses") or {}
            perc_senses = (senses_raw.get("value", []) if isinstance(senses_raw, dict)
                           else (senses_raw if isinstance(senses_raw, list) else []))
        for s in perc_senses:
            if isinstance(s, dict):
                stype  = s.get("type", s.get("value", ""))
                label  = stype.replace("-", " ").title()
                acuity = s.get("acuity", "")
                if acuity and acuity != "precise":
                    label += f" ({acuity})"
                if s.get("range"):
                    label += f" {s['range']} ft."
                if label:
                    senses.append(label)
            elif isinstance(s, str) and s:
                senses.append(s.replace("-", " ").title())

        def _parse_iwr(raw):
            out = []
            if not isinstance(raw, list):
                return out
            for entry in raw:
                if isinstance(entry, dict):
                    etype = entry.get("type", entry.get("value", ""))
                    if not etype:
                        continue
                    label = etype.replace("-", " ").title()
                    val = entry.get("value")
                    if isinstance(val, (int, float)) and val:
                        label += f" {_int(val)}"
                    exc = entry.get("exceptions", [])
                    if exc:
                        label += f" (except {', '.join(str(e) for e in exc)})"
                    out.append(label)
                elif isinstance(entry, str) and entry:
                    out.append(entry.replace("-", " ").title())
            return out

        immunities  = _parse_iwr(attrs.get("immunities",  []))
        weaknesses  = _parse_iwr(attrs.get("weaknesses",  []))
        resistances = _parse_iwr(attrs.get("resistances", []))

        conditions = []
        for cname in ("dying","wounded","doomed","drained","enfeebled",
                      "clumsy","stupefied","frightened","sickened","slowed","stunned"):
            node = attrs.get(cname) or {}
            val  = _int(node.get("value", 0) if isinstance(node, dict) else (node or 0))
            if val > 0:
                conditions.append({"name": cname.title(), "value": val})
        for item in items:
            if item.get("type") == "condition":
                cname = item.get("name", "")
                isys  = item.get("system") or {}
                cval  = (isys.get("value", {}).get("value")
                         if isinstance(isys.get("value"), dict)
                         else isys.get("value"))
                if cname and not any(c["name"].lower() == cname.lower() for c in conditions):
                    conditions.append({"name": cname, "value": cval})

        # ── Currency ──────────────────────────────────────────────────────
        # system.currency stores the wallet. Values may be plain ints or
        # {value: N} dicts depending on Foundry version.
        cur_node = system.get("currency") or {}

        def _coin(denom: str) -> int:
            v = cur_node.get(denom, 0)
            if isinstance(v, dict):
                return _int(v.get("value", 0))
            return _int(v)

        currency = {c: _coin(c) for c in ("pp", "gp", "sp", "cp")}

        # Coins can also be carried as treasure items in the inventory.
        # PF2e Remaster stores them WITHOUT a stackGroup — just a price
        # with a coin denomination and a quantity. We read any treasure
        # item whose price is expressed in pp/gp/sp/cp.
        # e.g. "Silver Pieces": quantity=5, price={value:{sp:1}, per:1}
        for item in items:
            if not self._is_coin_item(item):
                continue
            isys  = item.get("system") or {}
            qty   = isys.get("quantity", 1)
            qty   = _int(qty.get("value", 1) if isinstance(qty, dict) else qty)
            price = isys.get("price") or {}
            if isinstance(price, dict):
                pval = price.get("value") or {}
                per  = _int(price.get("per", 1) or 1)
                if per < 1:
                    per = 1
                if isinstance(pval, dict):
                    for denom in ("pp", "gp", "sp", "cp"):
                        pv = pval.get(denom)
                        pv = _int(pv.get("value") if isinstance(pv, dict) else pv)
                        if pv:
                            # price is per-unit value × quantity ÷ per
                            currency[denom] = (currency.get(denom, 0)
                                               + pv * qty // per)

        # ── Bulk ──────────────────────────────────────────────────────────
        # system.bulk is not pre-computed in Remaster — calculate from items.
        # Rules: L=0.1 bulk, "-"=0, integers as-is. Multiply by quantity.
        # Coins (treasure items priced at exactly 1 coin per unit) follow the
        # PF2e rule of 1 bulk per 1,000 coins; they must not use the standard
        # qty × bulk formula or the total becomes wildly inflated.
        # Encumbered threshold = STR_mod + 5; max = STR_mod + 10.
        def _item_bulk(item: dict) -> float:
            isys = item.get("system") or {}
            bn   = isys.get("bulk")
            val  = (bn.get("value", "-") if isinstance(bn, dict) else bn)
            if val in ("L", "l"):        return 0.1
            if val in ("-", "", None):   return 0.0
            try:    return float(val)
            except: return 0.0

        _PHYSICAL = {"weapon","armor","shield","consumable","ammo",
                     "equipment","treasure","backpack","kit"}

        coin_total = 0
        noncoin_bulk = 0.0
        # container_id -> raw Bulk of its direct contents, for the stowing
        # reduction below (e.g. a Backpack ignores the first 2 Bulk of its
        # contents; a Bag of Holding ignores up to its full capacity).
        contained_bulk_by_container: dict[str, float] = {}
        for i in items:
            if i.get("type") not in _PHYSICAL:
                continue
            isys = i.get("system") or {}
            qty_raw = isys.get("quantity", 1)
            qty = _int(qty_raw.get("value", 1) if isinstance(qty_raw, dict) else qty_raw)
            if self._is_coin_item(i):
                coin_total += qty
                continue
            item_bulk = _item_bulk(i) * qty
            noncoin_bulk += item_bulk
            cid = isys.get("containerId")
            if cid:
                contained_bulk_by_container[cid] = contained_bulk_by_container.get(cid, 0.0) + item_bulk

        # Apply each stowing container's Bulk reduction to its direct contents.
        for i in items:
            if i.get("type") != "backpack":
                continue
            isys = i.get("system") or {}
            if not isys.get("stowing"):
                continue
            bulk_field = isys.get("bulk") or {}
            ignored = float(bulk_field.get("ignored", 0) or 0) if isinstance(bulk_field, dict) else 0.0
            if ignored <= 0:
                continue
            contained = contained_bulk_by_container.get(i.get("_id", ""), 0.0)
            noncoin_bulk -= min(contained, ignored)

        bulk_current = round(max(noncoin_bulk, 0.0) + coin_total // 1000, 1)
        str_mod  = abilities.get("str", 0)
        bulk_enc = 5 + str_mod
        bulk_max = 10 + str_mod

        # Investiture: count items actually marked invested rather than reading
        # system.resources.investiture.value, which Foundry does not reliably update.
        invest_node = (system.get("resources") or {}).get("investiture") or {}
        invest_val  = sum(
            1 for i in items
            if isinstance((i.get("system") or {}).get("equipped"), dict)
            and (i.get("system") or {}).get("equipped", {}).get("invested") is True
        )
        invest_max  = _int(invest_node.get("max", 10) if isinstance(invest_node, dict) else 10)

        pronouns = bio.get("pronouns", details.get("pronouns", ""))
        if isinstance(pronouns, dict):
            pronouns = pronouns.get("value", "")
        attitude       = html_to_wikitext(bio.get("attitude",      ""))
        beliefs        = html_to_wikitext(bio.get("beliefs",       ""))
        edicts         = html_to_wikitext(bio.get("edicts",        ""))
        anathema       = html_to_wikitext(bio.get("anathema",      ""))
        likes          = html_to_wikitext(bio.get("likes",         ""))
        dislikes       = html_to_wikitext(bio.get("dislikes",      ""))
        catchphrases   = html_to_wikitext(bio.get("catchphrases",  ""))
        campaign_notes = html_to_wikitext(bio.get("campaignNotes", bio.get("notes", "")))
        allies         = html_to_wikitext(bio.get("allies",        ""))
        enemies        = html_to_wikitext(bio.get("enemies",       ""))
        organizations  = html_to_wikitext(bio.get("organizations", ""))

        skills_raw = system.get("skills") or {}
        SKILL_ABILITY = self.SKILL_ABILITY
        skills = {}
        for key, label in {
            "acrobatics":"Acrobatics","arcana":"Arcana","athletics":"Athletics",
            "crafting":"Crafting","deception":"Deception","diplomacy":"Diplomacy",
            "intimidation":"Intimidation","medicine":"Medicine","nature":"Nature",
            "occultism":"Occultism","performance":"Performance","religion":"Religion",
            "society":"Society","stealth":"Stealth","survival":"Survival","thievery":"Thievery",
        }.items():
            # Merge stored rank with rules-granted rank (e.g. heritage/class feature)
            raw = skills_raw.get(key)
            stored_data = dict(raw) if isinstance(raw, dict) else {}
            rules_rank  = rules_ranks.get(f"skills.{key}", 0)
            if rules_rank > self._rank_from_node(stored_data):
                stored_data["rank"] = rules_rank
                # Discard any stale precomputed total so _calc_skill recomputes
                # from the corrected rank instead of returning the old value.
                stored_data.pop("value", None)
            total, rank = self._calc_skill(stored_data,
                                           abilities[SKILL_ABILITY[key]], level)
            skills[label] = {"total": total, "rank": rank}

        for item in items:
            if item.get("type") == "lore":
                lore_name = item.get("name", "Unknown Lore").title()
                s = item.get("system") or {}
                lore_rank = _int(s.get("rank", {}).get("value", 0)
                                 if isinstance(s.get("rank"), dict) else s.get("rank", 0))
                # A Lore item only exists if the character is at least Trained;
                # rank 0 means the item was created without an explicit rank written back.
                if lore_rank == 0:
                    lore_rank = 1
                prof = level + lore_rank * 2
                skills[lore_name] = {"total": prof + abilities["int"], "rank": lore_rank}

        res         = system.get("resources") or {}
        hp_field    = res.get("heroPoints") or {}
        hero_pts    = _int(hp_field.get("value", 0) if isinstance(hp_field, dict) else hp_field)
        focus_field = res.get("focus") or {}
        focus_max   = _int(focus_field.get("max", 0)   if isinstance(focus_field, dict) else 0)
        focus_val   = _int(focus_field.get("value", 0) if isinstance(focus_field, dict) else 0)

        lang_f    = details.get("languages") or {}
        lang_list = (lang_f.get("value", []) if isinstance(lang_f, dict)
                     else (lang_f if isinstance(lang_f, list) else []))

        portrait = icon_url(actor.get("img", ""), "character", allow_iconics=True)

        feats, spells, spell_entries = [], [], {}
        for item in items:
            itype = item.get("type", "")

            if itype in ("feat","feature","ancestry-feature","heritage",
                         "class-feature","background","archetype","action"):
                isys     = item.get("system") or {}
                traits_v = isys.get("traits", {}).get("value", []) if isinstance(isys.get("traits"), dict) else []
                action_t = ""
                if itype == "action":
                    action_t = isys.get("actionType", {}).get("value", "") if isinstance(isys.get("actionType"), dict) else ""
                desc_node = isys.get("description") or {}
                desc = html_to_wikitext(desc_node.get("value", "") if isinstance(desc_node, dict) else "")
                feats.append({
                    "name": item.get("name", ""),
                    "type": itype if itype != "action" else f"action ({action_t})",
                    "traits": traits_v, "desc": desc,
                    "img": icon_url(item.get("img", ""), itype),
                })

            elif itype == "spellcastingEntry":
                entry_id = item.get("_id", "")
                isys     = item.get("system") or {}
                trad     = isys.get("tradition", {}).get("value", "") if isinstance(isys.get("tradition"), dict) else ""
                ctype    = isys.get("prepared",  {}).get("value", "") if isinstance(isys.get("prepared"),  dict) else ""
                dc_node  = isys.get("spelldc") or {}
                dc       = dc_node.get("dc")    if isinstance(dc_node, dict) else None
                atk      = dc_node.get("value") if isinstance(dc_node, dict) else None
                slots    = {}
                for slot_key, slot_data in (isys.get("slots") or {}).items():
                    if isinstance(slot_data, dict):
                        rn = slot_key.replace("slot", "")
                        if rn.isdigit():
                            smax = _int(slot_data.get("max", 0))
                            if smax:
                                slots[_int(rn)] = {"value": _int(slot_data.get("value", 0)), "max": smax}
                spell_entries[entry_id] = {
                    "name": item.get("name", ""), "tradition": trad, "type": ctype,
                    "dc": dc, "attack": atk, "spells": [], "slots": slots,
                    "img": icon_url(item.get("img", ""), itype),
                }

            elif itype == "spell":
                isys     = item.get("system") or {}
                rank_val = isys.get("level", {}).get("value", "?") if isinstance(isys.get("level"), dict) else isys.get("level", "?")
                if isinstance(isys.get("rank"), dict):
                    rank_val = isys["rank"].get("value", rank_val)
                traits_v  = isys.get("traits", {}).get("value", []) if isinstance(isys.get("traits"), dict) else []
                location  = isys.get("location") or {}
                entry_id  = location.get("value", "") if isinstance(location, dict) else ""
                desc_node = isys.get("description") or {}
                desc = html_to_wikitext(desc_node.get("value", "") if isinstance(desc_node, dict) else "")
                spell_obj = {"name": item.get("name", ""), "rank": rank_val,
                             "traits": traits_v, "desc": desc, "entry_id": entry_id,
                             "img": icon_url(item.get("img", ""), "spell")}
                if entry_id and entry_id in spell_entries:
                    spell_entries[entry_id]["spells"].append(spell_obj)
                else:
                    spells.append(spell_obj)

        for entry in spell_entries.values():
            entry["spells"].sort(key=lambda s: (_int(s["rank"], 99) if str(s["rank"]).isdigit() else 99, s["name"]))

        return {
            "name": actor.get("name"), "id": actor_id, "portrait": portrait,
            "level": level, "size": size_label,
            "hp": hp_val, "hp_max": hp_max, "hp_temp": hp_temp,
            "ac": ac,
            "perception": perc_total, "perception_rank": perc_rank,
            "speed": speed_val, "other_speeds": other_speeds,
            "senses": senses, "immunities": immunities,
            "weaknesses": weaknesses, "resistances": resistances,
            "conditions": conditions,
            "fortitude": fort_total, "fortitude_rank": fort_rank,
            "reflex":    ref_total,  "reflex_rank":    ref_rank,
            "will":      will_total, "will_rank":      will_rank,
            "abilities": abilities,
            "skills": skills,
            "hero_points": hero_pts,
            "focus": f"{focus_val}/{focus_max}" if focus_max else None,
            "languages": lang_list,
            "currency": currency,
            "bulk_current": bulk_current, "bulk_enc": bulk_enc, "bulk_max": bulk_max,
            "invest_val": invest_val, "invest_max": invest_max,
            "keyability":  self._get_detail_field(details, "keyability"),
            "xp":          self._get_detail_field(details, "xp"),
            "deity":       next((i["name"] for i in items if i.get("type") == "deity"), None)
                           or self._get_detail_field(details, "deity"),
            "deity_img":   icon_url(next((i.get("img","") for i in items
                                          if i.get("type") == "deity"), ""), "deity"),
            "age":         self._get_detail_field(details, "age"),
            "gender":      self._get_detail_field(details, "gender"),
            "pronouns":    str(pronouns) if pronouns else "",
            "height":      self._get_detail_field(details, "height"),
            "weight":      self._get_detail_field(details, "weight"),
            "ethnicity":   self._get_detail_field(details, "ethnicity"),
            "nationality": self._get_detail_field(details, "nationality"),
            "appearance":  html_to_wikitext(bio.get("appearance", "")),
            "backstory":   html_to_wikitext(bio.get("backstory",  "")),
            "attitude": attitude, "beliefs": beliefs, "edicts": edicts, "anathema": anathema,
            "likes": likes, "dislikes": dislikes, "catchphrases": catchphrases,
            "campaign_notes": campaign_notes, "allies": allies,
            "enemies": enemies, "organizations": organizations,
            "items": items, "feats": feats,
            "spell_entries": list(spell_entries.values()),
            "orphan_spells": spells,
        }

    # ── Wiki rendering ────────────────────────────────────────────────────

    def _fmt_prof(self, rank: int) -> str:
        p = PROFICIENCY_MAP.get(_int(rank), PROFICIENCY_MAP[0])
        return f"{{{{color|{p['color']}|'''{p['name']}'''}}}}"

    def _fmt_save(self, total: int, rank: int) -> str:
        return f"{fmt_mod(total)} ({self._fmt_prof(rank)})"

    # ── Section generators ────────────────────────────────────────────────

    def _section_details(self, char: dict) -> str:
        lines = []

        lines.append("== Details ==")
        for itype, label in [("ancestry","Ancestry"),("heritage","Heritage"),
                              ("background","Background"),("class","Class")]:
            found = next((i for i in char["items"] if i.get("type") == itype), None)
            if found:
                img    = icon_url(found.get("img", ""), itype)
                icon_s = wiki_img(img, 20, found["name"]) + " " if img else ""
                lines.append(f"* '''{label}:''' {icon_s}{wiki_escape(found['name'])}")
        if char.get("deity"):
            img    = char.get("deity_img", "")
            icon_s = wiki_img(img, 20, char["deity"]) + " " if img else ""
            lines.append(f"* '''Deity:''' {icon_s}{wiki_escape(char['deity'])}")
        for key, label in [("keyability","Key Ability"),("xp","XP"),
                            ("hero_points","Hero Points"),
                            ("focus","Focus Points"),("languages","Languages")]:
            val = char.get(key)
            if key == "languages" and val:
                val = ", ".join(str(l) for l in val)
            elif key == "keyability" and val:
                val = str(val).upper()
            # Hero Points at 0 is meaningful state (e.g. just spent them
            # all), not missing data — match the infobox, which already
            # shows it. Other fields keep the truthy check.
            present = (val is not None and val != "") if key == "hero_points" else bool(val)
            if present:
                lines.append(f"* '''{label}:''' {val}")
        lines.append("")

        phys = [(k, char.get(fk)) for k, fk in [
            ("Age","age"),("Pronouns","pronouns"),("Height","height"),
            ("Weight","weight"),("Ethnicity","ethnicity"),("Nationality","nationality"),
        ] if char.get(fk)]
        if phys:
            lines.append("== Identity ==")
            for k, v in phys:
                lines.append(f"* '''{k}:''' {v}")
            lines.append("")

        personality = [(k, char.get(fk)) for k, fk in [
            ("Attitude",     "attitude"),
            ("Beliefs",      "beliefs"),
            ("Edicts",       "edicts"),
            ("Anathema",     "anathema"),
            ("Likes",        "likes"),
            ("Dislikes",     "dislikes"),
            ("Catchphrases", "catchphrases"),
        ] if char.get(fk)]
        if personality:
            lines.append("== Personality & Beliefs ==")
            for k, v in personality:
                lines.append(f"; '''{k}'''")
                lines.append(f": {v}")
            lines.append("")

        if char.get("appearance"):
            lines.append(f"== Appearance ==\n{char['appearance']}\n")
        if char.get("backstory"):
            lines.append(f"== Biography ==\n{char['backstory']}\n")
        if char.get("campaign_notes"):
            lines.append(f"== Campaign Notes ==\n{char['campaign_notes']}\n")

        campaign = [(k, char.get(fk)) for k, fk in [
            ("Allies",        "allies"),
            ("Enemies",       "enemies"),
            ("Organizations", "organizations"),
        ] if char.get(fk)]
        if campaign:
            lines.append("== Campaign Connections ==")
            for k, v in campaign:
                lines.append(f"; '''{k}'''")
                lines.append(f": {v}")
            lines.append("")

        return "\n".join(lines)

    def _section_defense(self, char: dict) -> str:
        hp_str = f"{char['hp']}/{char['hp_max']}"
        if char.get("hp_temp"):
            hp_str += f" (+{char['hp_temp']} temp)"
        speed_parts = [f"{char['speed']} ft. (land)"]
        for sp in char.get("other_speeds", []):
            speed_parts.append(f"{sp['value']} ft. ({sp['type']})")

        lines = [
            f"* '''HP:''' {hp_str}",
            f"* '''AC:''' {char['ac']}",
            f"* '''Perception:''' {fmt_mod(char['perception'])} ({self._fmt_prof(char['perception_rank'])})",
            f"* '''Speed:''' {', '.join(speed_parts)}",
            f"* '''Fortitude:''' {self._fmt_save(char['fortitude'], char['fortitude_rank'])}",
            f"* '''Reflex:''' {self._fmt_save(char['reflex'], char['reflex_rank'])}",
            f"* '''Will:''' {self._fmt_save(char['will'], char['will_rank'])}",
        ]
        if char.get("senses"):
            lines.append(f"* '''Senses:''' {', '.join(char['senses'])}")
        if char.get("immunities"):
            lines.append(f"* '''Immunities:''' {', '.join(char['immunities'])}")
        if char.get("weaknesses"):
            lines.append(f"* '''Weaknesses:''' {', '.join(char['weaknesses'])}")
        if char.get("resistances"):
            lines.append(f"* '''Resistances:''' {', '.join(char['resistances'])}")
        if char.get("conditions"):
            cond_parts = [f"{c['name']} {c['value']}" if c.get("value") else c["name"]
                          for c in char["conditions"]]
            lines.append(f"* '''Active Conditions:''' {', '.join(cond_parts)}")
        return "\n".join(lines)

    def _section_abilities(self, char: dict) -> str:
        ab = char["abilities"]
        AB = {"str":"STR","dex":"DEX","con":"CON","int":"INT","wis":"WIS","cha":"CHA"}
        return (
            '{| class="wikitable" style="text-align:center; width:100%;"\n'
            "|-\n"
            "! " + " !! ".join(AB.values()) + "\n"
            "|-\n"
            "| " + " || ".join(fmt_mod(ab[k]) for k in AB) + "\n"
            "|}"
        )

    def _section_skills(self, char: dict) -> str:
        lines = [
            '{| class="wikitable sortable" style="width:100%;"',
            "! Skill !! Modifier !! Proficiency",
        ]
        for name, data in sorted(char["skills"].items()):
            lines.append(f"|-\n| {wiki_escape(name)} || {fmt_mod(data['total'])} || {self._fmt_prof(data['rank'])}")
        lines.append("|}")
        return "\n".join(lines)

    def _section_wealth(self, char: dict) -> str:
        cur = char["currency"]
        coin_parts = [f"{cur[c]} {c.upper()}" for c in ("pp","gp","sp","cp") if cur[c]]
        coin_str   = ", ".join(coin_parts) if coin_parts else "None"
        # Coin total in gp: pp=10, gp=1, sp=0.1, cp=0.01
        coin_gp = cur["pp"] * 10 + cur["gp"] + cur["sp"] / 10 + cur["cp"] / 100

        # Sum item prices for all physical non-coin inventory items
        DENOM_TO_GP = {"pp": 10.0, "gp": 1.0, "sp": 0.1, "cp": 0.01}
        PHYSICAL = {"weapon","armor","shield","consumable","ammo",
                    "equipment","treasure","backpack","kit"}
        item_gp = 0.0
        for item in char.get("items", []):
            if item.get("type") not in PHYSICAL:
                continue
            if self._is_coin_item(item):
                continue
            s    = item.get("system") or {}
            q    = s.get("quantity", 1) if isinstance(s, dict) else 1
            qty  = _int(q["value"] if isinstance(q, dict) else q)
            praw = s.get("price") if isinstance(s, dict) else None
            if not isinstance(praw, dict):
                continue
            pval = praw.get("value") or {}
            per  = _int(praw.get("per", 1) or 1)
            if not isinstance(pval, dict) or per < 1:
                continue
            unit_gp = sum(DENOM_TO_GP[d] * _int(pval.get(d, 0))
                          for d in DENOM_TO_GP if pval.get(d))
            item_gp += unit_gp * qty / per

        total_gp  = coin_gp + item_gp
        total_str = f"{total_gp:,.2f}".rstrip('0').rstrip('.') + " gp"
        bulk_cur = char.get("bulk_current", 0)
        bulk_enc = char.get("bulk_enc", 0)
        bulk_max = char.get("bulk_max", 0)
        bulk_str = f"{bulk_cur}"
        if bulk_enc:
            bulk_str += f" / {bulk_enc} enc / {bulk_max} max"
        invest_str = f"{char['invest_val']}/{char['invest_max']}" if char.get("invest_max") else "—"
        return (
            '{| class="wikitable"\n'
            "! Coin !! Total Value !! Bulk !! Invested\n"
            "|-\n"
            f"| {coin_str} || {total_str} || {bulk_str} || {invest_str}\n"
            "|}"
        )

    def _section_feats(self, char: dict) -> str:
        if not char["feats"]:
            return ""
        GROUP_ORDER  = ["class-feature","ancestry-feature","heritage","background",
                        "feat","archetype","action"]
        GROUP_LABELS = {
            "class-feature":"Class Features","ancestry-feature":"Ancestry Features",
            "heritage":"Heritage","background":"Background Features",
            "feat":"Feats","archetype":"Archetype Feats","action":"Actions",
        }
        groups: dict[str, list] = {}
        for feat in char["feats"]:
            groups.setdefault(feat["type"].split(" ")[0], []).append(feat)

        lines = []
        for gk in GROUP_ORDER + [k for k in groups if k not in GROUP_ORDER]:
            gfeats = groups.get(gk, [])
            if not gfeats:
                continue
            label = GROUP_LABELS.get(gk, gk.replace("-"," ").title())
            lines.append(f"====={label}=====")
            for feat in sorted(gfeats, key=lambda f: f["name"]):
                icon_s = wiki_img(feat["img"], 20, feat["name"]) + " " if feat.get("img") else ""
                trait_s = (" <small>(" + ", ".join(feat["traits"]) + ")</small>") if feat["traits"] else ""
                lines.append(f"; {icon_s}'''{wiki_escape(feat['name'])}'''{trait_s}")
                if feat["desc"]:
                    lines.append(f": {feat['desc']}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _container_chain_has_cycle(item_id: str, item_map: dict, depth_cap: int = 20) -> bool:
        """
        Walk an item's containerId chain toward the root. Returns True if
        the chain loops back on itself (self-referential or multi-item
        cycle) rather than terminating at a true top-level item — such an
        item would otherwise never resolve to a top-level slot and vanish
        from the rendered inventory with no error.
        """
        seen    = {item_id}
        current = item_id
        for _ in range(depth_cap):
            item = item_map.get(current)
            if not item:
                return False
            sys_data = item.get("system") or {}
            cid = sys_data.get("containerId") if isinstance(sys_data, dict) else None
            if not cid or cid not in item_map:
                return False
            if cid in seen:
                return True
            seen.add(cid)
            current = cid
        return True  # depth cap exceeded without resolving — treat as cyclic

    def _section_inventory(self, items: list) -> str:
        PHYSICAL = {"weapon","armor","shield","consumable","ammo",
                    "equipment","treasure","backpack","kit"}
        physical     = [i for i in items if i.get("type") in PHYSICAL]
        item_map     = {i["_id"]: i for i in physical if "_id" in i}
        cont_contents: dict[str, list] = {}
        top: dict[str, list] = {t: [] for t in PHYSICAL}

        for item in physical:
            sys_data = item.get("system") or {}
            cid      = sys_data.get("containerId") if isinstance(sys_data, dict) else None
            item_id  = item.get("_id")
            if (cid and cid in item_map
                    and not (item_id and self._container_chain_has_cycle(item_id, item_map))):
                cont_contents.setdefault(cid, []).append(item)
            else:
                top.setdefault(item.get("type"), []).append(item)

        SECTIONS = [
            ("weapon","Weapons"),("armor","Armor & Shields"),("shield",""),
            ("consumable","Consumables"),("ammo","Ammunition"),
            ("equipment","Equipment"),("backpack","Bags & Storage"),
            ("treasure","Treasure & Currency"),("kit","Kits"),
        ]
        lines = []
        shown = set()
        for itype, heading in SECTIONS:
            if not heading or itype in shown:
                continue
            group = top.get(itype, [])
            if itype == "armor":
                group = group + top.get("shield", [])
                shown.add("shield")
            shown.add(itype)
            if not group:
                continue

            lines.append(f"====={heading}=====")
            lines.append('{| class="wikitable" style="width:100%;"')
            lines.append("! !! Item !! Qty !! Bulk !! Level !! Price")
            for item in sorted(group, key=lambda x: x.get("name", "")):
                s     = item.get("system") or {}
                name  = item.get("name", "Unknown")
                q_raw = s.get("quantity", 1) if isinstance(s, dict) else 1
                qty   = _int(q_raw["value"] if isinstance(q_raw, dict) else q_raw)
                bulk  = (s.get("bulk", {}).get("value", "—") if isinstance(s.get("bulk"), dict)
                         else (s.get("bulk") or "—"))
                lvl   = (s.get("level", {}).get("value", "—") if isinstance(s.get("level"), dict)
                         else (s.get("level") or "—"))
                pnode = s.get("price", {}).get("value", {}) if isinstance(s.get("price"), dict) else {}
                price = ", ".join(f"{pnode[c]} {c}" for c in ("pp","gp","sp","cp")
                                  if isinstance(pnode, dict) and pnode.get(c)) or "—"
                img        = icon_url(item.get("img", ""), itype)
                desc_node  = s.get("description") or {}
                desc_raw   = desc_node.get("value", "") if isinstance(desc_node, dict) else ""
                desc_plain = strip_html(desc_raw)
                name_html  = wiki_tooltip(name, desc_plain, item_stat_line(item))
                lines.append("|-")
                lines.append(f"| {wiki_img(img, 24, name)} || {name_html} || {qty} || {bulk} || {lvl} || {price}")
                if itype == "backpack" and item.get("_id") in cont_contents:
                    lines.append("|-")
                    lines.append('| colspan="6" |')
                    lines.extend(self._render_container_tree(item["_id"], cont_contents))
            lines.append("|}")
            lines.append("")
        return "\n".join(lines)

    def _render_container_tree(self, cid: str, cont: dict, depth: int = 1,
                               visited: set = None) -> list:
        if visited is None:
            visited = set()
        lines = []
        for item in cont.get(cid, []):
            s          = item.get("system") or {}
            name       = item.get("name", "Unknown")
            qty        = _int(s.get("quantity", 1) if isinstance(s, dict) else 1)
            qty_s      = f" ×{qty}" if qty > 1 else ""
            img        = icon_url(item.get("img", ""), item.get("type", ""))
            icon_s     = wiki_img(img, 16, name) + " " if img else ""
            desc_node  = s.get("description") or {}
            desc_raw   = desc_node.get("value", "") if isinstance(desc_node, dict) else ""
            name_html  = wiki_tooltip(name, strip_html(desc_raw), item_stat_line(item))
            bullet     = "*" * depth
            if item.get("type") == "backpack":
                lines.append(f"{bullet} {icon_s}'''{name_html}'''")
                child_id = item.get("_id")
                if child_id and child_id not in visited:
                    visited.add(child_id)
                    lines.extend(self._render_container_tree(child_id, cont, depth + 1, visited))
            else:
                lines.append(f"{bullet} {icon_s}{name_html}{qty_s}")
        return lines

    @staticmethod
    def _spell_desc_block(desc: str) -> str:
        """
        Collapsible description shown beneath a spell's bullet line.

        Rendered as a standalone indented div rather than a list continuation:
        multi-paragraph descriptions would break out of a wikitext list, but a
        block-level div renders them intact. Each spell has its own `*` line,
        so the interrupted list simply restarts on the next spell.
        """
        return (
            '<div class="toccolours mw-collapsible mw-collapsed" '
            'style="margin-left:2em; font-size:95%;">\n'
            "''Description''\n"
            '<div class="mw-collapsible-content">\n'
            f"{desc}\n"
            "</div>\n"
            "</div>"
        )

    def _section_spells(self, char: dict) -> str:
        entries = char.get("spell_entries", [])
        orphans = char.get("orphan_spells", [])
        if not entries and not orphans:
            return ""

        lines = []
        for entry in entries:
            trad  = f" ({entry['tradition'].title()})" if entry["tradition"] else ""
            ctype = f" — {entry['type'].title()}"      if entry["type"]      else ""
            lines.append(f"====={wiki_escape(entry['name'])}{trad}{ctype}=====")
            if entry.get("dc") or entry.get("attack"):
                dc_s  = f"DC {entry['dc']}"                    if entry.get("dc")     else ""
                atk_s = f"Attack {fmt_mod(entry['attack'])}"   if entry.get("attack") else ""
                lines.append("; " + " | ".join(x for x in [dc_s, atk_s] if x))
            if entry["spells"]:
                current_rank = None
                for spell in entry["spells"]:
                    if spell["rank"] != current_rank:
                        current_rank = spell["rank"]
                        rl = f"Rank {current_rank}" if str(current_rank).isdigit() else str(current_rank)
                        slot_info = ""
                        if str(current_rank).isdigit():
                            slot = entry.get("slots", {}).get(_int(current_rank))
                            if slot:
                                slot_info = f" <small>({slot['value']}/{slot['max']} slots)</small>"
                        lines.append(f"\n'''{rl}'''{slot_info}")
                    icon_s  = wiki_img(spell["img"], 18, spell["name"]) + " " if spell.get("img") else ""
                    trait_s = (" <small>[" + ", ".join(spell["traits"]) + "]</small>") if spell["traits"] else ""
                    lines.append(f"* {icon_s}[[{wiki_escape(spell['name'])}]]{trait_s}")
                    if spell.get("desc"):
                        lines.append(self._spell_desc_block(spell["desc"]))
            lines.append("")

        if orphans:
            lines.append("=====Other Spells=====")
            for spell in sorted(orphans, key=lambda s: (_int(s["rank"], 99), s["name"])):
                icon_s = wiki_img(spell["img"], 18, spell["name"]) + " " if spell.get("img") else ""
                lines.append(f"* {icon_s}[[{wiki_escape(spell['name'])}]]")
                if spell.get("desc"):
                    lines.append(self._spell_desc_block(spell["desc"]))
            lines.append("")

        return "\n".join(lines)

    def _parse_npc(self, actor: dict) -> dict:
        """
        Parse a PF2e NPC actor. NPCs have a simpler schema than PCs —
        stats are largely pre-computed and stored directly as flat values.
        """
        system  = actor.get("system") or {}
        details = system.get("details") or {}
        attrs   = system.get("attributes") or {}

        actor_id  = actor.get("_id")
        raw_items = self._get_actor_items(actor_id)
        items     = [enrich_item(i, self.compendium) for i in raw_items]

        # ── Core stats — NPCs store final values directly ──────────────────
        hp_node  = attrs.get("hp") or {}
        hp_max   = _int(hp_node.get("max", hp_node.get("value", 0)) if isinstance(hp_node, dict) else 0)
        hp_val   = _int(hp_node.get("value", hp_max) if isinstance(hp_node, dict) else hp_max)
        hp_temp  = _int(hp_node.get("temp", 0) if isinstance(hp_node, dict) else 0)

        ac_node  = attrs.get("ac") or {}
        ac_val   = _int(ac_node.get("value", 10) if isinstance(ac_node, dict) else (ac_node or 10))

        perc_node = attrs.get("perception") or {}
        perc_val  = _int(perc_node.get("value", 0) if isinstance(perc_node, dict) else (perc_node or 0))
        perc_mod  = perc_node.get("mod", perc_val) if isinstance(perc_node, dict) else perc_val

        speed_node  = attrs.get("speed") or {}
        if isinstance(speed_node, dict):
            v = speed_node.get("value")
            speed_val = _int(v if v is not None else speed_node.get("total", 25))
        else:
            speed_val = _int(speed_node) if speed_node is not None else 25
        other_speeds = [{"type": sp["type"].title(), "value": _int(sp["value"])}
                        for sp in (speed_node.get("otherSpeeds", [])
                                   if isinstance(speed_node, dict) else [])
                        if isinstance(sp, dict) and sp.get("type") and sp.get("value")]

        # ── Ability modifiers — stored as flat mods on NPCs ────────────────
        abilities = {}
        for ab in ("str", "dex", "con", "int", "wis", "cha"):
            node = (system.get("abilities") or {}).get(ab) or {}
            mod  = node.get("mod", node.get("value", 0)) if isinstance(node, dict) else _int(node)
            abilities[ab] = _int(mod)

        # ── Saves — stored as flat {value, mod} on NPCs ────────────────────
        saves_raw = system.get("saves") or {}
        saves = {}
        for slug, label in (("fortitude","Fortitude"),("reflex","Reflex"),("will","Will")):
            node = saves_raw.get(slug) or {}
            val  = _int(node.get("value", node.get("totalModifier", 0)) if isinstance(node, dict) else node)
            saves[slug] = {"label": label, "value": val}

        # ── Skills ────────────────────────────────────────────────────────
        skills_raw = system.get("skills") or {}
        skills = {}
        for key, data in skills_raw.items():
            if not isinstance(data, dict):
                continue
            label = data.get("label", key.replace("-"," ").title())
            val   = _int(data.get("value", data.get("totalModifier", 0)))
            skills[label] = val

        # ── Traits / type / alignment ──────────────────────────────────────
        traits_node = system.get("traits") or {}
        trait_list  = traits_node.get("value", []) if isinstance(traits_node, dict) else []
        size_raw    = traits_node.get("size") or {}
        size_val    = size_raw.get("value", "med") if isinstance(size_raw, dict) else (size_raw or "med")
        SIZE_LABELS = {"tiny":"Tiny","sm":"Small","med":"Medium",
                       "lg":"Large","huge":"Huge","grg":"Gargantuan"}
        size_label  = SIZE_LABELS.get(str(size_val).lower(), str(size_val).title())
        alignment   = (system.get("details") or {}).get("alignment", {})
        if isinstance(alignment, dict):
            alignment = alignment.get("value", "")

        # ── Senses / IWR ──────────────────────────────────────────────────
        senses = []
        for s in (perc_node.get("senses", []) if isinstance(perc_node, dict) else []):
            if isinstance(s, dict):
                stype = s.get("type", "")
                label = stype.replace("-"," ").title()
                if s.get("acuity") and s["acuity"] != "precise":
                    label += f" ({s['acuity']})"
                if s.get("range"):
                    label += f" {s['range']} ft."
                if label:
                    senses.append(label)

        def _parse_iwr(raw):
            out = []
            for entry in (raw if isinstance(raw, list) else []):
                if isinstance(entry, dict):
                    etype = entry.get("type", "")
                    if not etype:
                        continue
                    label = etype.replace("-"," ").title()
                    if isinstance(entry.get("value"), (int, float)) and entry["value"]:
                        label += f" {_int(entry['value'])}"
                    out.append(label)
                elif isinstance(entry, str):
                    out.append(entry.replace("-"," ").title())
            return out

        immunities  = _parse_iwr(attrs.get("immunities",  []))
        weaknesses  = _parse_iwr(attrs.get("weaknesses",  []))
        resistances = _parse_iwr(attrs.get("resistances", []))

        # ── Languages ─────────────────────────────────────────────────────
        lang_f    = traits_node.get("languages") or {}
        lang_list = lang_f.get("value", []) if isinstance(lang_f, dict) else (
            lang_f if isinstance(lang_f, list) else [])

        # ── Description / flavour ─────────────────────────────────────────
        pub_notes  = html_to_wikitext(details.get("publicNotes",  details.get("notes", "")))
        priv_notes = html_to_wikitext(details.get("privateNotes", ""))
        blurb      = html_to_wikitext(details.get("blurb",        ""))
        creature_type = details.get("creatureType", details.get("type", ""))
        level_node = details.get("level") or {}
        level      = _int(level_node.get("value", 0) if isinstance(level_node, dict) else (level_node or 0))

        # ── Actions / abilities / spells from items ────────────────────────
        actions, spells, spell_entries = [], [], {}
        for item in items:
            itype = item.get("type", "")

            if itype in ("melee", "ranged", "action", "feat"):
                isys     = item.get("system") or {}
                traits_v = isys.get("traits", {}).get("value", []) if isinstance(isys.get("traits"), dict) else []
                desc_node = isys.get("description") or {}
                desc = html_to_wikitext(desc_node.get("value", "") if isinstance(desc_node, dict) else "")
                # Attack bonus for melee/ranged
                attack_bonus = None
                if itype in ("melee", "ranged"):
                    atk_node = isys.get("attack", isys.get("bonus", {}))
                    if isinstance(atk_node, dict):
                        attack_bonus = atk_node.get("value")
                    elif isinstance(atk_node, (int, float)):
                        attack_bonus = _int(atk_node)
                    # Damage
                dmg_rolls = []
                for droll in (isys.get("damageRolls") or {}).values() if isinstance(isys.get("damageRolls"), dict) else []:
                    if isinstance(droll, dict):
                        formula = droll.get("damage", "")
                        dtype   = droll.get("damageType", "")
                        if formula:
                            dmg_rolls.append(f"{formula} {dtype}".strip())
                actions.append({
                    "name":    item.get("name", ""),
                    "type":    itype,
                    "traits":  traits_v,
                    "attack":  attack_bonus,
                    "damage":  dmg_rolls,
                    "desc":    desc,
                    "img":     icon_url(item.get("img", ""), itype),
                })

            elif itype == "spellcastingEntry":
                entry_id = item.get("_id", "")
                isys     = item.get("system") or {}
                trad     = isys.get("tradition", {}).get("value", "") if isinstance(isys.get("tradition"), dict) else ""
                ctype    = isys.get("prepared", {}).get("value", "")  if isinstance(isys.get("prepared"),  dict) else ""
                dc_node  = isys.get("spelldc") or {}
                dc       = dc_node.get("dc")    if isinstance(dc_node, dict) else None
                atk      = dc_node.get("value") if isinstance(dc_node, dict) else None
                spell_entries[entry_id] = {
                    "name": item.get("name", ""), "tradition": trad, "type": ctype,
                    "dc": dc, "attack": atk, "spells": [], "slots": {},
                    "img": icon_url(item.get("img", ""), itype),
                }

            elif itype == "spell":
                isys     = item.get("system") or {}
                rank_val = isys.get("level", {}).get("value", "?") if isinstance(isys.get("level"), dict) else isys.get("level", "?")
                if isinstance(isys.get("rank"), dict):
                    rank_val = isys["rank"].get("value", rank_val)
                traits_v  = isys.get("traits", {}).get("value", []) if isinstance(isys.get("traits"), dict) else []
                location  = isys.get("location") or {}
                entry_id  = location.get("value", "") if isinstance(location, dict) else ""
                desc_node = isys.get("description") or {}
                desc = html_to_wikitext(desc_node.get("value", "") if isinstance(desc_node, dict) else "")
                spell_obj = {"name": item.get("name",""), "rank": rank_val,
                             "traits": traits_v, "desc": desc, "entry_id": entry_id,
                             "img": icon_url(item.get("img",""), "spell")}
                if entry_id and entry_id in spell_entries:
                    spell_entries[entry_id]["spells"].append(spell_obj)
                else:
                    spells.append(spell_obj)

        for entry in spell_entries.values():
            entry["spells"].sort(key=lambda s: (_int(s["rank"], 99) if str(s["rank"]).isdigit() else 99, s["name"]))

        return {
            "name":         actor.get("name"),
            "id":           actor_id,
            "portrait":     icon_url(actor.get("img", ""), "character", allow_iconics=True),
            "level":        level,
            "size":         size_label,
            "creature_type":creature_type,
            "alignment":    alignment,
            "traits":       trait_list,
            "languages":    lang_list,
            "hp":           hp_val,  "hp_max": hp_max, "hp_temp": hp_temp,
            "ac":           ac_val,
            "perception":   perc_val,
            "speed":        speed_val, "other_speeds": other_speeds,
            "senses":       senses,
            "immunities":   immunities,
            "weaknesses":   weaknesses,
            "resistances":  resistances,
            "abilities":    abilities,
            "saves":        saves,
            "skills":       skills,
            "actions":      actions,
            "spell_entries":list(spell_entries.values()),
            "orphan_spells":spells,
            "blurb":        blurb,
            "pub_notes":    pub_notes,
            "priv_notes":   priv_notes,
            "items":        items,
        }

    def render_npc_page(self, npc: dict) -> str:
        name_esc = wiki_escape(npc['name'])
        lines = [
            f"= {name_esc} =",
            "",
            "{{CharacterInfobox",
            f"| name       = {name_esc}",
        ]
        if npc["portrait"]:
            lines.append(f"| portrait   = {npc['portrait']}")
        lines += [
            f"| level      = {npc['level']}",
            f"| size       = {npc['size']}",
        ]
        if npc.get("creature_type"):
            lines.append(f"| type       = {npc['creature_type']}")
        if npc.get("alignment"):
            lines.append(f"| alignment  = {npc['alignment']}")
        if npc.get("traits"):
            lines.append(f"| traits     = {', '.join(npc['traits'])}")
        if npc.get("languages"):
            lines.append(f"| languages  = {', '.join(str(l) for l in npc['languages'])}")
        lines += ["}}", ""]

        # ── Flavour text visible at top ────────────────────────────────────
        if npc.get("blurb"):
            lines += [f"''{npc['blurb']}''", ""]
        if npc.get("pub_notes"):
            lines += ["== Description ==", npc["pub_notes"], ""]

        # GM notes (private)
        if npc.get("priv_notes"):
            lines.append(collapsible("GM Notes (Private)", npc["priv_notes"]))

        lines += [
            "",
            f"''Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}''",
            "[[Category:NPCs]]",
        ]
        if npc.get("creature_type"):
            lines.append(f"[[Category:{npc['creature_type'].title()}]]")
        return "\n".join(lines)

    # ── Full page renderer ────────────────────────────────────────────────

    def render_character_page(self, char: dict) -> str:
        ab     = char["abilities"]
        ab_str = " | ".join(f"'''{k.upper()}''' {fmt_mod(v)}" for k, v in ab.items())
        hp_str = f"{char['hp']}/{char['hp_max']}" + (f" (+{char['hp_temp']} temp)" if char.get("hp_temp") else "")
        speed_parts = [f"{char['speed']} ft."] + [f"{sp['value']} ft. {sp['type']}" for sp in char.get("other_speeds", [])]

        char_name_esc = wiki_escape(char['name'])
        lines = [
            f"= {char_name_esc} =",
            "",
            "{{CharacterInfobox",
            f"| name        = {char_name_esc}",
        ]
        if char["portrait"]:
            lines.append(f"| portrait    = {char['portrait']}")
        lines += [
            f"| level       = {char['level']}",
            f"| size        = {char['size']}",
            f"| hp          = {hp_str}",
            f"| ac          = {char['ac']}",
            f"| perception  = {fmt_mod(char['perception'])}",
            f"| speed       = {', '.join(speed_parts)}",
            f"| fortitude   = {fmt_mod(char['fortitude'])}",
            f"| reflex      = {fmt_mod(char['reflex'])}",
            f"| will        = {fmt_mod(char['will'])}",
            f"| attributes  = {ab_str}",
        ]
        for key, tmpl in [("keyability","key_ability"),("deity","deity"),
                          ("age","age"),("gender","gender"),("pronouns","pronouns"),
                          ("hero_points","hero_points"),("focus","focus")]:
            val = char.get(key)
            if key == "keyability" and val:
                val = str(val).upper()
            if val is not None and val != "":
                lines.append(f"| {tmpl:<12} = {val}")
        if char.get("languages"):
            lines.append(f"| languages   = {', '.join(str(l) for l in char['languages'])}")
        if char.get("senses"):
            lines.append(f"| senses      = {', '.join(char['senses'])}")
        cur = char["currency"]
        coin_parts = [f"{cur[c]} {c.upper()}" for c in ("pp","gp","sp","cp") if cur[c]]
        if coin_parts:
            lines.append(f"| wealth      = {', '.join(coin_parts)}")

        # ── Combat record — last kill / last knocked unconscious ───────────
        cr    = self.combat_record().get(char["id"]) or {}
        def _fmt_cr(entry: dict | None) -> str:
            """Format a combat record entry as 'Name (YYYY-MM-DD)'."""
            if not entry or not entry.get("name"):
                return ""
            ts   = entry.get("ts", 0)
            date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
            return f"{wiki_escape(entry['name'])}" + (f" ({date})" if date else "")

        last_kill    = _fmt_cr(cr.get("last_kill"))
        last_downed  = _fmt_cr(cr.get("last_downed_by"))
        if last_kill:
            lines.append(f"| last_kill   = {last_kill}")
        if last_downed:
            lines.append(f"| last_downed = {last_downed}")

        lines += ["}}", ""]

        lines.append(self._section_details(char))

        defense_content  = self._section_defense(char)
        ability_content  = self._section_abilities(char)
        skill_content    = self._section_skills(char)
        wealth_content   = self._section_wealth(char)
        feat_content     = self._section_feats(char)
        inv_content      = self._section_inventory(char["items"])
        spell_content    = self._section_spells(char)

        lines.append(collapsible("⚔ Defense & Saves",     defense_content))
        lines.append(collapsible("📊 Ability Scores",     ability_content))
        lines.append(collapsible("🎓 Skills",             skill_content))
        lines.append(collapsible("💰 Wealth & Carry",     wealth_content))
        if feat_content:
            lines.append(collapsible("✨ Feats & Features", feat_content))
        if inv_content.strip():
            lines.append(collapsible("🎒 Inventory",        inv_content))
        if spell_content.strip():
            lines.append(collapsible("🔮 Spellcasting",     spell_content))

        lines += [
            f"''Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}''",
            "[[Category:Characters]]",
        ]
        return "\n".join(lines)

    def render_party_stash_page(self, parties: list) -> str:
        """Render the shared party stash ('party' actor items) using the same
        inventory tables as a character page."""
        lines = ["= Party Stash =", ""]
        for party in parties:
            if len(parties) > 1:
                lines.append(f"== {wiki_escape(party['name'])} ==")
            inv_content = self._section_inventory(party["items"])
            lines.append(inv_content.strip() or "* ''No items.''")
            lines.append("")
        lines += [
            f"''Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}''",
            "[[Category:Party Stash]]",
        ]
        return "\n".join(lines)

    def render_campaign_stats_page(self) -> str:
        """
        Render the cumulative 'Campaign Stats' page: a per-PC leaderboard
        (kills, times downed, damage dealt/taken, healing given, nemesis,
        favorite prey) plus collapsible chronological kill and downing logs.

        Same player-facing constraints as the rest of the wiki: only PC
        stats and already-witnessed NPC token names appear — no NPC stat
        blocks or GM-only information.
        """
        stats = self.campaign_stats()

        def _date(ts) -> str:
            return (datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                    if ts else "—")

        def _pc_link(name: str) -> str:
            name_esc = wiki_escape(name)
            return f"[[Characters/{name_esc}|{name_esc}]]"

        lines = [
            "= Campaign Stats =",
            "",
            "''Cumulative combat statistics reconstructed from the campaign's "
            "chat log. Damage totals only include damage applied through the "
            "chat log, so they undercount fights where HP was adjusted by hand.''",
            "",
            "== Leaderboard ==",
        ]

        per_pc = stats["per_pc"]
        if per_pc:
            lines.append('{| class="wikitable sortable" style="width:100%;"')
            lines.append("! Character !! Kills !! Times Downed !! Damage Dealt "
                         "!! Damage Taken !! Healing Given !! Nemesis !! Favorite Prey")
            # Kills desc, then downings asc (fewer is better), then name.
            ranked = sorted(per_pc.values(),
                            key=lambda r: (-r["kills"], r["downed"], r["name"]))
            for r in ranked:
                nemesis  = wiki_escape(r["nemesis"])  if r["nemesis"]  else "—"
                fav_prey = wiki_escape(r["fav_prey"]) if r["fav_prey"] else "—"
                lines += [
                    "|-",
                    f"| {_pc_link(r['name'])} || {r['kills']} || {r['downed']} "
                    f"|| {r['dealt']} || {r['taken']} || {r['healed']} "
                    f"|| {nemesis} || {fav_prey}",
                ]
            lines.append("|}")
        else:
            lines.append("* ''No combat activity recorded yet.''")
        lines.append("")

        kill_log = stats["kill_log"]
        if kill_log:
            rows = ['{| class="wikitable sortable" style="width:100%;"',
                    "! Date !! Character !! Slew"]
            for ev in reversed(kill_log):   # newest first
                rows += ["|-",
                         f"| {_date(ev['ts'])} || {_pc_link(ev['attacker_name'])} "
                         f"|| {wiki_escape(ev['victim_name'])}"]
            rows.append("|}")
            lines.append(collapsible(f"⚔ Kill Log ({len(kill_log)})", "\n".join(rows)))

        downing_log = stats["downing_log"]
        if downing_log:
            rows = ['{| class="wikitable sortable" style="width:100%;"',
                    "! Date !! Character !! Downed By"]
            for ev in reversed(downing_log):   # newest first
                downer = (wiki_escape(ev["attacker_name"])
                          if ev["attacker_name"] else "—")
                rows += ["|-",
                         f"| {_date(ev['ts'])} || {_pc_link(ev['victim_name'])} "
                         f"|| {downer}"]
            rows.append("|}")
            lines.append(collapsible(f"💀 Downing Log ({len(downing_log)})", "\n".join(rows)))

        lines += [
            "",
            f"''Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}''",
            "[[Category:Campaign]]",
        ]
        return "\n".join(lines)

    # ── Push / preview ────────────────────────────────────────────────────

    _SYNC_LINE_RE = re.compile(r"^''Last synced:.*?''\n?", re.MULTILINE)

    @staticmethod
    def _comparable(markup: str) -> str:
        """Strip the timestamp line so content diffs ignore it."""
        return FullExporter._SYNC_LINE_RE.sub("", markup).strip()

    # MediaWiki title-invalid characters (a subset also block page creation
    # outright: '#' truncates to a section link, '<' '>' are rejected, and
    # '[' ']' '|' '{' '}' collide with wikitext syntax if left in a title).
    _INVALID_TITLE_CHARS_RE = re.compile(r'[#<>\[\]|{}]')

    @staticmethod
    def _sanitize_page_title(name: str) -> str:
        """
        Make a Foundry actor name safe as a MediaWiki page title segment.
        Actor names are freely player-editable, so this has to handle:
          '/' — otherwise silently creates a nested subpage
          '#','<','>','[',']','|','{','}' — invalid in MediaWiki titles;
              left in, page.edit fails on every sync for that actor.
        """
        name = (name or "Unnamed").replace("/", "-")
        name = FullExporter._INVALID_TITLE_CHARS_RE.sub("", name)
        return name.strip() or "Unnamed"

    def push_to_wiki(self, site, target_name: str | None = None, npcs: bool = False):
        """Push characters or NPCs. Accepts a shared mwclient.Site instance."""
        actors = self.get_all_npcs() if npcs else self.get_all_characters()
        if target_name:
            actors = [a for a in actors if a["name"].lower() == target_name.lower()]
            if not actors:
                kind = "NPC" if npcs else "character"
                print(f"✗  No {kind} named '{target_name}' found.")
                return

        prefix  = "NPCs" if npcs else "Characters"
        pushed = skipped = errors = renamed = 0

        page_names = self._load_page_names()
        section    = page_names.setdefault(prefix, {})

        for actor in actors:
            name     = self._sanitize_page_title(actor["name"])
            actor_id = actor.get("id")
            try:
                # If this actor id was last pushed under a different name,
                # move the existing page instead of creating a duplicate.
                old_name = section.get(actor_id) if actor_id else None
                if actor_id and old_name and old_name != name:
                    old_page = site.pages[f"{prefix}/{old_name}"]
                    if old_page.exists:
                        old_page.move(f"{prefix}/{name}",
                                      reason="Auto-sync: renamed in Foundry.",
                                      no_redirect=False)
                        print(f"↪  Renamed: {old_name} → {name}")
                        renamed += 1
                    # Record the new name immediately — the move (if any) has
                    # already happened on the wiki regardless of whether the
                    # content render/edit below succeeds, so tracking must
                    # not retry the move on a later run.
                    section[actor_id] = name

                new_markup = (self.render_npc_page(actor)
                              if npcs else self.render_character_page(actor))
                page           = site.pages[f"{prefix}/{name}"]
                current_markup = page.text()

                if current_markup and (
                    self._comparable(current_markup) == self._comparable(new_markup)
                ):
                    print(f"  –  Skipped (no changes): {name}")
                    skipped += 1
                else:
                    page.edit(new_markup,
                              summary=f"Auto-sync: PF2e {'NPC' if npcs else 'character'} exporter.")
                    status = "Created" if not current_markup else "Updated"
                    print(f"✓  {status}: {name}")
                    pushed += 1

                if actor_id:
                    section[actor_id] = name

            except Exception as e:
                print(f"✗  Error pushing {name}: {e}")
                errors += 1

        self._save_page_names(page_names)

        total = pushed + skipped + errors
        print(f"\nDone — {total} processed: "
              f"{pushed} pushed, {skipped} skipped (unchanged), "
              f"{renamed} renamed, {errors} errors.")

    def push_party_stash(self, site):
        """Push the party stash page. Accepts a shared mwclient.Site instance."""
        parties = self.get_party_actors()
        if not parties:
            print("  ⚠  No party actor found — skipping party stash page.")
            return

        new_markup     = self.render_party_stash_page(parties)
        page           = site.pages["Party Stash"]
        current_markup = page.text()

        if current_markup and self._comparable(current_markup) == self._comparable(new_markup):
            print("  –  Skipped (no changes): Party Stash")
            return

        page.edit(new_markup, summary="Auto-sync: PF2e party stash exporter.")
        print(f"✓  {'Created' if not current_markup else 'Updated'}: Party Stash")

    def push_campaign_stats(self, site):
        """Push the cumulative campaign stats page. Accepts a shared mwclient.Site."""
        new_markup     = self.render_campaign_stats_page()
        page           = site.pages["Campaign Stats"]
        current_markup = page.text()

        if current_markup and self._comparable(current_markup) == self._comparable(new_markup):
            print("  –  Skipped (no changes): Campaign Stats")
            return

        page.edit(new_markup, summary="Auto-sync: PF2e campaign stats exporter.")
        print(f"✓  {'Created' if not current_markup else 'Updated'}: Campaign Stats")

    def preview(self, target_name: str | None = None, npcs: bool = False):
        actors = self.get_all_npcs() if npcs else self.get_all_characters()
        if target_name:
            actors = [a for a in actors if a["name"].lower() == target_name.lower()]
            if not actors:
                kind = "NPC" if npcs else "character"
                print(f"✗  No {kind} named '{target_name}' found.")
                return
        out_dir = Path("wiki_preview")
        out_dir.mkdir(exist_ok=True)
        for actor in actors:
            markup   = self.render_npc_page(actor) if npcs else self.render_character_page(actor)
            filename = out_dir / f"{'NPC_' if npcs else ''}{actor['name'].replace(' ','_').replace('/','_')}.wiki"
            filename.write_text(markup, encoding="utf-8")
            print(f"✓  Preview written: {filename}")

    def preview_party_stash(self):
        parties = self.get_party_actors()
        if not parties:
            print("  ⚠  No party actor found.")
            return
        out_dir  = Path("wiki_preview")
        out_dir.mkdir(exist_ok=True)
        filename = out_dir / "Party_Stash.wiki"
        filename.write_text(self.render_party_stash_page(parties), encoding="utf-8")
        print(f"✓  Preview written: {filename}")

    def preview_campaign_stats(self):
        out_dir = Path("wiki_preview")
        out_dir.mkdir(exist_ok=True)
        filename = out_dir / "Campaign_Stats.wiki"
        filename.write_text(self.render_campaign_stats_page(), encoding="utf-8")
        print(f"✓  Preview written: {filename}")

    def close(self):
        self.actors_db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════
# Session exporter
# ════════════════════════════════════════════════════════════════════════════

class SessionExporter:
    """
    Builds a wiki page for the daily session window (yesterday 04:00 → today 04:00).
    Page title: Sessions/YYYYMMDD

    No page is created if no meaningful activity (combats, kills, or loot) occurred.

    Snapshot file: session_snapshots/snapshot_latest.json
      Written after every run. The NEXT run diffs inventories to detect loot gained.
      Archive copies saved as session_snapshots/snapshot_YYYYMMDD.json.
    """

    SNAPSHOT_DIR = Path("session_snapshots")
    SESSION_HOUR = 4

    def __init__(self, world_path: str, full_exporter: "FullExporter"):
        self.world_path    = Path(world_path)
        self.full_exporter = full_exporter
        self.SNAPSHOT_DIR.mkdir(exist_ok=True)

    def _read_ingame_date(self) -> str | None:
        """
        Read the current in-game date from the Seasons and Stars calendar.

        S&S epoch-based mode (no calendar.worldTime config): worldTime=0 maps to the
        Gregorian world-creation date (pf2e.worldClock.worldCreatedOn) expressed as a
        Vux calendar date with the same year number and day-of-year position. The epoch
        offset in Vux days is:
            epoch_offset = (worldCreatedOn_year - currentYear) * days_per_year
                           + worldCreatedOn_day_of_year   [1-indexed]

        Verified empirically: core.time=-834407816, worldCreatedOn=2025-08-12
        → epoch_offset = (2025-1994)*304 + 224 = 9648
        → adjusted_days = -9658 + 9648 = -10
        → year 1993, day 294 → 21 Baruus, Cycle 1993 PE ✓
        """
        settings_path = self.world_path / "data" / "settings"
        if not settings_path.exists():
            return None

        tmp = tempfile.mkdtemp()
        tmp_path = Path(tmp) / "settings"
        world_time: int | None = None
        cal: dict | None = None
        world_created_on: str | None = None

        try:
            shutil.copytree(str(settings_path), str(tmp_path))
            db = plyvel.DB(str(tmp_path))
            with db:
                for _, raw_val in db:
                    try:
                        val = json.loads(raw_val)
                    except Exception:
                        continue
                    key = val.get("key", "")
                    if key == "core.time":
                        try:
                            world_time = int(json.loads(val["value"]))
                        except Exception:
                            pass
                    elif key == "seasons-and-stars.activeCalendarData":
                        try:
                            cal = json.loads(val["value"])
                        except Exception:
                            pass
                    elif key == "pf2e.worldClock":
                        try:
                            wc = json.loads(val["value"])
                            world_created_on = wc.get("worldCreatedOn")
                        except Exception:
                            pass
        except Exception as e:
            print(f"  ⚠  Could not read settings for in-game date: {e}")
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if world_time is None or cal is None or not world_created_on:
            return None

        try:
            from datetime import timezone as _tz
            months = cal["months"]
            days_per_year = sum(m["days"] for m in months)
            t = cal["time"]
            spd = t["hoursInDay"] * t["minutesInHour"] * t["secondsInMinute"]
            total_days = math.floor(world_time / spd)

            year_cfg = cal.get("year", {})
            current_year = year_cfg.get("currentYear", 0)

            wco = datetime.fromisoformat(world_created_on.replace("Z", "+00:00"))
            ref_year = wco.year
            ref_day = wco.timetuple().tm_yday  # 1-indexed day of Gregorian year
            epoch_offset = (ref_year - current_year) * days_per_year + ref_day

            adjusted_days = total_days + epoch_offset
            years_offset = adjusted_days // days_per_year
            day_in_year = adjusted_days % days_per_year

            display_year = current_year + years_offset

            month_name = "Unknown"
            day_num = 1
            rem = day_in_year
            for m in months:
                if rem < m["days"]:
                    month_name = m["name"]
                    day_num = rem + 1
                    break
                rem -= m["days"]

            prefix = year_cfg.get("prefix", "")
            suffix = year_cfg.get("suffix", "")
            year_str = f"{prefix} {display_year} {suffix}".strip() if (prefix or suffix) else str(display_year)

            return f"{day_num} {month_name}, {year_str}"
        except Exception as e:
            print(f"  ⚠  Could not compute in-game date: {e}")
            return None

    def session_window(self, start_date: datetime = None) -> tuple[datetime, datetime]:
        """
        Return (start, end) for the 04:00->04:00 session window.

        With no argument, returns the most recently closed window relative to
        now. Pass `start_date` (any datetime on the desired start day) to
        target a specific past window instead — e.g. start_date=June 30
        returns (June 30 04:00, July 1 04:00), matching page Sessions/20260630.
        """
        if start_date is not None:
            start = start_date.replace(hour=self.SESSION_HOUR, minute=0, second=0, microsecond=0)
            end   = start + timedelta(days=1)
            return start, end

        now = datetime.now()
        end = now.replace(hour=self.SESSION_HOUR, minute=0, second=0, microsecond=0)
        # Use hour comparison so that exactly 4:00:00 correctly closes the prior window
        if now.hour < self.SESSION_HOUR:
            end -= timedelta(days=1)
        start = end - timedelta(days=1)
        return start, end

    def _read_initiative_rolls(self, start: datetime, end: datetime) -> list[dict]:
        """
        Read the chat message log for initiative rolls within the session window.

        Foundry deletes the `Combat` document (and its combatants) once an
        encounter ends, so `data/combats` is normally empty by the time we run
        this export. Chat messages persist, and every combatant — PC or NPC —
        posts a message flagged `flags.core.initiativeRoll = true` when they
        roll initiative, so that's the only durable record of who fought.
        """
        start_ms = start.timestamp() * 1000
        end_ms   = end.timestamp()   * 1000

        rolls: list[dict] = []
        for m in self.full_exporter._load_raw_messages():
            if not (m.get("flags") or {}).get("core", {}).get("initiativeRoll"):
                continue
            ts = m.get("timestamp", 0)
            if not (start_ms <= float(ts) < end_ms):
                continue
            spk      = m.get("speaker") or {}
            actor_id = spk.get("actor", "")
            if not actor_id:
                continue
            try:
                roll_total = int(str(m.get("content", "")).strip())
            except (TypeError, ValueError):
                roll_total = None
            rolls.append({"actor_id": actor_id, "token_id": spk.get("token", ""),
                          "scene": spk.get("scene", ""), "ts": ts, "roll_total": roll_total})

        return rolls

    ENCOUNTER_GAP_MS = 30 * 60 * 1000

    @staticmethod
    def _cluster_by_gap(events: list[dict], gap_ms: float = ENCOUNTER_GAP_MS) -> list[int]:
        """
        Assign a 0-based cluster id to each event (dicts with 'ts' and
        'scene' keys, pre-sorted by ts). A new cluster starts on a scene
        change or a gap of more than gap_ms since the previous event.

        Used to bucket messages into distinct encounter instances: PF2e
        round numbers reset to 1 each encounter, and data/combats is
        unreliable (see CLAUDE.md), so encounter boundaries have to be
        inferred from message timing instead.
        """
        cluster_ids = []
        cluster_id  = -1
        prev_scene  = None
        prev_ts     = None
        for e in events:
            scene = e.get("scene")
            ts    = e.get("ts", 0)
            if (prev_scene is None or scene != prev_scene
                    or (prev_ts is not None and float(ts) - float(prev_ts) > gap_ms)):
                cluster_id += 1
            cluster_ids.append(cluster_id)
            prev_scene, prev_ts = scene, ts
        return cluster_ids

    def _build_encounter_windows(self, rolls: list[dict]) -> list[dict]:
        """
        Cluster initiative rolls into distinct encounter instances (scene
        change or >30-min gap — see _cluster_by_gap) and return one window
        per encounter, chronologically: {"start_ts": <first roll's ts>,
        "rolls": [...]}.

        This is the single source of truth for "how many distinct
        encounters happened" (len() of the result — used for num_combats)
        and "which encounter does this later event belong to" (find the
        window with the latest start_ts <= the event's ts — used by
        _compute_combat_stats to bucket damage/turn events). Both must
        agree on the same boundaries, or e.g. the "Encounters: N" count at
        the top of a session page could disagree with the number of
        per-encounter breakdowns below it.

        Each window also carries "end_ts" = its last initiative roll's ts +
        ENCOUNTER_GAP_MS, since a new encounter always starts with a fresh
        initiative roll (there's no other way to open one in PF2e). Without
        this bound, any later message (post-combat healing-drag damage, a
        hazard hit hours later, GM HP fiddling) gets bucketed into the most
        recently started encounter no matter how much later it happened.
        """
        sorted_rolls = sorted(rolls, key=lambda r: r.get("ts", 0))
        cluster_ids  = self._cluster_by_gap(sorted_rolls)
        windows: list[dict] = []
        for roll, cid in zip(sorted_rolls, cluster_ids):
            if cid == len(windows):
                windows.append({"start_ts": roll.get("ts", 0), "rolls": []})
            windows[cid]["rolls"].append(roll)
            windows[cid]["end_ts"] = roll.get("ts", 0) + self.ENCOUNTER_GAP_MS
        return windows

    def _compute_combat_stats(self, rolls: list[dict], start: datetime, end: datetime,
                               char_by_id: dict, npc_by_id: dict = None) -> dict:
        """
        Per-encounter initiative order + round count, and per-PC damage
        dealt/taken (totals and per-turn/per-round averages) for encounters
        within this session's window.

        Damage dealt is averaged per OWN TURN taken (a "turn" belongs to one
        creature, so this measures productivity when it was actually their
        turn to act — including turns where they missed, so long as some
        pf2e-flagged roll on their turn was made). Damage taken is averaged
        per ROUND of combat they were present for instead, since taking
        damage isn't turn-scoped (it can happen on anyone's turn, or from
        reactions) — the natural denominator there is rounds survived, not
        the victim's own turns.

        Encounter boundaries come from _build_encounter_windows (initiative
        rolls only, matching num_combats). Damage/turn events are bucketed
        into whichever window most recently started before their timestamp
        — NOT reclustered from their own timing, since round numbers reset
        to 1 each encounter and reclustering on denser combat-message
        timing (rather than deferring to the roll-based boundaries) merges
        distinct back-to-back encounters on the same scene into one blob.
        """
        start_ms = start.timestamp() * 1000
        end_ms   = end.timestamp()   * 1000
        fe       = self.full_exporter

        windows = self._build_encounter_windows(rolls)
        window_starts = [w["start_ts"] for w in windows]

        def _window_for_ts(ts: float):
            idx = bisect.bisect_right(window_starts, ts) - 1
            if idx < 0:
                return None
            if float(ts) > windows[idx]["end_ts"]:
                return None
            return idx

        max_round: list[int] = [0] * len(windows)
        pc_stats: dict[str, dict] = {}
        pc_encounters: dict[str, set] = {}

        def _rec(actor_id: str) -> dict:
            return pc_stats.setdefault(actor_id, {"dealt": 0, "taken": 0, "own_turns": set()})

        for roll in rolls:
            if roll["actor_id"] not in char_by_id:
                continue
            widx = _window_for_ts(roll.get("ts", 0))
            if widx is not None:
                pc_encounters.setdefault(roll["actor_id"], set()).add(widx)

        for m in fe._load_raw_messages():
            ts = m.get("timestamp", 0)
            if not (start_ms <= float(ts) < end_ms):
                continue
            widx = _window_for_ts(ts)
            if widx is None:
                continue

            pf   = (m.get("flags") or {}).get("pf2e") or {}
            ctx  = pf.get("context") or {}
            opts = ctx.get("options") or []

            round_n = turn_n = None
            for o in opts:
                if o.startswith("encounter:round:"):
                    round_n = _int(o.split(":")[-1])
                elif o.startswith("encounter:turn:"):
                    turn_n = _int(o.split(":")[-1])

            # Turn presence: this message's own actor is acting on their turn.
            if "self:participant:own-turn" in opts and round_n is not None and turn_n is not None:
                actor_id = ctx.get("actor") or (m.get("speaker") or {}).get("actor", "")
                if actor_id in char_by_id:
                    _rec(actor_id)["own_turns"].add((widx, round_n, turn_n))

            # Damage dealt/taken (non-healing only).
            applied = pf.get("appliedDamage")
            if isinstance(applied, dict) and not applied.get("isHealing"):
                amount = sum(abs(_int(u.get("value", 0))) for u in (applied.get("updates") or [])
                             if u.get("path") == "system.attributes.hp.value")
                if amount > 0:
                    if round_n:
                        max_round[widx] = max(max_round[widx], round_n)
                    victim_id = fe._uuid_to_actor_id(applied.get("uuid", ""))
                    if not victim_id:
                        victim_id = (m.get("speaker") or {}).get("actor", "")
                    origin = pf.get("origin") or {}
                    atk_id = fe._uuid_to_actor_id(origin.get("actor", "") if isinstance(origin, dict) else "")
                    if victim_id in char_by_id:
                        _rec(victim_id)["taken"] += amount
                    if atk_id and atk_id in char_by_id and atk_id != victim_id:
                        _rec(atk_id)["dealt"] += amount

        per_char = {}
        for actor_id in set(pc_stats) | set(pc_encounters):
            name = char_by_id.get(actor_id)
            if not name:
                continue
            r        = pc_stats.get(actor_id, {"dealt": 0, "taken": 0, "own_turns": set()})
            n_turns  = len(r["own_turns"])
            n_rounds = sum(max_round[widx] for widx in pc_encounters.get(actor_id, ()))
            if not r["dealt"] and not r["taken"]:
                continue
            per_char[name] = {
                "total_dealt":         r["dealt"],
                "total_taken":         r["taken"],
                "avg_dealt_per_turn":  (r["dealt"] / n_turns)  if n_turns  else None,
                "avg_taken_per_round": (r["taken"] / n_rounds) if n_rounds else None,
            }

        npc_by_id = npc_by_id or {}

        def _actor_name(actor_id: str) -> str:
            if actor_id in char_by_id:
                return char_by_id[actor_id]
            npc = npc_by_id.get(actor_id)
            return npc["name"] if npc else actor_id

        encounter_orders = []
        for i, win in enumerate(windows):
            # Dedupe to each COMBATANT's first roll in this encounter (some
            # tables reroll initiative for arriving reinforcements/new
            # rounds — the seating order that actually ran the fight is
            # each combatant's earliest roll, not every reroll). Key by
            # token id, not actor id: multiple simultaneous NPC tokens of
            # the same monster type (e.g. two Crystal Claws) share one base
            # Actor id, so deduping by actor id would wrongly collapse them
            # into a single initiative entry.
            first_roll_by_token: dict[str, dict] = {}
            for r in sorted(win["rolls"], key=lambda r: r.get("ts", 0)):
                if r.get("roll_total") is None:
                    continue
                token_id = r.get("token_id") or r["actor_id"]
                first_roll_by_token.setdefault(token_id, r)
            order = sorted(
                ({"name": _actor_name(r["actor_id"]), "roll_total": r["roll_total"]}
                 for r in first_roll_by_token.values()),
                key=lambda r: -r["roll_total"]
            )
            encounter_orders.append({"encounter_num": i + 1, "rounds": max_round[i], "order": order})

        return {"per_char": per_char, "encounters": encounter_orders}

    def _extract_session_data(self, rolls: list[dict], all_chars: list, all_npcs: list,
                               start: datetime = None, end: datetime = None) -> dict:
        char_by_id = {c["id"]: c["name"] for c in all_chars if c.get("id")}
        npc_by_id  = {n["id"]: n for n in all_npcs if n.get("id")}

        characters_present: set[str]   = set()
        enemies_encountered: list[dict] = []
        seen_tokens:          set[str] = set()

        # Cluster initiative rolls into distinct encounters: a new encounter
        # starts when the scene changes or when there's a >30-minute gap
        # since the last roll. data/combats is unreliable (see CLAUDE.md), and
        # counting distinct scenes alone collapses two separate fights on the
        # same map into one. Same source as _compute_combat_stats's encounter
        # breakdown, so the two always agree.
        num_combats = len(self._build_encounter_windows(rolls))

        for roll in rolls:
            actor_id = roll["actor_id"]
            token_id = roll.get("token_id") or actor_id

            if actor_id in char_by_id:
                characters_present.add(char_by_id[actor_id])
            elif actor_id in npc_by_id and token_id not in seen_tokens:
                seen_tokens.add(token_id)
                npc = npc_by_id[actor_id]
                enemies_encountered.append({"id": actor_id, "token_id": token_id,
                                             "name": npc["name"],
                                             "img": npc.get("portrait", ""), "killed": False})

        # Enemies killed: token-level ActorDelta HP<=0 kills within the
        # session window (see _build_npc_kill_events — condition cards don't
        # exist for NPCs, so downing events can't be used here).
        killed_token_ids: set[str] = set()
        if start is not None and end is not None:
            start_ms = start.timestamp() * 1000
            end_ms   = end.timestamp()   * 1000
            for ev in self.full_exporter._build_npc_kill_events():
                vic_token_id = ev.get("victim_id", "")
                ts           = ev.get("ts", 0)
                if vic_token_id in seen_tokens and start_ms <= float(ts) < end_ms:
                    killed_token_ids.add(vic_token_id)

        for enemy in enemies_encountered:
            if enemy["token_id"] in killed_token_ids:
                enemy["killed"] = True

        return {
            "characters_present":  sorted(characters_present),
            "enemies_encountered": enemies_encountered,
            "enemies_killed":      len(killed_token_ids),
            "num_combats":         num_combats,
        }

    def _snapshot_path(self, label: str = "latest") -> Path:
        return self.SNAPSHOT_DIR / f"snapshot_{label}.json"

    def _baseline_snapshot_path(self, before_date_str: str) -> Path | None:
        """
        Most recent dated snapshot strictly before `before_date_str`
        (YYYYMMDD) — the correct diff baseline for both a same-day
        `--session` rerun (must not diff against a snapshot an earlier run
        already saved *today*) and a `--session-date` rebuild (must not diff
        against today's snapshot_latest.json, which has nothing to do with
        the historical window being rebuilt). Falls back to
        snapshot_latest.json only when no dated snapshots exist yet (very
        first run, before any dated archive has been written).
        """
        dated = sorted(
            f for f in self.SNAPSHOT_DIR.glob("snapshot_????????.json")
            if f.stem.split("_", 1)[1] < before_date_str
        )
        if dated:
            return dated[-1]
        legacy = self._snapshot_path("latest")
        return legacy if legacy.exists() else None

    def _save_snapshot(self, all_chars: list, date_label: str):
        PHYSICAL = {"weapon","armor","shield","consumable","ammo",
                    "equipment","treasure","backpack","kit"}
        snapshot = {}
        for char in all_chars:
            items = {}
            for item in char.get("items", []):
                if item.get("type") not in PHYSICAL:
                    continue
                iid  = item.get("_id", "")
                sys  = item.get("system") or {}
                qty  = _int(sys.get("quantity", 1) if isinstance(sys, dict) else 1)
                items[iid] = {
                    "name": item.get("name", ""),
                    "type": item.get("type", ""),
                    "qty":  qty,
                    "img":  item.get("img", ""),
                }
            snapshot[char["id"]] = {"name": char["name"], "items": items,
                                     "level": char.get("level"), "xp": char.get("xp")}

        data    = {"timestamp": datetime.now().isoformat(), "characters": snapshot}
        payload = json.dumps(data, indent=2)
        self._snapshot_path("latest").write_text(payload, encoding="utf-8")
        self._snapshot_path(date_label).write_text(payload, encoding="utf-8")
        print(f"  Snapshot saved → {self._snapshot_path(date_label)}")

        # Prune dated snapshots older than 30 days
        cutoff = datetime.now().timestamp() - 30 * 86400
        for f in self.SNAPSHOT_DIR.glob("snapshot_????????.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def _compute_loot(self, all_chars: list, party_ids: frozenset = frozenset(),
                       snap_file: Path | None = None) -> tuple[list[dict], list[dict]]:
        """Return (gained, removed) item lists relative to snap_file (defaults to the latest snapshot)."""
        if snap_file is None:
            snap_file = self._snapshot_path("latest")
        if not snap_file.exists():
            print("  ⚠  No previous snapshot — loot diff unavailable for first run.")
            return [], []

        try:
            prev = json.loads(snap_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠  Could not read snapshot: {e}")
            return [], []

        prev_chars = prev.get("characters", {})
        PHYSICAL   = {"weapon","armor","shield","consumable","ammo",
                      "equipment","treasure","backpack","kit"}
        gained  = []
        removed = []

        for char in all_chars:
            char_id    = char["id"]
            char_name  = char["name"]
            is_party   = char_id in party_ids
            prev_items = prev_chars.get(char_id, {}).get("items", {})
            curr_ids   = set()

            for item in char.get("items", []):
                if item.get("type") not in PHYSICAL:
                    continue
                iid  = item.get("_id", "")
                sys  = item.get("system") or {}
                qty  = _int(sys.get("quantity", 1) if isinstance(sys, dict) else 1)
                name = item.get("name", "Unknown")
                img  = icon_url(item.get("img", ""), item.get("type", ""))
                curr_ids.add(iid)

                if iid not in prev_items:
                    gained.append({"char": char_name, "name": name, "qty": qty, "img": img,
                                   "type": item.get("type", ""), "is_party": is_party})
                else:
                    delta = qty - prev_items[iid].get("qty", qty)
                    if delta > 0:
                        gained.append({"char": char_name, "name": name, "qty": delta, "img": img,
                                       "type": item.get("type", ""), "is_party": is_party})
                    elif delta < 0:
                        removed.append({"char": char_name, "name": name, "qty": -delta, "img": img,
                                        "type": item.get("type", ""), "is_party": is_party})

            # Items in snapshot that are entirely gone from current inventory
            for iid, prev_item in prev_items.items():
                if iid not in curr_ids:
                    removed.append({"char": char_name, "name": prev_item.get("name", "Unknown"),
                                    "qty": prev_item.get("qty", 1), "img": "", "type": "",
                                    "is_party": is_party})

        return gained, removed

    def _compute_xp(self, all_chars: list, characters_present: list,
                     snap_file: Path | None = None) -> list[dict]:
        """
        Return XP gained this session for each PC in characters_present,
        relative to snap_file (defaults to the latest snapshot).

        PF2e resets a character's xp field to a small remainder after a
        level-up, so a raw xp delta would go negative across a level-up
        mid-session. Track total progress as level*1000 + xp instead (PF2e's
        standard track is 1000 xp per level) so the delta stays correct
        regardless of how many level-ups happened in between.
        """
        if snap_file is None:
            snap_file = self._snapshot_path("latest")
        if not snap_file.exists():
            return []

        try:
            prev = json.loads(snap_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠  Could not read snapshot: {e}")
            return []

        prev_chars = prev.get("characters", {})
        present    = set(characters_present)
        results    = []

        for char in all_chars:
            if char["name"] not in present:
                continue
            level, xp = char.get("level"), char.get("xp")
            if level is None or xp is None or not str(xp).strip():
                continue

            prev_entry = prev_chars.get(char["id"])
            if (not prev_entry or prev_entry.get("level") is None
                    or prev_entry.get("xp") is None
                    or not str(prev_entry.get("xp")).strip()):
                continue

            start_level, start_xp = _int(prev_entry["level"]), _int(prev_entry["xp"])
            end_level,   end_xp   = _int(level), _int(xp)
            gained = (end_level * 1000 + end_xp) - (start_level * 1000 + start_xp)
            if gained == 0:
                continue

            results.append({
                "name": char["name"],
                "start_level": start_level, "start_xp": start_xp,
                "end_level":   end_level,   "end_xp":   end_xp,
                "gained":      gained,
            })

        return sorted(results, key=lambda r: r["name"])

    def render_session_page(self, date_str: str, session_data: dict,
                            loot: list, start: datetime, end: datetime,
                            removed: list = None,
                            ingame_date: str = None,
                            xp_data: list = None,
                            combat_stats: dict = None) -> str:
        lines = [
            f"= Session: {start.strftime('%B %d, %Y')} =",
            "",
        ]
        if ingame_date:
            lines += [f"'''In-game date:''' {ingame_date}", ""]
        lines += [
            f"''Window: {start.strftime('%Y-%m-%d %H:%M')} → "
            f"{end.strftime('%Y-%m-%d %H:%M')} | "
            f"Encounters: {session_data['num_combats']}''",
            "",
        ]

        lines.append("== Characters Present ==")
        if session_data["characters_present"]:
            for name in session_data["characters_present"]:
                name_esc = wiki_escape(name)
                lines.append(f"* [[Characters/{name_esc}|{name_esc}]]")
        else:
            lines.append("* ''No player characters recorded in combat this session.''")
        lines.append("")

        lines.append("== XP Gained ==")
        if xp_data:
            lines.append('{| class="wikitable sortable" style="width:100%;"')
            lines.append("! Character !! Start !! End !! Gained")
            for entry in xp_data:
                start_s  = f"Level {entry['start_level']} ({entry['start_xp']} xp)"
                end_s    = f"Level {entry['end_level']} ({entry['end_xp']} xp)"
                name_esc = wiki_escape(entry['name'])
                lines += [
                    "|-",
                    f"| [[Characters/{name_esc}|{name_esc}]] || {start_s} || {end_s} "
                    f"|| +{entry['gained']}",
                ]
            lines.append("|}")
            n_present = len(session_data["characters_present"])
            if n_present:
                total   = sum(e["gained"] for e in xp_data)
                average = total / n_present
                lines.append("")
                lines.append(f"''Average XP this session (total gained ÷ characters present): "
                             f"{average:.1f}''")
        else:
            lines.append("* ''No XP change recorded this session.''")
        lines.append("")

        lines.append("== Enemies Encountered ==")
        if session_data["enemies_encountered"]:
            n_encountered = len(session_data["enemies_encountered"])
            n_killed      = session_data["enemies_killed"]
            lines.append(f"''{n_killed} of {n_encountered} enem{'y' if n_encountered == 1 else 'ies'} killed this session.''")
            lines.append("")
            lines.append('{| class="wikitable" style="width:100%;"')
            lines.append("! !! Enemy !! Killed")
            grouped: dict[str, dict] = {}
            for enemy in session_data["enemies_encountered"]:
                g = grouped.setdefault(enemy["name"], {"img": enemy.get("img", ""),
                                                         "count": 0, "killed": 0})
                g["count"] += 1
                if enemy.get("killed"):
                    g["killed"] += 1
            for name, g in sorted(grouped.items()):
                icon_s = wiki_img(g["img"], 24, name) if g["img"] else ""
                name_esc = wiki_escape(name)
                name_s = name_esc if g["count"] == 1 else f"{name_esc} x{g['count']}"
                lines += ["|-", f"| {icon_s} || {name_s} || {g['killed']}/{g['count']}"]
            lines.append("|}")
        else:
            lines.append("* ''No enemies encountered this session.''")
        lines.append("")

        combat_stats = combat_stats or {}
        lines.append("== Combat Stats ==")
        encounters = combat_stats.get("encounters") or []
        if encounters:
            for enc in encounters:
                round_s = f"{enc['rounds']} round{'s' if enc['rounds'] != 1 else ''}" if enc["rounds"] else "round count unknown"
                lines.append(f"'''Encounter {enc['encounter_num']}''' ({round_s})")
                if enc["order"]:
                    order_s = ", ".join(
                        f"{wiki_escape(r['name'])} ({r['roll_total']})"
                        for r in enc["order"]
                    )
                    lines.append(f": Initiative order: {order_s}")
                lines.append("")

        per_char = combat_stats.get("per_char") or {}
        if per_char:
            lines.append('{| class="wikitable sortable" style="width:100%;"')
            lines.append("! Character !! Total Dealt !! Total Taken !! Avg Dealt/Turn !! Avg Taken/Round")
            for name in sorted(per_char):
                c = per_char[name]
                name_esc = wiki_escape(name)
                dealt_avg = f"{c['avg_dealt_per_turn']:.1f}"  if c["avg_dealt_per_turn"]  is not None else "—"
                taken_avg = f"{c['avg_taken_per_round']:.1f}" if c["avg_taken_per_round"] is not None else "—"
                lines += [
                    "|-",
                    f"| [[Characters/{name_esc}|{name_esc}]] || {c['total_dealt']} || {c['total_taken']} "
                    f"|| {dealt_avg} || {taken_avg}",
                ]
            lines.append("|}")
        elif not encounters:
            lines.append("* ''No combat data recorded this session.''")
        lines.append("")

        def _char_cell(entry: dict) -> str:
            name_esc = wiki_escape(entry["char"])
            if entry.get("is_party"):
                return name_esc
            return f"[[Characters/{name_esc}|{name_esc}]]"

        lines.append("== Loot Gained ==")
        if loot:
            lines.append('{| class="wikitable sortable" style="width:100%;"')
            lines.append("! !! Item !! Qty !! Character")
            for entry in sorted(loot, key=lambda x: (x["char"], x["name"])):
                icon_s = wiki_img(entry["img"], 24, entry["name"]) if entry.get("img") else ""
                lines += [
                    "|-",
                    f"| {icon_s} || {wiki_escape(entry['name'])} || {entry['qty']} "
                    f"|| {_char_cell(entry)}",
                ]
            lines.append("|}")
        else:
            lines.append("* ''No new items recorded this session.''")
        lines.append("")

        if removed:
            lines.append("== Items Spent / Removed ==")
            lines.append('{| class="wikitable sortable" style="width:100%;"')
            lines.append("! !! Item !! Qty !! Character")
            for entry in sorted(removed, key=lambda x: (x["char"], x["name"])):
                icon_s = wiki_img(entry["img"], 24, entry["name"]) if entry.get("img") else ""
                lines += [
                    "|-",
                    f"| {icon_s} || {wiki_escape(entry['name'])} || {entry['qty']} "
                    f"|| {_char_cell(entry)}",
                ]
            lines.append("|}")
            lines.append("")

        lines += [
            f"''Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}''",
            "[[Category:Sessions]]",
            f"[[Category:Sessions {start.strftime('%Y')}]]",
        ]
        return "\n".join(lines)

    @staticmethod
    def _comparable(markup: str) -> str:
        return re.sub(r"^''Generated:.*?''\n?", "", markup, flags=re.MULTILINE).strip()

    def run(self, site, all_chars: list, start_date: datetime = None):
        start, end = self.session_window(start_date)
        date_str   = start.strftime("%Y%m%d")

        print(f"\nSession window: {start.strftime('%Y-%m-%d %H:%M')} → "
              f"{end.strftime('%Y-%m-%d %H:%M')} (page: Sessions/{date_str})")

        rolls = self._read_initiative_rolls(start, end)
        print(f"  Initiative rolls in window: {len(rolls)}")

        session_data = self._extract_session_data(rolls, all_chars, self.full_exporter.get_all_npcs(),
                                                    start, end)
        print(f"  Characters present:  {len(session_data['characters_present'])}")
        print(f"  Enemies encountered: {len(session_data['enemies_encountered'])} "
              f"({session_data['enemies_killed']} killed)")

        # Party stash ('party' actor) holds shared loot not on any one character —
        # include it in inventory tracking alongside PCs.
        party_actors = self.full_exporter.get_party_actors()
        party_ids    = frozenset(p["id"] for p in party_actors)
        inventory_entities = all_chars + party_actors

        # Diff against the most recent dated snapshot strictly before this
        # window's date — never "latest", which a same-day rerun or a
        # --session-date rebuild of a past window could point at a snapshot
        # that has nothing to do with the state actually being diffed.
        baseline_path = self._baseline_snapshot_path(date_str)
        loot, removed = self._compute_loot(inventory_entities, party_ids, snap_file=baseline_path)
        print(f"  Loot items gained:  {len(loot)}")
        print(f"  Items removed:      {len(removed)}")

        xp_data = self._compute_xp(all_chars, session_data["characters_present"], snap_file=baseline_path)
        print(f"  XP changes:         {len(xp_data)}")

        char_by_id    = {c["id"]: c["name"] for c in all_chars if c.get("id")}
        npc_by_id     = {n["id"]: n for n in self.full_exporter.get_all_npcs() if n.get("id")}
        combat_stats  = self._compute_combat_stats(rolls, start, end, char_by_id, npc_by_id)
        print(f"  Combat stats:       {len(combat_stats['per_char'])} character(s), "
              f"{len(combat_stats['encounters'])} encounter(s) with initiative data")

        # ── Skip page creation if no meaningful activity occurred ──────────
        has_activity = (
            len(session_data["characters_present"])  > 0 or
            len(session_data["enemies_encountered"])  > 0 or
            len(loot)                                 > 0 or
            len(xp_data)                              > 0
        )
        # A --session-date rebuild targets a past window; the world's CURRENT
        # inventory/in-game-date have nothing to do with that day. Saving a
        # snapshot here would mislabel today's state as date_str's, clobbering
        # the one archival artifact that could make a future rebuild of that
        # day correct (and re-baselining snapshot_latest against today's
        # state right after diffing against it above). Skip both for rebuilds.
        is_rebuild = start_date is not None

        if not has_activity:
            print(f"  –  No activity detected — skipping session page for {date_str}.")
            if not is_rebuild:
                # Still save snapshot so next run has an accurate baseline
                self._save_snapshot(inventory_entities, date_str)
            return

        if is_rebuild:
            print(f"  –  Rebuild of a past window — snapshot not re-saved.")

        ingame_date = None if is_rebuild else self._read_ingame_date()
        if ingame_date:
            print(f"  In-game date:       {ingame_date}")

        markup     = self.render_session_page(date_str, session_data, loot, start, end, removed,
                                              ingame_date, xp_data, combat_stats)
        page_title = f"Sessions/{date_str}"
        page       = site.pages[page_title]
        current    = page.text()

        if current and self._comparable(current) == self._comparable(markup):
            print(f"  –  Session page unchanged: {page_title}")
        else:
            # page.edit can raise (network/auth/maxlag) — save the snapshot
            # only once the write is known to have gone through, so a failed
            # push doesn't silently advance the baseline and permanently
            # lose this session's loot/XP diff.
            page.edit(markup, summary=f"Auto-sync: Session log {date_str}")
            action = "Updated" if current else "Created"
            print(f"  ✓  {action}: {page_title}")

        if not is_rebuild:
            # Save snapshot for next run's diff, now that the push succeeded
            # (or the page was already up to date).
            self._save_snapshot(inventory_entities, date_str)


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Export PF2e characters, NPCs, and session logs to MediaWiki",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Preview all PCs:                python full-exporter.py
  Push all PCs:                   python full-exporter.py --push
  Preview one PC:                 python full-exporter.py --char "Seraphina Voss"
  Push one PC:                    python full-exporter.py --char "Seraphina Voss" --push
  Preview all NPCs:               python full-exporter.py --npcs
  Push all NPCs:                  python full-exporter.py --npcs --push
  Push NPCs + session:            python full-exporter.py --npcs --push --session
  Push PCs + session:             python full-exporter.py --push --session
  Push PCs + NPCs + session:      python full-exporter.py --push --npcs --session
  Push campaign stats page:       python full-exporter.py --push --stats
  Debug raw data:                 python full-exporter.py --char "Name" --debug
""")
    parser.add_argument("--push",          action="store_true",
                        help="Push to wiki (default: preview only)")
    parser.add_argument("--char",          metavar="NAME",
                        help="Single actor by name")
    parser.add_argument("--npcs",          action="store_true",
                        help="Include NPC export in this run")
    parser.add_argument("--party",         action="store_true",
                        help="Include the party stash export in this run")
    parser.add_argument("--stats",         action="store_true",
                        help="Include the cumulative campaign stats page in this run")
    parser.add_argument("--session",       action="store_true",
                        help="Build and push today's session log (4am window)")
    parser.add_argument("--session-date",  metavar="YYYY-MM-DD",
                        help="Rebuild a specific past session window instead of today's "
                             "(the window's START date, e.g. --session-date 2026-06-30 "
                             "rebuilds June 30 04:00 -> July 1 04:00, page Sessions/20260630)")
    parser.add_argument("--debug",         action="store_true",
                        help="Dump raw system data for --char")
    parser.add_argument("--world",         default=WORLD_PATH,
                        help="Foundry world path")
    parser.add_argument("--data",          default=FOUNDRY_DATA,
                        help="Foundry data root (for compendium packs)")
    parser.add_argument("--no-compendium", action="store_true",
                        help="Skip compendium enrichment (faster)")
    args = parser.parse_args()

    exporter = FullExporter(args.world,
                            data_root=("" if args.no_compendium else args.data))
    try:
        if args.debug:
            if not args.char:
                print("--debug requires --char NAME")
                sys.exit(1)
            exporter.debug_dump(args.char)

        elif args.push:
            # Single login — shared across all push operations this run
            site = make_site()

            # Always push PCs (filtered to --char if specified)
            print("\n── Player Characters ──────────────────────────────────")
            exporter.push_to_wiki(site, target_name=args.char, npcs=False)

            # Optionally also push NPCs in the same run
            if args.npcs:
                print("\n── NPCs ────────────────────────────────────────────")
                exporter.push_to_wiki(site, target_name=None, npcs=True)

            # Optionally push the party stash
            if args.party:
                print("\n── Party Stash ─────────────────────────────────────")
                exporter.push_party_stash(site)

            # Optionally push the cumulative campaign stats page
            if args.stats:
                print("\n── Campaign Stats ──────────────────────────────────")
                exporter.push_campaign_stats(site)

            # Optionally push session log
            if args.session:
                session_start = None
                if args.session_date:
                    try:
                        session_start = datetime.strptime(args.session_date, "%Y-%m-%d")
                    except ValueError:
                        print(f"--session-date must be YYYY-MM-DD, got: {args.session_date}")
                        sys.exit(1)
                all_chars = exporter.get_all_characters()
                sess = SessionExporter(args.world, exporter)
                sess.run(site, all_chars, session_start)

        else:
            # Preview mode
            if args.session:
                print("Note: --session only runs in --push mode. Previewing instead.")
            exporter.preview(target_name=args.char, npcs=args.npcs)
            if args.party:
                exporter.preview_party_stash()
            if args.stats:
                exporter.preview_campaign_stats()

    finally:
        exporter.close()