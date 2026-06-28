"""
database.py — SQLite обёртка для AvianVisitors.

Схема таблицы detections совместима с оригинальным BirdNET-Pi,
чтобы SQL-запросы из PHP можно было перенести в Python 1:1.

Поля:
  - Sci_Name    : scientific name (латинское), например "Columba livia"
  - Com_Name    : common name (английское), например "Rock Pigeon"
  - Confidence  : float 0..1, confidence от BirdNET
  - Date        : строка "YYYY-MM-DD" (локальная дата)
  - Time        : строка "HH:MM:SS" (локальное время)
  - File_Name   : имя файла аудио (в десктоп-версии — пустая строка,
                  записи не сохраняются на диск)
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional


class Database:
    def __init__(self, db_path: str = None):
        """
        Инициализация подключения к SQLite.

        Args:
            db_path: путь к файлу БД. По умолчанию — data/birds.db
                     относительно директории проекта.
        """
        if db_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(base_dir, "data")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, "birds.db")

        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------ #
    #  Connection management
    # ------------------------------------------------------------------ #

    def _get_conn(self) -> sqlite3.Connection:
        """Возвращает соединение (создаёт при первом вызове)."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=2000")
        return self._conn

    def init(self):
        """Создаёт таблицу detections, если её нет."""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                Sci_Name    TEXT    NOT NULL,
                Com_Name    TEXT    NOT NULL,
                Confidence  REAL    NOT NULL,
                Date        TEXT    NOT NULL,
                Time        TEXT    NOT NULL,
                File_Name   TEXT    DEFAULT ''
            )
        """)
        # Индексы для частых запросов
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_det_date
            ON detections (Date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_det_sci
            ON detections (Sci_Name)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_det_sci_date
            ON detections (Sci_Name, Date, Time)
        """)
        conn.commit()

    def close(self):
        """Закрывает соединение."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    #  INSERT
    # ------------------------------------------------------------------ #

    def insert_detection(
        self,
        sci_name: str,
        com_name: str,
        confidence: float,
        dt: datetime = None,
    ):
        """
        Записывает одну детекцию в БД.

        Args:
            sci_name:   научное название вида
            com_name:   английское название вида
            confidence: уверенность модели (0..1)
            dt:         datetime объект. По умолчанию — текущее локальное время.
        """
        if dt is None:
            dt = datetime.now()
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO detections (Sci_Name, Com_Name, Confidence, Date, Time, File_Name) "
            "VALUES (?, ?, ?, ?, ?, '')",
            (sci_name, com_name, confidence, date_str, time_str),
        )
        conn.commit()

    # ------------------------------------------------------------------ #
    #  API: action=stats
    # ------------------------------------------------------------------ #

    def stats(self) -> dict:
        """
        Возвращает общую статистику.
        Формат 1:1 с PHP birdnet-api.php?action=stats.
        """
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"]
        species = conn.execute(
            "SELECT COUNT(DISTINCT Sci_Name) AS n FROM detections"
        ).fetchone()["n"]
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM detections WHERE Date = DATE('now','localtime')"
        ).fetchone()["n"]
        today_spec = conn.execute(
            "SELECT COUNT(DISTINCT Sci_Name) AS n FROM detections "
            "WHERE Date = DATE('now','localtime')"
        ).fetchone()["n"]
        last_hour = conn.execute(
            "SELECT COUNT(*) AS n FROM detections "
            "WHERE Date = DATE('now','localtime') "
            "AND Time >= TIME('now','localtime','-1 hour')"
        ).fetchone()["n"]
        week = conn.execute(
            "SELECT COUNT(*) AS n FROM detections "
            "WHERE Date >= DATE('now','localtime','-7 day')"
        ).fetchone()["n"]
        week_spec = conn.execute(
            "SELECT COUNT(DISTINCT Sci_Name) AS n FROM detections "
            "WHERE Date >= DATE('now','localtime','-7 day')"
        ).fetchone()["n"]
        first = conn.execute("SELECT MIN(Date) AS d FROM detections").fetchone()

        return {
            "totals": {"detections": total, "species": species},
            "today": {"detections": today, "species": today_spec},
            "last_hour": {"detections": last_hour},
            "week": {"detections": week, "species": week_spec},
            "started": first["d"] if first and first["d"] else None,
            "as_of": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  API: action=lifelist
    # ------------------------------------------------------------------ #

    def lifelist(self) -> dict:
        """
        Все когда-либо обнаруженные виды.
        Формат 1:1 с PHP action=lifelist.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT Sci_Name AS sci, Com_Name AS com, "
            "  MIN(Date || ' ' || Time) AS first_seen, "
            "  MAX(Date || ' ' || Time) AS last_seen, "
            "  COUNT(*) AS n, "
            "  MAX(Confidence) AS best_conf "
            "FROM detections "
            "GROUP BY Sci_Name "
            "ORDER BY first_seen ASC"
        ).fetchall()
        return {
            "species": [dict(r) for r in rows],
            "as_of": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  API: action=recent
    # ------------------------------------------------------------------ #

    def recent(self, hours: int = 24) -> dict:
        """
        Виды за последние N часов (агрегация по виду).
        Формат 1:1 с PHP action=recent.
        """
        hours = max(1, min(1_000_000, int(hours)))
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT Sci_Name AS sci, Com_Name AS com, "
            "  COUNT(*) AS n, MAX(Confidence) AS best_conf, "
            "  MAX(Date || ' ' || Time) AS last_seen "
            "FROM detections "
            "WHERE (julianday('now','localtime') - julianday(Date || ' ' || Time)) * 24 <= ? "
            "GROUP BY Sci_Name "
            "ORDER BY last_seen DESC",
            (hours,),
        ).fetchall()

        species = []
        for r in rows:
            d = dict(r)
            # Для каждого вида — лучшая детекция в окне (для совместимости с фронтендом)
            best = conn.execute(
                "SELECT File_Name AS file, Date AS d, Time AS t, Confidence AS conf "
                "FROM detections "
                "WHERE Sci_Name = ? "
                "AND (julianday('now','localtime') - julianday(Date || ' ' || Time)) * 24 <= ? "
                "ORDER BY Confidence DESC LIMIT 1",
                (d["sci"], hours),
            ).fetchone()
            d["top_file"] = best["file"] if best else None
            d["top_at"] = (best["d"] + " " + best["t"]) if best and best["d"] else None
            species.append(d)

        return {
            "hours": hours,
            "species": species,
            "as_of": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  API: action=species
    # ------------------------------------------------------------------ #

    def species_detail(self, sci_name: str) -> dict:
        """
        Детали по одному виду: сводка + последние 500 детекций.
        Формат 1:1 с PHP action=species.
        """
        conn = self._get_conn()
        detections = conn.execute(
            "SELECT Date AS d, Time AS t, File_Name AS file, Confidence AS conf "
            "FROM detections "
            "WHERE Sci_Name = ? "
            "ORDER BY Date DESC, Time DESC LIMIT 500",
            (sci_name,),
        ).fetchall()
        summary = conn.execute(
            "SELECT Com_Name AS com, COUNT(*) AS total, "
            "  MIN(Date || ' ' || Time) AS first_seen, "
            "  MAX(Date || ' ' || Time) AS last_seen, "
            "  MAX(Confidence) AS best_conf "
            "FROM detections "
            "WHERE Sci_Name = ?",
            (sci_name,),
        ).fetchone()
        return {
            "sci": sci_name,
            "summary": dict(summary) if summary else None,
            "detections": [dict(r) for r in detections],
        }

    # ------------------------------------------------------------------ #
    #  API: action=timeseries
    # ------------------------------------------------------------------ #

    def timeseries(self, days: int = 30) -> dict:
        """
        Дневные + часовые агрегации для графиков в Stats.
        Формат 1:1 с PHP action=timeseries.
        """
        days = max(1, min(90, int(days)))
        conn = self._get_conn()
        daily = conn.execute(
            "SELECT Date AS date, "
            "  COUNT(*) AS detections, "
            "  COUNT(DISTINCT Sci_Name) AS species "
            "FROM detections "
            "WHERE Date >= DATE('now','localtime','-" + str(days - 1) + " day') "
            "GROUP BY Date "
            "ORDER BY Date"
        ).fetchall()
        by_hour = conn.execute(
            "SELECT CAST(strftime('%H', Time) AS INT) AS hour, "
            "  COUNT(*) AS detections "
            "FROM detections "
            "WHERE Date >= DATE('now','localtime','-30 day') "
            "GROUP BY hour "
            "ORDER BY hour"
        ).fetchall()
        return {
            "days": days,
            "daily": [dict(r) for r in daily],
            "by_hour": [dict(r) for r in by_hour],
            "as_of": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  API: action=firstseen
    # ------------------------------------------------------------------ #

    def firstseen(self, limit: int = 10) -> dict:
        """
        Новейшие добавления к lifelist (первые детекции по видам,
        отсортированные DESC).
        """
        limit = max(1, min(50, int(limit)))
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT Sci_Name AS sci, Com_Name AS com, "
            "  MIN(Date || ' ' || Time) AS first_seen, "
            "  COUNT(*) AS total "
            "FROM detections "
            "GROUP BY Sci_Name "
            "ORDER BY first_seen DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return {
            "species": [dict(r) for r in rows],
            "as_of": datetime.now().isoformat(),
        }