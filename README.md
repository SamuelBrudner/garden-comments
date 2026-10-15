# What Gardeners Are Saying (crop_explorer)

A small, local, growing-environment-first explorer of gardeners' firsthand comments about crop
varieties. A visitor describes a few conditions of their own garden (region, hardiness zone,
containers or in-ground, greenhouse, hot or humid summers, ...) and searches for a crop or
variety. The app shows the actual comments, grouped by **how well the conditions the commenter
stated match the visitor's conditions**:

1. **Matching reported conditions**: every selected condition that the commenter stated agrees.
2. **Partially matching conditions**: some match, the rest were not stated.
3. **Conditions not stated**: nothing comparable was said (kept visible: unknown is not a
   mismatch).
4. **Different reported conditions**: at least one stated condition differs (collapsed, never
   hidden by default).

Every condition on a card links to the exact passage of the comment it came from, and the
app always says plainly how few reports describe their growing conditions at all.

**What it is not.** It does not recommend varieties, score them, rank them by quality, predict
success, or summarise opinions with a model. A failure reported under conditions like yours is
exactly as relevant as a success. Matching is deterministic application logic over facts that
were extracted offline and checked against the comment text; no model is called when someone
searches. A hardiness zone is not treated as a climate, and a state is not treated as proof of
similar growing conditions.

The product spec is [`SPEC.md`](SPEC.md); the binding module contracts are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ and the locked
dependencies).

```bash
uv sync                                    # install the locked environment
uv run python -m crop_explorer demo        # build data/crop_explorer.sqlite from the synthetic fixtures
uv run streamlit run app.py                # open the public-safe demo viewer
```

The demo loads 69 fictional comments (`fixtures/synthetic_comments.jsonl`) and their
extractions, imported with rights status `synthetic`; the viewer labels them as demo data. See
[`fixtures/SCENARIOS.md`](fixtures/SCENARIOS.md) for what each one exercises.

To browse your own private imports locally (never for publication):

```bash
uv run streamlit run app.py -- --scope private
```

Viewer options go after `--`: `--scope {public,private}`, `--db PATH` and `--config PATH`. If the
database does not exist yet, the app tells you to run the `demo` command instead of creating an
empty one.

## Working with real comments (CLI round trip)

Real comments are imported as **`private_unverified`** and are only ever visible in the private
local view. Extraction is an offline model step: you run a capable model (Codex, Claude or
similar) yourself with the instructions in
[`docs/EXTRACTION_PROMPT.md`](docs/EXTRACTION_PROMPT.md); no hosted-model API is required by
the app.

```bash
# 1. Import a CSV or JSONL export as one private source (idempotent; re-run after updates).
#    Columns default to comment_id, text, permalink, created_at; map others with
#    --id-col, --text-col, --permalink-col and --date-col. --expires-at sets a rights expiry.
uv run python -m crop_explorer ingest data/comments.csv --scope private --source-id my-export

# 2. Export what still needs extraction: owner-only (0600) JSONL of {"comment_id", "text"}.
uv run python -m crop_explorer export-for-extraction --out data/to_extract.jsonl --only-unparsed

# 3. Model step (offline): follow Part 2 of docs/EXTRACTION_PROMPT.md over
#    data/to_extract.jsonl and write data/parsed_comments.jsonl (one ParsedComment per line).

# 4. Validate against the stored text and load. Prints counts only; every rejection is
#    written as {comment_id, line_number, mention_index, fact_index, reason}.
uv run python -m crop_explorer load-extractions data/parsed_comments.jsonl \
    --rejections-out data/rejections.jsonl

# 5. Browse privately.
uv run streamlit run app.py -- --scope private
```

The loader keeps nothing it cannot verify: every evidence quote must be an exact substring of
the stored comment, region/zone/year values must equal the loader's own normalisation of the
quoted words, a value may not contradict the quoted words ("pots" is never `raised_bed`), and
facts from sentences containing advice or hypothetical markers ("I'd try these in zone 5") are
rejected. A rejected fact never removes the comment from full-text search. Re-loading the same
file is idempotent; re-importing a comment with changed text drops its old extraction, so run
step 2 with `--only-unparsed` again.

Other commands:

```bash
uv run python -m crop_explorer summary [--scope public|private]   # what a viewer can see (counts)
uv run python -m crop_explorer delete-source my-export            # remove a source and everything derived from it
uv run python -m crop_explorer purge-expired                      # delete sources whose --expires-at has passed
uv run python -m crop_explorer --help
```

Every command prints one line of JSON with ids and counts and exits 0; invalid input prints one
`error: ...` line to stderr and exits 2. Comment text is never printed or logged; only
`export-for-extraction` writes it, to an owner-only file. `--db PATH` and `--config PATH` work
with every command.

## Rights and the publication gate

The first deliverable is a **private local prototype**, not a public copy of anyone's comments
(SPEC §9). Each imported source carries a rights status:

| `ingest --scope` | Rights status | Public viewer | Private viewer |
|---|---|---|---|
| `private` | `private_unverified` | never | yes, with a "do not publish" banner |
| `synthetic` | `synthetic` (fictional demo data) | yes, labelled as demo data | yes |
| `cleared` | `cleared` | yes | yes |

- `private_unverified` content never reaches a public result, count, quick chip, filter option,
  map cell, snippet or cache. Every viewer-facing database read applies one shared rights and
  expiry clause (`crop_explorer.visibility`), and the viewer never caches query results.
- The public-safe mode is the synthetic demo. Use `--scope cleared` only after the specific
  distribution and storage workflow for those comments has been cleared. A creator's approval
  does not by itself grant permission to republish other people's comments, and it does not
  override platform terms.
- Sources can carry an expiry (`--expires-at`): expired sources disappear from every view
  immediately, are skipped by `export-for-extraction`, and are removed by `purge-expired`.
  `delete-source` cascades through comments, extractions, search index, counts and map.
- Before any public use of YouTube-derived content, review:
  [YouTube Developer Policies](https://developers.google.com/youtube/terms/developer-policies),
  the [Developer Policy Guide](https://developers.google.com/youtube/terms/developer-policies-guide)
  and the [derived metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy).
- Do not imply creator endorsement or use channel branding without permission.
- `data/` (database, logs, exports, model output) is git-ignored. Exports and model output
  contain verbatim comment text and are as private as the source.

## Configuration

`config.yaml` (validated by `crop_explorer.config.AppConfig`; relative paths resolve against the
config file):

| Key | Meaning |
|---|---|
| `database.path` | SQLite file (default `data/crop_explorer.sqlite`; `--db` overrides it). |
| `viewer.scope` | Default viewer scope, `public` (fail-safe) or `private`. |
| `viewer.min_reports_for_chip` | A quick chip ("Containers", "Hot & humid", ...) appears only when at least this many visible reports support it. |
| `map.public_min_cell_count` | In public scope, states with fewer reports are shown as "fewer than k". Raise it (e.g. to 5) before publishing real cleared comments. |
| `extraction.hypothetical_markers` | Words that make a sentence advice or a plan; facts quoted from such sentences are rejected. |
| `quick_chips` | Label plus the garden conditions each chip sets. |
| `outside_context` | Opt-in outbound links (USDA zone map, NOAA normals, variety trials), each labelled `regional climate reference` or `published external trial`. |
| `logging.level`, `logging.json_path` | Loguru level and optional JSON-lines log file (never receives comment text). |

## Testing and quality

```bash
uv run pytest                                              # whole suite (unit, property, CLI, AppTest end-to-end)
uv run pytest --cov=crop_explorer --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy                                                # strict, over src/ and app.py
uv run pre-commit run --all-files                          # ruff, format and mypy, as in CI
```

Tests were written first and use synthetic comments only. They include Hypothesis property
tests for the pure logic (normalisation, matching, grouping, visibility, escaping), every SPEC
§11 acceptance case (`tests/test_acceptance.py`; #17, phone-width inputs and resets, in
`tests/test_app.py`), rights-isolation checks across search, chips, filters, counts and the map,
and Streamlit `AppTest` runs of the viewer. CI
(`.github/workflows/ci.yml`) runs the pre-commit hooks and the tests with coverage.

## Project layout

```text
├── SPEC.md                       # product spec (v0.5)
├── config.yaml                   # runtime configuration
├── app.py                        # Streamlit Browse screen (thin; logic lives in the package)
├── docs/
│   ├── ARCHITECTURE.md           # module contracts and exact behavioural rules
│   └── EXTRACTION_PROMPT.md      # operator guide + instructions for the offline model step
├── fixtures/
│   ├── synthetic_comments.jsonl  # 69 fictional comments (public-safe demo data)
│   ├── synthetic_extractions.jsonl
│   └── SCENARIOS.md              # what each synthetic comment exercises
├── src/crop_explorer/
│   ├── __main__.py               # CLI
│   ├── schemas.py                # extraction contract, raw records, canonical UTC time
│   ├── vocab.py, regions.py      # controlled vocabulary and region codes
│   ├── views.py                  # read-side types (garden, views, match and search results)
│   ├── config.py, logging_setup.py
│   ├── ingest.py                 # CSV/JSONL import
│   ├── extraction.py             # extractor-output validator and loader
│   ├── normalization.py          # small deterministic synonym tables
│   ├── environment_match.py      # pure, explainable matching and tiers
│   ├── search.py                 # candidates, groups, honest counts, chips, filter options
│   ├── geography.py              # conservative state counts and the choropleth
│   ├── db.py                     # SQLite + FTS5 persistence and scope-gated reads
│   ├── visibility.py             # rights/expiry gate shared by every viewer read
│   └── ui.py                     # pure viewer helpers (escaping, card text, state)
└── tests/                        # unit tests per module, plus acceptance, CLI and viewer tests
```

## Limitations

- **Synthetic tests are not evidence of real-world extraction accuracy.** The fixtures were
  written for this repository; passing tests show the pipeline enforces its rules, not that a
  model extracts real comments well. The validator guarantees that every stored fact is quoted
  exactly and normalised consistently, not that the model chose the right facts or attached
  them to the right variety.
- Extraction quality depends on the model you run offline and on following
  `docs/EXTRACTION_PROMPT.md`. Review `rejections.jsonl` and the extractor's warnings.
- Normalisation is deliberately conservative: negated, ambiguous or combined phrases ("not
  humid", "sandy loam", "zones 6-7", "Kansas City") stay display-only and are never used for
  matching. The hypothetical guard works sentence by sentence, so advice phrased without a
  configured marker is not caught.
- Regions are coarse (U.S. states, Canadian provinces, countries); the map shows U.S. states
  only. Small-cell suppression is per cell; with a threshold above 1, a suppressed count can
  sometimes be derived by subtraction from the "not reported" totals, so add complementary
  suppression before publishing real data with small cells.
- Built for small datasets: each viewer rerun loads all visible comments into Python (fine for
  thousands of comments, not millions). CSV cells over 131,072 characters are rejected.
- The viewer is a local, single-user tool with no accounts. Never expose a private-scope
  viewer on a network. Text copied from cards contains invisible U+2060 characters (part of
  rendering untrusted text literally).
- `demo` reads the fixtures from the source tree, so it needs a checkout or an editable install.
