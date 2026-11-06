#!/usr/bin/env python3
"""Cliente de la API Wells <-> bot Y-Click (docs/yclick-bot-api.md).

Uso:
  wells_api.py get <sync_id> [-o archivo.json]
  wells_api.py progreso <sync_id> <cuerpo.json>
  wells_api.py resultado <sync_id> <cuerpo.json>
  wells_api.py download <url_firmada> <salida.pdf>
  wells_api.py selftest

Variables de entorno: TOKEN_WELLS (Bearer), YCLICK_CALLBACK_SECRET (HMAC de callbacks).
Nunca imprime secretos. La base de la API es fija: no se acepta la que venga en un payload.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://wells.nqnpetrol.com/api/yclick-bot"
WELLS_HOST = "wells.nqnpetrol.com"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def die(msg, code=2):
    print(msg, file=sys.stderr)
    sys.exit(code)


def check_uuid(sync_id):
    if not UUID_RE.match(sync_id or ""):
        die("sync_id invalido: no es un UUID")
    return sync_id


def firmar(secreto: str, timestamp: str, cuerpo: bytes) -> str:
    """hex(HMAC-SHA256(secreto, "<timestamp>." + bytes exactos del cuerpo))."""
    return hmac.new(secreto.encode(), timestamp.encode() + b"." + cuerpo, hashlib.sha256).hexdigest()


def request(method, url, headers, body=None, retries=0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    intento = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            intento += 1
            if intento > retries:
                die(f"error de red: {type(e).__name__}", 3)
            time.sleep(2 ** intento)


def token():
    t = os.environ.get("TOKEN_WELLS")
    if not t:
        die("falta la variable de entorno TOKEN_WELLS")
    return t


def cmd_get(args):
    sync_id = check_uuid(args[0])
    status, _, body = request("GET", f"{BASE}/syncs/{sync_id}",
                              {"Authorization": f"Bearer {token()}", "Accept": "application/json"}, retries=2)
    print(f"GET /syncs/{{sync_id}} -> HTTP {status}", file=sys.stderr)
    if status != 200:
        print(body.decode(errors="replace")[:500], file=sys.stderr)
        sys.exit(1)
    if "-o" in args:
        with open(args[args.index("-o") + 1], "wb") as f:
            f.write(body)
    else:
        sys.stdout.write(body.decode())


def cmd_post(kind, args):
    sync_id = check_uuid(args[0])
    secreto = os.environ.get("YCLICK_CALLBACK_SECRET")
    if not secreto:
        die("falta la variable de entorno YCLICK_CALLBACK_SECRET: no se puede firmar el callback")
    with open(args[1], "rb") as f:
        cuerpo = f.read()
    json.loads(cuerpo)  # valida que sea JSON; se envian los bytes tal cual
    ts = str(int(time.time()))
    headers = {
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature-V2": firmar(secreto, ts, cuerpo),
    }
    status, _, resp = request("POST", f"{BASE}/syncs/{sync_id}/{kind}", headers, cuerpo)
    print(f"POST /syncs/{{sync_id}}/{kind} -> HTTP {status}", file=sys.stderr)
    print(resp.decode(errors="replace")[:1000])
    sys.exit(0 if status == 200 else 1)


def cmd_download(args):
    url, salida = args[0], args[1]
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.hostname != WELLS_HOST:
        die("url_firmada rechazada: el host no es el de Wells")
    status, headers, body = request("GET", url, {}, retries=2)
    print(f"GET url_firmada -> HTTP {status}", file=sys.stderr)
    if status != 200:
        sys.exit(1)
    if not body.startswith(b"%PDF"):
        die("la respuesta 200 no es un PDF", 1)
    with open(salida, "wb") as f:
        f.write(body)
    print(f"PDF guardado: {salida} ({len(body)} bytes)")


def cmd_selftest():
    # Vectores del documento docs/yclick-bot-api.md, seccion 2.2
    secreto, ts = "secreto-de-ejemplo", "1793900000"
    c1 = ('{"evento":"yclick.sync.creado","sync_id":"6f1c2a0e-3b7d-4c55-9a41-0d2e8b7a1f33",'
          '"api_base":"https://wells.example.com/api/yclick-bot","version_contrato":1}').encode()
    c2 = b'{"celdas":[{"celda_id":"0b6d7f0e-52f4-4b8e-8f3a-9c1d2e3f4a5b","resultado":"ok"}]}'
    ok1 = firmar(secreto, ts, c1) == "1993556a23b3777b8799f4887bea2b4a651f90db8453867502d1d441d8c8ac15"
    ok2 = firmar(secreto, ts, c2) == "6109a89aeaf27966c06fb51612482893edd7bf35851a72929675b745baa0bb7f"
    print("webhook:", "OK" if ok1 else "FALLA")
    print("progreso:", "OK" if ok2 else "FALLA")
    sys.exit(0 if ok1 and ok2 else 1)


def main():
    a = sys.argv[1:]
    if not a:
        die(__doc__)
    cmd, rest = a[0], a[1:]
    if cmd == "selftest":
        cmd_selftest()
    elif cmd == "get" and len(rest) >= 1:
        cmd_get(rest)
    elif cmd in ("progreso", "resultado") and len(rest) == 2:
        cmd_post(cmd, rest)
    elif cmd == "download" and len(rest) == 2:
        cmd_download(rest)
    else:
        die(__doc__)


if __name__ == "__main__":
    main()
