from datetime import datetime
from pathlib import Path
import shutil
import sqlite3


ROOT = Path(__file__).resolve().parent.parent


def main():
    database = ROOT / 'data' / 'game.db'

    if not database.is_file():
        raise FileNotFoundError(
            f'Рабочая база не найдена: {database}'
        )

    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    destination = ROOT / 'backups' / stamp
    destination.mkdir(parents=True, exist_ok=False)

    try:
        # Сохраняем код, контент и настройки запуска.
        for name in (
            'main.py',
            'requirements.txt',
            '.env.example',
            '.gitignore',
            'README.txt',
        ):
            source = ROOT / name

            if source.is_file():
                shutil.copy2(source, destination / name)

        for name in ('content', 'static', 'scripts', '.vscode'):
            source = ROOT / name

            if source.is_dir():
                shutil.copytree(source, destination / name)

        # Создаём согласованную копию базы через SQLite.
        saved_database = destination / 'data' / 'game.db'
        saved_database.parent.mkdir(parents=True, exist_ok=True)

        source = sqlite3.connect(
            database.resolve().as_uri() + '?mode=ro',
            uri=True,
        )

        try:
            target = sqlite3.connect(saved_database)

            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

        print(f'Бэкап создан: {destination}')
        print('Файл .env с токеном намеренно не скопирован.')

    except Exception:
        print(f'Бэкап не завершён: {destination}')
        print('Не используй эту папку для восстановления.')
        raise


if __name__ == '__main__':
    main()