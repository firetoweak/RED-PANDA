"""Strict internal metadata reads and durable atomic replacement."""
import json
import os
from pathlib import Path
import time
import uuid

def load(path): return json.loads(Path(path).read_text(encoding='utf-8'))

def atomic(path, value):
    path = Path(path)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.next')
    with temp.open('xb') as file:
        file.write(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
        file.flush();os.fsync(file.fileno())
    # Windows readers can briefly deny DELETE sharing on internal metadata.
    # Retry the still-unpublished rename, never the Command or host writes.
    for delay in (0,0.01,0.02,0.04,0.08):
        if delay: time.sleep(delay)
        try:
            os.replace(temp,path)
        except OSError as error:
            if getattr(error,'winerror',None) not in (5,32) or delay==0.08: raise
        else:
            return

def fields(value, expected):
    if type(value) is not dict or set(value)!=set(expected): raise ValueError('persistent fields differ')

