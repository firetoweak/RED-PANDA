from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[3]
destination = root/'.tools/wal-diagnostic'
assert not destination.exists(), destination
destination.mkdir()
for name in ('vfs','sandbox'):
    shutil.copytree(root/'native'/name, destination/'native'/name, ignore=shutil.ignore_patterns('target','__pycache__'))
pool = destination/'native/vfs/crates/vfs-core/src/pool.rs'
text = pool.read_text(encoding='utf-8')
text = text.replace('type Panic =', '#[path = "wal_diagnostic.rs"]\nmod wal_diagnostic;\n\ntype Panic =', 1)
text = text.replace('db.set_prepared_statement_cache_capacity(512);', 'db.set_prepared_statement_cache_capacity(512);', 1)
text = text.replace('                    let result = operation(db);', '''                    if is_new { unsafe { wal_diagnostic::install(db); } }
                    wal_diagnostic::record(format!("job start conn={:p}", unsafe { db.handle() }));
                    let result = operation(db);
                    wal_diagnostic::record(format!("job done conn={:p} result={:?}", unsafe { db.handle() }, result.as_ref().err()));''', 1)
text = text.replace('        let _checkpoint = self.inner.checkpoint.lock();', '''        wal_diagnostic::record(format!("checkpoint mutex wait conn={:p}", unsafe { conn.handle() }));
        let _checkpoint = self.inner.checkpoint.lock();
        wal_diagnostic::record(format!("checkpoint mutex acquired conn={:p}", unsafe { conn.handle() }));''', 1)
text = text.replace('        if busy != 0 {\n            return Err', '        if busy != 0 {\n            wal_diagnostic::dump();\n            return Err', 1)
pool.write_text(text, encoding='utf-8')
shutil.copyfile(root/'.tools/wal_diagnostic.rs', pool.with_name('wal_diagnostic.rs'))
print(destination)
