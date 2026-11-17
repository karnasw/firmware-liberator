// A D1 database on this computer, for the tests and for local.mjs: Node's own
// SQLite (the engine D1 runs) behind the part of D1's binding the API uses:
// prepare().bind().first()/all()/run(), batch() and exec(). As strict as D1
// where it matters: binding undefined throws, as it does there, and a batch
// is one transaction. Never deployed: index.js does not import it.
import { readdirSync, readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

const MIGRATIONS = new URL("./migrations/", import.meta.url);

// The migration files, in the order they are applied.
export const migrations = () => readdirSync(MIGRATIONS).filter((name) => name.endsWith(".sql")).sort();

export const migration = (name) => readFileSync(new URL(name, MIGRATIONS), "utf8");

class Statement {
  constructor(db, sql, values = []) {
    this.db = db;
    this.sql = sql;
    this.values = values;
  }

  bind(...values) {
    for (const value of values) {
      if (value === undefined) {
        throw new TypeError(`D1_TYPE_ERROR: undefined bound in ${this.sql.trim().slice(0, 60)}`);
      }
    }
    // D1 stores a boolean as 1 or 0.
    return new Statement(this.db, this.sql, values.map((value) => (typeof value === "boolean" ? Number(value) : value)));
  }

  rows() {
    return this.db.prepare(this.sql).all(...this.values).map((row) => ({ ...row }));
  }

  async first(column) {
    const [row] = this.rows();
    if (row === undefined) {
      return null;
    }
    return column === undefined ? row : row[column];
  }

  async all() {
    return { success: true, results: this.rows(), meta: {} };
  }

  async run() {
    const statement = this.db.prepare(this.sql);
    if (/\bRETURNING\b/i.test(this.sql)) {
      return { success: true, results: statement.all(...this.values).map((row) => ({ ...row })), meta: {} };
    }
    const { changes, lastInsertRowid } = statement.run(...this.values);
    return { success: true, results: [], meta: { changes: Number(changes), last_row_id: Number(lastInsertRowid) } };
  }
}

// `file`: where to keep it; by default in memory, gone with the process.
export function d1({ migrated = true, file = ":memory:" } = {}) {
  const db = new DatabaseSync(file);
  const binding = {
    db,
    prepare: (sql) => new Statement(db, sql),
    async batch(statements) {
      db.exec("BEGIN");
      try {
        const results = [];
        for (const statement of statements) {
          results.push(await statement.run());
        }
        db.exec("COMMIT");
        return results;
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
    async exec(sql) {
      db.exec(sql);
      return { count: 1, duration: 0 };
    },
  };
  if (migrated) {
    migrate(binding);
  }
  return binding;
}

// Every migration, recorded in d1_migrations as tools/repo/cloudflare_settings.py
// and wrangler record them.
export function migrate(binding, names = migrations()) {
  binding.db.exec(`CREATE TABLE IF NOT EXISTS d1_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)`);
  for (const name of names) {
    binding.db.exec(migration(name));
    binding.db.prepare("INSERT OR IGNORE INTO d1_migrations (name) VALUES (?)").run(name);
  }
}
