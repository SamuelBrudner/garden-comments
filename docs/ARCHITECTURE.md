# Architecture and module contracts (MVP, SPEC v0.5)

This is the binding implementation contract for the MVP. `SPEC.md` is the product spec; this
document pins down the interfaces and the exact behavioural rules so modules can be built and
tested independently. Shared types already exist and are **not to be redefined**:

| Module | Contents (already written) |
|---|---|
| `schemas.py` | Extraction contract (`EnvironmentFact`, `VarietyMention`, `ParsedComment`), validated forms (`ValidatedFact`, `ValidatedMention`, `ValidatedComment`, `Rejection`), raw records (`ColumnMapping`, `CommentRecord`, `SourceRecord`, `ImportStats`), literals (`Topic`, `EnvKey`, `RightsStatus`, `ImportScope`, `SCOPE_TO_RIGHTS`), `to_utc_iso`, `utc_now_iso` |
| `vocab.py` | `ComparableKey`, `COMPARABLE_KEYS`, `CATEGORICAL_VALUES`, `ZONE_PATTERN`, `YEAR_PATTERN`, `VIEWER_VALUES`, `PROMINENT_KEYS`, `KEY_LABELS`, `VALUE_LABELS`, `is_valid_normalized`, `value_label` |
| `regions.py` | `US_STATES`, `CA_PROVINCES`, `COUNTRIES`, `REGION_NAMES`, `is_us_state`, `us_postal`, `region_name` |
| `views.py` | `GardenContext`, `FactView`, `MentionView`, `CommentView`, `DimensionMatch`, `MatchResult`, `MentionMatch`, `CommentResult`, `SearchQuery`, `ResultGroup`, `SearchResult`, literals `ViewerScope`, `MatchStatus`, `Tier`, `RegionStatus`, `GroupKey`, constants `TIER_ORDER`, `GROUP_TITLES` |
| `config.py` | `AppConfig` and sub-models, `load_config(path)` |
| `logging_setup.py` | `configure_logging(cfg)` |

## Engineering rules (all modules)

- Python ≥ 3.12, full type hints, `mypy --strict` clean (`uv run mypy src app.py`).
- `uv run ruff check` and `uv run ruff format --check` clean. Ruff enforces numpy docstrings on
  public functions, ≤ 15 statements / ≤ 8 branches / ≤ 6 args per function, complexity ≤ 8.
  Keep functions small and pure; push I/O to thin edges.
- `pathlib.Path` only. No magic numbers: tunables come from `AppConfig`.
- **Fail loud and fast.** Raise on invalid input (`ValueError` subclasses with clear messages);
  no silent fallbacks. The only "drop and continue" path is extraction validation, which must
  *record* every rejection.
- **Never log comment bodies or evidence quotes.** Log ids, counts and reasons only (loguru).
- Treat every imported comment as untrusted text: never interpret it, never render it as HTML
  or Markdown.
- Deterministic: stable sort keys everywhere; no randomness; time is passed in (`now: str` in
  canonical UTC form from `schemas.to_utc_iso`) rather than read inside library functions.
- Tests: pytest, written first (red → green). Use Hypothesis for property tests of pure logic.

## Data flow

```
CSV/JSONL export --ingest.read_comments--> CommentRecord[] --db.sync_source_comments--> SQLite
                                                                     |
model step (Codex/Claude, offline) --> ParsedComment JSONL           |
   --extraction.load_extractions (validate vs stored text)--> db.replace_extraction
                                                                     |
viewer (app.py) --search.search(conn, scope, query, now)--> SearchResult (groups, counts)
               --geography.region_counts(...)--> map counts
```

No model call happens at request time.

## Storage (`db.py`)

SQLite via stdlib `sqlite3`. `PRAGMA foreign_keys = ON` on every connection. Tables (spec §8),
plus one small `parses` table for comment-level extraction status:

```sql
sources(source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_url TEXT,
        rights_status TEXT NOT NULL CHECK (rights_status IN ('private_unverified','synthetic','cleared')),
        imported_at TEXT NOT NULL, expires_at TEXT, dataset_hash TEXT NOT NULL)
comments(id INTEGER PRIMARY KEY, comment_id TEXT NOT NULL UNIQUE,
         source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
         text TEXT NOT NULL, permalink TEXT, source_order INTEGER NOT NULL, created_at TEXT,
         imported_at TEXT NOT NULL, deleted_at TEXT, text_hash TEXT NOT NULL)
parses(comment_id TEXT PRIMARY KEY REFERENCES comments(comment_id) ON DELETE CASCADE,
       status TEXT NOT NULL, extractor_version TEXT NOT NULL, warnings_json TEXT NOT NULL,
       loaded_at TEXT NOT NULL)
mentions(mention_id TEXT PRIMARY KEY,
         comment_id TEXT NOT NULL REFERENCES comments(comment_id) ON DELETE CASCADE,
         mention_index INTEGER NOT NULL, crop TEXT, variety_as_written TEXT, variety_key TEXT,
         topics_json TEXT NOT NULL, evidence_quote TEXT NOT NULL, extractor_version TEXT NOT NULL,
         status TEXT NOT NULL)
environment_facts(fact_id TEXT PRIMARY KEY,
         mention_id TEXT NOT NULL REFERENCES mentions(mention_id) ON DELETE CASCADE,
         fact_index INTEGER NOT NULL, key TEXT NOT NULL, value_as_written TEXT NOT NULL,
         normalized_value TEXT, evidence_quote TEXT NOT NULL, applicability TEXT NOT NULL,
         basis TEXT NOT NULL)
comments_fts  -- FTS5, tokenize='porter unicode61 remove_diacritics 2', kept in sync by
              -- triggers on comments (insert/update-of-text/delete, incl. cascaded deletes)
```

Indexes on `comments(source_id)`, `mentions(comment_id)`, `environment_facts(mention_id)`.
Ids are deterministic: `mention_id = f"{comment_id}#m{index}"`,
`fact_id = f"{mention_id}#f{index}"`. `mentions.status` stores the comment's parse status.
All timestamps are canonical UTC strings (`to_utc_iso`).

Public API (all writes transactional; on error the transaction rolls back and re-raises):

```python
def connect(path: Path) -> sqlite3.Connection
    # mkdir parents, foreign_keys ON, row_factory=sqlite3.Row, init_schema; ":memory:" allowed
def init_schema(conn: sqlite3.Connection) -> None            # idempotent
def sync_source_comments(conn, source: SourceRecord, comments: Sequence[CommentRecord]) -> ImportStats
def replace_extraction(conn, validated: ValidatedComment, loaded_at: str) -> None
def delete_source(conn, source_id: str) -> int                # returns #comments removed; raises if unknown
def purge_expired(conn, now: str) -> list[str]                # deletes sources with expires_at <= now
def stored_texts(conn, comment_ids: Sequence[str]) -> dict[str, str]   # pipeline-internal (no scope)
def iter_comments_for_extraction(conn, only_unparsed: bool) -> list[tuple[str, str]]  # pipeline-internal
def fts_query(text: str) -> str | None                         # pure sanitizer, see below
def visible_comment_ids(conn, scope: ViewerScope, now: str, text: str | None) -> list[str]
def load_comment_views(conn, scope: ViewerScope, now: str, comment_ids: Sequence[str] | None) -> list[CommentView]
def summary_counts(conn, scope: ViewerScope, now: str) -> dict[str, int]
```

`sync_source_comments` semantics (idempotent re-import):

- Upserts the `sources` row (updates `dataset_hash`, `imported_at`, rights, expiry, url, kind).
- A `comment_id` already owned by a *different* source → raise `ValueError` (collision).
- New comment → insert. Same id, same `text_hash` (sha256 of UTF-8 text) → unchanged (keep its
  extraction; refresh permalink/created_at/source_order). Same id, different text → update
  text and **delete its parse/mentions/facts** (their quotes may no longer be substrings).
- Comments of this source absent from the new import → deleted (cascade).
- Returns `ImportStats(inserted, updated, unchanged, deleted)`.

`replace_extraction` deletes the comment's existing `parses`/`mentions` (facts cascade) and
inserts the validated ones in one transaction. Re-loading identical JSONL is a no-op in effect.
Raises `KeyError` if the comment does not exist.

`fts_query(text)`: extract `\w+` tokens (Unicode); none → `None` (no text filter); otherwise
`" ".join(f'"{tok}"*' for tok in tokens)` (implicit AND, prefix match). User text must never be
passed to `MATCH` unsanitized.

`visible_comment_ids`: ids of comments visible in `scope` at `now` (see visibility), filtered by
FTS when `fts_query(text)` is not `None`, ordered by `(source_order, comment_id)` within
`(source_id)` — use ordering `ORDER BY c.source_id, c.source_order, c.comment_id`.

`load_comment_views`: `CommentView`s (with mentions ordered by `mention_index`, facts by
`fact_index`) for the given ids (or all visible when `None`), **re-applying the visibility
clause** (an id that is not visible is silently absent — it is not an error, because ids may come
from another scope's query; never return invisible rows). Same ordering as above.

`summary_counts`: `{"sources", "comments", "parsed_comments", "mentions", "facts"}` restricted
to the visible scope.

## Visibility / rights gating (`visibility.py`)

```python
PUBLIC_RIGHTS: frozenset[RightsStatus] = frozenset({"synthetic", "cleared"})
def visible_rights(scope: ViewerScope) -> frozenset[RightsStatus]   # private -> all statuses
def visibility_clause(scope: ViewerScope, now: str, *, source_alias: str = "s",
                      comment_alias: str = "c") -> tuple[str, list[str]]
```

`visibility_clause` returns a SQL boolean fragment plus parameters:
`s.rights_status IN (?, ...) AND (s.expires_at IS NULL OR s.expires_at > ?) AND c.deleted_at IS NULL`.
Unknown scope → `ValueError`. **Every** viewer-facing read in `db.py` (ids, views, counts) must
join `comments c JOIN sources s` and apply this clause, so that private rows can never reach a
public count, chip, map cell, snippet or search hit. The Streamlit app must not cache query
results across scopes (do not use `st.cache_data` for query results at all).

## Ingestion (`ingest.py`)

```python
class IngestError(ValueError)
def read_comments(path: Path, mapping: ColumnMapping) -> list[CommentRecord]
def dataset_hash(path: Path) -> str                       # sha256 hex of file bytes
def build_source(path: Path, *, source_id: str, kind: str, rights_status: RightsStatus,
                 source_url: str | None, expires_at: str | None, imported_at: str) -> SourceRecord
def import_file(conn, path: Path, mapping: ColumnMapping, source: SourceRecord) -> ImportStats
```

- Format from suffix: `.csv` (csv.DictReader) or `.jsonl` (one JSON object per line). Other →
  `IngestError`. Read bytes and decode strict UTF-8 (`IngestError` on failure; BOM tolerated).
- Required: id and text columns present; every row has non-empty id and non-blank text.
  Duplicate ids in one file → `IngestError` naming the id and row numbers. Empty file → error.
- Optional permalink/date columns: if the mapped column is absent from the file entirely, the
  field is `None` for all rows; blank cells → `None`. Dates → `to_utc_iso` (`IngestError` with
  row number when unparseable). Permalinks must start with `https://` or `http://` when present.
- `source_order` = 0-based row order in the file.
- `expires_at` passed to `build_source` is normalised via `to_utc_iso`.
- Text is stored exactly as given (no stripping/normalising) so evidence quotes stay exact
  substrings.

## Normalization (`normalization.py`)

```python
STRICT_KEYS: frozenset[EnvKey] = frozenset({"region", "zone", "growing_year"})
def normalize_written(key: EnvKey, written: str) -> str | None
def normalize_crop(crop: str) -> str
def variety_key(variety: str) -> str
def parse_zone(value: str) -> tuple[int, str | None]      # "6b" -> (6, "b"); "6" -> (6, None)
```

- `normalize_written` maps an as-written phrase to a normalized vocabulary value
  deterministically using small explicit synonym tables (case-insensitive, whitespace
  collapsed); returns `None` when nothing is recognised. Examples: region — full state/province/
  country names anywhere in the phrase ("in Connecticut" → `US-CT`), common abbreviations like
  "Conn."; two-letter postal codes only when the phrase contains them as an uppercase standalone
  token ("Hartford, CT" → `US-CT`, but "in it" never → `US-IN`); a phrase naming two different
  regions → `None` (ambiguous). Zone — "zone 6b", "Zone 6B", "6b", "zone 6" → `6b`/`6`; a
  phrase with two different zones (e.g. "zone 6b/7a") → `None`. Year — a single 4-digit year
  19xx/20xx. Categorical — e.g. "pots", "containers", "grow bags", "5 gallon buckets" →
  `container`; "raised beds" → `raised_bed`; "in the ground" → `in_ground`; "greenhouse",
  "hoop house", "high tunnel", "polytunnel" → `greenhouse`; "muggy", "humid" → `humid`;
  "hot summers", "brutal heat" → `hot`; "heat wave" → `heatwave`; "short season" → `short_season`;
  "clay" → `clay`; "drip" → `drip`; etc. Never infer region from a zone, or humidity from a
  region.
- `normalize_crop`: casefold, strip, collapse whitespace, deterministic singularisation
  (`tomatoes`→`tomato`, `potatoes`→`potato`, `strawberries`→`strawberry`, `peppers`→`pepper`,
  `beans`→`bean`; words ending in `ss` unchanged). Empty → `ValueError`.
- `variety_key`: casefold, strip quotes/punctuation except internal hyphens/apostrophes removed,
  collapse whitespace (`"Sun Gold"`→`sun gold`, `"Cherokee Purple"`→`cherokee purple`). Empty →
  `ValueError`.

## Extraction validation and loading (`extraction.py`)

```python
@dataclass(frozen=True)  # or pydantic
class ExtractionReport: lines_read, schema_errors, unknown_comment_ids, duplicate_comment_ids,
    comments_loaded, status_counts (dict), mentions_accepted, mentions_rejected,
    facts_accepted, facts_rejected, unresolved_identities, rejections: tuple[Rejection, ...]
def read_extraction_jsonl(path: Path) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]
def validate_parsed_comment(parsed: ParsedComment, text: str, cfg: ExtractionConfig) -> ValidatedComment
def load_extractions(conn, path: Path, cfg: ExtractionConfig, loaded_at: str) -> ExtractionReport
```

Validation rules (each failure appends a `Rejection` with a short machine-readable reason, e.g.
`mention_quote_not_substring`, `fact_quote_not_substring`, `value_not_in_quote`,
`variety_not_in_quote`, `invalid_normalized_value`, `strict_key_mismatch`, `hypothetical_context`,
`conflicting_region`, `conflicting_zone`):

1. Mention `evidence_quote` must be an **exact** (case-sensitive) substring of the comment text,
   else the mention and all its facts are rejected.
2. `variety_as_written`, when present, must occur case-insensitively within the mention quote.
3. `crop` is normalised with `normalize_crop`; `variety_key` from `variety_key()`.
4. Fact `evidence_quote` must be an exact substring of the comment text; `value_as_written` must
   occur case-insensitively within the fact's `evidence_quote`.
5. Normalised value: if provided it must satisfy `vocab.is_valid_normalized`; for `STRICT_KEYS`
   it must also equal `normalize_written(key, value_as_written)` (so "zone 6b" can never become a
   region, and a residence-only phrase cannot be re-labelled). If not provided, use
   `normalize_written` (may stay `None`: kept as an un-normalised, display-only fact).
6. Hypothetical/advice guard: split the comment text into sentences on `[.!?\n]`; if any sentence
   that overlaps the fact's evidence span contains a configured marker
   (`cfg.hypothetical_markers`, case-insensitive, word-bounded, apostrophes may be ’ or ')
   → reject the fact (`hypothetical_context`).
7. Within one mention, two accepted facts with key `region` (or `zone`) and different normalised
   values → reject all facts of that key in that mention (`conflicting_region` /
   `conflicting_zone`).
8. Status: keep the extractor's status, but downgrade `parsed` → `partial` if anything was
   rejected. A comment with zero mentions is valid.

`load_extractions`: reads JSONL (blank lines skipped; JSON/schema errors become rejections with
`line_number`, `reason="schema_error"`; text of the line is never logged), looks up stored text
via `db.stored_texts`, rejects unknown comment ids (`unknown_comment_id`) and duplicate ids
within the file (`duplicate_comment_id`, all copies rejected), validates, then calls
`db.replace_extraction` per comment. Logs the aggregate report (counts only).
`unresolved_identities` = accepted mentions with neither crop nor variety.

## Environment matching (`environment_match.py`) — pure, no I/O

```python
def applicable_facts(mention: MentionView, siblings: Sequence[MentionView]) -> tuple[FactView, ...]
def compare_dimension(key: ComparableKey, viewer_value: str, facts: Sequence[FactView]) -> DimensionMatch
def match_context(garden: GardenContext, facts: Sequence[FactView]) -> MatchResult | None
def tier_of(statuses: Sequence[MatchStatus]) -> Tier
def region_status(garden_region: str | None, facts: Sequence[FactView]) -> RegionStatus | None
def explain(result: MatchResult | None) -> str
```

- `applicable_facts`: the mention's own facts, plus `whole_comment` facts stored on sibling
  mentions **whose key the mention does not itself state** (own facts win per key). Deduplicate
  by `(key, normalized_value, value_as_written)`. Order: own facts first (fact order), then
  sibling facts (mention order, fact order). `this_mention` facts never leak to siblings.
- `compare_dimension`: considers facts of that key with a non-null `normalized_value`.
  None → `not_stated`. Per reported value r vs viewer value v:
  - zone: `r == v` → match; same number and exactly one side lacks a subzone → `imprecise`;
    otherwise `different` (adjacent zones and other subzones are different).
  - summer_heat: `r == v` → match; `r == "heatwave"` and `v == "hot"` → `imprecise`;
    `r == "heatwave"` otherwise → ignored (not comparable); else `different`.
  - every other key: equal → match, else `different`.
  Aggregate over values: any match → `match`; else any imprecise → `imprecise`; else any
  different → `different`; else `not_stated`. `reported_values` = distinct normalized values
  (first-seen order); `evidence` = facts of that key with non-null normalized value.
- `match_context`: `None` when `garden.selected()` is empty; otherwise one `DimensionMatch` per
  selected key (canonical `COMPARABLE_KEYS` order) and the tier.
- `tier_of`: any `different` → `different`; all `match` → `match`; any `match`/`imprecise` →
  `partial`; else `not_stated`. Empty input → `ValueError`.
  Outcome words (failure, success, great, died) **never** influence tier or order.
- `region_status`: `None` if no garden region; `same` if any fact with key `region` has
  `normalized_value == garden_region`; `elsewhere` if other normalized region facts exist;
  else `not_stated`. Region is never part of the tier (states differing ≠ climates differing).
- `explain`: one short, honest sentence, e.g.
  `"Matches your containers, zone 6b and humid."`,
  `"Matches your containers; hardiness zone and humidity not stated."`,
  `"Zone 6 reported (subzone unknown); growing method not stated."`,
  `"None of your selected conditions were stated."`,
  `"Different: in the ground (you: containers). Humidity not stated."`
  Use `vocab.value_label`/`KEY_LABELS`. `None` → `""`. Never mention success/quality/scores.

## Search (`search.py`)

```python
def search(conn, scope: ViewerScope, query: SearchQuery, now: str) -> SearchResult
def build_result(views: Sequence[CommentView], query: SearchQuery) -> SearchResult   # pure core
def relevant_mentions(view: CommentView, query: SearchQuery) -> tuple[MentionMatch, ...] | None
def chip_support(conn, scope, now, chips: Sequence[QuickChip]) -> dict[str, int]
def filter_options(conn, scope, now) -> FilterOptions   # crops, topics, years present (visible only)
```

1. Candidates: `db.visible_comment_ids(conn, scope, now, query.text)` → `db.load_comment_views`.
   Text search works for comments with no extraction at all.
2. Mention-level filters — `crop` (compare `normalize_crop(query.crop)` to mention crop),
   `topics` (non-empty intersection), `year` (an applicable `growing_year` fact equals it),
   `region_filter` (an applicable `region` fact equals it). If any of these is set, a comment
   is kept only if ≥ 1 mention passes **all** set filters; those are its relevant mentions.
   If none is set, all mentions are relevant and comments with zero mentions are kept.
3. For each relevant mention: `facts = applicable_facts(...)`, `match = match_context(...)`.
4. Comment tier = best (lowest `TIER_ORDER`) relevant-mention tier; a comment with no relevant
   mentions (only possible without mention filters) gets tier `not_stated` with all dimensions
   `not_stated`, when the garden has selections. Comment `region_status` = `same` if any relevant
   mention is `same`, else `elsewhere` if any, else `not_stated`. `explanation` = `explain` of the
   best mention's match (prefixed with `"<Variety or crop>: "` when the comment has > 1 relevant
   mention). `has_conditions` = any relevant mention has a normalized fact of a comparable key or
   region.
5. Groups: no comparable selection → single group `all`. Otherwise groups `match`, `partial`,
   `not_stated`, `different` (in that order; omit empty groups; `different` has
   `collapsed_by_default=True`). Within a group sort by `(region rank: same=0 else 1,
   source_id, source_order, comment_id)`.
6. `only_stated_matches=True` hides `not_stated` and `different` groups;
   `n_hidden_by_strict` counts the hidden comments. Counts are distinct comments, never mentions.
7. `limitation_message`: `n_with_conditions == 0` and `n_candidates > 0` →
   `"None of these {n} reports mention growing conditions, so none can be compared with your
   garden. All are shown below."`; `0 < n_with < n` → `"Only {n_with} of {n} reports mention
   growing conditions."`; otherwise `None`. (Use "report"/"reports" correctly for 1.)
8. `chip_support`: for each chip, the number of distinct visible comments having ≥ 1 mention
   whose applicable facts match **any** of the chip's (key, value) pairs.

## Geography (`geography.py`)

```python
@dataclass(frozen=True)
class RegionCounts: table: pd.DataFrame   # tidy: region_code, region_name, postal, n_comments, suppressed
                    not_reported: int; outside_us: int
def region_counts(results: Sequence[CommentResult], min_cell_count: int) -> RegionCounts
def choropleth(counts: RegionCounts) -> plotly.graph_objects.Figure
```

- Counts distinct comments per U.S. state from **relevant mentions' applicable region facts**
  (a comment naming CT for two varieties counts once for CT; a comment with CT and ME facts on
  different mentions counts once for each).
- Comments with no region fact count in `not_reported`; with only non-U.S. regions in
  `outside_us`. Zone-only facts never produce a region. No coordinates anywhere.
- Cells with `0 < n_comments < min_cell_count` are `suppressed=True` and their count replaced by
  `0` in the table/figure (shown as "fewer than k"). The app passes
  `cfg.map.public_min_cell_count` in public scope and `1` in private scope.
- `choropleth`: Plotly choropleth `locationmode="USA-states"`, `scope="usa"`, uses postal codes,
  hover shows count; suppressed cells hover "fewer than k reports". Never plots points.

## CLI (`__main__.py`) — `uv run python -m crop_explorer ...`

argparse; global `--config PATH` (default repo `config.yaml`) and `--db PATH` override.

```
ingest PATH --scope {private,synthetic,cleared} [--source-id ID (default: file stem)]
            [--kind KIND (default: comments_export)] [--source-url URL] [--expires-at ISO]
            [--id-col] [--text-col] [--permalink-col] [--date-col]
load-extractions PATH [--rejections-out PATH]   # prints ExtractionReport counts as JSON
export-for-extraction --out PATH [--only-unparsed]   # JSONL of {comment_id, text}; private data
delete-source SOURCE_ID
purge-expired
summary [--scope {public,private}]
demo                                             # ingest fixtures as synthetic + load extractions
```

Exit code 0 on success; on `IngestError`/`ValueError`/`KeyError` print a one-line error to
stderr and exit 2. Output only ids and counts — never comment text.

## Viewer (`app.py`, thin) + `ui.py` (pure helpers)

`uv run streamlit run app.py [-- --scope private]`. One Browse screen (`layout="centered"`,
phone-friendly; Streamlit stacks columns on narrow screens):

- Header "What Gardeners Are Saying", subtitle "Find real experiences from gardens like yours.",
  "About / Sources" expander. A banner states the scope: public → "Demo data: synthetic
  comments, not real gardener reports" when only synthetic sources are visible; private →
  "Private local view — includes unverified imported comments. Do not publish or share."
- **My growing conditions** panel (always visible, works with nothing selected): Region
  (selectbox of U.S. states then Canadian provinces then countries, "Not set" first), Zone
  (`VIEWER_VALUES["zone"]`), Growing method, Setting (shelter), Summer heat; "More conditions"
  expander: humidity, rainfall, season length, sun, soil, watering, year. Clear button resets
  every condition and filter (session state only; nothing persisted server-side).
- Quick chips from config, shown only when `chip_support >= min_reports_for_chip`; a chip sets
  its conditions.
- Search box ("Search crops, varieties, or anything people mentioned"), Crop selectbox,
  Topics multiselect, "Only show stated matches" checkbox (with the hidden count shown).
- Tabs: **Comments** | **Map**; plus an "Optional outside context" expander listing config links,
  each tagged with its label (`regional climate reference`, `published external trial`).
- The limitation message and per-group counts are always visible. Each card: title
  `"{variety} {crop} · {topics}"` (fallback "Comment"), the comment text rendered as **plain
  text** (escape Markdown via `ui.escape_markdown`; never `unsafe_allow_html` with source text),
  "Relevant: …", "Not stated: …", "Different: …" lines, region line, and an "Evidence" expander
  listing each chip's exact `evidence_quote` with the label `community report · reported
  conditions`. Source link (`st.link_button("Source ↗")`) only when a permalink exists; synthetic
  rows say "Synthetic demo comment — no source link".
- Map tab: choropleth + accessible state selectbox ("Filter by reported state") listing states
  with counts; selecting a state (or clicking the map, when Streamlit reports the selection)
  sets `region_filter`; "Region not reported: N" shown separately.
- `ui.py` pure helpers (unit tested): `escape_markdown`, `card_title`, `condition_lines`,
  `evidence_rows`, `scope_banner`, `region_options`, `reset_state_keys`.
