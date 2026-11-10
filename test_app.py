import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import app


class AtomNotebookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = tempfile.NamedTemporaryFile(delete=False)
        self.database.close()
        app.DATABASE_PATH = self.database.name
        self.client = app.app.test_client()

    def tearDown(self) -> None:
        os.unlink(self.database.name)

    def test_form_entry_is_available_in_atom_feed(self) -> None:
        response = self.client.post(
            "/entries",
            data={"title": "Hello", "author": "Ada", "content": "A first note"},
        )
        self.assertEqual(response.status_code, 302)
        feed = self.client.get("/feed.atom")
        self.assertEqual(feed.status_code, 200)
        root = ET.fromstring(feed.data)
        self.assertEqual(root.findtext("{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}title"), "Hello")
        jsonld_link = root.find(
            "{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}link[@type='application/ld+json']"
        )
        self.assertIsNotNone(jsonld_link)
        self.assertTrue(jsonld_link.get("href", "").endswith(".jsonld"))

    def test_entry_is_available_as_ro_crate_jsonld(self) -> None:
        create_response = self.client.post(
            "/entries",
            data={"title": "RO-Crate entry", "author": "Ada", "content": "Structured content"},
        )
        self.assertEqual(create_response.status_code, 302)
        entry = self.client.get("/entries/1.jsonld")
        self.assertEqual(entry.status_code, 200)
        self.assertTrue(entry.content_type.startswith("application/ld+json"))
        document = json.loads(entry.data)
        self.assertEqual(document["@context"], "https://w3id.org/ro/crate/1.1/context")
        graph = {item["@type"]: item for item in document["@graph"] if "@type" in item}
        self.assertEqual(graph["CreativeWork"]["name"], "RO-Crate entry")
        self.assertEqual(graph["CreativeWork"]["text"], "Structured content")
        self.assertEqual(graph["Person"]["name"], "Ada")

    def test_atompub_collection_creates_entry(self) -> None:
        payload = b"""<?xml version="1.0"?><entry xmlns="http://www.w3.org/2005/Atom">
          <title>From AtomPub</title><author><name>Grace</name></author>
          <content>Published over the protocol</content>
        </entry>"""
        response = self.client.post(
            "/collections/entries", data=payload, content_type="application/atom+xml"
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("/entries/", response.headers["Location"])
        self.assertEqual(self.client.get("/feed.atom").status_code, 200)

    def test_atompub_put_updates_existing_entry(self) -> None:
        create_response = self.client.post(
            "/collections/entries",
            data=b"<entry xmlns='http://www.w3.org/2005/Atom'><title>Original</title><content>Old content</content></entry>",
            content_type="application/atom+xml",
        )
        entry_url = create_response.headers["Location"].replace(app.BASE_URL, "")
        update_response = self.client.put(
            entry_url,
            data=b"<entry xmlns='http://www.w3.org/2005/Atom'><title>Updated</title><content>New content</content></entry>",
            content_type="application/atom+xml",
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertIn(b"<title>Updated</title>", update_response.data)
        self.assertIn(b"<content type=\"text\">New content</content>", update_response.data)

    def test_atompub_delete_removes_existing_entry(self) -> None:
        create_response = self.client.post(
            "/collections/entries",
            data=b"<entry xmlns='http://www.w3.org/2005/Atom'><title>To delete</title><content>Remove me</content></entry>",
            content_type="application/atom+xml",
        )
        entry_url = create_response.headers["Location"].replace(app.BASE_URL, "")
        delete_response = self.client.delete(entry_url)
        self.assertEqual(delete_response.status_code, 204)
        self.assertEqual(self.client.get(entry_url).status_code, 404)

    def test_atompub_if_match_rejects_stale_update(self) -> None:
        create_response = self.client.post(
            "/collections/entries",
            data=b"<entry xmlns='http://www.w3.org/2005/Atom'><title>Versioned</title><content>Original</content></entry>",
            content_type="application/atom+xml",
        )
        entry_url = create_response.headers["Location"].replace(app.BASE_URL, "")
        response = self.client.put(
            entry_url,
            headers={"If-Match": '"stale"'},
            data=b"<entry xmlns='http://www.w3.org/2005/Atom'><title>Changed</title><content>Changed</content></entry>",
            content_type="application/atom+xml",
        )
        self.assertEqual(response.status_code, 412)

    def test_websub_subscription_receives_new_entry(self) -> None:
        response = self.client.get(
            "/hub",
            query_string={
                "hub.mode": "subscribe",
                "hub.topic": f"{app.BASE_URL}/feed.atom",
                "hub.callback": "http://127.0.0.1:9/callback",
            },
        )
        self.assertEqual(response.status_code, 200)
        with app.db_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_standard_websub_handshake_delivers_atom_feed(self) -> None:
        notifications: list[bytes] = []

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                challenge = parse_qs(urlparse(self.path).query)["hub.challenge"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(challenge.encode("utf-8"))

            def do_POST(self) -> None:
                notifications.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            callback = f"http://127.0.0.1:{server.server_port}/websub"
            response = self.client.post(
                "/hub",
                data={
                    "hub.mode": "subscribe",
                    "hub.topic": f"{app.BASE_URL}/feed.atom",
                    "hub.callback": callback,
                },
            )
            self.assertEqual(response.status_code, 202)
            self.client.post(
                "/collections/entries",
                data=b"<entry xmlns='http://www.w3.org/2005/Atom'><title>T</title><content>C</content></entry>",
                content_type="application/atom+xml",
            )
            self.assertEqual(len(notifications), 1)
            self.assertIn(b"<feed", notifications[0])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
