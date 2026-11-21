# BillWise — Phase 1

BillWise will eventually explain US medical bills and insurance Explanations of
Benefits (EOBs) in plain English. **Phase 1** covers only the groundwork:

1. **Parsing**: turn a text bill or EOB into structured line items (service date,
   CPT/HCPCS code, description, billed, allowed, insurer paid, patient responsibility).
2. **Redaction**: remove personal identifiers *before* any later model call.
3. **Evaluation**: measure both against labeled ground truth and report the numbers
   the harness actually computes.

There is no retrieval, no LLM, no cloud service and no API key in this phase.

## Data: synthetic only

**This project uses only synthetic data. Do not put real patient data into this
repository, its tests, or its issue tracker.**

`data_gen/generate.py` creates fake bills and EOBs and writes a ground-truth JSON file
for each one:

| Set | Docs | Layouts | Purpose |
|---|---|---|---|
| `data/synthetic/` | 48 | hospital fixed-width statement, pipe-table EOB, key/value claim summary, clinic letter with CSV (12 each) | development set |
| `data/dev2/` | 12 | physician-group statement with different column names, modifier/units columns, page breaks, and heavier OCR noise | round-1 held-out set. Its failures drove the round-2 fixes, so it now counts as **development data** |
| `data/dev3/` | 12 | multi-claim benefits statement: several tab-separated tables, dot-leader labels, `Last First` names, dash-format DOBs | round-2 held-out set. Its failures drove the round-3 fixes, so it now counts as **development data** |
| `data/heldout/` | 12 | patient-portal visit summary: two-line items with inline `Billed $… · Plan paid $…` amounts, honorific names in a sentence, dotted DOBs, slash phones | **held out**: written before the round-3 fixes and never used to tune them |

How the fake identifiers are made:
- Phone numbers use the reserved fictional `555-01XX` range.
- SSN-like strings start with `9`, an area number that is never issued.
- Emails use the reserved `example.*` domains.
- Names, streets and cities are drawn at random from generic lists.

Messy cases the generator injects (each document records which ones it has in its
`messy` field):
- multi-line (wrapped) descriptions, including quoted multi-line CSV cells
- missing fields: allowed amount, insurer paid, code, phone, email
- blank repeated dates
- OCR-style typos in amounts (`1O5.00`, `82.7l`, `S45.00`, `82,40`), labels
  (`Patlent`, `Memher ID`, `Date of Birlh`) and descriptions
- duplicate charges

Each `bill_NNN.json` records:
- `pii`: exact character spans that must be redacted
- `allowed_redactions`: business contact details (insurer phone, clinic address,
  provider name) that may be redacted with no penalty either way
- `line_items`: the expected parse
- `duplicate_pairs`: the expected duplicate-charge flags

The generator is deterministic. A test regenerates the dataset and checks it is
byte-identical to the committed files.

```powershell
python -m data_gen.generate --out data/synthetic --n 48 --seed 2025
```

```powershell
python -m data_gen.generate --out data/dev2 --n 12 --seed 777 --ocr-p 0.25 --set dev2
```

```powershell
python -m data_gen.generate --out data/dev3 --n 12 --seed 4242 --ocr-p 0.2 --set dev3
```

```powershell
python -m data_gen.generate --out data/heldout --n 12 --seed 9001 --ocr-p 0.2 --set heldout
```

## Privacy approach

- **Redact before any model call.** Later phases must pass only `redacted_text` to an LLM.
- **Typed placeholders.** Values are replaced with `[NAME_1]`, `[ADDRESS_2]`,
  `[PHONE_1]`, `[EMAIL_1]`, `[DOB_1]`, `[SSN_1]`, `[MEMBER_ID_1]` and `[ACCOUNT_ID_1]`.
  The same value always gets the same placeholder, so `DOE, JANE`, `Jane Doe` and
  `Dear Jane,` all become `[NAME_1]` and the text stays readable.
- **Reversible, in memory only.** The placeholder→original mapping lives only on the
  `RedactionResult` object. `restore()` rebuilds the exact original text. The mapping is
  left out of `repr()`, never written to disk, and never returned by the API.
- **The API stores nothing.** `/redact` returns the redacted text plus each entity's
  type, offsets and placeholder, but never the original values. The Docker image runs
  uvicorn with `--no-access-log`.
- **Detection rules** in [billwise/redact.py](billwise/redact.py):
  - Labeled fields: `Patient Name:`, `Member ID:`, `DOB`, `Claim Number:`, and so on.
    Labels are case-insensitive and tolerate common OCR confusions (`Patlent`,
    `Memher ID`, `Acc0unt`). Name labels must be followed by `:`, `#` or dot leaders,
    except `Dear`.
  - Pattern regexes for SSNs, phones, emails, street addresses, and city/state/ZIP.
  - Names with no label: a name-shaped line directly above a street address, or a
    first name from a built-in list of common names.
  - Propagation: once a name or ID is found, every other occurrence of it is
    redacted too.
  - When matches overlap, a priority order decides which one wins.

## Running

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
```

```powershell
.venv\Scripts\python -m pytest -q
```

```powershell
.venv\Scripts\python -m eval.run_eval
```

`run_eval` evaluates all four datasets, prints a report with every failure, and writes
[eval/results.json](eval/results.json). The most recent full console output is saved
in [eval/last_run.txt](eval/last_run.txt).

API:

```powershell
.venv\Scripts\uvicorn billwise.api:app --port 8000
```

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/redact -ContentType 'application/json' -Body '{"text": "Patient Name: DOE, JANE\nMember ID: W123456789"}'
```

- `POST /redact` and `POST /parse` take `{"text": "..."}`.
- Input limits: text must be 1–100,000 characters, not blank, with no NUL bytes
  (otherwise 422). The raw request body is capped at 512 KB (otherwise 413), including
  chunked uploads.
- `GET /health` is a liveness check.

Docker:

```powershell
docker build -t billwise .
```

```powershell
docker run -p 8000:8000 billwise
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests and the full
evaluation on Python 3.11 and 3.12, uploads `results.json`, then builds the Docker
image and smoke-tests it.

## How metrics are computed

**PII**
- A gold span counts as recalled when a predicted span of the same type overlaps it.
- A predicted span counts as correct when it overlaps a gold span of the same type.
- Predictions that overlap only `allowed_redactions` are counted as *neutral* and
  left out of precision.
- *Leak-free* means every character of the gold value is covered by some
  redaction (of any type). This is the privacy-relevant number.
- Exact-span matches are reported as well.

**Parsing**
- Predicted lines are aligned to gold lines by how many fields agree (at least 3 of 7).
- Field accuracy = correct fields / gold lines. A missed line counts as wrong on
  every field.
- Descriptions are compared after case-folding and collapsing whitespace. OCR typos
  in descriptions are part of the expected text.

## Results (from `eval/last_run.txt`; synthetic data only)

These are copied from the latest run of `python -m eval.run_eval`. Rerun it to
reproduce them.

| | `synthetic` (dev, 48) | `dev2` (dev, 12) | `dev3` (dev, 12) | `heldout` (12) |
|---|---|---|---|---|
| PII recall (same-type overlap) | 100.00% (380/380) | 100.00% (79/79) | 100.00% (123/123) | **44.44%** (32/72) |
| PII leak-free | 100.00% | 100.00% | 100.00% | 44.44% |
| PII precision | 100.00% | 100.00% | 100.00% | 100.00% |
| Lines found | 225/225 | 60/60 | 60/60 | **0/64** |
| Line-item field accuracy | 100.00% | 100.00% | 100.00% | **0.00%** |
| Bills parsed fully correctly | 48/48 | 12/12 | 12/12 | 0/12 |
| Duplicate pairs flagged | 8/8 | 2/2 | 1/1 | 0/3 |

What fails on the held-out set:
- **DOB 0/12:** dotted dates (`1986.02.28`) aren't an accepted DOB format.
- **PHONE 0/12:** a slash after the area code (`212/555-0166`) isn't an accepted
  phone format.
- **MEMBER_ID 0/12:** the label is a bare `ID` after the plan name
  (`Plan: Summit Mutual Health, ID W123…`), which no ID rule covers.
- **NAME 8/12:** the name sits in a sentence (`This statement is for Mx. Thanh
  Sullivan.`). It's caught only when the first name is on the built-in list.
- **Parsing 0/64 lines:** each item is a heading line (`Item 1 of 8 — 2025-07-09 —
  CPT 99214`), a description line, and a line of inline `Label $amount` pairs. There
  is no table header and no `Label: value` blocks, so none of the five strategies
  applies.

### History

| Round | Held-out set at the time | PII recall | Field accuracy |
|---|---|---|---|
| 1 | group statement (now `dev2`) | 54.43% | 57.14% |
| 2 | multi-claim statement (now `dev3`) | 90.24% | 0.00% |
| 3 | portal visit summary (`heldout`) | 44.44% | 0.00% |

Round-2 fixes, all driven by failures in `synthetic` and `dev2`:
- OCR-tolerant and case-insensitive labels
- more ID labels, phones with spaces, names found above a street address
- modifier/units columns kept out of descriptions; `pmt` and `balance` header words
- duplicate detection that tolerates OCR typos in descriptions

Round-3 fixes, all driven by failures in `dev3`:
- one reader for `|`, `,` and tab tables, with any number of tables per document
- columns with unfamiliar headers typed from their contents (dates, codes)
- header words `dt`, `covered`, `pymt`, `member resp`; dashes read as missing values
- `MM-DD-YYYY` accepted for dates of birth

Every round's development scores reached 100%, and every new held-out layout exposed
new gaps. Treat the development numbers as "fits the known formats", not as accuracy.
Fixing one format at a time is not converging: what's missing is a parser that doesn't
depend on a recognisable table, and redaction that doesn't depend on exact formats.

## Limitations

- **All results are on synthetic data.** They say nothing reliable about real bills.
  Real documents vary far more than seven templates can.
- **The development scores mean little on their own.** The same author wrote the
  generators, parser and redactor, and tuned against the development sets. Even the
  held-out layouts were written by the same person who wrote the code, which is a
  weaker separation than having someone else write them.
- **The parser depends on recognising a table or `Label: value` blocks.** In both
  round 2 and round 3 it found 0 lines on the new layout. It fails completely instead
  of degrading gracefully.
- **Rule-based redaction is brittle:** each unseen date, phone or label format leaks
  every value written that way. Names with no label are caught only when they sit
  above an address or start with a first name from the built-in list.
- **The parser reads text only.** There is no PDF or image input and no OCR.
- Ground truth was made by the same program that renders the text, so it carries none
  of the label noise that human annotation would.
- Business-contact redactions (insurer phones, clinic addresses) are neutral by design,
  so their precision cost doesn't show in the numbers.
