"""Read saved snapshots or page checkpoints without losing SKU leading zeroes."""
import json
from pathlib import Path
import pandas as pd
from history import read_snapshot


def restore(path: Path):
    if path.is_dir():
        if (path / 'checkpoint_metadata.json').exists():
            report = json.loads((path / 'checkpoint_metadata.json').read_text(encoding='utf-8'))
            frame = pd.read_csv(path / 'checkpoint.csv', dtype=str, keep_default_na=False)
            rows = frame.drop_duplicates('product_id', keep='last').to_dict('records')
        else:
            saved = json.loads((path / 'checkpoint.json').read_text(encoding='utf-8'))
            rows, report = saved['rows'], saved['report']
    else:
        report = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        rows = read_snapshot(path).to_dict('records')
    for row in rows:
        price = row['regular_price']
        row['regular_price'] = float(price) if price is not None and price != '' else None
        row['product_id'] = str(row['product_id'])
    return rows, report
