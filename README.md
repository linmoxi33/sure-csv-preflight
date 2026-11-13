# sure-csv-preflight

What Sure will actually do to your bank CSV — before you upload it.

Sure is a personal-finance app that imports bank CSV exports by mapping columns to
transactions. The mapping is not a lossless read: one global number format decides
how *every* cell is interpreted, and the parser is deliberately forgiving. A value
like `1,234.56` is a perfectly valid amount under the US format and a completely
different number under the EU format — and nothing warns you until the transactions
land in your ledger with the wrong magnitude.

Upstream: https://github.com/we-promise/sure

## What the upstream does, and what it does not do

Sure's CSV import is a server-side pipeline. The upload + configuration step is
documented as an authenticated API call (`POST /api/v1/imports`, see
[`docs/api/imports.md`](https://github.com/we-promise/sure/blob/main/docs/api/imports.md)),
so the only way to learn how a file will be parsed is to upload it into a running
instance.

The parsing rules live in
[`app/models/import.rb#L714-L756`](https://github.com/we-promise/sure/blob/main/app/models/import.rb#L714-L756):

```ruby
sanitized = sanitized.gsub(/[^\d#{delimiter}#{separator}\-]/, "")
sanitized = sanitized.gsub(delimiter, "") if delimiter.present?
sanitized = sanitized.gsub(separator, ".") if separator.present?
```

Only four formats are accepted, one per import
([`import.rb#L57-L60`](https://github.com/we-promise/sure/blob/main/app/models/import.rb#L57-L60)):

| `number_format` | delimiter (thousands) | separator (decimal) |
| --- | --- | --- |
| `1,234.56` | `,` | `.` |
| `1.234,56` | `.` | `,` |
| `1 234,56` | space (incl. NBSP / narrow NBSP) | `,` |
| `1,234` | `,` | *(none — zero-decimal currencies)* |

Because the delimiter is deleted and the separator becomes `.`, the same cell has
different values under different formats. Measured with this tool (see
`examples/out/report.json`):

| cell | chosen `1.234,56` | `1,234.56` | `1,234` |
| --- | --- | --- | --- |
| `1,234` | `1.234` | `1234` | `1234` |
| `1.234` | `1234` | `1.234` | `1234` |
| `1,234.56` | `1.23456` | `1234.56` | `123456` |
| `-4.50` | `-450` | `-4.50` | `-450` |

The space-delimited branch is the only one with an explicit guard against this
misreading — its comment says interior characters are kept "so a misconfigured
US-style value like `1,234.56` keeps its period and is rejected … rather than being
silently reinterpreted as 1.23456"
([`import.rb#L723-L731`](https://github.com/we-promise/sure/blob/main/app/models/import.rb#L723-L731)).
The comma/dot branches do exactly that reinterpretation. On top of that,
[`Import::Row#signed_amount`](https://github.com/we-promise/sure/blob/main/app/models/import/row.rb#L23-L30)
flips the sign because positive amounts mean *outflows* in Sure, so a misread also
moves money between inflow and outflow.

## What this tool does

Zero-dependency Python CLI that reimplements those rules locally and
answers, per row:

- how the cell is tokenized under the configured `number_format`;
- whether the value is **ambiguous** (syntactically plausible under two formats and
  they disagree) or merely **format-sensitive** (only one plausible reading, but a
  different configured format would still change the number);
- what the final signed amount is after Sure's signage convention;
- which rows Sure will reject (bad date, blank required cell, unparseable amount,
  missing mapped header).

It does **not** talk to Sure, does not import anything, does not check duplicates
against your existing ledger, and does not model trade/PDF/QIF imports.

## Install

Python 3.9+ standard library only. No install step:

```bash
git clone git@github.com:linmoxi33/sure-csv-preflight.git
cd sure-csv-preflight
python3 sure_csv_preflight.py --help
```

## Usage

```bash
python3 sure_csv_preflight.py --csv examples/in/us-bank.csv \
    --config examples/in/config-eu.json --today 2026-11-13
```

```
rows: 5 total, 5 checked, 4 importable, 1 rejected
amounts: 0 unparseable, 2 ambiguous, 2 format-sensitive | dates: 1 invalid | signed total: -2019.234

WARNING AMOUNT_FORMAT_AMBIGUOUS  row 2 col=amount value='1,234'
        chosen 1.234,56 -> 1.234 ; 1,234.56 -> 1234 ; 1,234 -> 1234
ERROR   DATE_INVALID             row 5 col=date value='2026-13-40'
```

Config keys mirror Sure's import parameters (defaults in parentheses):
`date_col_label` (`date`), `amount_col_label` (`amount`), `name_col_label` (`name`),
`currency_col_label`, `entity_type_col_label`, `date_format` (`%Y-%m-%d`),
`number_format` (`1,234.56`), `signage_convention` (`inflows_positive`),
`amount_type_strategy` (`signed_amount`), `amount_type_inflow_value`,
`amount_type_identifier_value`, `col_sep` (`,`), `rows_to_skip` (0),
`default_currency` (`USD`).

`--json` prints the full report, including per-cell witnesses:

```json
{
  "code": "AMOUNT_FORMAT_AMBIGUOUS",
  "row": 2,
  "column": "amount",
  "value": "1,234",
  "witness": {
    "chosen": "1.234,56",
    "chosen_value": "1.234",
    "alternatives": [{"number_format": "1,234.56", "value": "1234"}],
    "ratio": "0.001"
  }
}
```

Exit codes:

| code | meaning |
| --- | --- |
| `0` | no findings at or above `--fail-on` (default `warning`) |
| `1` | at least one finding at or above `--fail-on` |
| `2` | usage error, unreadable/invalid CSV, or invalid config JSON |

`--fail-on error|warning|never` tunes the gate; `--today YYYY-MM-DD` pins the date
range check so output is reproducible.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

25 stdlib tests: 10 for the number-format port, 3 for the signage port, 4 for the
date port, 8 CLI behaviour tests (exit codes 0/1/2, JSON equality with
`examples/out/report.json`, byte-identical repeat runs, missing-header detection).

## 本轮创新

Sure's importer has never been modelled outside a running Rails instance: the only
way to see a cell's fate is to upload it. This tool turns the parser into a pure
function and adds a **per-cell dual-parse witness** — for every format-sensitive
cell it prints the same raw string's value under each of Sure's four formats, so a
user can see the exact 1000× delta before committing. The four-format comparison
also exposes an asymmetry in the upstream code itself: the space-delimited branch
was written specifically to reject cross-format values, while the other three
silently reinterpret them. See "与同类项目的差异" for why an "import preflight" that
merely checks RFC 4180 syntax does not cover this.

## 与同类项目的差异

`gh search repos "csv import preflight"` (2026-11-13) returns six repositories, all
0 stars, all targeting CRM/Letterboxd imports or generic CSV cleanup
(`lenon9001-yuan/csv-import-preflight`, `Congre123/csv-import-preflight`,
`TravellerVS/csv-import-preflight`, `sgagestudio/op-088-letterboxd-csv-fixer`,
`Throokie/importguard`, `wildan-m/frappe-instrument-desk`). None models a specific
finance app's amount/locale semantics, and none reports per-cell alternative
readings. `gh search repos "bank csv validator"` returns no results. This tool is
scoped to one upstream's documented parser rather than being a generic CSV linter.

## Limitations

- It is a port, not a call into Sure. If upstream changes `sanitize_number`, the
  port can drift; the line references above are the contract (`v1.0.0`, checked
  2026-11-13).
- Ruby's `Date.strptime` is stricter than Python's `strptime` about zero padding in
  some patterns; a date accepted here can still be rejected by Sure. Dates are
  reported as errors only when Python rejects them too.
- Duplicate detection against existing transactions (the subject of upstream issues
  such as #3779 and #4957) is out of scope: it needs the ledger, not the file.
- Encoding is assumed UTF-8; Sure transcodes Windows-1250/1252 uploads, this tool
  does not. Non-UTF-8 input exits 2 instead of guessing.
- Only transaction-style imports are modelled; `TradeImport` sign rules for
  `qty`/`price` are not implemented.

## License

MIT. Not affiliated with Sure. The upstream project is AGPL-3.0; no upstream code
is copied — the rules are re-expressed in Python with line-level citations.
