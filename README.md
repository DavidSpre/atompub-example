# Atom Notebook prototype

This prototype lets users publish short entries through a web form. Entries are stored in SQLite and exposed as Atom:

- `GET /feed.atom` returns the Atom feed.
- `GET /service` returns an Atom Publishing Protocol service document.
- `GET /collections/entries` reads the AtomPub collection.
- `POST /collections/entries` accepts an Atom `<entry>` and publishes it.
- `GET /hub` and `POST /hub` implement the WebSub subscription handshake.
- New entries are delivered as Atom feed updates to registered WebSub callbacks.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ATOM_BASE_URL = "http://localhost:5000"
flask --app app run --debug
```

Open <http://localhost:5000>. For a public WebSub callback, expose the app with a tunnel and set `ATOM_BASE_URL` to its public HTTPS URL.

Example AtomPub request:

```powershell
curl.exe -X POST http://localhost:5000/collections/entries `
  -H "Content-Type: application/atom+xml" `
  --data-binary "@entry.xml"
```
