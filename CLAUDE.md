# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python script (`full-export.py`) that reads a FoundryVTT PF2e world's LevelDB databases, computes character stats via a custom math engine, and pushes formatted MediaWiki pages to a private wiki.

## Running tests

```bash
# Run full test suite against temporary-title world data
python test_export.py

# Run with rendered wiki markup printed per actor
python test_export.py --verbose

# Test a single character
python test_export.py --char "Name"

# Also test NPC parsing/rendering
python test_export.py --npcs
```

`test_export.py` loads `full-export.py` via `importlib.util` (the hyphen prevents normal import). It stubs out `mwclient` so no wiki connection is needed. It uses `temporary-title/` as its local world fixture (synced from the ChaosMarine server via `sync_foundry_world.sh`) and sets `FOUNDRY_DATA=""` to skip compendium enrichment. Tests cover field presence, sanity ranges on stats, and that `render_character_page` / `render_npc_page` / `_section_wealth` produce valid markup.

## Running the script

```bash
# Preview all PCs (writes .wiki files to wiki_preview/)
python full-export.py

# Preview a single character
python full-export.py --char "Name"

# Push all PCs to wiki
python full-export.py --push

# Push one PC
python full-export.py --char "Name" --push

# Push PCs + NPCs + today's session log
python full-export.py --push --npcs --session

# Push the cumulative campaign stats leaderboard page (works in preview mode too)
python full-export.py --push --stats

# Dump raw Foundry JSON for debugging stat issues
python full-export.py --char "Name" --debug

# Skip compendium enrichment (faster, no item descriptions/icons)
python full-export.py --no-compendium
```

Dependencies: `plyvel`, `mwclient` (install via pip). Requires access to the Foundry LevelDB files on disk.

Wiki password is read from `wiki_password.txt` (plain text, in the same directory as `full-export.py`, gitignored — `chmod 600` it) if present, otherwise falls back to the `WIKI_PASSWORD` environment variable. `make_site()` raises loudly if neither is set — there is no hardcoded fallback. Preview/non-push runs don't need it.

## Architecture

### Data flow

1. **LevelDB read** — `FullExporter.__init__` copies the actors LevelDB to a temp dir (plyvel requires exclusive lock), then opens it. `build_compendium_index` does the same for each compendium pack.
2. **Actor parsing** — `_parse_character` / `_parse_npc` extract raw JSON from the DB. PC parsing runs the full math engine; NPCs use pre-computed values stored directly.
3. **Compendium enrichment** — `enrich_item` fills missing icons/descriptions/traits from the compendium index, keyed by normalized name (`norm_name`).
4. **Stat computation** (PCs only) — ability scores, saves, skills, AC, and perception are all reconstructed from first principles because Foundry doesn't always store pre-computed values.
5. **Wiki rendering** — `render_character_page` / `render_npc_page` produce MediaWiki markup using `{{CharacterInfobox}}`, `mw-collapsible` divs, and wikitables.
6. **Push** — `push_to_wiki` skips pages where content is unchanged (ignoring the timestamp line via `_comparable`).

### Stat math engine (PCs)

The most complex part. PF2e stores ability scores differently across Foundry versions (pre-computed mods, raw scores, or absent entirely). The resolution order in `_get_ability_mod`:
1. `system.abilities.<slug>.mod` — pre-computed modifier
2. `system.abilities.<slug>.value` > 6 — treat as ability score, convert to mod
3. Rebuild from boost/flaw slots across `build.attributes` (free-pick tiers) and ancestry/background/class/heritage items

**The `> 6` heuristic**: Foundry stores either a raw ability score (8–18+) or a pre-computed modifier (-5 to +5). Since modifiers are never > 5, a value > 6 is unambiguously a raw score. A value ≤ 6 is treated as a modifier (a raw score of 6 or below is indistinguishable from a modifier in older Foundry versions). This is the least-bad disambiguation across Foundry schema versions.

Proficiency ranks follow a similar cascade in `_save_rank` / `_perception_rank` / `_armor_prof_rank`:
1. Pre-computed rank on actor
2. `ActiveEffectLike` rules on class/class-feature items (handles level-gated upgrades like "Expert Fortitude at level 3") via `_rules_ranks`
3. Static fields on the class item
4. Hard fallbacks (all classes get Trained unarmored, all get Trained perception)

`_rank_from_node`: a `value` key on a skill/save node is a **total modifier**, not a rank — only `rank` key or plain-int nodes are ranks.

### Item prices

Item price is stored in `system.price.value` (a dict of `pp/gp/sp/cp`) and `system.price.per` (the quantity that price covers — e.g. arrows have a price per 10). Always divide by `per` and multiply by `quantity` to get total value.

### NPC rendering

`render_npc_page` intentionally omits the NPC stat block. The wiki is player-facing, and players can read it during a session — publishing an NPC's defenses, ability scores, skills, actions, and spells would let them metagame (e.g. reading a monster's AC/resistances/attacks before or during a fight). `_parse_npc` still computes the full stat model (abilities, saves, skills, AC, HP, perception, speed, senses, IWR, actions, spellcasting, inventory) since it's needed internally (e.g. `combat_record()`, session enemy tracking), but `render_npc_page` only emits name, portrait, level, size, type, alignment, traits, languages, and flavor text (`blurb`/`pub_notes`). `priv_notes` renders as a collapsible "GM Notes (Private)" section — collapsed by default but still visible to anyone with wiki access, so treat it as GM-eyes-only by convention, not by access control. Do not "complete" this rendering to add the missing stat block unless the wiki gains a real permissions/visibility mechanism to keep it GM-only.

### Combat record

`combat_record()` returns, per actor id, `{"last_kill": {...} | None, "last_downed_by": {...} | None}`. It merges two independently-built, chronologically-sorted event lists and lets the latest timestamp win per actor:

- **`_build_downing_events()`** — PC downing events. `hit_history` (per-victim list of `(ts, attacker_id, attacker_name)`) is built from two sources: structured `appliedDamage` flags (when the "Apply Damage" button is used) and `context.target` on any non-healing roll message (catches manual HP edits/drags that never generate an appliedDamage flag). A downing is then detected from **two independent signals**, since not every downing produces a public chat message:
  - Plain "condition card" messages (no `pf2e` flags) whose `<span class="name">` tags include Dying/Unconscious. Match against the extracted condition *names* only, never the raw HTML blob — condition tooltips carry full rules text (e.g. the Wounded condition's tooltip prose mentions "unconscious"), and a raw substring search on the whole blob false-positives on that. This bug shipped for a while and corrupted `last_downed_by` on at least one character page before being caught.
  - Any `pf2e`-flagged message whose `context.options` includes a `self:condition:dying`/`self:condition:unconscious` tag (actor resolved via `context.actor`, falling back to `speaker.actor` since `context.actor` is often unset on healing-received messages). This catches downings that never got a broadcast condition card at all — e.g. a PC can go Dying from an AoE spell mid-combat, get healed a few seconds later with no card ever posted, and the only surviving evidence is the "current conditions" snapshot embedded in that healing roll's `context.options`.
  Each signal timestamp gets attributed via the most recent `hit_history` entry at or before it.

- **`_build_npc_kill_events()`** — NPC kills, merged from **two** sources, because neither sees every death. NPCs never get a Dying/Unconscious condition card (that's a PC-only mechanic — confirmed by exhaustively scanning a full world's message log for any Dead/Dying/Unconscious card or "dead" token status effect tied to an NPC, and finding none), so death has to be inferred:

  1. **Replayed applied-damage messages** (`_build_npc_damage_kill_events()`, the primary source). A `damage-taken` chat message carries the victim's **pre**-damage HP in `flags.pf2e.appliedDamage.updates[].value` — that field exists so the "Revert Damage" button can restore it, and is emphatically **not** the post-damage HP (a token killed from full HP 8 records `value: 8`) — while the damage amount appears only in the rendered `content` HTML ("Pirate Goblin takes 11 damage."), parsed with `_DAMAGE_TAKEN_RE`. Subtract one from the other and `<= 0` means that blow killed the token. Skip `isHealing` and `isReverted` (the GM undid it). NPCs carrying shields would break the arithmetic (`appliedDamage.shield` non-null), but no NPC in the reference world has one.
  2. **Per-token `ActorDelta` HP** in the scenes LevelDB (`!scenes.tokens.delta!<sceneId>.<tokenId>.<deltaId>`), which overrides HP for that specific unlinked-token instance independently of the shared base Actor. Delta `hp.value <= 0` on an actor-type `npc` token is a confirmed kill, attributed via the *last* hit recorded against that **token id** (not actor id — see below) anywhere in the chat log. This is the only way to catch a killing blow applied by **dragging the token's HP bar to 0**, which posts no chat message at all.

  Source 1 wins on conflict — it pins the exact killing blow, whereas the delta signal only knows the token is dead *now* and must guess the attacker from the last recorded hit.

  **Why source 1 exists:** the delta signal alone silently loses kills, because **deleting a dead token from the scene deletes its ActorDelta**, destroying the only record it ever hit 0 HP. Clearing dead tokens off the map is routine play. In the reference world this hid 14 of 69 kills — most visibly a fight with 11 Pirate Goblins where all but one token had been cleared, so only 1 kill was detected. Do not "simplify" this back to a single source.

  A narrower, monster-specific signal also exists and was evaluated but not used: some NPC stat blocks have an explicit "reduced to 0 Hit Points" reaction (e.g. Reefclaw's "Death Frenzy"), which *does* post a real, token-scoped chat card when it fires. Nice confirmation, but only covers monsters with such an ability — most (e.g. Cave Scorpion) have none, so it doesn't generalize.

  **Known limitation, left as-is:** attacker names (and victim names on the delta path) are resolved via `name_by_actor`, keyed by actor id and reflecting the actor's *current* name — not its name at the time of the historical event. GMs routinely reuse/reset a base Actor as a stat-block template across unrelated later encounters (e.g. the same "Cave Scorpion" Actor got renamed and its HP reset to full when repurposed as a "Crystal Claw"), so a kill attributed to a since-renamed monster displays under its current name. The damage-message path avoids this for **victims** by taking the name from the message's `speaker.alias` — the name as it was at the moment of death. Attacker names, and names on PC-downing events, still go through `name_by_actor`.

- **Token id vs actor id:** multiple simultaneous instances of the same monster type (e.g. 5 Cave Scorpion tokens in one fight) share a single base Actor id, so actor-id-keyed hit tracking merges their combat histories together and misattributes kills between them. `_build_npc_kill_events()` keys entirely on token id (via `_uuid_to_token_id`, extracting the `Token.xxx` segment from a UUID) to keep each instance's hits and death separate. `_build_downing_events()` still keys PCs by actor id, which is fine in practice since PCs are effectively one token each.

- **`data/combats` is typically empty** — don't rely on it for round/turn numbers or a combatant "defeated" flag (checked: 0 combat documents in the reference world despite dozens of real fights having happened). Per-round data does exist, just not there: individual PF2e roll messages embed `encounter:round:N` / `encounter:turn:N` tags inside `context.options`, which is the only place round/turn numbers survive.

### Reading applied damage (`_applied_hp_amount`) — the field that lies

Every consumer of HP change (kill detection, session combat stats, campaign stats) must go through `FullExporter._applied_hp_amount(msg)`. **Do not read the damage amount off `flags.pf2e.appliedDamage.updates[].value`.** That field is the victim's **pre-damage HP**, stored so the "Revert Damage" button can restore it — not the amount applied. A token killed from full HP 8 records `value: 8`, which reads exactly like "took 8 damage" and is why the mistake is so easy to make: summing it yields "the victim's HP before each hit", which lands in a plausible-looking range and looks correct on the page. This bug shipped in *both* `campaign_stats()` and `_compute_combat_stats()` and corrupted every damage figure on the wiki before it was caught (one session's party damage read 113 dealt / 179 taken; the true figures were 169 / 221, and the error was not a constant factor — it reordered the leaderboard).

The real amount exists **only in the rendered `content` HTML**, in one of two phrasings, which `_APPLIED_HP_RE` matches:

- `"Pirate Goblin takes 11 damage."`
- `"Greykor is healed for 6 damage."` (healing — note it also says "damage")

Consequences worth knowing:

- **Resistances/weaknesses are already applied.** PF2e computes IWR and any ×0.5/×2 button *before* rendering that line, so these numbers are post-mitigation. This is why applied damage — not damage *rolls*, which are pre-mitigation and would over-count against a resistant enemy — is the correct source, and why `pre_hp − applied <= 0` is a sound kill test.
- **Always skip `isReverted`** (the GM undid the damage, so it never landed) and branch on `isHealing`.
- **`appliedDamage.shield` non-null would break the arithmetic** (a shield absorbs part of the blow). No NPC in the reference world carries one, so this is currently moot — revisit if that changes.
- Totals **undercount**: damage applied by dragging a token's HP bar posts no chat message at all, so there is nothing to sum.

### Campaign stats page

`campaign_stats()` aggregates the same event sources campaign-wide (no session window) into a per-PC record: kill count (from `_build_npc_kill_events`), times downed, damage dealt/taken and healing given (from `appliedDamage` chat messages only, read via `_applied_hp_amount` — see the section above; manual HP-bar edits leave no message to sum, so totals undercount), plus "nemesis" (most frequent downer) and "favorite prey" (most-slain victim). Raw downing *signals* are collapsed into downing *episodes* per victim using a 10-minute gap (`DOWNING_EPISODE_GAP_MS`): one downing generates multiple signals (the condition card plus every recovery-check/heal roll made while still Dying), so counting raw signals would badly inflate "times downed". PCs with zero activity (test sheets) are excluded from the leaderboard. `render_campaign_stats_page()` emits the "Campaign Stats" page (leaderboard + collapsible kill/downing logs); pushed/previewed via `--stats`. Player-facing like the rest of the wiki — only PC stats and already-witnessed NPC token names, no NPC stat blocks.

### Session exporter

`SessionExporter` builds a `Sessions/YYYYMMDD` wiki page covering the 04:00→04:00 window. It diffs current inventory against a JSON snapshot (`session_snapshots/snapshot_latest.json`) to detect loot gained. No page is created if no combats, kills, or loot occurred that day.

Loot/XP diffs always compare two snapshot-shaped views (`_snapshot_characters`): on a normal run the current side is live world state, but a `--session-date` rebuild uses the **end-of-window dated snapshot** (`snapshot_<YYYYMMDD>.json`) as the current side — diffing today's live state against a historical baseline would attribute everything gained *since* that window to it (this bug shipped once). If the end-of-window snapshot is missing, the rebuild warns and renders the page with the loot/XP sections empty rather than inflated — do not "fix" that by substituting live state.

`--cleanup` (push mode only) deletes `Sessions/*` wiki pages that recorded no data — older script versions pushed a page even when nothing happened. A page is "empty" only if every data section shows its empty-state placeholder (`_EMPTY_SESSION_MARKERS`); any real content (loot, XP, characters, enemies, items spent, combat stats) disqualifies it. The decision is made purely from the rendered wiki page text — nothing is recomputed from snapshots, and `session_snapshots/` is never touched. Deleted dates are dropped from `session_index.json`, surviving pages' nav links are re-patched, and the Sessions index / Campaign Timeline / LatestSession pages are regenerated.

Each `--session` push also updates the `LatestSession` wiki page — a `#REDIRECT` to the newest session that had characters present or XP awarded (loot-only bookkeeping windows don't qualify). Eligibility comes from the session index's `characters_present`/`has_xp` fields; `has_xp` is backfilled for older index entries and pre-existing wiki pages by checking the page's "XP Gained" section for the empty-state placeholder.

## Key configuration (top of file)

- `WORLD_PATH` — path to the Foundry world directory. Points at the live ChaosMarine server path (`/var/www/java/foundry.atkennedy.com/foundrydata/Data/worlds/temporary-title/`) — correct as-is for production runs on the server.
- `FOUNDRY_DATA` — Foundry data root (for compendium packs at `systems/pf2e/packs/`). Also a ChaosMarine server path (`/var/www/java/foundry.atkennedy.com/foundrydata/Data`) — correct as-is for production runs on the server.
- `FOUNDRY_URL` — base URL for resolving icon paths
- `WIKI_URL` / `WIKI_USER` — MediaWiki connection details
- `COMPENDIUM_PACKS` — list of pf2e pack names to index

## Local development data (this machine)

This machine doesn't run against the live server paths above — it works from a local mirror synced down via `sync_foundry_world.sh` (uses the `ChaosMarine` SSH alias):

- `./temporary-title/` — mirror of the live world, used as the `test_export.py` fixture (`WORLD_PATH` override)
- `./systems/pf2e/packs/` — mirror of the compendium packs, matching the `<FOUNDRY_DATA>/systems/pf2e/packs` layout `build_compendium_index` expects

`monday-alkenstar/` (an older world fixture with a stale LevelDB schema — top-level `actors.ldb` instead of `data/actors/`) has been removed; `temporary-title` is the only local fixture now.

To exercise `full-export.py` locally against real data (not just `test_export.py`'s stubbed run), pass `--world ./temporary-title --data .` so `FOUNDRY_DATA` resolves to the local `systems/pf2e/packs/` mirror instead of the hardcoded server path.

Re-sync with `./sync_foundry_world.sh temporary-title` (world + packs) or `./sync_foundry_world.sh --packs-only` (packs only).

`wiki_preview/` (generated `.wiki` output from preview runs) is not checked in and gets recreated on demand — safe to delete anytime.

## Helper patterns used throughout

- `_int(v)` — safe int conversion, returns 0 on failure
- `_str_mod(n)` — formats int as `+N` / `-N` string
- `norm_name(s)` — lowercases and strips punctuation for compendium key lookup
- `_rank_from_node(node)` — extracts proficiency rank integer from varied Foundry node shapes

## MediaWiki requirements

- `$wgRawHtml = true` in `LocalSettings.php` — required for `<html>` tags used by `wiki_img()` to render external Foundry icon URLs
- `{{CharacterInfobox}}` template must exist on the wiki
- `{{color}}` template must exist on the wiki — `_fmt_prof` emits `{{color|#hex|'''Name'''}}` for every proficiency label
- `mw-collapsible` is built into MediaWiki core (no extension needed)
