# Исчерпан пул соединений к PostgreSQL

**Симптомы.** PoolTimeout, «connection pool exhausted», «FATAL: sorry, too many clients already»; ошибки 500 на списках и отчётах.

**Почему так бывает.** Соединения не возвращаются в пул (нет закрытия в finally, долгие транзакции) или размер пула всех реплик больше max_connections сервера.

**Как подтвердить.** `select state, count(*) from pg_stat_activity group by state;` — много idle in transaction; метрики пула приложения.

**Что сделать.** Закрывать соединения через контекстный менеджер, ограничить время транзакций (idle_in_transaction_session_timeout), посчитать пул: реплики × размер пула < max_connections.
