# Atom Notebook prototype

This prototype lets users publish short entries through a web form. Entries are stored in SQLite and exposed as Atom:

- `GET /feed.atom` returns the Atom feed.
- `GET /service` returns an Atom Publishing Protocol service document.
- `GET /collections/entries` reads the AtomPub collection.
- `POST /collections/entries` accepts an Atom `<entry>` and publishes it.
- `PUT /entries/<id>.atom` replaces an existing Atom entry.
- `GET /hub` and `POST /hub` implement the WebSub subscription handshake.
- New entries are delivered as Atom feed updates to registered WebSub callbacks.

## Start the base app

Open a terminal in the project directory and create a virtual environment:

```sh
python3 -m venv .venv
```

On Windows, use `python` instead of `python3` if `python3` is not available:

```powershell
python -m venv .venv
```

Install the dependencies. On Linux and macOS:

```sh
. .venv/bin/activate
pip install -r requirements.txt
export ATOM_BASE_URL="http://localhost:5000"
python -m flask --app app run --debug --port 5000
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ATOM_BASE_URL = "http://localhost:5000"
python -m flask --app app run --debug --port 5000
```

Open <http://localhost:5000>. Keep this terminal running.

## Test WebSub locally

WebSub requires a callback endpoint. This project includes [`callback.py`](./callback.py), which listens on port `5001`, verifies subscription challenges, and prints received Atom notifications.

Open a second terminal in the project directory and start the callback.
On Linux and macOS:

```sh
. .venv/bin/activate
python callback.py
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe .\callback.py
```

Keep this terminal running as well. In a third terminal, subscribe the callback to the Atom feed. On Linux and macOS:

```sh
curl -X POST "http://localhost:5000/hub" \
  --data-urlencode "hub.mode=subscribe" \
  --data-urlencode "hub.topic=http://localhost:5000/feed.atom" \
  --data-urlencode "hub.callback=http://127.0.0.1:5001/callback"
```

On Windows PowerShell, use `curl.exe` and the PowerShell line-continuation character:

```powershell
curl.exe -X POST "http://localhost:5000/hub" `
  --data-urlencode "hub.mode=subscribe" `
  --data-urlencode "hub.topic=http://localhost:5000/feed.atom" `
  --data-urlencode "hub.callback=http://127.0.0.1:5001/callback"
```

The expected response is:

```text
Subscription accepted.
```

Publish a test entry through AtomPub. On Linux and macOS:

```sh
curl -X POST "http://localhost:5000/collections/entries" \
  -H "Content-Type: application/atom+xml" \
  --data-binary "<entry xmlns='http://www.w3.org/2005/Atom'><title>WebSub test</title><content>Notification test</content></entry>"
```

On Windows PowerShell:

```powershell
curl.exe -X POST "http://localhost:5000/collections/entries" `
  -H "Content-Type: application/atom+xml" `
  --data-binary "<entry xmlns='http://www.w3.org/2005/Atom'><title>WebSub test</title><content>Notification test</content></entry>"
```

The callback terminal should print the updated Atom feed. You can also publish an entry with the web form at <http://localhost:5000>.

To update an existing entry, first note its numeric ID from its `Location` header or Atom entry URL, then send a complete replacement Atom entry:

```sh
curl -X PUT "http://localhost:5000/entries/1.atom" \
  -H "Content-Type: application/atom+xml" \
  --data-binary "<entry xmlns='http://www.w3.org/2005/Atom'><title>Updated title</title><content>Updated content</content></entry>"
```

On Windows PowerShell:

```powershell
curl.exe -X PUT "http://localhost:5000/entries/1.atom" `
  -H "Content-Type: application/atom+xml" `
  --data-binary "<entry xmlns='http://www.w3.org/2005/Atom'><title>Updated title</title><content>Updated content</content></entry>"
```

The server returns the updated Atom entry with status `200 OK` and sends the updated feed to WebSub subscribers.

For a real external feed reader or callback service, the callback must be publicly reachable. Use a tunnel such as `ngrok http 5001` and subscribe using its HTTPS callback URL.
