# Repository Guidelines

## Project Structure & Module Organization

This workspace currently contains CSV datasets under `data/`:

- `closemate_products_2026-09-04.csv`: product metadata, with 11 columns.
- `closemate_reviews_tags_2026-09-04.csv`: reviews and analysis tags, with 24 columns.
- `sample.csv`: a smaller review dataset using the same 24-column schema.

There are no source-code, test, or asset directories and no dependency manifest. Keep dataset additions in `data/`; document any new tooling and its setup when introducing it.

## Build, Test, and Development Commands

No build, development server, or test runner is configured. If Python 3 is available, run this structural check from the repository root:

```sh
python3 - <<'PY'
import csv
from pathlib import Path
for path in sorted(Path('data').glob('*.csv')):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = csv.reader(stream)
        header = next(rows)
        assert len(header) == len(set(header)), path
        count = 0
        for count, row in enumerate(rows, 1):
            assert len(row) == len(header), (path, count)
    print(path, count, 'records validated')
PY
```

This checks duplicate column names and inconsistent record widths without third-party dependencies.

## Data Style & Naming Conventions

Preserve existing column names, order, and lowercase `snake_case` headers. Use UTF-8 CSV and a CSV-aware reader or writer so commas, quotes, and embedded newlines remain intact. Treat identifiers as strings to avoid accidental numeric conversion. Follow `closemate_<dataset>_YYYY-MM-DD.csv` for dated exports. Exclude `.DS_Store` from contributions. No formatter, linter, or code indentation convention is established.

## Testing Guidelines

Run the structural check after every dataset change. Confirm that `sample.csv` retains the review export's schema. Review record-count changes and representative edited fields; structural validation does not establish semantic correctness. No testing framework or coverage target is configured.

## Commit & Pull Request Guidelines

No Git metadata is present in this workspace, so existing commit conventions cannot be verified. Use concise, imperative messages such as `data: add September product export`. Pull requests should describe the data source, affected files, schema or record-count changes, and validation performed. Link a relevant issue when available.
