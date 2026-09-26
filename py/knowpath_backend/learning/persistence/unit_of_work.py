"""One SQL session and commit boundary for related repository writes."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging

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
    def _managed_session(self):
        with self._sessions() as session:
            try:
                with session.begin():
                    yield session
            except BaseException:
                # 事务已回滚并释放锁；提交结果不确定时，由回调再次检查数据库。
                for callback in reversed(session.info.get('raw_rollback', [])):
                    try:
                        callback()
                    except Exception:
                        logging.getLogger(__name__).warning('RAW_OBJECT_ROLLBACK_CLEANUP_PENDING')
                raise

    @contextmanager
    def transaction(self):
        if self.active:
            yield
            return
        with self._managed_session() as session:
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
            with self._managed_session() as session:
                yield session
