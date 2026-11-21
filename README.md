# BillWise

BillWise explains US medical bills and insurance Explanations of Benefits (EOBs) in
plain English.

- **Phase 1 (groundwork):**
  1. **Parsing**: turn a text bill or EOB into structured line items (service date,
     CPT/HCPCS code, description, billed, allowed, insurer paid, patient responsibility).
  2. **Redaction**: remove personal identifiers *before* any model call.
  3. **Evaluation**: measure both against labeled ground truth and report the numbers
     the harness actually computes.
- **Phase 2 (explanations):** retrieval over a plain-language knowledge base plus an
  explainer model. It answers "what is this charge and why do I owe it?" using only the
  parsed numbers, and checks the answer against them. See
  [Phase 2](#phase-2-plain-english-explanations).

Nothing calls a cloud service by default. The Claude explainer runs only when you
opt in with your own Anthropic credentials.

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
| `data/dev4/` | 12 | patient-portal visit summary: two-line items with inline `Billed $… · Plan paid $…` amounts, honorific names in a sentence, dotted DOBs, slash phones | round-3 held-out set. Its failures drove the round-4 rework, so it now counts as **development data** |
| `data/dev5/` | 12 | scanned urgent-care statement whose OCR collapsed the columns to single spaces; labels without colons (`Pt Name`, `Mbr No`), comma-less addresses, compact DOBs | round-4 held-out set. Its failures drove the round-5 fixes, so it now counts as **development data** |
| `data/heldout/` | 12 | billing-system export: `KEY=value` fields separated by `;`, HL7-style caret names and addresses (`DOE^JANE^Q`), `YYYYMMDD` dates, medical record number | **held out**: written before the round-5 fixes and never used to tune them |

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
python -m data_gen.generate --out data/dev4 --n 12 --seed 9001 --ocr-p 0.2 --set dev4
```

```powershell
python -m data_gen.generate --out data/dev5 --n 12 --seed 31337 --ocr-p 0.2 --set dev5
```

```powershell
python -m data_gen.generate --out data/heldout --n 12 --seed 5150 --ocr-p 0.2 --set heldout
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

`run_eval` evaluates all six datasets, prints a report with every failure, and writes
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
- `POST /explain` takes `{"text": "...", "question": "..."}`; the question is optional,
  up to 1,000 characters. It uses the mock explainer unless `BILLWISE_LLM=claude` is set.
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

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests and both
evaluations on Python 3.11 and 3.12, uploads the results, then builds the Docker image
and smoke-tests `/redact` and `/explain`. CI never calls a model API.

## Phase 2: plain-English explanations

```
bill text ──► parse ──► line items ──► totals, flags (computed in code)
    │                                   │
    └──► redact (document + question + descriptions, one pass)
                                        ▼
            ExplainRequest = redacted descriptions, numbers, flags,
                             retrieved snippets, redacted question
                                        ▼
                       explainer (mock, or Claude if opted in)
                                        ▼
       grounding check (numbers, citations, line numbers) ──► restore placeholders locally
```

**Privacy by data minimization.** The model never receives the document text, not even
redacted. It gets only an `ExplainRequest` (see
[billwise/explain.py](billwise/explain.py)):
- the parsed line items, with descriptions passed through the redactor;
- totals computed in code;
- flags (possible duplicate, insurance paid $0, pending, "not a bill");
- the knowledge-base snippets it may cite;
- the question, redacted in the context of the whole document, so a name found behind
  `Patient Name:` is also caught when the user types it.

Placeholders in the answer are restored locally. `/explain` returns the exact request,
so you can see what was sent.

**Knowledge base.** [billwise/knowledge_base.json](billwise/knowledge_base.json) holds
21 billing and EOB terms, plus explanations for the 22 codes the generator uses. It's
all written in our own words, not AMA CPT descriptor text (which is copyrighted), and
is general information, not medical or legal advice.

**Retrieval.** [billwise/retrieve.py](billwise/retrieve.py) looks up codes exactly,
then falls back to pure-Python BM25 over each entry's title, keywords and text. That
covers free-text questions and lines whose code is missing or unknown.

**Explainers.**
- `MockExplainer` (default) is deterministic and template-based, so tests, CI and
  offline use need no network.
- `ClaudeExplainer` uses `claude-opus-5-5` through the official Anthropic SDK, with
  structured JSON output (a fixed schema) and server-side refusal fallback enabled
  (`fallbacks: "default"`), at effort `medium`. It's used only when `BILLWISE_LLM=claude`
  is set, and needs Anthropic credentials and `pip install -r requirements-llm.txt`. It
  has been tested only against a fake client; **it has not been run against the live
  API in this repository.**
- The default reading level is "plain English at about a 6th–8th grade reading level",
  and can be changed per call.

**Grounding check.** Every dollar figure in an answer must equal a parsed value or a
computed total. Every cited snippet must be one that was provided, and every line
number must exist. Failures are returned as `warnings` and `unsupported_amounts`.

```powershell
.venv\Scripts\python -m eval.run_phase2
```

The Phase 2 eval ([eval/run_phase2.py](eval/run_phase2.py)) writes
[eval/phase2_results.json](eval/phase2_results.json), with console output in
[eval/phase2_last_run.txt](eval/phase2_last_run.txt). `--live N` also runs Claude on N
bills per dataset. That costs money and is never run by default or in CI.

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

**Held-out set (round 5: `KEY=value` billing export, 12 bills, never used for tuning)**

| Metric | Result |
|---|---|
| PII recall (same-type overlap) | 33.33% (24/72) |
| PII leak-free (every character covered) | 16.67% |
| PII precision | 100.00% |
| Lines found | 56/56 |
| Line-item field accuracy | 71.43% |
| Bills parsed fully correctly | 0/12 |
| Duplicate pairs flagged | 4/4 |

This is the worst privacy result of any round. Only the phone and the street part of
the address were caught. The cause is one structural assumption the round-4 rework kept:
**every label rule expects `:`, `#`, a space or dot leaders between a label and its value.**
This format uses `=` instead, so:
- **NAME, DOB, MEMBER_ID, ACCOUNT_ID: 0/12 each.** `NAME=DOE^KEISHA^A`, `DOB=19531120`,
  `MRN=MR26136582` and `SUBSCRIBER_ID=…` are all missed. On top of the separator,
  `SUBSCRIBER_ID` has an underscore inside the label, names use `^` instead of spaces,
  and `MRN` (medical record number) is not a known label.
- **ADDRESS: 12/12 overlapped but 0/12 leak-free.** The street (`7598 RIVERBEND AVE`) is
  caught, but `^^OAK HARBOR^WA^98277` is not, because the city/state/ZIP pattern expects
  spaces or commas.
- **Parsing:** every line and every amount is right; the arithmetic fallback mapped
  `CHG/ALLOWED/PAID/PTRESP` correctly. But service date is 0/56, because `YYYYMMDD` is not
  a recognised date format. Description is 0/56, because the fallback keeps the
  `;DESC=…;CHG=` keys as part of the text.

**Development sets** (all tuned against; the fallback-only row is a diagnostic that runs
the new line-oriented fallback on its own)

| | `synthetic` (48) | `dev2` (12) | `dev3` (12) | `dev4` (12) | `dev5` (12) |
|---|---|---|---|---|---|
| PII recall / leak-free | 100.00% / 100.00% | 100.00% / 100.00% | 100.00% / 100.00% | 100.00% / 100.00% | 100.00% / 100.00% |
| PII precision | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| Line-item field accuracy | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| Strategy used | fixed_width, pipe_table, key_value, csv | fixed_width | tsv | anchored | anchored |
| Fallback alone: field accuracy | 47.24% | 85.71% | 79.29% | 100.00% | 100.00% |

The fallback alone scores 0% on the key/value and CSV layouts (dates sit on a separate
line from the code, or codes are comma-joined). It runs only when no other strategy
matches, so those layouts are still handled by their own strategies.

### History

| Round | Held-out set at the time | PII recall | PII leak-free | Field accuracy |
|---|---|---|---|---|
| 1 | group statement (now `dev2`) | 54.43% | 54.43% | 57.14% |
| 2 | multi-claim statement (now `dev3`) | 90.24% | 90.24% | 0.00% |
| 3 | portal visit summary (now `dev4`) | 44.44% | 44.44% | 0.00% |
| 4 | collapsed-column scan (now `dev5`) | 84.42% | 89.61% | 98.18% |
| 5 | `KEY=value` billing export (`heldout`) | 33.33% | 16.67% | 71.43% |

- **Round 2**, from `synthetic` and `dev2`: OCR-tolerant and case-insensitive labels,
  more ID labels, spaced phones, names above a street address, modifier/units columns,
  `pmt`/`balance` header words, OCR-tolerant duplicate detection.
- **Round 3**, from `dev3`: one reader for `|`/`,`/tab tables with several tables per
  document, columns typed from their contents, `dt`/`covered`/`pymt`/`member resp`
  header words, dashes as missing values, `MM-DD-YYYY` DOBs.
- **Round 4**, a structural rework from `dev4`:
  - **Parser:** a line-oriented fallback anchored on billing codes. Amounts are mapped
    by adjacent label words, then a header line, then arithmetic (allowed = paid +
    patient share), then column order.
  - **Redactor:** format-agnostic rules. Any date shape after a birth label; phones by
    3-3-4 digit grouping with any separator; a generic digit-heavy-token-after-ID-word
    rule; names after courtesy titles or colon-less `… Name` labels.

- **Round 5**, from `dev5`: overlapping address candidates merge into their union; a
  minus sign separated by a space is a missing-value dash; `Mbr`/`Memb`/`Subscr`/`Ins`
  count as member-type context for generic IDs.

Rounds 1–3 patched specific formats, and each new held-out layout broke something
basic. Round 4 was the first where an unseen layout was mostly handled:
- The parser found every line using the arithmetic fallback, even though none of the
  layout's header words (`Chg`, `Allow`, `Ins Pd`, `Pt Owes`) are in its vocabulary.
- Every PII value was at least partly redacted.

One caveat: I proposed the round-4 rules ("any date shape near a birth word", "phones
by digit grouping") before writing that held-out layout, so the separation is weaker
than it would be with an outside author.

Round 5 shows the limits of that progress. The parser's arithmetic fallback still
generalised (every amount right on an unseen export format). But the redactor's
"format-agnostic" rules still assumed human-style label punctuation, and a
machine-export format leaked most identifiers. Rule-based redaction keeps failing on
whatever convention it hasn't seen. A redaction step that over-redacts by default (for
example, masking any token after a key that isn't on a known-safe list) would fail
safer than one that only redacts what it recognises.

## Phase 2 results (from `eval/phase2_last_run.txt`; synthetic data, mock explainer)

**Privacy of the model request.** Each bill is also asked a question that contains the
patient's name exactly as printed ("Hi, this is SMITH, PATRICE…"). The table counts
ground-truth PII values found anywhere in the request JSON. "Baseline" is what would
leak if the Phase 1 redacted text were sent instead.

| Dataset | PII values | Leaked into request | Request leak-free | Baseline leak-free |
|---|---|---|---|---|
| `synthetic` (48) | 380 | 0 | 100.00% | 100.00% |
| `dev2` (12) | 79 | 0 | 100.00% | 100.00% |
| `dev3` (12) | 123 | 0 | 100.00% | 100.00% |
| `dev4` (12) | 72 | 0 | 100.00% | 100.00% |
| `dev5` (12) | 77 | 0 | 100.00% | 100.00% |
| `heldout` (12) | 72 | **12** | **83.33%** | 33.33% |

- **What leaks on the held-out set:** all 12 leaks are the patient's name in caret form
  (`DOE^KEISHA^A`). The test question repeats it, and the Phase 1 redactor doesn't
  recognise it in the document either.
- **What minimization saves:** sending the redacted document would have leaked 48 values
  (DOBs, IDs, partial addresses). Sending only parsed facts cut that to 12.
- **An earlier bug:** the first Phase 2 run found 12–36 leaked values per dataset,
  because the question was redacted on its own, without the document's context. The fix
  (one redaction pass over document + question) is committed separately, and the numbers
  above are after it.

**Retrieval and grounding**

| Metric | `synthetic` | `dev2` | `dev3` | `dev4` | `dev5` | `heldout` |
|---|---|---|---|---|---|---|
| Gold codes whose snippet is in the request | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| Description-only code search, hit@1 | 95.48% | 90.00% | 91.67% | 96.88% | 96.36% | 98.21% |
| Description-only code search, hit@3 | 100.00% | 98.33% | 100.00% | 100.00% | 100.00% | 100.00% |
| Dollar figures in answers / unsupported | 997 / 0 | 228 / 0 | 285 / 0 | 296 / 0 | 268 / 0 | 275 / 0 |
| Locally computed billed total correct | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| Locally computed patient-share total correct | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |

- **Question retrieval** (36 hand-written questions): hit@1 **86.11%**, hit@3 **94.44%**.
  Five questions miss the top spot:
  - "charge was $2,450 but I only owe $450" ranks an IV-push code first, not adjustment.
  - "Is this an actual bill…" ranks denial first, not the EOB entry.
  - "code starting with J" ranks CPT first, not HCPCS.
  - "How much do I actually owe?" ranks billed amount first.
  - "the Toradol shot" ranks the flu vaccine first, not ketorolac.
- **Description-only code search** mostly confuses `99285` with `99283`. Both are
  emergency visits, and the description's "high severity" doesn't match the entry's
  "highest". It's used only when a line's code is missing or unknown.
- **Grounding is 0 unsupported for the mock by construction:** it only writes numbers
  from the request. These figures check the pipeline, not a language model's
  faithfulness. That needs a live run (`--live N`), which hasn't been done.
- **Totals are 100% on the held-out set too,** because round 4's parser reads every
  amount on that format. Totals stop being right as soon as parsing misses an amount.

## Limitations

- **All results are on synthetic data.** They say nothing reliable about real bills.
  Real documents vary far more than nine templates can.
- **The development scores mean little on their own.** The same author wrote the
  generators, parser and redactor, and tuned against the development sets. Even the
  held-out layouts were written by the same person who wrote the code, which is a
  weaker separation than having someone else write them.
- **The parser still leans on structure it can recognise.** The line-oriented fallback
  helps a lot (0/64 → 55/55 lines across rounds 3 and 4). But when labels and headers
  are unfamiliar, it relies on arithmetic and on the usual left-to-right column order,
  which a bill can violate. It also runs only when no other strategy finds anything, so
  a table strategy that half-works is never second-guessed.
- **Rule-based redaction is still brittle in places:**
  - Overlap handling can drop part of an address.
  - Unfamiliar labels give the wrong type, though the value is still redacted.
  - Names with no label, title or nearby address are caught only if the first name is
    on the built-in list.
- **The parser reads text only.** There is no PDF or image input and no OCR.
- Ground truth was made by the same program that renders the text, so it carries none
  of the label noise that human annotation would.
- Business-contact redactions (insurer phones, clinic addresses) are neutral by design,
  so their precision cost doesn't show in the numbers.
- **Phase 2 has not been evaluated with a real model.** Every explanation number above
  comes from the deterministic mock. The Claude client is tested only against a fake
  client, so how well real answers stay grounded, and how readable they are, is
  unmeasured.
- **The knowledge base is small and hand-written:** 21 terms and only the 22 codes the
  generator uses. A real bill with any other code gets no code explanation, only the
  description-based search, which can't help when nothing matches.
- **Data minimization limits leaks but doesn't remove them.** Line descriptions and the
  question still pass through the rule-based redactor, so a format it doesn't recognise
  can still leak, as the caret names on the held-out set show.
- **I wrote the question set myself,** right after the knowledge base, so its retrieval
  numbers are optimistic in the same way the parsing numbers were.
