# Architecture and module contracts (MVP, SPEC v0.5)

This is the binding implementation contract for the MVP, updated to describe the code as built.
`SPEC.md` is the product spec; this document pins down the interfaces and the exact behavioural
rules so modules can be built and tested independently. Shared types live in:

| Module | Contents |
|---|---|
| `schemas.py` | Extraction contract (`EnvironmentFact`, `VarietyMention`, `ParsedComment`), validated forms (`ValidatedFact`, `ValidatedMention`, `ValidatedComment`, `Rejection`), raw records (`ColumnMapping`, `CommentRecord`, `SourceRecord`, `ImportStats`), literals (`Topic`, `EnvKey`, `RightsStatus`, `ImportScope`, `SCOPE_TO_RIGHTS`), `to_utc_iso`, `utc_now_iso` |
| `vocab.py` | `ComparableKey`, `COMPARABLE_KEYS`, `SELF_DESCRIBED_KEYS`, `DESCRIBED_AS`, `CATEGORICAL_VALUES`, `ZONE_PATTERN`, `YEAR_PATTERN`, `VIEWER_VALUES`, `PROMINENT_KEYS`, `KEY_LABELS`, `VALUE_LABELS`, `is_valid_normalized`, `value_label` |
| `regions.py` | `US_STATES`, `CA_PROVINCES`, `COUNTRIES`, `REGION_NAMES`, `is_us_state`, `us_postal`, `region_name` |
| `views.py` | `GardenContext`, `FactView`, `MentionView`, `CommentView` (`parse_status=None`, never extracted, requires `mentions == ()`, else `ValidationError`), `DimensionMatch` (incl. display-only `written_values`), `MatchResult`, `MentionMatch`, `CommentResult`, `SearchQuery`, `ResultGroup`, `SearchResult` (incl. `n_not_analysed`), `ChipSupport(n_any, n_all)` (non-negative, `n_all <= n_any`), literals `ViewerScope`, `MatchStatus` (incl. `described_differently`), `Tier`, `RegionStatus` (`same`, `same_country`, `elsewhere`, `not_stated`), `GroupKey`, constants `TIER_ORDER`, `GROUP_TITLES` (titles: Search rule 5) |
| `config.py` | `AppConfig` and sub-models (incl. `OverviewConfig(top_varieties, heatmap_varieties)`, `heatmap_varieties <= top_varieties`), `load_config(path)`, `ContextLabel` (every evidence label), `OutsideLabel` (labels allowed on outside links: `regional climate reference`, `published external trial`, `community report` — never `reported conditions`), `is_official_host(url)` (a `community report` outside link on a government host such as `*.gov`, `*.mil`, `*.gov.au` or `*.gc.ca` fails validation: an official resource is a reference or a trial, never gardener testimony) |
| `logging_setup.py` | `configure_logging(cfg)` |

## Engineering rules (all modules)

- Python ≥ 3.12, full type hints, `mypy --strict` clean (`uv run mypy`, files `src`, `app.py`
  and `streamlit_app.py`).
- `uv run ruff check .` and `uv run ruff format --check .` clean (`SPEC.md` is excluded from the
  formatter because it is kept verbatim). Ruff limits every function to ≤ 15 statements
  (`PLR0915`), ≤ 8 branches, ≤ 6 arguments and complexity ≤ 8.
- **Function size: at most 15 code lines.** `tests/test_code_standards.py` parses `src/`,
  `app.py` and `streamlit_app.py` and fails for any function (including methods and nested functions) whose body has
  more than 15 non-blank, non-comment lines, not counting the signature, decorators and
  docstring. Long SQL and literal tables live in module-level constants.
- **Docstrings: numpy convention.** Ruff's pydocstyle rules (`D`, `convention = "numpy"`)
  require a docstring on every public module, class, method and function and check its
  formatting and section layout. They do **not** require a `Parameters` / `Returns` section to
  exist or to list every argument (`D417` is off under the numpy convention): public functions
  with non-obvious inputs document them in numpy sections by convention, and one-line helpers
  often do not. Tests are exempt from `D`.
- Keep functions small and pure; push I/O to thin edges.
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
db.iter_comments_for_extraction --CLI export-for-extraction--> {comment_id, text, text_hash} JSONL (0600)
model step (offline, docs/EXTRACTION_PROMPT.md) --> ParsedComment JSONL
   --extraction.load_extractions (validate vs stored text)--> db.replace_extraction
                                                                     |
viewer (app.py) --search.search(conn, scope, query, now)--> SearchResult (groups, counts)
               --geography.region_counts(...)--> map counts
               --overview.summarize(views, cfg.overview)--> Overview --overview_charts.*--> figures

hosted demo (streamlit_app.py) --demo.prepare_hosted_demo (temp dir)--> app.main(--scope public)
```

No model call happens at request time.

## Storage (`db.py`)

SQLite via stdlib `sqlite3`. `PRAGMA foreign_keys = ON` and `PRAGMA secure_delete = ON` on every
connection (verified; otherwise `RuntimeError`). A missing database file is created owner-only
(mode 0600, before SQLite opens it) in a parent directory created 0700 when missing; existing
files and directories keep their modes (the database holds comment text verbatim). A fresh
database gets `PRAGMA user_version = SCHEMA_VERSION` (1) and FTS5 `secure-delete`; `connect`
refuses (`sqlite3.DatabaseError`, nothing written) a database with tables but another
`user_version` (including 0, i.e. not created by this module) or without the `ON DELETE
CASCADE` foreign keys that deletion relies on. Connections use SQLite autocommit (`isolation_level=None`); every write runs in
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
SCHEMA_VERSION = 1                     # PRAGMA user_version of a database created here
MAX_FTS_TOKENS = 32                    # distinct words fts_query keeps (bounds FTS cost)
class ExtractableComment(NamedTuple): comment_id: str; text: str; text_hash: str
def connect(path: Path) -> sqlite3.Connection
    # private file/dir creation (not for MEMORY_PATH), foreign_keys + secure_delete ON,
    # row_factory=sqlite3.Row, init_schema
def init_schema(conn: sqlite3.Connection) -> None   # create when empty, else check version (idempotent)
def sync_source_comments(conn, source: SourceRecord, comments: Sequence[CommentRecord], *,
                         replace: bool = False, clear_expiry: bool = False) -> ImportStats
def replace_extraction(conn, validated: ValidatedComment, loaded_at: str) -> None
def delete_source(conn, source_id: str) -> int                # #comments removed; KeyError if unknown
def purge_expired(conn, now: str) -> list[str]                # deletes sources with expires_at <= now
def stored_texts(conn, comment_ids: Sequence[str]) -> dict[str, str]   # pipeline-internal (no scope, no expiry)
def extractable_texts(conn, comment_ids: Sequence[str], now: str) -> dict[str, tuple[str, str]]
    # id -> (text, text_hash) for existing, not soft-deleted comments of unexpired sources
def iter_comments_for_extraction(conn, only_unparsed: bool, now: str) -> list[ExtractableComment]
def fts_query(text: str) -> str | None                         # pure sanitizer, see below
def visible_comment_ids(conn, scope: ViewerScope, now: str, text: str | None) -> list[str]
def load_comment_views(conn, scope: ViewerScope, now: str, comment_ids: Sequence[str] | None) -> list[CommentView]
def summary_counts(conn, scope: ViewerScope, now: str) -> dict[str, int]
```

`sync_source_comments` semantics (idempotent re-import):

- Validates first (nothing written on failure): duplicate ids within one call,
  non-canonical `imported_at` / `expires_at` / `created_at`, and `clear_expiry` combined with an
  `expires_at` raise `ValueError`.
- Refuses (`ValueError`, ids and counts only, nothing written) to re-import an existing source
  whose stored `expires_at <= source.imported_at` ("run purge-expired or delete-source first"),
  whatever the flags. Unless `replace=True` it also refuses a re-import that would change the
  stored `rights_status`, or one that shares **no** comment id with the source's stored
  comments (a different dataset under the same source id, e.g. two files both named
  `comments.csv`; an empty `comments` counts). A refresh that drops some comments is fine.
- Upserts the `sources` row (updates `dataset_hash`, `imported_at`, rights, url, kind). The
  expiry is **kept** when `source.expires_at` is `None`, replaced when one is given, and removed
  only with `clear_expiry=True` — leaving out an expiry never loosens it.
- A `comment_id` already owned by a *different* source → `ValueError` (collision; the message
  lists ids and owning source ids, never text).
- New comment → insert. Same id, same `text_hash` (sha256 of UTF-8 text) → unchanged (keeps its
  extraction and its original `imported_at`; permalink, `created_at` and `source_order` are
  refreshed). Same id, different text → update text (`imported_at` = the source's) and
  **delete its parse/mentions/facts** (their quotes may no longer be substrings).
- Comments of this source absent from the new import → deleted (cascade). An empty `comments`
  sequence is allowed and deletes them all (ingest never passes one: an empty file is an error).
- A soft-deleted comment (`deleted_at` set) stays soft-deleted across re-imports.
- When any comment was deleted or had its text changed, the FTS index is optimised
  (`'optimize'` command) in the same transaction, so old words leave no residue.
- Returns `ImportStats(source_id, inserted, updated, unchanged, deleted, rights_status,
  expires_at)`; the last two are the values stored after the import.

`delete_source` and `purge_expired` also optimise the FTS index after deleting (with
`secure_delete` and FTS5 `secure-delete`, no deleted word stays in `comments_fts_data` or in the
database file).

`replace_extraction` deletes the comment's existing `parses`/`mentions` (facts cascade) and
inserts the validated ones in one transaction. Re-loading identical JSONL is a no-op in effect.
Raises `KeyError` if the comment does not exist, and `ValueError` (writing nothing) if
`loaded_at` is not canonical or any mention/fact quote is not an exact substring of the stored
text — defence in depth behind `extraction.py` (SPEC §11 #18). `ValidatedComment.rejections` are
not stored; they are reported by the loader.

`iter_comments_for_extraction` is pipeline-internal: it ignores rights status (extraction runs
on private data) but excludes soft-deleted comments **and comments of sources whose
`expires_at <= now`** (permission to process them has ended even before `purge_expired` runs).
Each item carries the stored `text_hash` (sha256 of the UTF-8 text) so the loader can tell
which version of the text an extraction was made from. Ordered `(source_id, source_order,
comment_id)`. `extractable_texts` applies the same filter to a list of ids (ordered by id). The
extraction loader uses both reads: `stored_texts` (which ignores expiry and soft deletion) only
to tell an unknown id (`unknown_comment_id`) from a stored comment that may no longer be
processed (`source_expired`), and `extractable_texts` for the text and hash it validates
against.

`fts_query(text)`: NFC-normalise, then take words = runs of Unicode word characters (`\w`)
together with the combining marks inside them (a mark never starts a word; a decomposed
`n` + U+0303 stays one word, as in FTS5's tokenizer); drop repeated words (first-seen order) and
keep at most `MAX_FTS_TOKENS`; none → `None` (no text filter); otherwise
`" ".join(f'"{tok}"*' for tok in tokens)` (implicit AND, prefix match). User text is never
passed to `MATCH` unsanitized.

`visible_comment_ids`: ids of comments visible in `scope` at `now` (see visibility), filtered by
FTS when `fts_query(text)` is not `None`, ordered `ORDER BY c.source_id, c.source_order,
c.comment_id`. Text search finds comments that were never extracted. A comment also matches
when one of its mentions has a stored `variety_key` equal to `normalization.variety_key(text)`
(no match when the text has no key), so "Sungold" finds "Sun Gold" and "Sun-Gold" reports and
the reverse; this only ever adds comments. Keys stored before `variety_key` dropped spaces and
hyphens match only after the extractions are re-loaded.

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
def import_file(conn, path: Path, mapping: ColumnMapping, source: SourceRecord, *,
                replace: bool = False, clear_expiry: bool = False) -> ImportStats
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
  mapped values must be JSON strings (or null / absent for optional fields). A cell holding
  invalid Unicode (an escaped lone surrogate, e.g. a truncated emoji) → `IngestError` naming
  file, line and column; any other record validation error is re-raised the same way.
- Required: id and text present; every row has a non-blank id without surrounding whitespace
  and non-blank text. Duplicate ids → `IngestError` naming the id column and the rows/lines of
  each duplicate group (at most 10 groups / 10 numbers each), never the ids themselves.
  Empty input (zero bytes, whitespace, BOM only, header-only CSV) → `IngestError`, because
  importing it would delete every stored comment of the source. The mapping must name distinct
  columns.
- Optional permalink/date columns: unmapped or absent → `None` for all rows; blank cells →
  `None` (cells are trimmed before this check). `ColumnMapping.permalink_required` /
  `date_required` (set by the CLI when the operator names `--permalink-col` / `--date-col`)
  make the column mandatory: missing from the CSV header, or from every JSONL object →
  `IngestError`. Dates → `to_utc_iso` (`IngestError` with
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
  it), parses the same bytes and calls `db.sync_source_comments` (passing `replace` and
  `clear_expiry` through). Nothing is written on error.
- Error messages name the file, row/line and column but never a cell value (a swapped mapping
  would otherwise echo comment text — this includes duplicated ids); exceptions whose own text
  contains a cell (`UnicodeEncodeError`, pydantic `ValidationError`, ...) are re-raised
  `from None`. Only `Read N comments from <file name>` is logged.

## Normalization (`normalization.py`)

```python
STRICT_KEYS: frozenset[EnvKey] = frozenset({"region", "zone", "growing_year"})
SpanKind = Literal["value", "negated", "blocked"]
@dataclass(frozen=True)
class WrittenSpan: start: int; end: int; value: str | None; kind: SpanKind
def resolve_spans(key: EnvKey, text: str) -> tuple[WrittenSpan, ...]
def normalize_written(key: EnvKey, written: str) -> str | None
def normalize_crop(crop: str) -> str
CROP_SYNONYMS: Mapping[str, str]          # casefolded word -> normalized crop ("cukes" -> "cucumber")
def crops_named(text: str) -> frozenset[str]
def mentions_crop(text: str, crop: str) -> bool
def variety_key(variety: str) -> str
def parse_zone(value: str) -> tuple[int, str | None]      # "6b" -> (6, "b"); "6" -> (6, None)
```

`normalize_written` maps an as-written phrase to a normalized vocabulary value deterministically
using small explicit synonym tables; it returns `None` when nothing is recognised, the only
match is negated or blocked, or the phrase is ambiguous (fail-safe: an unnormalized fact stays
display-only, it is never mislabelled). `ValueError` for a key that is not an `EnvKey`.

`resolve_spans` returns the spans the rules keep for one key over any text (e.g. a fact's
evidence quote): resolved longest-first, non-overlapping, in text order, with offsets into the
**original** text (`text[start:end]` is exactly the matched phrase, even where whitespace was
collapsed for matching). `kind` is `"value"` (the phrase states `value`), `"negated"` (a
negation cue precedes it in its clause, or a low-quantity cue directly qualifies it; `value` is
what it would name) or
`"blocked"` (a look-alike; `value` is `None`). For a `"value"`/`"negated"` span, `value` is
`None` when the phrase names something outside the vocabulary: a range or list ("zones 6-7",
"zone 6b/7a", "2022-2023") or an out-of-range zone ("zone 14"). `normalize_written` is computed
from these spans, so the two never disagree: `None` if there is no `"value"` span or any has
value `None`; otherwise the single distinct value (for region, one subdivision may absorb its
own country). A validator can therefore judge a short value at its position inside a quote:
"humid" in "it wasn't humid" lies in a `"negated"` span, "Virginia" in "West Virginia" in a
`US-WV` span, "Kansas" in "Kansas City" in a `"blocked"` span, "zone 6b" in "zone 6b/7a" in a
range span.

Cost: resolution is near-linear in the text length. Kept spans are found by bisection over the
already kept (disjoint) spans, and the negation check looks up precomputed clause-break and word
positions, so a long repetitive text ("z6 z6 z6 ...", 50,000 characters) resolves in about a
tenth of a second.

Rules:

- Matching: typographic apostrophes/hyphens unified, whitespace collapsed, case-insensitive
  (except postal codes and dotted abbreviations). Phrases are bounded by non-word characters and
  a hyphen counts as part of a word ("non-humid", "semi-arid" never match "humid"/"arid"; a
  trailing "-grown" is allowed). Inside a phrase, words may be joined by a space, a hyphen or
  nothing ("heat wave" / "heat-wave" / "heatwave"). Overlaps are resolved **longest phrase
  first**; on a tie (same start and length) a blocker wins.
- Negation: a phrase is dropped (`"negated"`) when
  - one of the 3 words before it in the same clause (clauses split on `, ; : . ! ?` and
    brackets) is a negation cue (no, not, never, without, nor, neither, hardly, barely, didn't,
    isn't, can't, ...): "not humid", "didn't use drip", "No problems in zone 6b" → `None`. The
    intensifiers even, ever, really and actually do not count towards the 3 words, so "I've
    never even been to Arkansas" and "I don't even have a greenhouse" → `None` as well; or
  - a low-quantity cue (less, lack, lacked, lacking, insufficient, rarely, seldom, zero)
    **directly qualifies** it: the cue is the word right before the phrase, or only fillers
    stand between them — a/an/the/of/any, a form of be/get (is, was, were, be, been, get, gets,
    got, getting) or a degree word (very, too, so, that, really, ever), at most 3 words before
    the phrase. "less humid", "it rarely rained", "it rarely got very hot", "lacking a
    greenhouse", "for lack of a greenhouse", "zero shade" → `None`. A cue further away
    qualifies something else, and "in" is never a filler: "Zero problems in zone 6b" → `6b`,
    "Less splitting in containers" → `container`, "It rarely rains in Arizona" → `US-AZ`,
    "they split less in the greenhouse" / "below zero in the greenhouse" → `greenhouse`.
    Low-quantity cues never negate region, zone or year ("less Texas heat" → `US-TX`, "below
    zero in zone 4b" → `4b`).

  Phrases that start with the negation ("no rain", "lack of rain" → `dry_season`) still match.
- Bare dimension nouns name no value: "humidity", "heat", "rain"/"rains" → `None`, so "Humidity
  was in the single digits", "lack of heat" and "it rarely rains" stay display-only. Explicit
  forms still map: "humid", "high humidity", "muggy" → `humid`; "low humidity" → `dry_air`;
  "hot", "brutal/extreme/intense/high heat" → `hot`; "rained", "rainy", "lots of rain", "heavy
  rains", "too much rain", "downpours" → `rainy`; "no rain", "little rain", "not enough rain",
  "low rainfall" → `dry_season`.
- Blockers: look-alike phrases consume their text without yielding a value.
  - Categorical: "hot peppers", "heat mat", "heat tolerant", "clay pots", "shade cloth" (also
    "light/partial/... shade cloth"), "humidity dome", "rain barrel", "drought tolerant",
    "container mix" (method); seed starting ("started indoors", "started (them/seeds) under
    (grow) lights", "seedlings under lights"); overwintering ("overwintered indoors", "brought
    them inside"); season extenders ("cold frame", "inside the cold frame", "low tunnel");
    structures near a greenhouse ("next to / beside / behind / near / in front of the
    greenhouse"); other seasons ("cool/mild spring, fall, autumn, winter"); hand work that is
    not watering ("pollinated/planted/seeded/thinned/picked off/... by hand").
  - Region: places named after a region they are not (only) in — cities ("Kansas City",
    "Nevada City", "Michigan City", "Virginia City", "Missouri City", "Arkansas City"),
    neighbourhoods ("Washington Heights", "Jamaica Plain"), lakes ("Lake Michigan/Ontario/
    Erie/Huron/Superior/Champlain/Tahoe"), valleys and rivers ("Ohio/Tennessee/Delaware/
    Mississippi/Missouri/Hudson Valley", "Ohio/.../Colorado/Connecticut River"), waters
    ("Gulf of Mexico", "Georgia Strait", "Strait of Georgia", "Colorado Desert") and
    "Tbilisi, Georgia". So "Kansas City, Missouri" is `US-MO` and "Chicago, Illinois near Lake
    Michigan" is `US-IL`, while a bare "Kansas City" or "Lake Michigan" stays `None`; "New
    York City", "Oklahoma City", "Iowa City", "Mexico City" and "Quebec City" still resolve.
    "West VA" (usually West Virginia) → `None`. Non-North-American "City, XX" forms whose code
    is also a U.S./Canadian postal code are blocked (comma optional, city case-insensitive):
    "Perth/Fremantle/Bunbury/Broome, WA", "Darwin/Alice Springs, NT", "Catania, CT",
    "Palermo/Belém, PA", "Milano/Milan, MI", "Varese, VA", "Como, CO", "Messina, ME",
    "Chennai, TN", "Monterrey, NL", "Cagliari, CA", "Florianópolis, SC", "Recife, PE",
    "Tijuana, BC", ... → `None`; "Perth, WA, Australia" → `AU`.
  - Zone: other zone systems ("Sunset (Western Garden) zone 9", "heat zone 9", "AHS (heat)
    zone 7", "Canadian/Canada zone 5a", "Australian (climate) zone 5", "climate zone 3",
    "RHS zone 4") → `None`; they are never stored as USDA zones.
- Region: full state/province/country names and aliases anywhere in the phrase
  ("in Connecticut" → `US-CT`; "New England" → `US`; "New South Wales" → `AU`, not Wales;
  Washington DC forms → `US-DC`; "SoCal"/"So Cal"/"So-Cal" and "NorCal"/"Nor Cal"/"Nor-Cal" →
  `US-CA`, while "so calm" names nothing). Dotted abbreviations ("Conn.", "Calif.", "N.Y.") match
  case-sensitively ("a huge mass." is not Massachusetts). Uppercase two-letter postal codes only
  (a) as the whole phrase ("CT"), (b) right after a comma and followed by the end, punctuation,
  a ZIP code or a country ("Hartford, CT 06103"), or (c) right after a directional/area
  qualifier: upstate, downstate, central, north(ern), south(ern), east(ern), west(ern),
  northeast(ern) and the other compass points, NE/NW/SE/SW, north/south central, middle, mid,
  coastal ("upstate NY", "SE PA", "middle TN", "mid-MI", "northern CA" → the state). A bare
  "City ST" without a comma ("Austin TX") stays `None`; "north OR south" is not Oregon; "in it"
  never → `US-IN`; "US"/"GB" alone → `None`. Hierarchy: one subdivision plus only its own
  country gives the subdivision ("Ontario, Canada" → `CA-ON`); any other mix of places is
  ambiguous → `None` ("Toronto, ON, USA", "I live in Florida but grew these in Maine").
- Zone: "zone 6b", "Zone 6B", "z6b", "zn 5", "Zn5", "zone: 6b", "zone - 7a", "USDA 7a",
  "usda7a", "USDA zone: 7a", "hardiness zone 5" → `6b`/`5`/`7a`; a bare value ("6b") only when
  it is the whole phrase. Ranges, lists, split subzones ("zones 6-7", "6b/7a"), out-of-range
  zones and two different zones → `None`; the same zone repeated is fine.
- Year: exactly one distinct 19xx/20xx year (repeats allowed; "mid-2021" → `2021`); two years or
  a range ("2022-2023", "2023/24") → `None`.
- Categorical examples: "pots", "containers", "grow bags", "5 gallon buckets" → `container`;
  "raised beds" → `raised_bed`; "in the ground" → `in_ground`; "greenhouse", "hoop house",
  "high tunnel", "polytunnel" (and "inside the greenhouse") → `greenhouse`; "outside the
  greenhouse" → `outdoors`; "muggy", "humid" → `humid`; "hot summers", "brutal heat" → `hot`;
  "heat wave" → `heatwave`; "short season" → `short_season`; "clay" → `clay`; "drip" → `drip`.
  Sun: qualified shade is `part_shade` ("part/partial/some/a bit of/a little/light/filtered/
  half/semi/dappled/morning/afternoon shade", "shade in the afternoon/morning", "shade (for)
  part of the day", "shade after/until ...", "part/partly/partially shaded", "part sun",
  "morning sun"); only unqualified "shade", "shady", "full shade", "deep shade" are `shade`.
  Two conditions in one phrase ("sandy loam", "full sun with afternoon shade") → `None`.
- Never infer region from a zone, or humidity from a region.

`normalize_crop`: casefold, unify typographic apostrophes/hyphens, strip, collapse whitespace,
singularise the **last word** only ("brussels sprouts" → "brussels sprout"): drop a trailing
`'s` ("tomato's" → "tomato"); explicit irregular plurals first (`chilies`→`chili`,
`chillies`→`chilli`, `irises`→`iris`); then `-sses`→`-ss`, `-ches`→`-ch`, `-shes`→`-sh`,
`-oes`→`-o`, `-ies`→`-y` (words of 5+ letters), otherwise drop a final `-s` ("zucchinis" →
"zucchini", "kiwis" → "kiwi"). Words shorter than 4 letters, words ending in `ss`/`us`, and a
small explicit set of singular `-is` words (iris, cannabis, clematis, physalis, ...) are
unchanged. Idempotent. Empty → `ValueError`.

`CROP_SYNONYMS`: a small explicit map from casefolded as-written crop words to the normalized
crop they also name: cuke(s) → cucumber, courgette(s) → zucchini, aubergine(s) → eggplant,
spud(s) → potato, toms/tomatos → tomato, capsicum/chile/chili/chilli (and plurals) and the
misspellings peper/pepers → pepper. Every value is a `normalize_crop` fixed point (checked at
import).

`mentions_crop(text, crop)`: `True` iff some window of 1–3 consecutive words of `text` (words
split on whitespace, hyphens and punctuation; inner apostrophes kept), after `normalize_crop` or
via `CROP_SYNONYMS`, equals `crop` normalized the same way. Word-bounded ("pepperoni" does not
name pepper, "Tom" does not name tomato); a synonym adds a name, it never replaces one
("chillies" names both `chilli` and `pepper`). A crop with no letters or digits → `ValueError`.
It is computed as `normalize_crop(crop) in crops_named(text)`.

`crops_named(text)`: every crop the 1–3 word windows of `text` name (each window casefolded,
after `normalize_crop`, and via `CROP_SYNONYMS`; the set also holds the casefolded windows
themselves). Compute it once to test many crops against one text: search does this once per
query instead of rescanning the query text for every mention.

`variety_key`: a **grouping key only** (people always see `variety_as_written`). Casefold;
typographic apostrophes/hyphens → ASCII; keep apostrophes only between two word characters;
every other quote/punctuation mark becomes a space; then remove every space and hyphen, so
spellings of one variety share a key: `"Sun Gold"`, `'"Sun Gold"'`, `"Sungold"`, `"Sun-Gold"`
→ `sungold`; `"Early Girl"`, `"Early-Girl"` → `earlygirl`; `"Aunt Ruby's German Green"` →
`auntruby'sgermangreen`; `"Mr. Stripey"` → `mrstripey`. Different names stay apart (`"Sun
Sugar"`, `"Early Girl 2"`). Empty result → `ValueError`. Keys stored before this rule (with
spaces/hyphens, e.g. `sun gold`) change when extractions are re-loaded. The key has no word
boundaries, so do not split it into words to look for a variety in text.

`parse_zone` accepts only already-normalized values (`vocab.ZONE_PATTERN`); `"6B"`, `"zone 6"`
→ `ValueError`.

## Extraction validation and loading (`extraction.py`)

```python
class ExtractionReport(BaseModel):  # frozen pydantic model
    lines_read, schema_errors, unknown_comment_ids, duplicate_comment_ids, expired_comment_ids,
    stale_texts, comments_loaded, status_counts: dict[ParseStatus, int], mentions_accepted,
    mentions_rejected, facts_accepted, facts_rejected, unresolved_identities,
    rejections: tuple[Rejection, ...]
REJECTION_REASONS: frozenset[str]  # every reason code a Rejection from this module can carry
def read_extraction_jsonl(path: Path) -> tuple[list[tuple[int, ParsedComment]], list[Rejection]]
def validate_parsed_comment(parsed: ParsedComment, text: str, cfg: ExtractionConfig) -> ValidatedComment
def load_extractions(conn, path: Path, cfg: ExtractionConfig, loaded_at: str,
                     *, now: str | None = None) -> ExtractionReport
```

The extraction contract (`schemas.py`): `ParsedComment` requires `text_hash`, the lowercase
SHA-256 hex digest of the UTF-8 comment text, copied from the `export-for-extraction` line
(pattern `^[0-9a-f]{64}$`). `EnvironmentFact.value_as_written` is at most 200 characters
(`MAX_VALUE_CHARS`), and every evidence quote (mention or fact) at most 2000
(`MAX_QUOTE_CHARS`); longer lines are `schema_error`s, which bounds the validator's work.

Validation rules. Each failure appends a `Rejection` with a machine-readable reason. Line
level: `schema_error`, `unknown_comment_id`, `duplicate_comment_id`, `source_expired`,
`stale_text`. Mention level: `mention_quote_not_substring`, `variety_not_in_quote`,
`crop_not_in_quote`, `invalid_crop`, `invalid_variety`. Fact level:
`fact_quote_not_substring`, `value_not_in_quote`, `value_in_variety_name`,
`invalid_normalized_value`, `negated_value`, `blocked_value`, `strict_key_mismatch`,
`normalized_value_mismatch`, `hypothetical_context`, `question_context`, `conflicting_region`,
`conflicting_zone`. Rejection `mention_index`/`fact_index` are positions in the extractor's
input lists.

"Whole words" below means: case-insensitive, not preceded or followed by a word character or a
hyphen (a hyphen counts as part of a word, as in normalisation), optionally followed by a plural
`s`/`es` (`(?<![\w-])value(?:s|es)?(?![\w-])`). When the value occurs with its exact case, only
those occurrences count; otherwise every case-insensitive one does.

1. Mention `evidence_quote` must be an **exact** (case-sensitive) substring of the comment text,
   else the mention and all its facts are rejected (one mention-level rejection covers them).
2. `variety_as_written`, when present, must occur in the mention quote as whole words
   (`variety_not_in_quote`: "Roma" is not in "aroma"; "Sungolds" holds "Sungold"). A mention
   with a `crop` and no variety must name its crop in the quote:
   `normalization.mentions_crop(quote, crop)` (word, plural or `CROP_SYNONYMS` entry), else
   `crop_not_in_quote` ("Great video" cannot carry a pepper mention).
3. `crop` is normalised with `normalize_crop` and `variety_key` from `variety_key()`; a value
   they reject (blank crop, crop "--", variety "--") rejects the mention (`invalid_crop` /
   `invalid_variety`). Topics are de-duplicated in first-seen order.
4. Fact `evidence_quote` must be an exact substring of the comment text
   (`fact_quote_not_substring`); `value_as_written` must occur in it as whole words
   (`value_not_in_quote`: "Maine" is not in "remained", "pot" is not in "potatoes"). Every such
   occurrence is a **position** of the value. A position that, at some occurrence of the quote
   in the text, lies wholly inside an occurrence in the comment text (whole words, any case)
   of any mention's `variety_as_written` is not evidence and is dropped ("Arkansas" of
   "Arkansas Traveler", "Hot" of "Hot Portugal", "Shade" of "Shade Star"); if no position is
   left → `value_in_variety_name`, for every key and also for a display-only fact. So "My
   California Wonder peppers did great here in California" as the quote of `US-CA` is
   accepted, while the bare quote "California" (which also occurs inside the name) is not. The
   stored `value_as_written` stays as the extractor wrote it.
5. Normalised value. `null` always means display-only: the loader never derives a value for
   it. The only check on a `null` fact is that a strict-key phrase is not really another strict
   key's value (region "zone 6b", zone "Connecticut", region "2023" → `strict_key_mismatch`).
   A given value must satisfy `vocab.is_valid_normalized` (`invalid_normalized_value`). Then
   the quote is read with `normalization.resolve_spans(key, evidence_quote)` and each
   remaining position of the value is judged by the resolved spans that overlap it. First,
   **look-alike positions are dropped**: overlapping spans exist, none is `"negated"`, and
   either none is a `"value"` span or a `"blocked"` span extends past the position ("Michigan"
   in "Lake Michigan", "clay" in "clay pots", "hot" in "hot peppers"). If no position is left →
   `blocked_value` ("clay" in "clay pots", "Hot" in "Hot peppers", "Kansas" in "Kansas City",
   "Perth, WA", "Lake Michigan", "Sunset zone 9"). So "I live in Michigan near Lake Michigan"
   gives `US-MI` and "heavy clay ... clay pots" gives `clay`. Every other position must hold:
   - any overlapping span is `"negated"` → `negated_value` ("humid" in "it wasn't humid", "not
     humid", "no greenhouse"; "Not humid in June, humid in July" is rejected although the
     second occurrence states the value);
   - a blocker wholly inside the value next to a value span is fine ("Kansas City, Missouri" →
     `US-MO`);
   - otherwise the `"value"` spans must name the given value, and nothing else except, for a
     region, its own country ("Maine, USA" → `US-ME`), else `normalized_value_mismatch`
     ("Virginia" in "West Virginia" for `US-VA`, "zone 6b" in "zone 6b/7a", "Zones 6-7" for
     `6`, "pots" for `raised_bed`);
   - no span overlaps the position → the occurrence is normalised on its own
     (`normalize_written(key, occurrence)`): a non-null result that differs →
     `normalized_value_mismatch`; `None` is accepted for non-strict keys (the extractor may
     normalise what the loader cannot: "balcony railing" → `container`) and rejected for
     `STRICT_KEYS` (`strict_key_mismatch`): region, zone and year must be derivable where they
     are quoted ("upstate NY" with value "NY" → `US-NY`; "CT" alone → `US-CT`; region "zone
     6b" → rejected).
6. Hypothetical/advice/question guard: split the comment text into sentences; `!`, `?` and line
   breaks always end a sentence, `.` ends one only when followed by whitespace, a closing quote
   or bracket, or the end of the text, and not after a known abbreviation in any case: titles
   (Mr, Mrs, Ms, Dr, Prof, Jr, Sr), places (St, Mt, Mtn, Ft, Pt, Co, Cty, Twp, Apt, Rd, Ave,
   Blvd, Hwy, nr), references (vs, e.g, i.e, eg, ie, cf, approx), botany (var, cv, ssp, subsp,
   spp) and units (gal, lb, lbs, oz, qt, cu, sq, yd); looked for within the 32 characters
   before the full stop, which keeps splitting linear. An uppercase U.S. postal code ("Denver,
   CO.", "Bozeman, MT.", "Jackson, MS.") is not an abbreviation and does end its sentence.
   Each delimiter belongs to the sentence it ends. If any sentence overlapping **any
   occurrence** of the fact's evidence quote contains a configured marker
   (`cfg.hypothetical_markers`: case-insensitive, word-bounded, a space in a marker matches any
   whitespace, apostrophes `'` `’` `‘` `ʼ` `´` and the backtick interchangeable; a marker
   written with a leading `^` counts only at the start of a sentence, after any non-word
   characters) → reject the fact (`hypothetical_context`); otherwise, if such a sentence ends
   with `?` → `question_context`. The repository markers are narrow phrases ("would do",
   "would be", "wish i", "has anyone", "^maybe", ...), never bare "would", "maybe" or
   "anyone", so "gets maybe 7 hrs of full sun", "would grow it again" and "If anyone's
   curious, I grow ..." stay reports while "Maybe Sungold in zone 5." is rejected. Joining
   sentences can only make the guard stricter, so "I'd try Mr. Stripey in zone 5", "I'd try it
   in Co. Cork in zone 8" and "I'd space them 2.5 feet apart in zone 5" are rejected. A blank
   configured marker (including a bare `^`) is a config error (`ValueError`).
   `docs/EXTRACTION_PROMPT.md` lists the configured markers, every reason code, the region
   vocabulary and the phrasings the loader reads; `tests/test_prompt_consistency.py` keeps
   them equal to the code and checks that its worked example loads without a rejection.
7. Conflicts. Within one mention, accepted facts with key `region` (or `zone`) and two or more
   distinct non-null normalised values → reject every fact of that key in that mention. Then,
   across the comment, if the accepted `whole_comment` facts of key `region` (or `zone`) on all
   mentions carry two or more distinct non-null values → reject every `whole_comment` fact of
   that key (`conflicting_region` / `conflicting_zone`), so a sibling never inherits two zones.
   The same value repeated is not a conflict; `this_mention` facts never conflict across
   mentions.
8. Status: keep the extractor's status, but downgrade `parsed` → `partial` if anything was
   rejected. A comment with zero mentions is valid.

`read_extraction_jsonl`: lines split on `\n` only (a trailing `\r` is tolerated, a leading BOM
ignored), numbered by physical line, blank lines skipped. A line that is not UTF-8, not JSON,
repeats a key, nests too deeply or does not match `ParsedComment` (including a missing or
malformed `text_hash` and an over-long value or quote) becomes a `schema_error` rejection that
keeps only its `comment_id` (when readable). An unreadable file → `ValueError`.

`load_extractions`: checks `loaded_at`, `now` (default: `loaded_at`, the time a load happens)
and the markers, reads the file, then — inside **one** savepoint, all-or-nothing — looks up
the known ids via `db.stored_texts` and the processable ones via `db.extractable_texts(conn,
ids, now)` (not soft-deleted, source not expired at `now`). Each valid line is checked in
order: an id repeated in the file → `duplicate_comment_id` (every copy, including a malformed
copy's readable id); an id not in the database → `unknown_comment_id`; a stored comment that
may no longer be processed → `source_expired`; a `text_hash` that differs from the stored
hash → `stale_text` (the extraction was made from another version of the text). A rejected
line writes nothing, so a comment without a previous extraction stays in `export-for-extraction
--only-unparsed`, and a previous extraction stays as it was. The rest are validated and stored
with `db.replace_extraction`. Every non-blank line lands in exactly one of `schema_errors`,
`duplicate_comment_ids`, `unknown_comment_ids`, `expired_comment_ids`, `stale_texts`,
`comments_loaded`. `status_counts` always lists parsed, partial, unresolved. Mention and fact
counts cover loaded comments only; `unresolved_identities` = accepted mentions with neither
crop nor variety. Logs one INFO line of counts and one WARNING with per-reason counts (file
name only, never text).

## Environment matching (`environment_match.py`) — pure, no I/O

```python
def applicable_facts(mention: MentionView, siblings: Sequence[MentionView]) -> tuple[FactView, ...]
def compare_dimension(key: ComparableKey, viewer_value: str, facts: Sequence[FactView]) -> DimensionMatch
def match_context(garden: GardenContext, facts: Sequence[FactView]) -> MatchResult | None
def tier_of(statuses: Sequence[MatchStatus]) -> Tier
def values_with(dimension: DimensionMatch, status: MatchStatus) -> tuple[str, ...]
def region_status(garden_region: str | None, facts: Sequence[FactView]) -> RegionStatus | None
def explain(result: MatchResult | None) -> str
```

- `applicable_facts`: the mention's own facts, plus `whole_comment` facts stored on sibling
  mentions **whose key the mention does not itself state** (any own fact of a key, even an
  unnormalized one, blocks inheritance of that key). `siblings` may include the mention itself
  (recognised by `mention_id`). Deduplicate by `(key, normalized_value, value_as_written)`.
  Order: own facts first (fact order), then sibling facts (mention order, fact order).
  `this_mention` facts never leak to siblings.
- **Self-described keys** (`vocab.SELF_DESCRIBED_KEYS` = `summer_heat`, `humidity`,
  `precipitation`, `season_length`): "hot", "humid", "rainy" or "a short season" are the
  gardener's own words, not calibrated across gardeners (a Minnesotan's "hot" is not a
  Phoenician's). An equal value still matches, but an opposite value is
  `described_differently`, which is **never** counted as a mismatch. Zone, method, shelter,
  sun, soil and irrigation are checkable facts and keep strict comparison; region stays a
  preference only. Deliberate deviation from SPEC §5 ("Conflicts" and the example table's
  last row, "Different: method and humidity"): a self-described disagreement is shown in the
  explanation but never puts a report under "Different reported conditions", so that row is
  `different` because of the method alone.
- `compare_dimension`: considers facts of that key with a non-null `normalized_value`.
  None → `not_stated`. Per reported value r vs viewer value v:
  - zone: `r == v` → match; same number and exactly one side lacks a subzone → `imprecise`;
    otherwise `different` (adjacent zones and other subzones are different).
  - summer_heat: `r == v` → match; `r == "heatwave"` and `v == "hot"` → `imprecise`;
    `r == "heatwave"` otherwise → ignored (not comparable); else `described_differently`.
  - humidity, precipitation, season_length: equal → match, else `described_differently`.
  - every other key: equal → match, else `different`.
  Aggregate over values: any match → `match`; else any imprecise → `imprecise`; else any
  different → `different`; else any described differently → `described_differently` (a key
  gives one or the other, never both); else `not_stated` (so a mild-summer report with a
  heatwave is `imprecise` for a hot-summer viewer: the precedence is deliberate; a heatwave
  against mild/cool summers is still ignored, never a disagreement). `reported_values` =
  distinct normalized values (first-seen order); `written_values` = distinct `value_as_written`
  of that key's facts whose `normalized_value` is null (first-seen order; display only,
  untrusted text, never compared, so the status stays `not_stated` when only these exist);
  `evidence` = facts of that key with non-null normalized value.
  `ValueError` for a non-comparable key (e.g. `region`), a viewer value not in
  `VIEWER_VALUES[key]`, or a stored normalized value that fails `is_valid_normalized`.
- `values_with(dimension, status)`: the `reported_values` (in order) whose **own** comparison
  with the viewer value gives `status`, so a card labels only the values that produced a
  dimension's status (mild + heatwave vs hot: `imprecise` → `("heatwave",)`,
  `described_differently` → `("mild",)`, `different` → `()`; `not_stated` selects values
  that are not comparable, e.g. a heatwave vs mild).
  `ValueError` for an unknown status.
- `match_context`: `None` when `garden.selected()` is empty; otherwise one `DimensionMatch` per
  selected key (canonical `COMPARABLE_KEYS` order) and the tier.
- `tier_of`: any `different` → `different`; all `match` → `match`; any `match`/`imprecise` →
  `partial`; else `not_stated`. Empty input or an unknown status → `ValueError`. So
  `described_differently` never yields `different`: alone it yields `not_stated`, next to a
  match `partial` (and strict mode, "Only show stated matches", hides a report whose only
  selected condition was described differently).
  Outcome words (failure, success, great, died) **never** influence tier or order.
- `region_status`: `None` if no garden region; `same` if any fact with key `region` has
  `normalized_value == garden_region`; else `same_country` if the garden region is a country
  and a reported region is one of its subdivisions (code prefix `"<country>-"`: `US` vs
  `US-CT`, `CA` vs `CA-ON`; never the reverse, so `US-CT` vs `US` is `elsewhere`); else
  `elsewhere` if other normalized region facts exist; else `not_stated`. Unknown region codes →
  `ValueError`. Region is never part of the tier (states differing ≠ climates differing).
- `explain`: short, honest sentences built only from dimension statuses, in canonical key
  order: an optional `"Different: <reported> (you: <yours>), ..."` sentence (hard keys only),
  then one sentence joining the matched (`"Matches your ..."`, hard keys), matched
  self-described (`"they described <subject> as <description>"`, phrased as what the gardener
  said, from `vocab.DESCRIBED_AS` and grouped by subject), imprecise, not-comparable,
  display-only and not-stated clauses with `"; "`, then an optional sentence for keys
  described differently: `"They described summers as mild (you chose hot summers);
  self-descriptions aren't counted as a mismatch."` (several items joined with "and"; never
  the word "Different"). Each clause names only the values whose own status it describes
  (`values_with`). Examples:
  `"Matches your zone 6b and containers; they described it as humid."` (SPEC §5 first row),
  `"They described summers as hot and it as humid and rainy."`,
  `"Matches your containers; hardiness zone and humidity not stated."`,
  `"Zone 6 reported (subzone unknown); growing method not stated."`,
  `"None of your selected conditions were stated."` (only when nothing selected was reported,
  not even a display-only phrase),
  `"Different: in the ground (you: containers). Humidity not stated."`,
  `"Different: in the ground (you: containers). Hardiness zone not stated. They described the
  air as dry (you chose humid); self-descriptions aren't counted as a mismatch."` (SPEC §5
  last row),
  `"A heatwave reported (you: hot summers)."` (unchanged: imprecise, not a description),
  `"summer heat not comparable (a heatwave reported)"` for a heatwave against mild/cool
  summers, and `'soil mentioned as "rocky hillside dirt" (not comparable)'` for a selected key
  whose only statements are display-only `written_values` (quoted, whitespace collapsed, joined
  with "and"). Such quotes are untrusted comment text: render the explanation as plain text.
  `None` → `""`. Never mentions success/quality/scores/recommendations in its own words.

## Search (`search.py`)

```python
class FilterOptions(BaseModel): crops: tuple[str, ...]; topics: tuple[Topic, ...]; years: tuple[str, ...]
def search(conn, scope: ViewerScope, query: SearchQuery, now: str) -> SearchResult
def build_result(views: Sequence[CommentView], query: SearchQuery) -> SearchResult   # pure core
def relevant_mentions(view: CommentView, query: SearchQuery) -> tuple[MentionMatch, ...] | None
def chip_support(conn, scope, now, chips: Sequence[QuickChip]) -> dict[str, ChipSupport]
def filter_options(conn, scope, now) -> FilterOptions   # distinct, sorted, visible data only
```

1. Candidates: `db.visible_comment_ids(conn, scope, now, query.text)` → `db.load_comment_views`.
   Text search works for comments with no extraction at all, and a variety's other spellings
   are found through the stored `variety_key` ("Sungold" finds "Sun Gold"). In `build_result`, `query.text`
   never adds or drops a comment; it only chooses the relevant mentions (rule 2).
2. Mention-level filters — `crop` (`normalize_crop(query.crop)` equals the stored mention crop
   exactly), `topics` (non-empty intersection), `year` (an applicable `growing_year` fact equals
   it), `region_filter` (an applicable `region` fact equals it). If any of these is set, a
   comment is kept only if ≥ 1 mention passes **all** set filters (`relevant_mentions` returns
   `None` otherwise); if none is set, comments with zero mentions are kept (`()`). Among the
   passing mentions the **text names the relevant mentions** (SPEC §4: the variety mention is
   the query unit): a mention is relevant when its whole `variety_as_written` occurs in the
   text as consecutive words, its words split or joined anywhere ("Sun Gold", "Sun-Gold" and
   "sungold" name each other, as they share a `normalization.variety_key`), or the text names
   its crop (`normalization.mentions_crop`: 1-3 word windows via `normalize_crop` or
   `CROP_SYNONYMS`, so "tomatoes" names `tomato` and "chillies" names `pepper`). Both sides are
   casefolded, stripped of diacritics (NFKD, like the FTS `remove_diacritics`) and split into
   letter/digit words, so "Black-Krim" names "Black Krim" and "jalapeno" names "Jalapeño";
   "Krim" alone does not. **If the text names none of the passing mentions (or there is no
   text), every passing mention stays relevant** — a documented rule, not a fallback for
   errors: "hornworms" or "pots" behave as before. So a filter only ever narrows the mentions
   the text names and never lets another variety's conditions decide the group (SPEC §11 #2):
   "Roma" makes a greenhouse-pepper / outdoor-tomato comment `different` for a greenhouse
   garden, with or without a topic both mentions share, and strict mode hides it; a "Sungold"
   search keeps an in-ground Sungold `different` for a container garden when a state click,
   crop, topic or year filter also keeps its container-grown Juliet sibling; "Roma" with crop
   `pepper` compares the Jalapeño, because the text names no passing mention. The text is read
   once per query (its words joined into one string, and `normalization.crops_named` of them);
   each variety name becomes one cached regular expression that never backtracks and each crop
   a set lookup, so a pasted 100,000-character text stays cheap even before the viewer's
   `max_query_chars` cap.
   Invalid filters fail loudly (`ValueError`): a year not matching `YEAR_PATTERN`, an unknown
   region code, a blank crop, and duplicate comment ids in the `build_result` input.
3. For each relevant mention: `facts = applicable_facts(...)`, `match = match_context(...)`.
   Every relevant mention's `MentionMatch` (with its own `match.tier`) is on
   `CommentResult.mention_matches`, so the viewer can state each mention's tier line.
4. Comment tier = best (lowest `TIER_ORDER`) relevant-mention tier, ties to the earlier mention;
   a comment with no relevant mentions under a selected garden gets `match_context(garden, ())`
   (tier `not_stated`). Comment `region_status` = `same` if any relevant mention is `same`, else
   `same_country` if any, else `elsewhere` if any, else `not_stated`. `explanation` = `explain`
   of the best mention's match, prefixed with `"<variety_as_written, else crop, else
   'Unidentified variety'>: "` when the comment has > 1 relevant mention and the explanation is
   not empty. `has_conditions` = an applicable fact of a relevant mention has a non-null
   normalized value for a comparable key (`vocab.COMPARABLE_KEYS`). A region is a location
   preference that is never compared, so a region alone, like a growing year alone or a
   display-only fact, does not count.
5. Groups: no comparable selection → single group `all`. Otherwise groups `match`, `partial`,
   `not_stated`, `different` (in that order; `different` has `collapsed_by_default=True`),
   titled by `views.GROUP_TITLES`: "Matching reported conditions", "Partially matching
   conditions", "Conditions not stated or not comparable", "Different reported conditions".
   The third title is a deliberate small deviation from SPEC §2 ("Conditions not stated"):
   besides reports that state nothing comparable, that group holds reports whose only selected
   conditions were display-only phrases, a heatwave against mild or cool summers, or a
   self-description unlike the viewer's (`described_differently`, see Environment matching),
   none of which is "not stated". Empty
   groups are always omitted, including `all` (zero candidates → `groups == ()`). Within a group
   (including `all`) sort by `(region rank: same=0, same_country=1, else 2, source_id,
   source_order, comment_id)`; region never changes a tier.
6. `only_stated_matches=True` hides the `not_stated` and `different` groups (so also a report
   whose only selected condition was described differently) and
   `n_hidden_by_strict` counts their comments; with no comparable condition selected there is
   nothing to match, so it hides nothing. `tier_counts` has all four tiers (zeros included) when
   a comparable condition is selected, else `{}`. `n_not_analysed` counts candidates with
   `parse_status is None` (never extracted; disjoint from `n_with_conditions` because such a
   view has no mentions; always 0 when a mention-level filter is set). Counts are distinct
   comments before hiding, never mentions.
7. `limitation_message` (independent of whether a garden is selected; "comparable" because a
   display-only phrase or a region is a mention but cannot be compared; an unanalysed report
   is never said to lack conditions; reports hidden by strict mode are never said to be shown
   below):
   - `n_candidates == 0` or every candidate has conditions → `None`.
   - Some but not all have conditions → `"Only {n_with} of {n} reports mention comparable
     growing conditions."` ("mentions" when `n_with == 1`), with `" ({k} not yet analysed)"`
     before the full stop when `k = n_not_analysed > 0`, e.g. `"Only 33 of 69 reports mention
     comparable growing conditions (3 not yet analysed)."` for the whole demo.
   - None has conditions → a reason sentence, then a where sentence. Reason: none analysed →
     `"None of these {n} reports have been analysed for growing conditions yet, so none can be
     compared with your garden."`; none unanalysed → `"None of these {n} reports mention
     comparable growing conditions, so none can be compared with your garden."`; mixed →
     `"None of these {n} reports can be compared with your garden: {a} do not mention comparable
     growing conditions and {k} have not been analysed yet."` ("does"/"has" for 1); exactly one
     report → `"The 1 report found has not been analysed for growing conditions yet, so it
     cannot be compared with your garden."` or `"The 1 report found does not mention comparable
     growing conditions, so it cannot be compared with your garden."`. Where: nothing hidden →
     `"All are shown below."` (`"It is shown below."`); all hidden by strict mode →
     `'All {n} are hidden by "Only show stated matches"; untick it to see them.'` (`'It is
     hidden by "Only show stated matches"; untick it to see it.'`); some hidden (not reachable
     today) → `'{h} of them are hidden by "Only show stated matches"; untick it to see them.'`.
8. `chip_support`: for each chip (labels must be distinct, else `ValueError`), a
   `ChipSupport(n_any, n_all)` of distinct visible comments: `n_any` = comments having ≥ 1
   mention whose applicable facts state **any** of the chip's (key, value) pairs (the display
   gate `>= min_reports_for_chip`); `n_all` = comments having **one** mention whose applicable
   facts state **all** of them (the reports the chip would put under "Matching reported
   conditions"; whole-comment facts count through inheritance). A heatwave does not support
   "hot". Result in chip order. `chip_support` and `filter_options` load every visible view
   (fine at MVP size; move to SQL aggregates that keep `visibility_clause` if the corpus grows a
   lot).

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
  shown), no lat/lon, no comment text, a static borderless layout (`dragmode=False`, zero
  margins, transparent background). Never plots points. Plotly draws the state outlines from
  topojson it fetches from `cdn.plot.ly`, so an offline browser shows the counts and selector
  but an empty map.
- Privacy limit: small-cell suppression only **coarsens the map display** (shading, hover text
  and the state selector's labels). It does not protect the reports: every visible card shows
  its comment text and region line, and filtering by a suppressed state lists its reports and
  their exact count. It is also per cell, so with `min_cell_count > 1` a suppressed count can
  sometimes be derived by subtraction from the bucket totals. Real small-cell protection would
  have to cover the cards too (a design change, not a threshold).

## Overview (`overview.py`) and its charts (`overview_charts.py`) — pure, no I/O

The data behind the viewer's "At a glance" header. Every count is a number of **distinct
comments** over the `CommentView`s passed in (the viewer passes its run's scope-gated views, so
public scope never counts `private_unverified` rows). Counts measure conversation, never
quality: no score, rate, ranking of "best" varieties or recommendation is computed or shown.

```python
SETUP_KEYS = ("method", "shelter")
VARIETY_COLUMNS = ("variety_key", "label", "crop", "n_comments")
TOPIC_COLUMNS = ("variety_key", "label", "topic", "n_comments")
SETUP_COLUMNS = ("key", "value", "label", "n_comments")
class Headline(BaseModel):  # frozen; n_with_conditions <= n_reports; property n_places
    n_reports: int; n_varieties: int; n_with_conditions: int; n_us_states: int; n_other_regions: int
@dataclass(frozen=True) class Overview: headline; varieties; topics; setups; n_with_setup
def headline(views: Sequence[CommentView]) -> Headline
def variety_counts(views, top_n: int) -> pd.DataFrame
def topic_matrix(views, variety_keys: Sequence[str]) -> pd.DataFrame
def setup_counts(views) -> pd.DataFrame
def n_stating_setup(views) -> int
def summarize(views, cfg: OverviewConfig) -> Overview
```

- `headline`: `n_reports` = visible comments (analysed or not); `n_varieties` = distinct
  `variety_key`s (a crop-only mention is no variety); `n_with_conditions` = comments with an
  applicable fact (`environment_match.applicable_facts`) giving a normalized value for a
  comparable key: exactly `CommentResult.has_conditions` for an empty query, so it equals
  `search.build_result(views, SearchQuery()).n_with_conditions` (a property test checks it);
  `n_us_states` / `n_other_regions` = distinct normalized region codes that are U.S. states /
  Canadian provinces or countries other than the United States (the bare country `US` is not a
  place of its own). An invalid region code raises `ValueError` naming the comment.
- `variety_counts`: one row per variety key, `n_comments` = distinct comments with a mention of
  it (two mentions in one comment count once). `label` = the `variety_as_written` used by the
  most comments, ties alphabetical (casefolded, then exact); `crop` likewise, `None` when no
  mention names one. Sorted by `n_comments` descending, label (casefolded), label, key; cut to
  `top_n` (`ValueError` below 1). The total is `Headline.n_varieties` ("top 10 of 44").
- `topic_matrix`: tidy, every `(variety, topic)` cell for the topics occurring on the given
  varieties' mentions, zeros included; rows in the given key order, topics by total comments
  across the grid descending, ties in `schemas.TOPICS` order. A repeated or invisible key
  raises.
- `setup_counts`: per `method` / `shelter` value, the distinct comments whose mentions'
  applicable facts state it (so a `whole_comment` fact on one mention counts its comment once);
  display-only facts never count; only values stated at least once; `label` =
  `vocab.value_label` capitalised ("Containers", "Greenhouse / tunnel"). Sorted by key (method
  first), count descending, vocabulary order. `n_stating_setup` = comments stating either key.
- `summarize`: varieties cut to `cfg.top_varieties`; the topic grid's rows are the first
  `cfg.heatmap_varieties` of them, in the same order. Every function rejects duplicate
  comment ids; results do not depend on the order of the views (property tests).
- `overview_charts` (form chosen first, per the dataviz rules): `varieties_bar` = one horizontal
  `Bar` trace in **one** colour (categorical slot 1: light `#2a78d6`, dark `#3987e5`), never a
  value ramp, largest at the top, tick labels "Sungold · tomato" (`short_label`: at most
  `MAX_LABEL_CHARS` = 24 characters, ending in "…"; hover and table keep the full name), the
  count at each tip in primary ink, no legend, hidden value axis, hairline baseline, `BAR_PX` =
  20 px bars in `ROW_PX` = 32 px bands (≤ 24 px), `CORNER_PX` = 4 px rounded data ends,
  `hovermode="y"` (the whole row is the hover and click target), `customdata = [label]`.
  `topics_heatmap` = a `Heatmap` on the 13-step sequential blue ramp (zero = the step nearest
  the surface), 2 px cell gaps, a horizontal colorbar titled "Comments" above the grid, rotated
  topic labels ("Weather tolerance", `TOPIC_TITLES`), no number in the cells, under an invisible
  `Scatter` with one point per cell that carries hover ("Sungold · Flavor: 4 comments") and
  `customdata = [label, topic]`, because Plotly can select scatter points but not heatmap cells.
  `setups_bar` = two single-colour bar facets ("Growing method", "Setting", `SETUP_TITLES`) on
  one scale with equal bar thickness; an empty facet is left out. Every builder raises
  `ValueError` for other columns or no rows (the viewer shows a note instead).
- Themes: `ChartTheme` (series, ramp, meter track, text primary/secondary/muted, gridline,
  baseline, surface, tile border) is defined once as `LIGHT` and `DARK`, both selected from the
  same validated ramps; `DARK.ramp` runs from step 700 (no comments: recedes into the dark
  surface) to step 150. `theme_for(st.context.theme.type)` picks one (`None` → light; anything
  else raises). Text never wears the series colour. Figures are static: transparent
  backgrounds, `template="none"`, `dragmode=False`, `fixedrange` axes, `CHART_CONFIG` (no mode
  bar, zoom or double-click), fixed heights that include every axis band.
- Escaping: Plotly renders `<a href>`, `<b>`, `<br>`, `<span>` inside tick labels, bar text and
  hover labels, so every comment-derived string in a figure (ticks, hover text, `customdata`)
  goes through `plotly_text` (`&`, `<`, `>` → `&amp;`, `&lt;`, `&gt;`; quotes → `&#34;`,
  `&#39;`, since Plotly does not decode `&quot;`). Shortening happens before escaping. The
  viewer `html.unescape`s click data.

## Demo database (`demo.py`) and the hosted demo (`streamlit_app.py`)

```python
DEMO_SOURCE_ID = "synthetic-demo"; DEMO_KIND = "synthetic_fixture"; FIXTURES_DIR
DEMO_COMMENTS; DEMO_EXTRACTIONS; HOSTED_DATABASE_NAME = "demo.sqlite"; HOSTED_CONFIG_NAME = "config.yaml"
class DemoBuild(BaseModel): stats: ImportStats; report: ExtractionReport     # frozen
class HostedDemo(BaseModel): config: Path; database: Path                    # frozen
def build_demo_database(path: Path, cfg: AppConfig, now: str) -> DemoBuild
def prepare_hosted_demo(directory: Path, cfg: AppConfig, now: str) -> HostedDemo
```

- `build_demo_database` is the one way the demo database is built (the CLI `demo` command and
  the hosted demo): it imports `fixtures/synthetic_comments.jsonl` as source `synthetic-demo`
  (kind `synthetic_fixture`, rights `synthetic`, no expiry, `imported_at = now`) into `path`
  (created with its parent directories by `db.connect`) and loads
  `fixtures/synthetic_extractions.jsonl` with `cfg.extraction`. Re-running it is idempotent
  (69 `unchanged`, an identical report). The fixtures must load cleanly: any rejection raises
  `ValueError` naming the count and the first rejections by comment id (or line) and reason,
  never comment text. The fixtures are found relative to the source tree (checkout or editable
  install).
- `prepare_hosted_demo` requires an empty `directory` (created if missing; anything already
  there raises `ValueError`, so an existing database is never reused), builds
  `directory/demo.sqlite` and writes `directory/config.yaml`: `cfg` with `database.path` = that
  database, `viewer.scope = public` and `logging.json_path = null` (stderr only), as YAML that
  `load_config` reads back unchanged.
- `streamlit_app.py` (repository root) is the Streamlit Community Cloud entrypoint. Cloud runs
  it from the repository root, cannot pass script arguments, health-checks `localhost:8501` and
  may install the dependencies without installing this project, so the entrypoint (1) puts
  `<repo>/src` (and the repository root, for `app`) on `sys.path` explicitly, always importing
  the package from the checkout; (2) in an `st.cache_resource` function, once per server
  process, calls `prepare_hosted_demo(tempfile.mkdtemp(prefix="crop-explorer-demo-"),
  load_config(DEFAULT_CONFIG_PATH), utc_now_iso())` (only the paths are cached; query results
  never are); (3) on every run calls `app.main(["--scope", "public", "--db", database,
  "--config", config])`. It ignores `sys.argv`, never opens `data/` or any database other than
  the one it built, and runs only as `__main__`. Tests: `tests/test_demo.py`,
  `tests/test_streamlit_app.py` (AppTest with an isolated cwd and temp directory: "Reports
  (69)" and the demo banner; the database is built once, under the temp directory; a private
  database at the configured path, a private-scope config and private command-line options are
  all ignored).
- `.streamlit/config.toml` sets no `[server]` address or port (Cloud chooses both); the private
  view stays local through the viewer's loopback guard (see Viewer).

## CLI (`__main__.py`) — `uv run python -m crop_explorer ...`

```python
def main(argv: Sequence[str] | None = None) -> int
def build_parser() -> argparse.ArgumentParser
EXIT_OK = 0; EXIT_ERROR = 2; DEFAULT_KIND = "comments_export"
DEMO_SOURCE_ID; DEMO_KIND; DEMO_COMMENTS; DEMO_EXTRACTIONS   # re-exported from demo.py
```

argparse; global `--config PATH` (default repo `config.yaml`) and `--db PATH` override, accepted
before or after the command (a relative `--db` resolves against the current directory).

```
ingest PATH --scope {private,synthetic,cleared} [--source-id ID (default: file stem)]
            [--kind KIND (default: comments_export)] [--source-url URL]
            [--expires-at ISO | --clear-expiry] [--replace]
            [--id-col] [--text-col] [--permalink-col] [--date-col]
load-extractions PATH [--rejections-out PATH]   # report counts; rejections written as JSONL
export-for-extraction --out PATH [--only-unparsed]   # JSONL of {comment_id, text, text_hash}; private
delete-source SOURCE_ID
purge-expired
summary [--scope {public,private}]               # default: viewer.scope from the config
demo                                             # ingest fixtures as synthetic + load extractions
```

- `ingest`: without `--expires-at` the stored expiry is kept; `--clear-expiry` removes it
  (mutually exclusive with `--expires-at`, a usage error). `--replace` is needed to replace a
  source wholesale (no comment id in common with what is stored) or to change its rights
  status (`--scope`); re-importing an expired source is refused until `purge-expired` or
  `delete-source`. `--permalink-col` / `--date-col` default to none: when given, the column
  must exist (`ColumnMapping.*_required`); when not, `permalink` / `created_at` are used if
  present.
- Success: exactly one line of JSON on stdout, exit 0. `ingest` → `ImportStats` (counts plus
  the effective `rights_status` and `expires_at`);
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
- Output files (`--out`, `--rejections-out`) are checked and opened **before** the database is
  touched, so a bad path exits 2 with nothing loaded. Refused: an existing directory (no mode
  change), a symlink (`O_NOFOLLOW`), the database file or its `-wal`/`-journal`/`-shm` files,
  and (for `--rejections-out`) the command's input file; another regular file is overwritten.
  Files are opened with `os.open(O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW)`.
- `export-for-extraction` makes the output owner-only (mode 0600, `fchmod`) before writing,
  writes `{comment_id, text, text_hash}` lines (`text_hash` = sha256 of the UTF-8 text, as
  stored) as `ensure_ascii=False` JSON with U+0085/U+2028/U+2029 escaped (one record per
  physical line for any splitter), and skips soft-deleted comments and expired sources.
- `demo` runs `demo.build_demo_database(cfg.database.path, cfg, now)`: it ingests
  `fixtures/synthetic_comments.jsonl` as source `synthetic-demo` (kind `synthetic_fixture`,
  rights `synthetic`, no expiry) and loads `fixtures/synthetic_extractions.jsonl`; re-running
  it is idempotent. Any extraction rejection is an error (`ValueError`, exit 2). The fixtures
  are found relative to the source tree (checkout or editable install).
- `imported_at`, `loaded_at` and `now` come from `schemas.utc_now_iso()` at the CLI edge.

## Viewer (`app.py`, thin) + `ui.py` (pure helpers)

`uv run streamlit run app.py [--server.address localhost] [-- --scope private --db PATH
--config PATH]`; a private view needs `--server.address localhost` (it is refused otherwise).
`app.main(argv: Sequence[str] | None = None)` renders the page (`None` reads `sys.argv[1:]`,
the arguments Streamlit passes after `--`); the module only calls it under
`if __name__ == "__main__":` (Streamlit runs the script as `__main__`), so the hosted demo can
import it and pass its own options. One Browse screen
(`layout="centered"`, phone-friendly; Streamlit stacks columns on narrow screens). Every
function in `app.py`, `ui.py` and `geography.py` has at most 15 code lines
(`tests/test_code_standards.py`).

- **Startup guards** (each shows `st.error` and calls `st.stop()`, never a traceback):
  `ui.parse_app_args` uses `parse_args` (not `parse_known_args`, `allow_abbrev=False`), so an
  unknown, abbreviated or invalid option (`--database`, `--confg`, `--scope everyone`) raises
  `ValueError("Invalid viewer options: ...")` instead of silently opening the default database
  or config; Streamlit passes only the arguments after `--`. A **private** scope runs only
  when `st.get_option("server.address")` is a loopback address (`ui.is_loopback_address`:
  `localhost`, `127.0.0.0/8`, `::1`; unset/empty = every interface = refused), because the
  viewer has no authentication; the error (`ui.private_bind_error`) gives the command
  `uv run streamlit run app.py --server.address localhost -- --scope private`.
  `.streamlit/config.toml` sets no address (Streamlit Community Cloud must choose it), so the
  private command must include `--server.address localhost`; without it the private view is
  refused. If the database file does not exist, the app
  shows how to build it (`uv run python -m crop_explorer demo`); if `db.connect` raises
  `sqlite3.DatabaseError` (not a database, or an unsupported schema version) it shows the
  reason and the same command. It never creates an empty database.
- Header "What Gardeners Are Saying", subtitle "Find real experiences from gardens like yours.",
  "About / Sources" expander (visible sources with rights labels and summary counts). A banner
  states the scope: public → "Demo data: synthetic comments, not real gardener reports" when
  only synthetic sources are visible (a mixed notice when synthetic and cleared are both
  visible; none for cleared only); private → "Private local view — includes unverified imported
  comments. Do not publish or share." A `private_unverified` row in public scope raises.
- **At a glance** (subheader, right after the banner, before "My growing conditions"), from
  `overview.summarize(views, cfg.overview)` over the views `_browse` already loaded (same scope
  gating; computed once per run, never cached). Stat tiles (`ui.kpi_tiles_html(headline,
  theme)`: trusted HTML built from numbers and fixed labels only, a CSS grid four across and two
  by two below 640 px, proportional figures): "Reports", "Varieties mentioned", "State growing
  conditions" ("33 of 69" with a `role="meter"` bar whose track is a lighter step of the fill's
  ramp, drawn with flex ratios so no percentage appears) and "Places named" ("18 U.S. states ·
  3 outside the U.S."). With no visible report the tiles show zeros and no tabs follow. Then
  `st.tabs(ui.GLANCE_TABS)` = **Most-discussed varieties** | **What people talk about** | **How
  people grow**, each with a caption (`ui.varieties_caption`: "Comments that mention each
  variety, top 10 of 44 across all 69 reports. This counts conversation, not quality: a failed
  crop counts the same as a great one. Select a bar to read those comments."; `ui.topics_caption`;
  `ui.setups_caption`: "Only reports that state a growing method or setting are counted: 20 of
  69. Most don't say. A report can state more than one."), the chart (`st.plotly_chart(...,
  theme=None, width="stretch", config=CHART_CONFIG)`) and a "Show as table" expander with
  `st.dataframe` of the tidy data (`ui.variety_table`, `ui.topic_table`, `ui.setup_table`:
  readable column names, names as written, shown as plain text). An empty chart is replaced by
  `ui.NO_VARIETIES`, `ui.NO_TOPICS` or `ui.NO_SETUPS`. Tokens come from
  `overview_charts.theme_for(st.context.theme.type)`; `.streamlit/config.toml` sets
  `[theme.light]` and `[theme.dark]` accents (a single `[theme]` colour would pin a light custom
  theme while `st.context.theme` reports the visitor's dark setting). A theme switched in the
  settings menu applies to the charts on the next rerun.
- **Click-through** (like the map): the variety and topic charts use `on_select` callbacks with
  `selection_mode="points"`. `ui.variety_click_updates(selection, max_chars)` sets the search
  text to the bar's label (unescaped, cut to `viewer.max_query_chars`) and clears Topics;
  `ui.cell_click_updates` sets the search text to the variety and Topics to the cell's topic.
  Anything else (no point, wrong shape, unknown topic) changes nothing. Other filters and the
  garden conditions are left as they are. Chart keys are `ui.glance_key(chart, state)`, which
  change with the search text and topics, so a chart remounts without a stale selection.
- **My growing conditions** panel (always visible, works with nothing selected), laid out per
  SPEC §2 "Which controls are prominent?": Region (selectbox: "Not set", then U.S. states,
  Canadian provinces, countries, each by name) and `ui.ALWAYS_VISIBLE_KEYS`
  (= `vocab.PROMINENT_KEYS`) = zone (with a
  note on what zones do not describe), growing method, setting; the "More conditions"
  expander holds `ui.MORE_KEYS` = summer heat, humidity, rainfall, season length, sun, soil,
  watering. The expander's label names the conditions set inside it
  (`ui.more_conditions_label(ui.active_more_conditions(state))`, e.g. "More conditions (hot
  summers, humid)") and it opens whenever one is set, so a chip never leaves a condition active
  out of sight. Clear resets every condition and filter (session state only; nothing persisted
  server-side).
- Every select whose first option is `None` starts unselected (`index=None`) and has
  `placeholder=` its `None` label ("Not set", "Any crop", "Any year", `ui.ALL_STATES`), so
  first load, Clear, "Show all states" and "Any year" all look the same (never "Choose an
  option"). Resets stay in `on_click` callbacks.
- Quick chips from config, shown only when `chip_support[label].n_any >= min_reports_for_chip`
  (`ui.visible_chips`); a chip sets its conditions and is highlighted while they are all set.
  Its tooltip (`ui.chip_help`) states what is counted: one condition → "Set this condition
  (8 reports state it)"; several → both counts, e.g. "6 reports mention containers or
  greenhouse / tunnel; 2 mention both". A chip with a self-described condition
  (`vocab.SELF_DESCRIBED_KEYS`) counts reports that describe it in their own, uncalibrated
  words and says so: "6 reports describe hot summers or humid conditions in their own words;
  2 describe both", "Set this condition (4 reports describe it in their own words)" (bare
  adjectives get a noun: "humid conditions", "rainy conditions").
- Search box ("Search crops, varieties, or anything people mentioned",
  `max_chars=cfg.viewer.max_query_chars`; `ui.query_from_state(state, max_query_chars=...)`
  also cuts the text, as defence in depth), then the filter row **Crop | Topics | Year grown**
  (hard mention-level filters, unlike the garden conditions), then "Only show stated
  matches"; a caption always says how many reports the checkbox hides (or would hide): "N
  reports hidden because their conditions were not stated, not comparable or differ." /
  "Ticking this would hide N reports whose conditions were not stated, not comparable or
  differ." (the checkbox help says the same). With
  only a Region set it says Region only lists reports from your area first and a condition
  such as zone or growing method is needed (`ui.strict_note`). Filter values that vanish from
  the visible data mid-session are reset (`ui.stale_state_fixes`).
- "Optional outside context" expander directly before the tabs (SPEC §2 mockup), listing
  config links each tagged with its label badge (`regional climate reference`, `published
  external trial`, `community report` for outside gardeners' ratings such as Cornell's; never
  on an official host, see `config.is_official_host`).
- Result tabs: **Comments** | **Map** (after the three "At a glance" tabs). The Comments tab
  starts with the active hard-filter notes, each
  with its reset button: "Showing only reports that say they grew in YEAR." + "Any year"
  (`ui.year_filter_note`), and the reported-state note + "Show all states". Then the
  limitation message and the count summary (`ui.count_summary`, e.g. "70 reports · 3
  matching · 4 partially matching · 55 not stated or not comparable · 8 different", its tier
  words echoing the group titles), which are always visible. When no group has a
  report, `ui.empty_state(result)` gives "No reports to show. Try a different search, or
  remove a filter or condition." or, when strict mode hid everything, "All N reports are
  hidden by "Only show stated matches". Untick it to see them." (`None`, i.e. no second box,
  when no report states a comparable condition, because the limitation message then already
  says so).
- **Paging:** each group renders at most `cfg.viewer.page_size` cards
  (`ui.cards_to_show(state, group, page_size)`) with a "Show N more" button
  (`ui.show_more_label`) that adds a page; headings keep the full count. Page counts live in
  session state (`ui.page_key(group)`) and reset to one page whenever the query changes
  (`ui.paging_fixes(state, query)` compares a `SearchQuery.model_dump_json()` fingerprint).
  The map always counts every shown result, not only the rendered page.
- **Cards:** heading `ui.card_heading(result)` = bold escaped title
  `"{variety} {crop}, ... · {topics}"` (fallback "Comment") plus a `community report` badge;
  the comment text rendered as **plain text** through `ui.escape_markdown`; then one caption,
  `ui.condition_caption(result, garden)`: the escaped `ui.condition_lines` plus (for an analysed
  comment) the region line, led by a `reported conditions` badge when a condition line shows a
  comparable condition the commenter gave (a normalized or written value, including an
  own-words line that is described differently or not comparable). A region alone gets
  no badge: it is never compared, the badge would sit before "Growing conditions not stated",
  and the heading's `community report` badge already labels the region line. Condition lines:
  - never extracted (`parse_status is None`) → only "Not yet analysed for growing conditions"
    (no region line, no badge);
  - with a selection, for the best relevant mention: "Relevant: …" (matches, and imprecise
    matches with both values), "Not stated: …", "Mentioned as written, not comparable: soil
    "rocky hillside dirt"" (a selected key whose only facts are display-only, phrases quoted
    with whitespace collapsed) and "Different: …"; each labels only the reported values whose
    own comparison produced that status (`environment_match.values_with`);
  - then, for each selected **self-described** condition (`vocab.SELF_DESCRIBED_KEYS`: summer
    heat, humidity, rainfall, season length) with a reported value, its own line in the
    commenter's words instead of a place in "Relevant" or "Different" (a self-described key
    with no reported value stays in "Not stated" or "Mentioned as written"). The words are
    the `value_as_written` of the facts behind the values that produced the status
    (deduplicated, whitespace collapsed, in typographic quotes, joined with " / "), then the
    normalized reading, then that mention's region (names of its applicable `region` facts,
    an unmapped one as "Cape Cod (not mapped)"; omitted when none is stated, since the card's
    region line already says so), because "hot" means
    something different in Connecticut and Texas. Lines that count (match, imprecise) come
    first, in canonical key order:
    - match: "In their own words: “brutal and muggy” (counted as humid) · Connecticut";
    - imprecise (a heatwave for a hot-summer viewer, also when mild summers were reported
      too): "In their own words: “that July heat wave” (a heatwave; you chose hot summers) ·
      Texas";
    - described differently: "In their own words: “mild summers” (mild summers; you chose hot
      summers) · not counted as a difference" (never "Different");
    - not comparable (a heatwave for a mild- or cool-summer viewer): "… (a heatwave; you
      chose mild summers) · not comparable";
  - several relevant mentions: led by "Shown for X (1 of n mentions)" and followed by one
    "Y: <tier line>" per other mention whose tier differs from the best, e.g. "Roma:
    Different: outdoors (you: greenhouse / tunnel)". The tier line is the first present of:
    match/partial → "Relevant", a counting own-words line; not stated → "Not stated",
    "Mentioned as written", an own-words line that does not count; different → "Different";
  - without a selection: "Reported: …" (or "Reported for X: …" per mention), then "Mentioned
    as written, not comparable: …" for display-only comparable-key phrases, else "Growing
    conditions not stated".
  Then an "Evidence" expander listing each fact's exact `evidence_quote` with the label
  `community report · reported conditions`, its basis and what it applies to.
  `CommentResult.explanation` is not shown separately because the condition lines carry the
  same information in structured form.
- **Source links:** `st.link_button("Source ↗")` only for a non-synthetic comment with a
  permalink (`ui.source_link` returns `None` for synthetic rows: fiction never links out, and a
  non-http(s) permalink raises). `ui.provenance_note` checks rights first: a synthetic row
  always says "Synthetic demo comment — no source link"; a real one without a permalink says
  "No source link available".
- Map tab: `community report` and `reported conditions` badges and its intro caption, then the
  choropleth built from the same query **without** `region_filter` (so it stays usable as a
  selector) over the shown groups, plus an accessible "Filter by reported state" selectbox
  (alphabetical, labelled with `geography.count_label`, so suppressed counts never show; the
  current filter stays selectable). Selecting a state, or clicking it on the map, sets
  `region_filter`. The chart's widget key is `ui.map_key(region_filter)`, so it remounts
  without a stale click selection whenever the filter changes. "Region not reported: N",
  "Outside the U.S.: N" and (when non-zero) "In the U.S., state not reported: N" are shown
  separately. Suppression coarsens this display only (see Geography).
- Logging is configured once per server process (`st.cache_resource` around
  `configure_logging` only); a database connection is opened per script run. Visible views are
  loaded by `_browse`, `search.filter_options` and `search.chip_support` separately (each
  takes a connection); paging removes the per-card rendering cost that dominated.
- `ui.escape_markdown` backslash-escapes every ASCII punctuation character, inserts an invisible
  U+2060 word joiner after every `:` and before every `.` (so emoji/icon shortcodes and GFM
  autolink literals cannot form), turns leading spaces/tabs into NBSP and line breaks into hard
  breaks. Copied text therefore contains invisible U+2060 characters.
- `ui.py` pure helpers (unit tested), main ones: `escape_markdown`, `card_title`,
  `card_heading`, `condition_lines(result, garden)`, `condition_caption`, `region_line`,
  `evidence_rows` (`EvidenceRow`), `scope_banner`, `region_options`,
  `reset_values`/`reset_state_keys`, `garden_from_state`, `query_from_state`,
  `stale_state_fixes`, `visible_chips`/`chip_help`/`chip_state`/`chip_is_active`,
  `active_more_conditions`/`more_conditions_label`, `count_summary`, `group_heading`,
  `strict_note`, `empty_state`, `year_filter_note`, `region_filter_note`,
  `paging_fixes`/`cards_to_show`/`show_more_label`/`page_key`, `source_link`,
  `provenance_note`, `source_rows`, `summary_line`, `outside_link_markdown`, `label_badge`,
  `min_cell_count`, `map_key`, `map_notes`, `map_colorbar`, `state_choices`,
  `selected_state`, `parse_app_args`, `is_loopback_address`, `private_bind_error`, and for
  the header `kpi_tiles_html`, `varieties_caption`, `topics_caption`, `setups_caption`,
  `variety_click_updates`, `cell_click_updates`, `glance_key`, `variety_table`, `topic_table`,
  `setup_table`.
- Tests: `tests/test_overview.py` and `tests/test_overview_charts.py` (counts, tie-breaks,
  marks, colours per theme, escaping, Hypothesis properties), `tests/test_ui.py` (pure
  helpers), `tests/test_app.py` (AppTest end to end, incl. the header's place and numbers on
  the demo database, a public view whose `private_unverified` variety, crop and state never
  reach a tile, figure or table, no quality words in the header, and a
  cleared comment's "Source ↗" URL, the synthetic/no-link notes, the loopback guard and
  invalid options) and `tests/test_browser.py` (marker `browser`: `streamlit run` headless on a
  free loopback port, Playwright Chromium at 390x844 — no horizontal overflow, search box and
  always-visible selects shown, zone 6b regroups, Clear restores the first-load values and
  placeholders, the Source link's href, the Hot & humid chip's "In their own words" lines
  without overflow, the "At a glance" tiles two by two with the first chart rendered — then a
  1280x900 smoke test, tiles four across, and a real click on a variety bar (search becomes
  "Juliet", 5 reports) and on a topic cell (search "Sungold", Topics "productivity"); CI runs
  `uv run playwright install --with-deps chromium` first).
