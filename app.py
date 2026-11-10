from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from collections.abc import Generator
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from flask import Flask, Response, flash, redirect, render_template_string, request, url_for
from flask.typing import ResponseReturnValue


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.environ.get("ATOM_DATABASE", os.path.join(APP_DIR, "atom_app.sqlite3"))
BASE_URL = os.environ.get("ATOM_BASE_URL", "http://localhost:5000").rstrip("/")
ATOM_NS = "http://www.w3.org/2005/Atom"
APP_NS = "http://www.w3.org/2007/app"
NS = {"atom": ATOM_NS, "app": APP_NS}
ET.register_namespace("", ATOM_NS)
ET.register_namespace("app", APP_NS)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))
db_lock = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def db_connection() -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                callback TEXT NOT NULL,
                secret TEXT,
                expires_at TEXT
            )
            """
        )
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_entry(title: str, content: str, author: str) -> sqlite3.Row:
    created_at = utc_now().isoformat()
    entry_id = f"urn:uuid:{uuid.uuid4()}"
    with db_lock, db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO entries (entry_id, title, content, author, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry_id, title, content, author, created_at),
        )
        row = connection.execute(
            "SELECT * FROM entries WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    if row is None:
        raise RuntimeError("The published entry could not be loaded.")
    notify_subscribers()
    return row


def update_entry(entry_id: int, title: str, content: str, author: str) -> sqlite3.Row | None:
    updated_at = utc_now().isoformat()
    with db_lock, db_connection() as connection:
        connection.execute(
            """
            UPDATE entries
            SET title = ?, content = ?, author = ?, created_at = ?
            WHERE id = ?
            """,
            (title, content, author, updated_at, entry_id),
        )
        row = connection.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
    if row is not None:
        notify_subscribers()
    return row


def delete_entry(entry_id: int) -> bool:
    with db_lock, db_connection() as connection:
        cursor = connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        deleted = cursor.rowcount == 1
    if deleted:
        notify_subscribers()
    return deleted


def list_entries() -> list[sqlite3.Row]:
    with db_connection() as connection:
        return connection.execute(
            "SELECT * FROM entries ORDER BY created_at DESC, id DESC"
        ).fetchall()


def get_entry(entry_id: int) -> sqlite3.Row | None:
    with db_connection() as connection:
        return connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def xml_text(parent: ET.Element, tag: str, value: str, namespace: str = ATOM_NS) -> ET.Element:
    child = ET.SubElement(parent, f"{{{namespace}}}{tag}")
    child.text = value
    return child


def atom_entry(row: sqlite3.Row) -> ET.Element:
    entry = ET.Element(f"{{{ATOM_NS}}}entry")
    xml_text(entry, "id", row["entry_id"])
    xml_text(entry, "title", row["title"])
    xml_text(entry, "updated", row["created_at"])
    self_link = ET.SubElement(entry, f"{{{ATOM_NS}}}link")
    self_link.set("rel", "self")
    self_link.set("href", f"{BASE_URL}/entries/{row['id']}.atom")
    jsonld_link = ET.SubElement(entry, f"{{{ATOM_NS}}}link")
    jsonld_link.set("rel", "alternate")
    jsonld_link.set("type", "application/ld+json")
    jsonld_link.set("href", f"{BASE_URL}/crates/{row['id']}/ro-crate-metadata.jsonld")
    crate_link = ET.SubElement(entry, f"{{{ATOM_NS}}}link")
    crate_link.set("rel", "alternate")
    crate_link.set("type", "application/zip")
    crate_link.set("title", "Complete RO-Crate")
    crate_link.set("href", f"{BASE_URL}/crates/{row['id']}.zip")
    edit_link = ET.SubElement(entry, f"{{{ATOM_NS}}}link")
    edit_link.set("rel", "edit")
    edit_link.set("href", f"{BASE_URL}/entries/{row['id']}.atom")
    author = ET.SubElement(entry, f"{{{ATOM_NS}}}author")
    xml_text(author, "name", row["author"])
    xml_text(entry, "content", row["content"]).set("type", "text")
    return entry


def ro_crate_entry(row: sqlite3.Row) -> dict[str, object]:
    entry_id = "data/entry.txt"
    author_id = "#author"
    return {
        "@context": "https://w3id.org/ro/crate/1.1/context",
        "@graph": [
            {
                "@id": "ro-crate-metadata.jsonld",
                "@type": "CreativeWork",
                "about": {"@id": "./"},
                "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
            },
            {
                "@id": "./",
                "@type": "Dataset",
                "name": row["title"],
                "datePublished": row["created_at"],
                "hasPart": {"@id": entry_id},
            },
            {
                "@id": entry_id,
                "@type": "File",
                "name": "Entry content",
                "encodingFormat": "text/plain",
                "author": {"@id": author_id},
                "isPartOf": {"@id": "./"},
            },
            {
                "@id": author_id,
                "@type": "Person",
                "name": row["author"],
            },
        ],
    }


def ro_crate_archive(row: sqlite3.Row) -> bytes:
    metadata = json.dumps(ro_crate_entry(row), indent=2).encode("utf-8")
    content = row["content"].encode("utf-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as crate:
        crate.writestr("ro-crate-metadata.jsonld", metadata)
        crate.writestr("data/entry.txt", content)
    return archive.getvalue()


def atom_feed(rows: list[sqlite3.Row]) -> bytes:
    feed = ET.Element(f"{{{ATOM_NS}}}feed")
    xml_text(feed, "id", f"{BASE_URL}/feed.atom")
    xml_text(feed, "title", "Shared Atom entries")
    xml_text(feed, "updated", rows[0]["created_at"] if rows else utc_now().isoformat())
    author = ET.SubElement(feed, f"{{{ATOM_NS}}}author")
    xml_text(author, "name", "Atom prototype")
    link = ET.SubElement(feed, f"{{{ATOM_NS}}}link")
    link.set("rel", "self")
    link.set("href", f"{BASE_URL}/feed.atom")
    hub_link = ET.SubElement(feed, f"{{{ATOM_NS}}}link")
    hub_link.set("rel", "hub")
    hub_link.set("href", f"{BASE_URL}/hub")
    for row in rows:
        feed.append(atom_entry(row))
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def atom_response(payload: bytes, status: int = 200) -> Response:
    return Response(payload, status=status, content_type="application/atom+xml; charset=utf-8")


def jsonld_response(payload: dict[str, object], status: int = 200) -> Response:
    return Response(
        json.dumps(payload, indent=2),
        status=status,
        content_type="application/ld+json; charset=utf-8",
    )


def entry_etag(row: sqlite3.Row) -> str:
    return f'"{row["created_at"]}"'


def matches_if_match(row: sqlite3.Row) -> bool:
    requested_etag = request.headers.get("If-Match")
    return requested_etag is None or requested_etag == "*" or requested_etag == entry_etag(row)


def parse_atom_entry(payload: bytes) -> tuple[str, str, str]:
    try:
        root = ET.fromstring(payload)
        if root.tag != f"{{{ATOM_NS}}}entry":
            raise ValueError("The request body must be an Atom <entry>.")
        title = root.findtext("atom:title", default="", namespaces=NS).strip()
        content = root.findtext("atom:content", default="", namespaces=NS).strip()
        author = root.findtext("atom:author/atom:name", default="Anonymous", namespaces=NS).strip()
    except ET.ParseError as error:
        raise ValueError("The request body is not valid XML.") from error
    if not title or not content:
        raise ValueError("Atom entries require non-empty title and content.")
    return title, content, author or "Anonymous"


def subscription_rows() -> list[sqlite3.Row]:
    with db_connection() as connection:
        return connection.execute(
            "SELECT * FROM subscriptions WHERE topic = ?", (f"{BASE_URL}/feed.atom",)
        ).fetchall()


def notify_subscribers() -> None:
    payload = atom_feed(list_entries())
    for subscription in subscription_rows():
        try:
            request_object = urllib.request.Request(
                subscription["callback"],
                data=payload,
                headers={
                    "Content-Type": "application/atom+xml; charset=utf-8",
                    "Link": f'<{BASE_URL}/hub>; rel="hub"',
                    "X-WebSub-Topic": f"{BASE_URL}/feed.atom",
                },
                method="POST",
            )
            if subscription["secret"]:
                signature = hmac.new(
                    subscription["secret"].encode("utf-8"), payload, hashlib.sha256
                ).hexdigest()
                request_object.add_header("X-Hub-Signature-256", f"sha256={signature}")
            with urllib.request.urlopen(request_object, timeout=5):
                pass
        except (urllib.error.URLError, TimeoutError, ValueError):
            app.logger.warning("WebSub delivery failed for %s", subscription["callback"])


def register_websub_subscription(
    mode: str, topic: str, callback: str, secret: str | None, lease_seconds: str | None
) -> bool:
    challenge = secrets.token_urlsafe(24)
    verification_query = urllib.parse.urlencode(
        {
            "hub.mode": mode,
            "hub.topic": topic,
            "hub.challenge": challenge,
            "hub.lease_seconds": lease_seconds or "86400",
        }
    )
    try:
        with urllib.request.urlopen(f"{callback}?{verification_query}", timeout=5) as response:
            verified = response.read().decode("utf-8").strip() == challenge
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError):
        verified = False
    if not verified:
        return False
    with db_lock, db_connection() as connection:
        if mode == "subscribe":
            connection.execute(
                "INSERT INTO subscriptions (topic, callback, secret, expires_at) VALUES (?, ?, ?, ?)",
                (topic, callback, secret, lease_seconds),
            )
        else:
            connection.execute(
                "DELETE FROM subscriptions WHERE topic = ? AND callback = ?",
                (topic, callback),
            )
    return True


def page_template() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Atom Notebook</title>
        <style>
          body { font: 16px system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #172033; }
          textarea, input { box-sizing: border-box; width: 100%; padding: .65rem; margin: .25rem 0 1rem; }
          textarea { min-height: 8rem; } button { padding: .65rem 1rem; cursor: pointer; }
          article { border-top: 1px solid #ccd3df; padding: 1rem 0; } .notice { background: #e8f4ff; padding: .75rem; }
          code { overflow-wrap: anywhere; } a { color: #07599c; }
        </style>
      </head>
      <body>
        <h1>Atom Notebook</h1>
        <p>Publish entries as Atom and receive updates through WebSub.</p>
        {% with messages = get_flashed_messages() %}
          {% for message in messages %}<p class="notice">{{ message }}</p>{% endfor %}
        {% endwith %}
        <h2>Publish an entry</h2>
        <form method="post" action="{{ url_for('create_entry') }}">
          <label>Title <input name="title" required maxlength="200"></label>
          <label>Author <input name="author" value="Anonymous" maxlength="100"></label>
          <label>Content <textarea name="content" required></textarea></label>
          <button type="submit">Publish</button>
        </form>
        <h2>Share and subscribe</h2>
        <p>Atom feed: <a href="{{ url_for('feed') }}">{{ base_url }}/feed.atom</a></p>
        <p>AtomPub service document: <a href="{{ url_for('service_document') }}">{{ base_url }}/service</a></p>
        <p>WebSub hub: <code>{{ base_url }}/hub</code>. Subscribe with <code>hub.topic={{ base_url }}/feed.atom</code>.</p>
        <h2>Published entries</h2>
        {% for entry in entries %}
          <article>
            <h3>{{ entry["title"] }}</h3>
            <small>By {{ entry["author"] }} · {{ entry["created_at"] }}</small>
            <p>{{ entry["content"] }}</p>
            <a href="{{ url_for('entry_atom', entry_id=entry['id']) }}">Atom entry</a>
            · <a href="{{ url_for('crate_metadata', entry_id=entry['id']) }}">RO-Crate metadata</a>
            · <a href="{{ url_for('crate_archive', entry_id=entry['id']) }}">Download RO-Crate ZIP</a>
          </article>
        {% else %}<p>No entries yet.</p>{% endfor %}
      </body>
    </html>
    """


@app.get("/")
def index() -> str:
    return render_template_string(
        page_template(), entries=list_entries(), base_url=BASE_URL
    )


@app.post("/entries")
def create_entry() -> ResponseReturnValue:
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    author = request.form.get("author", "Anonymous").strip() or "Anonymous"
    if not title or not content:
        flash("Title and content are required.")
        return redirect(url_for("index"))
    row = save_entry(title, content, author)
    if request.accept_mimetypes.best == "application/atom+xml":
        return atom_response(
            ET.tostring(atom_entry(row), encoding="utf-8", xml_declaration=True),
            status=201,
        )
    flash(f"Published “{title}”.")
    return redirect(url_for("index"))


@app.get("/feed.atom")
def feed() -> Response:
    return atom_response(atom_feed(list_entries()))


@app.get("/entries/<int:entry_id>.jsonld")
def entry_jsonld(entry_id: int) -> Response:
    row = get_entry(entry_id)
    if row is None:
        return Response("Entry not found", status=404)
    response = jsonld_response(ro_crate_entry(row))
    response.headers["ETag"] = entry_etag(row)
    return response


@app.get("/crates/<int:entry_id>/ro-crate-metadata.jsonld")
def crate_metadata(entry_id: int) -> Response:
    row = get_entry(entry_id)
    if row is None:
        return Response("Crate not found", status=404)
    response = jsonld_response(ro_crate_entry(row))
    response.headers["ETag"] = entry_etag(row)
    return response


@app.get("/crates/<int:entry_id>.zip")
def crate_archive(entry_id: int) -> Response:
    row = get_entry(entry_id)
    if row is None:
        return Response("Crate not found", status=404)
    response = Response(
        ro_crate_archive(row),
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ro-crate-{entry_id}.zip"'},
    )
    response.headers["ETag"] = entry_etag(row)
    return response


@app.route("/entries/<int:entry_id>.atom", methods=["GET", "PUT", "DELETE"])
def entry_atom(entry_id: int) -> Response:
    row = get_entry(entry_id)
    if row is None:
        return Response("Entry not found", status=404)
    if request.method == "GET":
        response = atom_response(
            ET.tostring(atom_entry(row), encoding="utf-8", xml_declaration=True)
        )
        response.headers["ETag"] = entry_etag(row)
        return response
    if not matches_if_match(row):
        return Response("The entry has changed; refresh it before modifying it.", status=412)
    if request.method == "DELETE":
        if not delete_entry(entry_id):
            return Response("Entry not found", status=404)
        return Response(status=204)
    if request.method == "PUT":
        try:
            title, content, author = parse_atom_entry(request.get_data())
        except ValueError as error:
            return Response(str(error), status=400, content_type="text/plain; charset=utf-8")
        row = update_entry(entry_id, title, content, author)
        if row is None:
            return Response("Entry not found", status=404)
    response = atom_response(
        ET.tostring(atom_entry(row), encoding="utf-8", xml_declaration=True)
    )
    response.headers["ETag"] = entry_etag(row)
    return response


@app.get("/service")
def service_document() -> Response:
    service = ET.Element(f"{{{APP_NS}}}service")
    workspace = ET.SubElement(service, f"{{{APP_NS}}}workspace")
    xml_text(workspace, "title", "Atom Notebook", APP_NS)
    collection = ET.SubElement(workspace, f"{{{APP_NS}}}collection")
    collection.set("href", f"{BASE_URL}/collections/entries")
    xml_text(collection, "title", "Entries", APP_NS)
    xml_text(collection, "accept", "application/atom+xml", APP_NS)
    return Response(
        ET.tostring(service, encoding="utf-8", xml_declaration=True),
        content_type="application/atomsvc+xml; charset=utf-8",
    )


@app.route("/collections/entries", methods=["GET", "POST"])
def collection() -> Response:
    if request.method == "GET":
        return atom_response(atom_feed(list_entries()))
    try:
        title, content, author = parse_atom_entry(request.get_data())
    except ValueError as error:
        return Response(str(error), status=400, content_type="text/plain; charset=utf-8")
    row = save_entry(title, content, author)
    response = atom_response(
        ET.tostring(atom_entry(row), encoding="utf-8", xml_declaration=True), status=201
    )
    response.headers["Location"] = f"{BASE_URL}/entries/{row['id']}.atom"
    response.headers["ETag"] = entry_etag(row)
    return response


@app.route("/hub", methods=["GET", "POST"])
def hub() -> Response:
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        topic = request.args.get("hub.topic")
        callback = request.args.get("hub.callback")
        if mode not in {"subscribe", "unsubscribe"} or topic != f"{BASE_URL}/feed.atom" or not callback:
            return Response("Invalid WebSub verification request.", status=400)
        # A GET is retained as a convenient local demo shortcut; standard clients
        # should use POST below so the callback can verify the subscription.
        with db_lock, db_connection() as connection:
            if mode == "subscribe":
                connection.execute(
                    "INSERT INTO subscriptions (topic, callback, secret, expires_at) VALUES (?, ?, ?, ?)",
                    (topic, callback, request.args.get("hub.secret"), request.args.get("hub.lease_seconds")),
                )
            else:
                connection.execute(
                    "DELETE FROM subscriptions WHERE topic = ? AND callback = ?",
                    (topic, callback),
                )
        return Response(topic, content_type="text/plain; charset=utf-8")
    mode = request.form.get("hub.mode")
    topic = request.form.get("hub.topic")
    callback = request.form.get("hub.callback")
    if mode not in {"subscribe", "unsubscribe"} or topic != f"{BASE_URL}/feed.atom" or not callback:
        return Response("Invalid WebSub request.", status=400)
    if not register_websub_subscription(
        mode, topic, callback, request.form.get("hub.secret"), request.form.get("hub.lease_seconds")
    ):
        return Response("The callback did not verify the WebSub subscription.", status=409)
    return Response("Subscription accepted.", status=202)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
