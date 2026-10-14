"""
tools/check_db.py — Проверяет строку подключения к PostgreSQL.

Ничего не устанавливает: работает на голых сокетах, asyncpg не нужен.
Показывает, на каком именно шаге всё ломается — DNS, TCP или SSL.

Запуск:
    python tools/check_db.py "postgresql://user:pass@host:port/db"
"""

import socket
import ssl
import sys
from urllib.parse import urlsplit


def fail(msg, hint=""):
    print(f"\n❌ {msg}")
    if hint:
        print(f"   → {hint}")
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        fail("Не передана строка подключения",
             'python tools/check_db.py "postgresql://user:pass@host:5432/bot"')

    dsn = sys.argv[1].strip().strip('"').strip("'")
    parts = urlsplit(dsn)
    host = parts.hostname
    port = parts.port or 5432
    dbname = (parts.path or "/").lstrip("/")

    if not host:
        fail("В строке нет хоста", "Формат: postgresql://user:pass@host:port/dbname")

    print(f"Хост:  {host}")
    print(f"Порт:  {port}")
    print(f"База:  {dbname or '(не указана)'}")
    print(f"Логин: {parts.username or '(нет)'}  Пароль: "
          f"{'задан' if parts.password else 'НЕТ'}")
    print()

    # ── 1. DNS ──
    print("1. Резолвим имя хоста...")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        print(f"   ✅ {host} → {', '.join(addrs)}")
    except socket.gaierror as exc:
        if host.endswith(".railway.internal"):
            fail(f"Имя {host} не резолвится: {exc}",
                 "Это ВНУТРЕННИЙ адрес Railway. Он работает только изнутри "
                 "Railway и только при включённой приватной сети.\n"
                 "     С ноутбука подключайся по публичному адресу: в Railway "
                 "открой сервис Postgres → Variables → DATABASE_PUBLIC_URL "
                 "(хост вида *.rlwy.net).")
        fail(f"Имя {host} не резолвится: {exc}",
             "Проверь, что хост скопирован целиком и без пробелов.")

    # ── 2. TCP ──
    print("2. Стучимся на порт...")
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=10)
        print(f"   ✅ TCP-соединение с {host}:{port} установлено")
    except socket.timeout:
        fail(f"Порт {port} не отвечает (таймаут)",
             "Обычно значит, что база спит, удалена или закрыта файрволом.")
    except OSError as exc:
        fail(f"Не удалось подключиться к {host}:{port} — {exc}",
             "Проверь порт: у публичного адреса Railway он НЕ 5432, "
             "а случайный пятизначный.")

    # ── 3. Готов ли сервер говорить по SSL ──
    print("3. Спрашиваем сервер про SSL...")
    try:
        # SSLRequest: длина 8, код 80877103
        sock.sendall(b"\x00\x00\x00\x08\x04\xd2\x16\x2f")
        answer = sock.recv(1)
        if answer == b"S":
            print("   ✅ Сервер поддерживает SSL")
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.wrap_socket(sock, server_hostname=host)
            print("   ✅ SSL-рукопожатие прошло")
        elif answer == b"N":
            print("   ⚠️  Сервер отвечает, но SSL не поддерживает")
            print("      Для внутреннего адреса Railway это нормально.")
            print("      Если в строке стоит sslmode=require — убери его.")
        else:
            print(f"   ⚠️  Неожиданный ответ: {answer!r}")
    except ssl.SSLError as exc:
        fail(f"SSL-рукопожатие не удалось: {exc}",
             "Попробуй добавить в конец строки ?sslmode=require")
    except OSError as exc:
        fail(f"Соединение оборвалось на этапе SSL: {exc}")
    finally:
        try:
            sock.close()
        except Exception:
            pass

    print("\n🎉 Адрес живой: имя резолвится, порт отвечает, SSL согласован.")
    print("   Если бот всё равно не подключается — дело в логине, пароле")
    print("   или имени базы. Их эта проверка не проверяет (нужен psql).")


if __name__ == "__main__":
    main()
