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
| `data/heldout/` | 12 | physician-group statement with different column names, modifier/units columns, page breaks, and heavier OCR noise | **held out**: written after the parser and redactor were built, and never used to tune them |

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
python -m data_gen.generate --out data/heldout --n 12 --seed 777 --ocr-p 0.25 --heldout
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
  - Pattern regexes for SSNs, phones, emails, street addresses, and city/state/ZIP.
  - A common-first-name list, to catch names that have no label.
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

`run_eval` evaluates both datasets, prints a report with every failure, and writes
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

**Development set (48 docs)**

| PII type | gold | recall | precision | leak-free |
|---|---|---|---|---|
| NAME | 108 | 100.00% | 100.00% | 98.15% |
| ADDRESS | 84 | 100.00% | 100.00% | 100.00% |
| PHONE | 28 | 100.00% | 100.00% | 100.00% |
| EMAIL | 16 | 100.00% | 100.00% | 100.00% |
| DOB | 48 | 93.75% | 100.00% | 93.75% |
| SSN | 12 | 100.00% | 100.00% | 100.00% |
| MEMBER_ID | 36 | 97.22% | 100.00% | 97.22% |
| ACCOUNT_ID | 48 | 97.92% | 100.00% | 97.92% |
| **ALL** | 380 | **98.68%** | **100.00%** | **98.16%** |

- Parsing: 225/225 lines matched, field accuracy 100.00% on every field, 48/48
  documents fully correct.
- Duplicate flags: 8/8 found, no false flags.

**Held-out layout (12 docs)**

| PII type | gold | recall | leak-free |
|---|---|---|---|
| NAME | 12 | 0.00% | 0.00% |
| PHONE | 12 | 0.00% | 0.00% |
| ACCOUNT_ID | 12 | 0.00% | 0.00% |
| ADDRESS, EMAIL, DOB, MEMBER_ID | 43 | 100.00% | 100.00% |
| **ALL** | 79 | **54.43%** | **54.43%** |

- Parsing: 60/60 lines found, but overall field accuracy is only **57.14%**.
  Description, insurer paid and patient responsibility are each at 0.00%.
- 0/12 documents fully correct.
- 0/2 duplicate pairs flagged.

## Limitations

- **All results are on synthetic data.** They say nothing reliable about real bills.
  Real documents vary far more than five templates can.
- **The development-set scores are optimistic.** The same author wrote the generator,
  the parser and the redactor, so the 100% parsing score mainly shows the parser fits
  four known layouts. The held-out layout shows what happens on an unseen format:
  - Unknown column names (`INS PMT`, `BALANCE`) are not mapped.
  - Extra columns (`MOD`, `UNITS`) leak into descriptions.
  - New labels (`STATEMENT FOR:`, `Guarantor #:`) and space-separated phone numbers
    (`+1 845 555 0101`) are missed entirely.
- **Rule-based redaction is brittle:**
  - OCR-damaged labels (`Date of Birlh`, `Memher ID`, `Acc0unt Number`) cause misses.
  - Names with no label are caught only if the first name is on the built-in list, so
    less common names leak, as in bill_040 and bill_042.
- **The parser reads text only.** There is no PDF or image input and no OCR, and it
  expects one table per document.
- Ground truth was made by the same program that renders the text, so it carries none
  of the label noise that human annotation would.
- The low precision of business-contact redactions doesn't show in the numbers, because
  those spans are neutral by design. The redactor removes insurer phones and clinic
  addresses too.
