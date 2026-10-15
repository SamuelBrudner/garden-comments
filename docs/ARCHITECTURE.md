# Architecture and module contracts (MVP, SPEC v0.5)

This is the binding implementation contract for the MVP, updated to describe the code as built.
`SPEC.md` is the product spec; this document pins down the interfaces and the exact behavioural
rules so modules can be built and tested independently. Shared types live in:

| Module | Contents |
|---|---|
| `schemas.py` | Extraction contract (`EnvironmentFact`, `VarietyMention`, `ParsedComment`), validated forms (`ValidatedFact`, `ValidatedMention`, `ValidatedComment`, `Rejection`), raw records (`ColumnMapping`, `CommentRecord`, `SourceRecord`, `ImportStats`), literals (`Topic`, `EnvKey`, `RightsStatus`, `ImportScope`, `SCOPE_TO_RIGHTS`), `to_utc_iso`, `utc_now_iso` |
| `vocab.py` | `ComparableKey`, `COMPARABLE_KEYS`, `CATEGORICAL_VALUES`, `ZONE_PATTERN`, `YEAR_PATTERN`, `VIEWER_VALUES`, `PROMINENT_KEYS`, `KEY_LABELS`, `VALUE_LABELS`, `is_valid_normalized`, `value_label` |
| `regions.py` | `US_STATES`, `CA_PROVINCES`, `COUNTRIES`, `REGION_NAMES`, `is_us_state`, `us_postal`, `region_name` |
| `views.py` | `GardenContext`, `FactView`, `MentionView`, `CommentView`, `DimensionMatch`, `MatchResult`, `MentionMatch`, `CommentResult`, `SearchQuery`, `ResultGroup`, `SearchResult`, literals `ViewerScope`, `MatchStatus`, `Tier`, `RegionStatus`, `GroupKey`, constants `TIER_ORDER`, `GROUP_TITLES` |
| `config.py` | `AppConfig` and sub-models, `load_config(path)` |
| `logging_setup.py` | `configure_logging(cfg)` |

## Engineering rules (all modules)

- Python ≥ 3.12, full type hints, `mypy --strict` clean (`uv run mypy`, files `src` and `app.py`).
- `uv run ruff check .` and `uv run ruff format --check .` clean (`SPEC.md` is excluded from the
  formatter because it is kept verbatim). Ruff enforces numpy docstrings on public functions,
  ≤ 15 statements / ≤ 8 branches / ≤ 6 args per function, complexity ≤ 8. Keep functions small
  and pure; push I/O to thin edges.
- `pathlib.Path` only. No magic numbers: tunables come from `AppConfig`.
- **Fail loud and fast.** Raise on invalid input (`ValueError` subclasses with clear messages);
  no silent fallbacks. The only "drop and continue" path is extraction validation, which must
  *record* every rejection.
- **Never log comment bodies or evidence quotes**, and never put them in error messages. Log
  ids, counts and reasons only (loguru).
- Treat every imported comment as untrusted text: never interpret it, never render it as HTML
  or Markdown.
- Deterministic: stable sort keys everywhere; no randomness; time is passed in (`now: str`)
  rather than read inside library functions.
- **Canonical time.** Every timestamp a library function accepts or stores is the canonical
  UTC form `YYYY-MM-DDTHH:MM:SSZ` produced by `schemas.to_utc_iso` (always a four-digit year, so
  string order equals time order). `visibility.require_utc_iso(value, name)` enforces it and
  raises `ValueError` for anything else (bare dates, offsets, fractions, spaces).
- Tests: pytest, written first (red → green). Use Hypothesis for property tests of pure logic.

## Data flow

```
CSV/JSONL export --ingest.read_comments--> CommentRecord[] --db.sync_source_comments--> SQLite
                                                                     |
db.iter_comments_for_extraction --CLI export-for-extraction--> {comment_id, text} JSONL (0600)
model step (offline, docs/EXTRACTION_PROMPT.md) --> ParsedComment JSONL
   --extraction.load_extractions (validate vs stored text)--> db.replace_extraction
                                                                     |
viewer (app.py) --search.search(conn, scope, query, now)--> SearchResult (groups, counts)
               --geography.region_counts(...)--> map counts
```

No model call happens at request time.

## Storage (`db.py`)

SQLite via stdlib `sqlite3`. `PRAGMA foreign_keys = ON` on every connection (verified; otherwise
`RuntimeError`). Connections use SQLite autocommit (`isolation_level=None`); every write runs in
its own `SAVEPOINT`, so writes nest safely inside a caller's transaction. Connections keep
sqlite3's default `check_same_thread=True`: open one connection per thread / script run (the
viewer opens one per Streamlit run). Tables (spec §8), plus a small `parses` table for
comment-level extraction status:

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
comments_fts  -- external-content FTS5 (content='comments', content_rowid='id'),
              -- tokenize='porter unicode61 remove_diacritics 2', kept in sync by triggers on
              -- comments (insert / update of id or text / delete, incl. cascaded deletes)
```

Indexes on `comments(source_id)`, `mentions(comment_id)`, `environment_facts(mention_id)`.
Ids are deterministic: `mention_id = f"{comment_id}#m{index}"`,
`fact_id = f"{mention_id}#f{index}"`, where the indexes are 0-based positions among the
*accepted* mentions / facts. `mentions.status` stores the comment's parse status. Id lists are
bound as one JSON parameter (`json_each(?)`), so there is no SQLite variable-count limit; a bare
`str` or non-`str` ids raise `TypeError`.

Public API (all writes transactional; on error the savepoint rolls back and the error
re-raises):

```python
MEMORY_PATH = Path(":memory:")
SUMMARY_KEYS = ("sources", "comments", "parsed_comments", "mentions", "facts")
def connect(path: Path) -> sqlite3.Connection
    # mkdir parents (not for MEMORY_PATH), foreign_keys ON, row_factory=sqlite3.Row, init_schema
def init_schema(conn: sqlite3.Connection) -> None            # idempotent
def sync_source_comments(conn, source: SourceRecord, comments: Sequence[CommentRecord]) -> ImportStats
def replace_extraction(conn, validated: ValidatedComment, loaded_at: str) -> None
def delete_source(conn, source_id: str) -> int                # #comments removed; KeyError if unknown
def purge_expired(conn, now: str) -> list[str]                # deletes sources with expires_at <= now
def stored_texts(conn, comment_ids: Sequence[str]) -> dict[str, str]   # pipeline-internal (no scope)
def iter_comments_for_extraction(conn, only_unparsed: bool, now: str) -> list[tuple[str, str]]
def fts_query(text: str) -> str | None                         # pure sanitizer, see below
def visible_comment_ids(conn, scope: ViewerScope, now: str, text: str | None) -> list[str]
def load_comment_views(conn, scope: ViewerScope, now: str, comment_ids: Sequence[str] | None) -> list[CommentView]
def summary_counts(conn, scope: ViewerScope, now: str) -> dict[str, int]
```

`sync_source_comments` semantics (idempotent re-import):

- Validates first (nothing written on failure): duplicate ids within one call and
  non-canonical `imported_at` / `expires_at` / `created_at` raise `ValueError`.
- Upserts the `sources` row (updates `dataset_hash`, `imported_at`, rights, expiry, url, kind).
- A `comment_id` already owned by a *different* source → `ValueError` (collision; the message
  lists ids and owning source ids, never text).
- New comment → insert. Same id, same `text_hash` (sha256 of UTF-8 text) → unchanged (keeps its
  extraction and its original `imported_at`; permalink, `created_at` and `source_order` are
  refreshed). Same id, different text → update text (`imported_at` = the source's) and
  **delete its parse/mentions/facts** (their quotes may no longer be substrings).
- Comments of this source absent from the new import → deleted (cascade). An empty `comments`
  sequence is allowed and deletes them all (ingest never passes one: an empty file is an error).
- A soft-deleted comment (`deleted_at` set) stays soft-deleted across re-imports.
- Returns `ImportStats(source_id, inserted, updated, unchanged, deleted)`.

`replace_extraction` deletes the comment's existing `parses`/`mentions` (facts cascade) and
inserts the validated ones in one transaction. Re-loading identical JSONL is a no-op in effect.
Raises `KeyError` if the comment does not exist, and `ValueError` (writing nothing) if
`loaded_at` is not canonical or any mention/fact quote is not an exact substring of the stored
text — defence in depth behind `extraction.py` (SPEC §11 #18). `ValidatedComment.rejections` are
not stored; they are reported by the loader.

`iter_comments_for_extraction` is pipeline-internal: it ignores rights status (extraction runs
on private data) but excludes soft-deleted comments **and comments of sources whose
`expires_at <= now`** (permission to process them has ended even before `purge_expired` runs).
Ordered `(source_id, source_order, comment_id)`.

`fts_query(text)`: extract `\w+` tokens (Unicode); none → `None` (no text filter); otherwise
`" ".join(f'"{tok}"*' for tok in tokens)` (implicit AND, prefix match). User text is never
passed to `MATCH` unsanitized.

`visible_comment_ids`: ids of comments visible in `scope` at `now` (see visibility), filtered by
FTS when `fts_query(text)` is not `None`, ordered `ORDER BY c.source_id, c.source_order,
c.comment_id`. Text search finds comments that were never extracted.

`load_comment_views`: `CommentView`s (mentions ordered by `mention_index`, facts by
`fact_index`) for the given ids (or all visible when `None`), **re-applying the visibility
clause** (an id that is not visible is silently absent — ids may come from another scope's
query; invisible rows are never returned). Same ordering as above. The three reads run in one
savepoint, so they see one consistent snapshot.

`summary_counts`: keys `SUMMARY_KEYS`, restricted to the visible scope. `sources` counts sources
with at least one visible comment; `parsed_comments` counts visible comments with a `parses`
row of any status.

## Visibility / rights gating (`visibility.py`)

```python
PUBLIC_RIGHTS: frozenset[RightsStatus] = frozenset({"synthetic", "cleared"})
def visible_rights(scope: ViewerScope) -> frozenset[RightsStatus]   # private -> all statuses
def visibility_clause(scope: ViewerScope, now: str, *, source_alias: str = "s",
                      comment_alias: str = "c") -> tuple[str, list[str]]
def require_utc_iso(value: str, name: str) -> str                    # canonical-time guard
```

`visibility_clause` returns a parenthesised SQL boolean term plus parameters:
`(s.rights_status IN (?, ...) AND (s.expires_at IS NULL OR s.expires_at > ?) AND
c.deleted_at IS NULL)` with parameters `[*sorted(rights), now]`. Unknown scope, non-canonical
`now` or an alias that is not a plain identifier → `ValueError`. **Every** viewer-facing read in
`db.py` (ids, views, counts) joins `comments c JOIN sources s` and applies this clause, so
private rows can never reach a public count, chip, map cell, snippet or search hit. The
Streamlit app never caches query results (no `st.cache_data`; the only `st.cache_resource` is
the once-per-process logging setup).

## Ingestion (`ingest.py`)

```python
SUPPORTED_SUFFIXES = (".csv", ".jsonl")
class IngestError(ValueError)
def read_comments(path: Path, mapping: ColumnMapping) -> list[CommentRecord]
def dataset_hash(path: Path) -> str                       # sha256 hex of file bytes
def build_source(path: Path, *, source_id: str, kind: str, rights_status: RightsStatus,
                 source_url: str | None, expires_at: str | None, imported_at: str) -> SourceRecord
def import_file(conn, path: Path, mapping: ColumnMapping, source: SourceRecord) -> ImportStats
```

- Format from the suffix, case-insensitively: `.csv` or `.jsonl`; other → `IngestError` (checked
  before the file is read). Bytes are decoded as strict UTF-8 (`IngestError` with line and byte
  offset on failure; a leading BOM is tolerated). Unreadable files → `IngestError`.
- CSV: `csv.DictReader(strict=True)` (non-strict mode silently alters text). Malformed CSV, a
  row whose field count differs from the header, a mapped column missing or repeated in the
  header → `IngestError`. Fields are limited by `csv.field_size_limit()` (131072 characters);
  longer comments fail loudly.
- JSONL: split on `\n` only (raw U+2028/U+2029 are legal inside JSON strings; CRLF is fine);
  whitespace-only lines are skipped. Each line must be one JSON object without duplicate keys;
  mapped values must be JSON strings (or null / absent for optional fields).
- Required: id and text present; every row has a non-blank id without surrounding whitespace
  and non-blank text. Duplicate ids → `IngestError` naming every duplicated id with its rows.
  Empty input (zero bytes, whitespace, BOM only, header-only CSV) → `IngestError`, because
  importing it would delete every stored comment of the source. The mapping must name distinct
  columns.
- Optional permalink/date columns: unmapped or absent → `None` for all rows; blank cells →
  `None` (cells are trimmed before this check). Dates → `to_utc_iso` (`IngestError` with
  row/line and column). Permalinks must match `https?://` followed by non-space characters.
- `source_order` = 0-based row order (CSV row numbers in errors are 1-based data rows, JSONL
  line numbers are 1-based physical lines).
- Text is stored exactly as given (no stripping/normalising) so evidence quotes stay exact
  substrings.
- `build_source` validates every argument (`IngestError` naming it): `source_id` and `kind`
  non-blank without surrounding whitespace, `source_url` an http(s) URL when given, a known
  `rights_status`, `imported_at`/`expires_at` normalised with `to_utc_iso`.
- `import_file` reads the file **once**, checks that the SHA-256 of those bytes equals
  `source.dataset_hash` (`IngestError` otherwise: the file changed after `build_source` hashed
  it), parses the same bytes and calls `db.sync_source_comments`. Nothing is written on error.
- Error messages name the file, row/line and column but never a cell value (a swapped mapping
  would otherwise echo comment text); exceptions whose own text contains a cell are re-raised
  `from None`. The one exception is duplicate-id errors, which name the ids. Only
  `Read N comments from <file name>` is logged.

## Normalization (`normalization.py`)

```python
STRICT_KEYS: frozenset[EnvKey] = frozenset({"region", "zone", "growing_year"})
def normalize_written(key: EnvKey, written: str) -> str | None
def normalize_crop(crop: str) -> str
def variety_key(variety: str) -> str
def parse_zone(value: str) -> tuple[int, str | None]      # "6b" -> (6, "b"); "6" -> (6, None)
```

`normalize_written` maps an as-written phrase to a normalized vocabulary value deterministically
using small explicit synonym tables; it returns `None` when nothing is recognised or the phrase
is ambiguous (fail-safe: an unnormalized fact stays display-only, it is never mislabelled).
`ValueError` for a key that is not an `EnvKey`. Rules:

- Matching: typographic apostrophes/hyphens unified, whitespace collapsed, case-insensitive
  (except postal codes and dotted abbreviations). Phrases are bounded by non-word characters and
  a hyphen counts as part of a word ("non-humid", "semi-arid" never match "humid"/"arid"; a
  trailing "-grown" is allowed). Inside a phrase, words may be joined by a space, a hyphen or
  nothing ("heat wave" / "heat-wave" / "heatwave"). Overlaps are resolved **longest phrase
  first**.
- Negation: a phrase is dropped when one of the 3 words before it in the same clause (clauses
  split on `, ; : . ! ?` and brackets) is a negation cue (no, not, never, without, didn't,
  isn't, ...). "not humid" → `None`; phrases that start with the negation ("no rain" →
  `dry_season`) still match.
- Blockers: look-alike phrases consume their text without yielding a value ("hot peppers",
  "heat mat", "clay pots", "shade cloth", "humidity dome", "rain barrel", "started indoors",
  ...; for regions the city names "Kansas City" and "Nevada City", so "Kansas City, Missouri"
  is `US-MO` while a bare "Kansas City" stays `None`).
- Region: full state/province/country names and aliases anywhere in the phrase
  ("in Connecticut" → `US-CT`; "New England" → `US`; "New South Wales" → `AU`, not Wales;
  Washington DC forms → `US-DC`). Dotted abbreviations ("Conn.", "Calif.", "N.Y.") match
  case-sensitively ("a huge mass." is not Massachusetts). Two-letter postal codes only as the
  whole phrase ("CT") or right after a comma and followed by the end, punctuation, a ZIP code or
  a country ("Hartford, CT 06103"); "in it" never → `US-IN`; "US"/"GB" alone → `None`.
  Hierarchy: one subdivision plus only its own country gives the subdivision
  ("Ontario, Canada" → `CA-ON`); any other mix of places is ambiguous → `None` ("Toronto, ON,
  USA", "I live in Florida but grew these in Maine").
- Zone: "zone 6b", "Zone 6B", "z6b", "USDA 7a", "hardiness zone 5" → `6b`/`7a`/`5`; a bare value
  ("6b") only when it is the whole phrase. Ranges, lists, split subzones ("zones 6-7", "6b/7a"),
  out-of-range zones and two different zones → `None`; the same zone repeated is fine.
- Year: exactly one distinct 19xx/20xx year (repeats allowed; "mid-2021" → `2021`); two years or
  a range ("2022-2023", "2023/24") → `None`.
- Categorical examples: "pots", "containers", "grow bags", "5 gallon buckets" → `container`;
  "raised beds" → `raised_bed`; "in the ground" → `in_ground`; "greenhouse", "hoop house",
  "high tunnel", "polytunnel" (and "inside the greenhouse") → `greenhouse`; "outside the
  greenhouse" → `outdoors`; "muggy", "humid" → `humid`; "hot summers", "brutal heat" → `hot`;
  "heat wave" → `heatwave`; "short season" → `short_season`; "clay" → `clay`; "drip" → `drip`.
  Two conditions in one phrase ("sandy loam", "full sun with afternoon shade") → `None`.
- Never infer region from a zone, or humidity from a region.

`normalize_crop`: casefold, strip, collapse whitespace, singularise the **last word** only
("brussels sprouts" → "brussels sprout"): `-sses`→`-ss`, `-ches`→`-ch`, `-shes`→`-sh`,
`-oes`→`-o`, `-ies`→`-y` (words of 5+ letters), otherwise drop a final `-s`; words shorter than 4
letters and words ending in `ss`/`us`/`is` are unchanged. Idempotent. Empty → `ValueError`.

`variety_key`: casefold; typographic apostrophes/hyphens → ASCII; keep hyphens and apostrophes
only between two word characters; every other quote/punctuation mark becomes a space; collapse
whitespace (`'"Sun Gold"'` → `sun gold`, `"Aunt Ruby's German Green"` → `aunt ruby's german
green`, `"Mr. Stripey"` → `mr stripey`). Empty result → `ValueError`.

`parse_zone` accepts only already-normalized values (`vocab.ZONE_PATTERN`); `"6B"`, `"zone 6"`
→ `ValueError`.

## Extraction validation and loading (`extraction.py`)

```python
class ExtractionReport(BaseModel):  # frozen pydantic model
    lines_read, schema_errors, unknown_comment_ids, duplicate_comment_ids, comments_loaded,
    status_counts: dict[ParseStatus, int], mentions_accepted, mentions_rejected,
    facts_accepted, facts_rejected, unresolved_identities, rejections: tuple[Rejection, ...]
def read_extraction_jsonl(path: Path) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]
def validate_parsed_comment(parsed: ParsedComment, text: str, cfg: ExtractionConfig) -> ValidatedComment
def load_extractions(conn, path: Path, cfg: ExtractionConfig, loaded_at: str) -> ExtractionReport
```

Validation rules. Each failure appends a `Rejection` with a machine-readable reason:
`schema_error`, `unknown_comment_id`, `duplicate_comment_id`, `mention_quote_not_substring`,
`variety_not_in_quote`, `invalid_crop`, `invalid_variety`, `fact_quote_not_substring`,
`value_not_in_quote`, `invalid_normalized_value`, `strict_key_mismatch`,
`normalized_value_mismatch`, `hypothetical_context`, `conflicting_region`, `conflicting_zone`.
Rejection `mention_index`/`fact_index` are positions in the extractor's input lists.

1. Mention `evidence_quote` must be an **exact** (case-sensitive) substring of the comment text,
   else the mention and all its facts are rejected (one mention-level rejection covers them).
2. `variety_as_written`, when present, must occur case-insensitively within the mention quote.
3. `crop` is normalised with `normalize_crop` and `variety_key` from `variety_key()`; a value
   they reject (blank crop, variety "--") rejects the mention (`invalid_crop` /
   `invalid_variety`). Topics are de-duplicated in first-seen order.
4. Fact `evidence_quote` must be an exact substring of the comment text; `value_as_written` must
   occur case-insensitively within the fact's `evidence_quote`. Normalisation (rule 5) runs on
   the value **as spelled in the quote** (the exact-case occurrence if there is one), so an
   extractor writing "IN" for a quote containing only the word "in" cannot create `US-IN`.
   The stored `value_as_written` stays as the extractor wrote it.
5. Normalised value: if provided it must satisfy `vocab.is_valid_normalized`
   (`invalid_normalized_value`); for `STRICT_KEYS` it must equal `normalize_written(key,
   value)` (`strict_key_mismatch`), so "zone 6b" can never become a region and a residence-only
   phrase cannot be re-labelled; for every other key it must not contradict a non-null
   `normalize_written` result (`normalized_value_mismatch`: "pots" marked `raised_bed`), while a
   value the loader cannot derive ("balcony railing" → `container`) is accepted. If not provided,
   use `normalize_written`; it may stay `None` (a display-only fact such as "Cape Cod"), except
   that a strict-key fact whose phrase is really another strict key's value (region "zone 6b",
   zone "Connecticut", region "2023") is rejected (`strict_key_mismatch`).
6. Hypothetical/advice guard: split the comment text into sentences; `!`, `?` and line breaks
   always end a sentence, `.` ends one only when followed by whitespace, a closing quote or
   bracket, or the end of the text, and not after a known abbreviation (Mr, Mrs, Dr, St, e.g,
   i.e, vs, var, cv, gal, lb, oz, ...). Each delimiter belongs to the sentence it ends. If any
   sentence overlapping **any occurrence** of the fact's evidence quote contains a configured
   marker (`cfg.hypothetical_markers`: case-insensitive, word-bounded, a space in a marker
   matches any whitespace, apostrophes `'` `’` `‘` `ʼ` interchangeable) → reject the fact
   (`hypothetical_context`). Joining sentences can only make the guard stricter, so
   "I'd try Mr. Stripey in zone 5" and "I'd space them 2.5 feet apart in zone 5" are rejected.
   A blank configured marker is a config error (`ValueError`).
7. Within one mention, accepted facts with key `region` (or `zone`) and two or more distinct
   non-null normalised values → reject every fact of that key in that mention
   (`conflicting_region` / `conflicting_zone`). The same value repeated is not a conflict.
8. Status: keep the extractor's status, but downgrade `parsed` → `partial` if anything was
   rejected. A comment with zero mentions is valid.

`read_extraction_jsonl`: lines split on `\n` only (a trailing `\r` is tolerated, a leading BOM
ignored), numbered by physical line, blank lines skipped. A line that is not UTF-8, not JSON,
repeats a key, nests too deeply or does not match `ParsedComment` becomes a `schema_error`
rejection that keeps only its `comment_id` (when readable). An unreadable file → `ValueError`.

`load_extractions`: checks `loaded_at` and the markers, reads the file, then — inside **one**
savepoint, all-or-nothing — looks up stored text via `db.stored_texts`, rejects ids repeated in
the file (`duplicate_comment_id`, every copy, including a malformed copy's readable id) and ids
not in the database (`unknown_comment_id`), validates the rest and calls
`db.replace_extraction` per comment. Every non-blank line lands in exactly one of
`schema_errors`, `duplicate_comment_ids`, `unknown_comment_ids`, `comments_loaded`.
`status_counts` always lists parsed, partial, unresolved. Mention and fact counts cover loaded
comments only; `unresolved_identities` = accepted mentions with neither crop nor variety. Logs
one INFO line of counts and one WARNING with per-reason counts (file name only, never text).

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
  mentions **whose key the mention does not itself state** (any own fact of a key, even an
  unnormalized one, blocks inheritance of that key). `siblings` may include the mention itself
  (recognised by `mention_id`). Deduplicate by `(key, normalized_value, value_as_written)`.
  Order: own facts first (fact order), then sibling facts (mention order, fact order).
  `this_mention` facts never leak to siblings.
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
  `ValueError` for a non-comparable key (e.g. `region`), a viewer value not in
  `VIEWER_VALUES[key]`, or a stored normalized value that fails `is_valid_normalized`.
- `match_context`: `None` when `garden.selected()` is empty; otherwise one `DimensionMatch` per
  selected key (canonical `COMPARABLE_KEYS` order) and the tier.
- `tier_of`: any `different` → `different`; all `match` → `match`; any `match`/`imprecise` →
  `partial`; else `not_stated`. Empty input or an unknown status → `ValueError`.
  Outcome words (failure, success, great, died) **never** influence tier or order.
- `region_status`: `None` if no garden region; `same` if any fact with key `region` has
  `normalized_value == garden_region`; `elsewhere` if other normalized region facts exist;
  else `not_stated`. Unknown region codes → `ValueError`. Region is never part of the tier
  (states differing ≠ climates differing).
- `explain`: short, honest sentences built only from dimension statuses, in canonical key
  order: an optional `"Different: <reported> (you: <yours>), ..."` sentence, then one sentence
  joining the matched, imprecise, not-comparable and not-stated clauses with `"; "`. Examples:
  `"Matches your zone 6b, containers and humid."`,
  `"Matches your containers; hardiness zone and humidity not stated."`,
  `"Zone 6 reported (subzone unknown); growing method not stated."`,
  `"None of your selected conditions were stated."` (only when nothing selected was reported),
  `"Different: in the ground (you: containers). Humidity not stated."`,
  and `"summer heat not comparable (a heatwave reported)"` for a heatwave against mild/cool
  summers. `None` → `""`. Never mentions success/quality/scores/recommendations.

## Search (`search.py`)

```python
class FilterOptions(BaseModel): crops: tuple[str, ...]; topics: tuple[Topic, ...]; years: tuple[str, ...]
def search(conn, scope: ViewerScope, query: SearchQuery, now: str) -> SearchResult
def build_result(views: Sequence[CommentView], query: SearchQuery) -> SearchResult   # pure core
def relevant_mentions(view: CommentView, query: SearchQuery) -> tuple[MentionMatch, ...] | None
def chip_support(conn, scope, now, chips: Sequence[QuickChip]) -> dict[str, int]
def filter_options(conn, scope, now) -> FilterOptions   # distinct, sorted, visible data only
```

1. Candidates: `db.visible_comment_ids(conn, scope, now, query.text)` → `db.load_comment_views`.
   Text search works for comments with no extraction at all. `build_result` ignores
   `query.text`.
2. Mention-level filters — `crop` (`normalize_crop(query.crop)` equals the stored mention crop
   exactly), `topics` (non-empty intersection), `year` (an applicable `growing_year` fact equals
   it), `region_filter` (an applicable `region` fact equals it). If any of these is set, a
   comment is kept only if ≥ 1 mention passes **all** set filters; those are its relevant
   mentions (`relevant_mentions` returns `None` otherwise). If none is set, all mentions are
   relevant and comments with zero mentions are kept (`()`). Invalid filters fail loudly
   (`ValueError`): a year not matching `YEAR_PATTERN`, an unknown region code, a blank crop, and
   duplicate comment ids in the `build_result` input.
3. For each relevant mention: `facts = applicable_facts(...)`, `match = match_context(...)`.
4. Comment tier = best (lowest `TIER_ORDER`) relevant-mention tier, ties to the earlier mention;
   a comment with no relevant mentions under a selected garden gets `match_context(garden, ())`
   (tier `not_stated`). Comment `region_status` = `same` if any relevant mention is `same`, else
   `elsewhere` if any, else `not_stated`. `explanation` = `explain` of the best mention's match,
   prefixed with `"<variety_as_written, else crop, else 'Unidentified variety'>: "` when the
   comment has > 1 relevant mention and the explanation is not empty. `has_conditions` = an
   applicable fact of a relevant mention has a non-null normalized value for a comparable key or
   `region` (a growing year alone or a display-only fact does not count).
5. Groups: no comparable selection → single group `all`. Otherwise groups `match`, `partial`,
   `not_stated`, `different` (in that order; `different` has `collapsed_by_default=True`). Empty
   groups are always omitted, including `all` (zero candidates → `groups == ()`). Within a group
   (including `all`) sort by `(region rank: same=0 else 1, source_id, source_order,
   comment_id)`; region never changes a tier.
6. `only_stated_matches=True` hides the `not_stated` and `different` groups and
   `n_hidden_by_strict` counts their comments; with no comparable condition selected there is
   nothing to match, so it hides nothing. `tier_counts` has all four tiers (zeros included) when
   a comparable condition is selected, else `{}`. Counts are distinct comments before hiding,
   never mentions.
7. `limitation_message`: `n_candidates == 0` or every candidate has conditions → `None`;
   no candidate has conditions → `"None of these {n} reports mention growing conditions, so none
   can be compared with your garden. All are shown below."` (for exactly one: `"The 1 report
   found does not mention growing conditions, so it cannot be compared with your garden. It is
   shown below."`); otherwise `"Only {n_with} of {n} reports mention growing conditions."`
   ("mentions" when `n_with == 1`). Independent of whether a garden is selected.
8. `chip_support`: for each chip (labels must be distinct, else `ValueError`), the number of
   distinct visible comments having ≥ 1 mention whose applicable facts state **any** of the
   chip's (key, value) pairs; a heatwave does not support "hot". Result in chip order.
   `chip_support` and `filter_options` load every visible view (fine at MVP size; move to SQL
   aggregates that keep `visibility_clause` if the corpus grows a lot).

## Geography (`geography.py`)

```python
TABLE_COLUMNS = ("region_code", "region_name", "postal", "n_comments", "suppressed")
@dataclass(frozen=True)
class RegionCounts: table: pd.DataFrame; not_reported: int; outside_us: int
                    us_state_not_reported: int; min_cell_count: int
def region_counts(results: Sequence[CommentResult], min_cell_count: int) -> RegionCounts
def count_label(n_comments: int, *, suppressed: bool, min_cell_count: int) -> str
def choropleth(counts: RegionCounts) -> plotly.graph_objects.Figure
```

- Counts distinct comments per U.S. state from **relevant mentions' applicable region facts**
  (`result.mention_matches[*].facts`; a comment naming CT for two varieties counts once for CT;
  a comment with CT and ME facts on different mentions counts once for each). Mentions dropped
  by other filters never place a comment.
- Every comment lands in exactly one place, checked in order: at least one U.S. state code → one
  count per distinct state; else the country code `US` only → `us_state_not_reported`; else only
  non-U.S. codes → `outside_us`; else (no normalized region, including display-only regions and
  zone-only comments) → `not_reported`. No coordinates anywhere.
- Cells with `0 < n_comments < min_cell_count` are `suppressed=True` with their count replaced
  by `0` (labelled "fewer than k reports" by `count_label`). The table holds states with ≥ 1
  report, sorted by displayed count descending then `region_code`. The app passes
  `cfg.map.public_min_cell_count` in public scope and `1` in private scope.
- `ValueError` for `min_cell_count < 1`, duplicate comment ids, or an unknown region code (the
  message names ids, never the value). `RegionCounts` validates its own shape.
- `choropleth`: one `go.Choropleth`, `locationmode="USA-states"`, `geo.scope="usa"`, postal
  codes, single-hue sequential blue scale, hover text from `count_label` (the raw `z` is never
  shown), no lat/lon, no comment text. Never plots points.
- Privacy limit: suppression is per cell. With `min_cell_count > 1`, a suppressed count can
  sometimes be derived by subtraction from the bucket totals; add complementary suppression
  before publishing real cleared data with small cells.

## CLI (`__main__.py`) — `uv run python -m crop_explorer ...`

```python
def main(argv: Sequence[str] | None = None) -> int
def build_parser() -> argparse.ArgumentParser
EXIT_OK = 0; EXIT_ERROR = 2; DEFAULT_KIND = "comments_export"
DEMO_SOURCE_ID = "synthetic-demo"; DEMO_KIND = "synthetic_fixture"; DEMO_COMMENTS; DEMO_EXTRACTIONS
```

argparse; global `--config PATH` (default repo `config.yaml`) and `--db PATH` override, accepted
before or after the command (a relative `--db` resolves against the current directory).

```
ingest PATH --scope {private,synthetic,cleared} [--source-id ID (default: file stem)]
            [--kind KIND (default: comments_export)] [--source-url URL] [--expires-at ISO]
            [--id-col] [--text-col] [--permalink-col] [--date-col]
load-extractions PATH [--rejections-out PATH]   # report counts; rejections written as JSONL
export-for-extraction --out PATH [--only-unparsed]   # JSONL of {comment_id, text}; private data
delete-source SOURCE_ID
purge-expired
summary [--scope {public,private}]               # default: viewer.scope from the config
demo                                             # ingest fixtures as synthetic + load extractions
```

- Success: exactly one line of JSON on stdout, exit 0. `ingest` → `ImportStats`;
  `load-extractions` → the report counts with `rejections` as a number; `export-for-extraction`
  → `{"exported", "out"}`; `delete-source` → `{"source_id", "comments_deleted"}`;
  `purge-expired` → `{"purged_sources": [...]}`; `summary` → `{"scope", ...SUMMARY_KEYS}`;
  `demo` → `{"import": ImportStats, "extraction": report counts}`.
- Errors: `IngestError`/`ValueError`/`KeyError`, `OSError` (missing config, unwritable output),
  `yaml.YAMLError` (malformed config) and `sqlite3.Error` (a file that is not a database) print
  exactly one `error: <message>` line to stderr and exit 2; argparse usage errors also exit 2.
  `--help` exits 0. Output and logs carry only ids and counts — never comment text.
- `load-extractions` exits 0 even with rejections (they are recorded and counted);
  `--rejections-out` is always written (one `Rejection` per line, possibly empty).
- `export-for-extraction` makes the output owner-only (mode 0600) before writing, writes
  `ensure_ascii=False` JSON with U+0085/U+2028/U+2029 escaped (one record per physical line for
  any splitter), and skips soft-deleted comments and expired sources.
- `demo` ingests `fixtures/synthetic_comments.jsonl` as source `synthetic-demo` (kind
  `synthetic_fixture`, rights `synthetic`, no expiry) and loads
  `fixtures/synthetic_extractions.jsonl`; re-running it is idempotent. The fixtures are found
  relative to the source tree (checkout or editable install).
- `imported_at`, `loaded_at` and `now` come from `schemas.utc_now_iso()` at the CLI edge.

## Viewer (`app.py`, thin) + `ui.py` (pure helpers)

`uv run streamlit run app.py [-- --scope private --db PATH --config PATH]`. One Browse screen
(`layout="centered"`, phone-friendly; Streamlit stacks columns on narrow screens):

- Header "What Gardeners Are Saying", subtitle "Find real experiences from gardens like yours.",
  "About / Sources" expander (visible sources with rights labels and summary counts). A banner
  states the scope: public → "Demo data: synthetic comments, not real gardener reports" when
  only synthetic sources are visible (a mixed notice when synthetic and cleared are both
  visible; none for cleared only); private → "Private local view — includes unverified imported
  comments. Do not publish or share." A `private_unverified` row in public scope raises.
- If the database file does not exist, the app shows how to build it
  (`uv run python -m crop_explorer demo`) and stops; it never creates an empty database.
- **My growing conditions** panel (always visible, works with nothing selected): Region
  (selectbox: "Not set", then U.S. states, Canadian provinces, countries, each by name), Zone
  (`VIEWER_VALUES["zone"]`, with a note on what zones do not describe), Growing method,
  Setting (shelter), Summer heat; "More conditions" expander: humidity, rainfall, season length,
  sun, soil, watering, growing year. Clear resets every condition and filter (session state
  only; nothing persisted server-side).
- Quick chips from config, shown only when `chip_support >= min_reports_for_chip`; a chip sets
  its conditions and is highlighted while they are all set.
- Search box ("Search crops, varieties, or anything people mentioned"), Crop selectbox, Topics
  multiselect, "Only show stated matches" checkbox; a caption always says how many reports the
  checkbox hides (or would hide). Filter values that vanish from the visible data mid-session
  are reset (`ui.stale_state_fixes`).
- Tabs: **Comments** | **Map**; plus an "Optional outside context" expander listing config links,
  each tagged with its label badge (`regional climate reference`, `published external trial`).
- The limitation message and per-group counts are always visible. Each card: title
  `"{variety} {crop}, ... · {topics}"` (fallback "Comment"), the comment text rendered as
  **plain text** through `ui.escape_markdown`, then one caption with the condition lines from
  `ui.condition_lines(result, garden)` ("Relevant: …" incl. imprecise matches with both values,
  "Not stated: …", "Different: …", led by "Shown for X (1 of n mentions)" when several mentions
  are relevant; without a selection "Reported: …" or "Growing conditions not stated") and the
  region line; an "Evidence" expander listing each fact's exact `evidence_quote` with the label
  `community report · reported conditions`, its basis and what it applies to. Source link
  (`st.link_button("Source ↗")`) only when a permalink exists; synthetic rows say "Synthetic demo
  comment — no source link". `CommentResult.explanation` is not shown separately because the
  condition lines carry the same information in structured form.
- Map tab: choropleth built from the same query **without** `region_filter` (so it stays usable
  as a selector) over the shown groups, plus an accessible "Filter by reported state" selectbox
  (alphabetical, labelled with `geography.count_label`, so suppressed counts never show; the
  current filter stays selectable). Selecting a state, or clicking it on the map, sets
  `region_filter`. The chart's widget key is `ui.map_key(region_filter)`, so it remounts
  without a stale click selection whenever the filter changes. "Region not reported: N",
  "Outside the U.S.: N" and (when non-zero) "In the U.S., state not reported: N" are shown
  separately.
- Logging is configured once per server process (`st.cache_resource` around
  `configure_logging` only); a database connection is opened per script run.
- `ui.escape_markdown` backslash-escapes every ASCII punctuation character, inserts an invisible
  U+2060 word joiner after every `:` and before every `.` (so emoji/icon shortcodes and GFM
  autolink literals cannot form), turns leading spaces/tabs into NBSP and line breaks into hard
  breaks. Copied text therefore contains invisible U+2060 characters.
- `ui.py` pure helpers (unit tested), main ones: `escape_markdown`, `card_title`,
  `condition_lines(result, garden)`, `region_line`, `evidence_rows` (`EvidenceRow`),
  `scope_banner`, `region_options`, `reset_values`/`reset_state_keys`, `garden_from_state`,
  `query_from_state`, `stale_state_fixes`, `visible_chips`/`chip_state`/`chip_is_active`,
  `count_summary`, `group_heading`, `strict_note`, `source_link`, `provenance_note`,
  `source_rows`, `summary_line`, `outside_link_markdown`, `label_badge`, `min_cell_count`,
  `map_key`, `map_notes`, `map_colorbar`, `state_choices`, `selected_state`, `parse_app_args`.
