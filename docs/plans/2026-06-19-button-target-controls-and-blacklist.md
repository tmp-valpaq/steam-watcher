# Button-only target controls, per-target intervals, and Steam blacklist Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace command-driven target management with button-driven flows, add per-target polling interval controls, and introduce a global Steam profile blacklist that blocks monitoring of selected SteamIDs.

**Architecture:** Keep the current single-bot / single-SQLite architecture. Reuse the existing `targets.interval_seconds` field and callback/ownership patterns already used in `bot.py`. Add one new persistent table for the Steam blacklist plus a small set of DB helpers, then wire button-first flows in `bot.py` with minimal watcher changes.

**Tech Stack:** Python, aiogram, aiosqlite, SQLite schema migrations via `schema.sql` + `init_db()`, pytest.

---

## Current repo truth

Verified from the repo before planning:

- Per-target interval already exists in model/schema/runtime:
  - `src/models.py` → `Target.interval_seconds`
  - `schema.sql` → `targets.interval_seconds INTEGER NOT NULL DEFAULT 30`
  - `src/watcher.py` → skips polling until `now - state.last_checked >= target.interval_seconds`
- Current inline target actions are limited to:
  - pause/resume
  - history
  - session
  - alert settings
  - remove
  - manual check
- Rename exists only as a command path today:
  - `src/bot.py` → `/rename`
  - `README.md` still documents `/rename`
- Ownership checks already exist for callbacks through `get_target_by_id(..., telegram_id)` and `callback.from_user.id`
- Tests already exist for:
  - bot keyboard helpers → `tests/test_bot_ui.py`
  - DB CRUD → `tests/test_db.py`

This means the missing work is mostly UI wiring, DB helpers, blacklist policy, and doc/test updates — not a watcher redesign.

---

## Scope

### In scope
- Button-driven target interval changes
- Button-driven target rename flow
- Global Steam profile blacklist
- Add-flow denial for blacklisted SteamIDs
- Optional forced deactivation/removal policy for already-added blacklisted targets
- README / architecture refresh for the new operator UX
- Tests for DB helpers and bot UI helpers

### Out of scope
- Free-form custom intervals beyond presets
- Per-user or per-target sharing policy (`owner-only` vs `shared`)
- Full admin role system
- Migrating every legacy slash command out of existence in one pass
- Multi-process / Postgres / infra changes

---

## Product decisions to freeze before coding

### 1. Button-first UX
Primary target operations must be reachable from target cards, not by remembering commands.

### 2. Interval presets
Ship fixed presets only:
- 60 seconds
- 180 seconds
- 300 seconds
- 600 seconds
- 900 seconds

Display labels in UI as:
- `1 мин`
- `3 мин`
- `5 мин`
- `10 мин`
- `15 мин`

### 3. Rename semantics
Rename changes the local bot label (`targets.name`) only. It must not claim to change the Steam profile nickname.

### 4. Blacklist semantics
Blacklist is a hard global deny-list by `steam_id`.

Behavior:
- blacklisted SteamIDs cannot be newly added by anyone
- manual add flow and button-driven add flow must both block them
- if a SteamID is newly blacklisted and already exists in `targets`, existing rows should be immediately deactivated rather than silently left live

Why deactivate instead of delete:
- safer and reversible
- preserves history / audit trail
- minimal schema churn

### 5. Ownership/security semantics
Every new callback path must reuse the current ownership guard pattern:
- resolve target by `target_id + callback.from_user.id`
- fail closed with `Не найден.` or equivalent

---

## Files to modify

### Database / schema
- Modify: `schema.sql`
- Modify: `src/db.py`
- Modify: `src/models.py`
- Test: `tests/test_db.py`

### Bot UX / flows
- Modify: `src/bot.py`
- Test: `tests/test_bot_ui.py`
- Possibly add focused handler-level tests later if helper-only tests become too shallow

### Runtime behavior
- Likely no core watcher logic change needed in `src/watcher.py`
- Only revisit watcher if blacklist enforcement needs a startup cleanup pass

### Documentation
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`

---

## Data model additions

### New table
Add a global blacklist table in `schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS steam_profile_blacklist (
    steam_id TEXT PRIMARY KEY,
    reason TEXT,
    created_at INTEGER NOT NULL,
    created_by INTEGER
);
```

### New model
Add a dataclass in `src/models.py`:

```python
@dataclass
class SteamProfileBlacklistEntry:
    steam_id: str
    reason: Optional[str] = None
    created_at: int = 0
    created_by: Optional[int] = None
```

This is enough for repo consistency; no need to over-model yet.

---

## DB helper contract

Add helpers in `src/db.py`:

```python
async def is_steam_profile_blacklisted(db, steam_id: str) -> bool: ...
async def get_steam_profile_blacklist_entry(db, steam_id: str) -> Optional[SteamProfileBlacklistEntry]: ...
async def add_steam_profile_blacklist_entry(db, entry: SteamProfileBlacklistEntry) -> None: ...
async def remove_steam_profile_blacklist_entry(db, steam_id: str) -> bool: ...
async def deactivate_targets_by_steam_id(db, steam_id: str) -> int: ...
async def set_target_interval(db, telegram_id: int, steam_id: str, interval_seconds: int) -> bool: ...
```

Migration work in `init_db()`:
- create the new blacklist table if missing
- no destructive migration needed

Notes:
- `set_target_interval()` must be ownership-scoped by `telegram_id`
- `deactivate_targets_by_steam_id()` is global by `steam_id` because blacklist is global

---

## UX changes to implement

### Target card layout
Update `_build_target_keyboard(target)` in `src/bot.py`.

Current rows:
- `⏸ Пауза / ▶️ Возобновить`
- `📜 История`
- `⏱ Сессия`
- `⚙️ Уведомления`
- `🗑 Удалить`
- `🔍 Проверить`

Planned rows:
- row 1: `⏸ Пауза|▶️ Возобновить`, `📜 История`, `⏱ Сессия`
- row 2: `⏱ Интервал`, `📝 Переименовать`, `🔍 Проверить`
- row 3: `⚙️ Уведомления`, `🚫 В блэклист`, `🗑 Удалить`

This keeps the old mental model and inserts the new actions where users actually look.

### Interval picker
Add a keyboard builder:

```python
def _build_interval_picker_keyboard(target_id: int, current_interval: int) -> types.InlineKeyboardMarkup:
    ...
```

Buttons:
- `✅ 1 мин` when selected else `1 мин`
- `✅ 3 мин` ...
- `✅ 5 мин` ...
- `✅ 10 мин` ...
- `✅ 15 мин` ...
- `🔙 Назад`

Callback shape:
- `interval:{target_id}`
- `interval_pick:{target_id}:{seconds}`
- `interval_back:{target_id}` or reuse the target-card redraw path

### Rename flow
Introduce pending rename state in `src/bot.py` similar to `_pending_add`.

Recommended shape:

```python
_pending_rename: Dict[int, int] = {}  # telegram user id -> target_id
```

Flow:
1. user taps `📝 Переименовать`
2. bot verifies ownership and prompts: `Пришли новое имя для этого профиля (или напиши «Отмена»).`
3. next text message for that user is treated as rename input
4. blank/whitespace-only names are rejected
5. cancel clears pending state
6. successful rename updates `targets.name`

### Blacklist flow
Do not make blacklist a one-tap destructive action.

Add confirm UI:
- `🚫 В блэклист` → prompt with confirm keyboard
- buttons:
  - `🚫 Да, в блэклист`
  - `❌ Отмена`

Callback shape:
- `blacklist:{target_id}`
- `blacklist_confirm:{target_id}`
- `blacklist_cancel:{target_id}`

Apply logic:
1. verify ownership
2. insert blacklist entry with `created_by = callback.from_user.id`, `created_at = now`
3. deactivate all existing targets for that `steam_id`
4. redraw or replace card text with a short result summary

Suggested response copy:
- `Профиль добавлен в блэклист. Активные наблюдения отключены.`

### Add-flow denial
Before adding a target in both add entry points, check blacklist:
- slash command `/add`
- pending add flow from `➕ Добавить`

If blacklisted, short-circuit with:
- `Этот профиль запрещён для мониторинга.`

Do not hit Steam API after the blacklist denial if the SteamID is already known.
If input is vanity URL and must be resolved first, the deny check can happen right after `steam_id` resolution.

---

## Documentation changes

### README.md
Update these sections:
- `Команды`
  - keep `/rename` only if still supported, but mark button-first UX as preferred
- `Inline-кнопки`
  - add `⏱ Интервал`
  - add `📝 Переименовать`
  - add `🚫 В блэклист`
- `Важно` / `Безопасность`
  - describe blacklist behavior briefly

### ARCHITECTURE.md
Update:
- target lifecycle / operator controls
- mention per-target interval configuration
- mention global Steam blacklist gate in add-flow

---

## Task-by-task execution plan

### Task 1: Add failing DB tests for interval updates and blacklist helpers

**Objective:** Freeze the persistence contract before touching DB code.

**Files:**
- Modify: `tests/test_db.py`
- Reference: `src/db.py`, `schema.sql`, `src/models.py`

**Step 1: Write failing tests**

Add tests for:
- `set_target_interval()` updates only the owner’s target
- `is_steam_profile_blacklisted()` is false by default
- `add_steam_profile_blacklist_entry()` stores an entry retrievable by SteamID
- `deactivate_targets_by_steam_id()` flips matching targets inactive

Sketch:

```python
updated = await set_target_interval(db_conn, 111, "76561198000000001", 600)
assert updated is True
assert (await get_targets(db_conn, 111))[0].interval_seconds == 600
```

```python
entry = SteamProfileBlacklistEntry(
    steam_id="76561198000000001",
    reason="manual block",
    created_at=1710000000,
    created_by=111,
)
await add_steam_profile_blacklist_entry(db_conn, entry)
assert await is_steam_profile_blacklisted(db_conn, entry.steam_id) is True
```

**Step 2: Run targeted tests to verify failure**

Run:

```bash
cd /root/projects/steam-watcher && pytest tests/test_db.py -q
```

Expected: fail with missing functions/model/table assertions.

**Step 3: Commit**

No commit yet; wait until implementation passes.

---

### Task 2: Implement schema/model/DB support

**Objective:** Make the new persistence behavior real with minimal schema churn.

**Files:**
- Modify: `schema.sql`
- Modify: `src/models.py`
- Modify: `src/db.py`

**Step 1: Add the new dataclass**

In `src/models.py`, add `SteamProfileBlacklistEntry`.

**Step 2: Add schema table**

In `schema.sql`, append the new table definition.

**Step 3: Add migration/bootstrap in `init_db()`**

Either rely on `executescript(schema.sql)` for new DBs plus explicit `CREATE TABLE IF NOT EXISTS steam_profile_blacklist (...)` in `init_db()`, or keep it only in schema if that path is guaranteed to run for existing DBs too. Prefer the explicit `CREATE TABLE IF NOT EXISTS` in `init_db()` to make migration intent obvious.

**Step 4: Add DB helpers**

Implement:
- `set_target_interval()`
- `is_steam_profile_blacklisted()`
- `get_steam_profile_blacklist_entry()`
- `add_steam_profile_blacklist_entry()`
- `remove_steam_profile_blacklist_entry()`
- `deactivate_targets_by_steam_id()`

**Step 5: Run targeted DB tests**

Run:

```bash
cd /root/projects/steam-watcher && pytest tests/test_db.py -q
```

Expected: pass.

**Step 6: Commit**

```bash
cd /root/projects/steam-watcher && git add schema.sql src/models.py src/db.py tests/test_db.py && git commit -m "feat: add target interval and steam blacklist persistence"
```

---

### Task 3: Add failing bot UI helper tests for new buttons

**Objective:** Freeze the new target-card and interval-picker UX.

**Files:**
- Modify: `tests/test_bot_ui.py`
- Reference: `src/bot.py`

**Step 1: Write failing tests**

Add tests for:
- target card includes `⏱ Интервал`, `📝 Переименовать`, `🚫 В блэклист`
- interval picker renders preset labels and current selection marker

Sketch:

```python
rows = _flatten_button_rows(_build_target_keyboard(target))
assert "⏱ Интервал" in rows[1]
assert "📝 Переименовать" in rows[1]
assert "🚫 В блэклист" in rows[2]
```

**Step 2: Run targeted tests to verify failure**

Run:

```bash
cd /root/projects/steam-watcher && pytest tests/test_bot_ui.py -q
```

Expected: fail because helper outputs are still old.

---

### Task 4: Implement button builders and pending-state plumbing in `bot.py`

**Objective:** Add the non-destructive UI scaffolding before wiring every callback.

**Files:**
- Modify: `src/bot.py`
- Modify: `tests/test_bot_ui.py`

**Step 1: Add constants/helpers**

Add interval presets near the top of `bot.py`:

```python
INTERVAL_PRESETS = [60, 180, 300, 600, 900]
```

Helper label function:

```python
def _format_interval_label(seconds: int) -> str:
    return {
        60: "1 мин",
        180: "3 мин",
        300: "5 мин",
        600: "10 мин",
        900: "15 мин",
    }.get(seconds, f"{seconds} сек")
```

**Step 2: Add `_pending_rename` state**

```python
_pending_rename: Dict[int, int] = {}
```

**Step 3: Update target card keyboard**

Modify `_build_target_keyboard()` to include the new buttons.

**Step 4: Add interval picker / blacklist confirm keyboards**

Implement helper builders for:
- interval picker
- blacklist confirm dialog

**Step 5: Run helper tests**

Run:

```bash
cd /root/projects/steam-watcher && pytest tests/test_bot_ui.py -q
```

Expected: pass.

**Step 6: Commit**

```bash
cd /root/projects/steam-watcher && git add src/bot.py tests/test_bot_ui.py && git commit -m "feat: add button-first target management ui"
```

---

### Task 5: Wire interval callbacks

**Objective:** Make per-target interval changes work from buttons.

**Files:**
- Modify: `src/bot.py`
- Modify: `src/db.py` if tiny follow-up adjustments are needed

**Step 1: Add `interval:{target_id}` callback handler**

Behavior:
- resolve target by ownership
- show current interval + picker keyboard

**Step 2: Add `interval_pick:{target_id}:{seconds}` callback handler**

Behavior:
- resolve target by ownership
- validate `seconds in INTERVAL_PRESETS`
- call `db.set_target_interval(...)`
- update target object in memory for redraw
- redraw card or edit message with success text

**Step 3: Verify ownership failure path**

Non-owner must get `Не найден.` / alert.

**Step 4: Run targeted tests**

If no handler tests exist yet, at minimum rerun helper + DB suites to ensure no regressions:

```bash
cd /root/projects/steam-watcher && pytest tests/test_bot_ui.py tests/test_db.py -q
```

**Step 5: Commit**

```bash
cd /root/projects/steam-watcher && git add src/bot.py src/db.py tests/test_bot_ui.py tests/test_db.py && git commit -m "feat: allow per-target poll interval changes from buttons"
```

---

### Task 6: Wire rename button flow

**Objective:** Replace “remember `/rename` syntax” with a guided interaction.

**Files:**
- Modify: `src/bot.py`
- Modify: `README.md`

**Step 1: Add `rename_prompt:{target_id}` callback handler**

Behavior:
- ownership check
- write `_pending_rename[user_id] = target_id`
- ask for new name with cancel hint

**Step 2: Extend generic text-message handler**

In the catch-all text handler that already serves pending add, add rename handling first or otherwise carefully order the checks.

Flow:
- if user is in `_pending_rename`
- if cancel → clear state, send cancellation message
- else normalize text
- reject empty / all-whitespace
- rename target by target id ownership path (preferred) or via `steam_id` from the resolved target
- clear pending state
- respond with success

**Step 3: Keep `/rename` temporarily or remove it**

Recommendation:
- keep `/rename` for backward compatibility in this patch
- but stop treating it as the primary UX in docs

**Step 4: Run tests**

```bash
cd /root/projects/steam-watcher && pytest tests/test_bot_ui.py tests/test_db.py -q
```

**Step 5: Commit**

```bash
cd /root/projects/steam-watcher && git add src/bot.py README.md tests/test_bot_ui.py tests/test_db.py && git commit -m "feat: add button-driven target rename flow"
```

---

### Task 7: Wire blacklist flow and add-flow denial

**Objective:** Make blacklist a real policy, not only a DB table.

**Files:**
- Modify: `src/bot.py`
- Modify: `src/db.py`
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`

**Step 1: Add blacklist confirm handlers**

Handlers:
- `blacklist:{target_id}` → show confirm keyboard
- `blacklist_confirm:{target_id}` → apply blacklist + deactivate existing targets
- `blacklist_cancel:{target_id}` → return to normal target card

**Step 2: Add add-flow checks**

In both add entry points, after SteamID resolution and before DB insert, call:

```python
if await db.is_steam_profile_blacklisted(db_conn, steam_id):
    await message.answer("Этот профиль запрещён для мониторинга.")
    return
```

For the callback-based/pending add flow, use the same denial copy.

**Step 3: Decide UI copy for already-blacklisted target**

If user taps blacklist on an already-blacklisted profile, respond idempotently:
- `Профиль уже в блэклисте.`

**Step 4: Update docs**

README:
- button-first target controls
- blacklist behavior

ARCHITECTURE:
- blacklist gate in add-flow
- existing targets are deactivated on blacklist

**Step 5: Run tests**

Run:

```bash
cd /root/projects/steam-watcher && pytest tests/ -q
```

Expected: full suite green.

**Step 6: Commit**

```bash
cd /root/projects/steam-watcher && git add src/bot.py src/db.py README.md ARCHITECTURE.md schema.sql src/models.py tests/test_db.py tests/test_bot_ui.py && git commit -m "feat: block blacklisted steam profiles from monitoring"
```

---

### Task 8: Optional cleanup pass for command/help surface

**Objective:** Reduce command-first copy without risky behavior churn.

**Files:**
- Modify: `src/bot.py`
- Modify: `README.md`

**Step 1: Update help text**

Make `_help_text()` emphasize buttons over slash-command memorization.

Example wording:
- start with `Нажми ➕ Добавить`
- mention that rename and interval are available from profile buttons

**Step 2: Reassess command registration**

Do not remove working commands in the same patch unless clearly requested. Minimal safe path:
- keep commands live
- de-emphasize them in help/docs

**Step 3: Run focused tests**

```bash
cd /root/projects/steam-watcher && pytest tests/test_bot_ui.py -q
```

**Step 4: Commit**

```bash
cd /root/projects/steam-watcher && git add src/bot.py README.md tests/test_bot_ui.py && git commit -m "docs: shift bot guidance toward button-first target management"
```

---

## Verification checklist

Before calling the feature done, verify all of this manually or with tests:

- [ ] target card shows `⏱ Интервал`
- [ ] target card shows `📝 Переименовать`
- [ ] target card shows `🚫 В блэклист`
- [ ] interval picker highlights current preset
- [ ] non-owner cannot use new callbacks on another user’s target id
- [ ] rename flow accepts text and cancel path
- [ ] add flow refuses blacklisted SteamID
- [ ] blacklisting an existing active SteamID deactivates all matching targets
- [ ] README matches shipped UX
- [ ] full test suite passes

Recommended commands:

```bash
cd /root/projects/steam-watcher && pytest tests/ -q
python -m compileall src
```

If running live in Docker afterward:

```bash
cd /root/projects/steam-watcher && docker compose build && docker compose up -d
```

Then verify:
- open target card in Telegram
- change interval
- rename target
- blacklist target
- try to add it again

---

## Risks / pitfalls

### 1. Pending-state collisions
`_pending_add` and `_pending_rename` must not fight each other. Make the text-handler precedence explicit.

### 2. Over-broad blacklist side effects
Deactivation by `steam_id` is intentionally global. Keep the user-facing copy honest.

### 3. Callback clutter
Avoid inventing too many callback shapes that do the same thing. Prefer one simple family per feature.

### 4. Docs drift
README currently still advertises `/rename` as the obvious path. Update that in the same implementation cycle.

---

## Suggested execution order

1. DB tests
2. schema/model/db implementation
3. bot UI helper tests
4. target-card + interval/confirm keyboards
5. interval callbacks
6. rename flow
7. blacklist flow + add denial
8. docs/help cleanup
9. full test pass

---

## Definition of done

This feature batch is done when:
- users can manage intervals and renames without remembering commands,
- selected SteamIDs can be globally blocked from monitoring,
- old active entries for blacklisted SteamIDs are neutralized safely,
- docs and tests match the shipped behavior.
