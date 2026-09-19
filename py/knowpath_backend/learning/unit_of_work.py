"""One SQL session and commit boundary for related repository writes."""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.orm import sessionmaker


class SqlAlchemyUnitOfWork:
    def __init__(self, engine):
        self.engine = engine
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._current = ContextVar(f"learning_transaction_{id(self)}", default=None)

    @property
    def active(self):
        return self._current.get() is not None

    @contextmanager
    def transaction(self):
        if self.active:
            yield
            return
        with self._sessions.begin() as session:
            # Serialize SQLite writers before reading an idempotency key. MySQL
            # coordinates concurrent inserts using its unique key and row locks.
            if self.engine.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            token = self._current.set(session)
            try:
                yield
            finally:
                self._current.reset(token)

    @contextmanager
    def session(self):
        current = self._current.get()
        if current is not None:
            yield current
            current.flush()
        else:
            with self._sessions.begin() as session:
                yield session
