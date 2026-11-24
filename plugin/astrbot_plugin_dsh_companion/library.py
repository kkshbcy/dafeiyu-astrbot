"""Small, owner-private library. Files are read from a dedicated mounted folder."""
from pathlib import Path
import os
import sqlite3


class Library:
    def __init__(self, data):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.books = self.data / "books"
        self.books.mkdir(exist_ok=True)
        self.db = self.data / "progress.sqlite3"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS progress (title TEXT PRIMARY KEY, note TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS deliveries (scope TEXT, msg TEXT, stamp INTEGER, PRIMARY KEY(scope,msg))")

    def first_delivery(self, scope, message_id):
        if not message_id or str(message_id) in {"0","-1","None"}:
            return True
        with sqlite3.connect(self.db, timeout=5) as conn:
            conn.execute("DELETE FROM deliveries WHERE stamp < strftime('%s','now')-604800")
            return conn.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,strftime('%s','now'))",
                                (scope, str(message_id))).rowcount == 1

    def progress(self, title, note=""):
        title, note = title.strip(), note.strip()
        if not 0 < len(title) <= 160 or len(note) > 3000:
            raise ValueError("标题或进度长度超限")
        with sqlite3.connect(self.db) as conn:
            if note:
                conn.execute("INSERT INTO progress VALUES (?,?) ON CONFLICT(title) DO UPDATE SET note=excluded.note",
                             (title, note))
                return "已保存：" + note
            result = conn.execute("SELECT note FROM progress WHERE title=?", (title,)).fetchone()
            return result[0] if result else "这项还没有进度记录。"

    def titles(self):
        return sorted(p.name for p in self.books.iterdir() if p.is_file() and not p.is_symlink()
                      and p.suffix.lower() in {".md", ".txt", ".pdf"})[:250]

    def read(self, name, part):
        if not isinstance(part, int) or part < 1 or name not in self.titles():
            raise ValueError("书名或页段无效，请先列出书架。")
        with os.fdopen(os.open(self.books / name, os.O_RDONLY | os.O_NOFOLLOW), "rb") as f:
            if os.fstat(f.fileno()).st_size > 16 * 1024 * 1024:
                raise ValueError("单份文件上限为 16 MiB")
            if name.lower().endswith(".pdf"):
                from pypdf import PdfReader
                doc = PdfReader(f)
                if part > len(doc.pages):
                    raise ValueError("已到文档末尾")
                return (doc.pages[part-1].extract_text() or "本页没有可提取的文字。")[:9000]
            text = f.read().decode("utf-8-sig")
            page = text[(part-1)*4000:part*4000]
            if not page:
                raise ValueError("已到文档末尾")
            return page
