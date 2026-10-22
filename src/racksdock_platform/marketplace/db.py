from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _fk(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def _add_missing_columns(self) -> None:
        """Lightweight forward migration for columns added after a database was created (sqlite ALTER TABLE ADD COLUMN)."""
        from sqlalchemy import inspect, text

        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if not insp.has_table(table.name):
                    continue
                have = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in have:
                        continue
                    default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                    ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(self.engine.dialect)}"
                    if default is not None:
                        ddl += f" DEFAULT {default!r}" if isinstance(default, str) else f" DEFAULT {default}"
                    conn.execute(text(ddl))
                    if table.name == "nodes" and col.name in ("profile", "deployment") and self.engine.dialect.name == "sqlite":
                        # backfill from the signed registration the node row already holds
                        conn.execute(text(f"UPDATE nodes SET {col.name} = COALESCE(json_extract(registration, '$.{col.name}'), {col.name})"))
                    if table.name == "instances" and col.name == "deployment":
                        # existing rentals ran in their node's (single) tier; a dual node's earlier rentals were VMs (the only tier it had)
                        conn.execute(text("UPDATE instances SET deployment = COALESCE((SELECT CASE WHEN n.deployment = 'dual' THEN 'vm' ELSE n.deployment END "
                                          "FROM nodes n WHERE n.node_id = instances.node_id), 'vm')"))

    @contextmanager
    def session(self):
        s: Session = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
