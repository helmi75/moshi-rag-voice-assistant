import json
from datetime import datetime
import os

os.makedirs('./data', exist_ok=True)
DBPATH = './data/reservations.json'

def _load():
    try:
        with open(DBPATH,'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def _save(rows):
    with open(DBPATH,'w') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

def create_reservation(payload: dict):
    rows = _load()
    row = {
        'id': len(rows)+1,
        'created_at': datetime.utcnow().isoformat(),
        'payload': payload
    }
    rows.append(row)
    _save(rows)
    return row

def list_reservations():
    return _load()
