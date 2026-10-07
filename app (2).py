"""
Backend del cotizador LCS (versión hospedada — PostgreSQL)
=============================================================

Qué hace este servidor
-----------------------
1. Consulta de RUC (SUNAT vía Decolecta) sin exponer el token en el navegador.
2. Guarda en una base de datos PostgreSQL (hospedada, por ejemplo en Neon)
   TODO lo que antes vivía en el navegador de una sola computadora:
   - Catálogo (categorías y productos)
   - Cartera de clientes (RUC, razón social, dirección)
   - Numeración de cotizaciones (un solo contador compartido)
   - Historial de cotizaciones emitidas, con estado

Así, hospedado en internet (por ejemplo en Render), Miguel, Michelle y tú
pueden usar el cotizador desde cualquier lugar y ver exactamente lo mismo.

Cómo usarlo
-----------
1. pip install -r requirements.txt
2. Crea una base de datos gratis en https://neon.tech y copia su cadena de
   conexión (empieza con postgresql://...)
3. Define dos variables de entorno (en tu .env si lo corres local, o en el
   panel de Render si lo hospedas ahí):
     DATABASE_URL=postgresql://... (la de Neon)
     SUNAT_TOKEN=tu_token_de_decolecta
4. python app.py (local) — o despliega en Render (ver guía).
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import psycopg2
import psycopg2.extras
import json
from datetime import datetime
from collections import defaultdict

# Si tienes un archivo .env con DATABASE_URL / SUNAT_TOKEN, esto lo carga automáticamente.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

# Permite que el cotizador (abierto como archivo local, u hospedado en otra
# dirección) pueda llamar a este servidor sin ser bloqueado por el navegador.
CORS(app)

# ---------------------------------------------------------------
# Pega aquí tu token gratuito de https://decolecta.com (Perfil → API Keys),
# o defínelo en tu .env / en Render como SUNAT_TOKEN (recomendado).
# ---------------------------------------------------------------
SUNAT_TOKEN = os.environ.get('SUNAT_TOKEN', 'PEGA_AQUI_TU_TOKEN_DE_DECOLECTA')
SUNAT_URL = 'https://api.decolecta.com/v1/sunat/ruc'

# Cadena de conexión a PostgreSQL (de Neon, Render, etc.). En desarrollo local
# sin Postgres configurado, el servidor avisa claramente en vez de fallar feo.
DATABASE_URL = os.environ.get('DATABASE_URL', '')


# =====================================================================
# Base de datos
# =====================================================================

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    if not DATABASE_URL:
        print('\n ADVERTENCIA: no hay DATABASE_URL configurada. Define esa variable')
        print(' (en tu .env o en Render) con la cadena de conexión de tu base de')
        print(' datos Postgres (por ejemplo, de https://neon.tech). El servidor')
        print(' seguirá iniciando, pero las rutas que usan base de datos fallarán.\n')
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            icon TEXT
        );
        CREATE TABLE IF NOT EXISTS products (
            sku TEXT PRIMARY KEY,
            cat TEXT,
            name TEXT NOT NULL,
            desc_text TEXT,
            unit TEXT,
            price DOUBLE PRECISION
        );
        CREATE TABLE IF NOT EXISTS clients (
            label TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            ruc TEXT,
            address TEXT
        );
        CREATE TABLE IF NOT EXISTS counters (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quotes (
            id TEXT PRIMARY KEY,
            client TEXT,
            client_ruc TEXT,
            client_address TEXT,
            issue_date TEXT,
            valid_until TEXT,
            items_json TEXT,
            subtotal DOUBLE PRECISION,
            disc_pct DOUBLE PRECISION,
            disc_amount DOUBLE PRECISION,
            igv DOUBLE PRECISION,
            total DOUBLE PRECISION,
            payment_method TEXT,
            seller TEXT,
            status TEXT DEFAULT 'Emitida',
            created_at TEXT
        );
    ''')
    cur.execute('SELECT value FROM counters WHERE name = %s', ('quote',))
    if cur.fetchone() is None:
        cur.execute('INSERT INTO counters(name, value) VALUES (%s, %s)', ('quote', 142))
    conn.commit()
    cur.close()
    conn.close()


try:
    init_db()
except Exception as e:
    print(f'\n ADVERTENCIA: no se pudo inicializar la base de datos al arrancar: {e}')
    print(' El servidor sigue arriba; /api/salud mostrará el detalle del error.\n')


def db_configured_or_error():
    if not DATABASE_URL:
        return jsonify({'error': 'El servidor no tiene configurada DATABASE_URL (la base de datos). Avisa al administrador.'}), 500
    return None


# =====================================================================
# Catálogo (categorías + productos)
# =====================================================================

@app.route('/api/catalog', methods=['GET'])
def get_catalog():
    err = db_configured_or_error()
    if err: return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM categories')
    categories = [dict(r) for r in cur.fetchall()]
    cur.execute('SELECT sku, cat, name, desc_text AS desc, unit, price FROM products')
    products = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({'categories': categories, 'products': products})


@app.route('/api/catalog', methods=['PUT'])
def save_catalog():
    err = db_configured_or_error()
    if err: return err
    data = request.get_json(force=True) or {}
    categories = data.get('categories', [])
    products = data.get('products', [])
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM categories')
    cur.execute('DELETE FROM products')
    cur.executemany(
        'INSERT INTO categories(id, name, icon) VALUES (%s, %s, %s)',
        [(c.get('id', ''), c.get('name', ''), c.get('icon', '▤')) for c in categories]
    )
    cur.executemany(
        'INSERT INTO products(sku, cat, name, desc_text, unit, price) VALUES (%s, %s, %s, %s, %s, %s)',
        [(p.get('sku', ''), p.get('cat', ''), p.get('name', ''), p.get('desc', ''),
          p.get('unit', ''), p.get('price', 0)) for p in products]
    )
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'ok': True})


# =====================================================================
# Cartera de clientes
# =====================================================================

@app.route('/api/clients', methods=['GET'])
def get_clients():
    err = db_configured_or_error()
    if err: return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM clients')
    rows = cur.fetchall()
    cur.close(); conn.close()
    registry = {r['label']: {'name': r['name'], 'ruc': r['ruc'], 'address': r['address']} for r in rows}
    return jsonify(registry)


@app.route('/api/clients', methods=['PUT'])
def save_clients():
    err = db_configured_or_error()
    if err: return err
    data = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM clients')
    cur.executemany(
        'INSERT INTO clients(label, name, ruc, address) VALUES (%s, %s, %s, %s)',
        [(label, v.get('name', ''), v.get('ruc', ''), v.get('address', '')) for label, v in data.items()]
    )
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'ok': True})


# =====================================================================
# Numeración de cotizaciones (compartida entre todos)
# =====================================================================

@app.route('/api/quote-counter', methods=['GET'])
def get_quote_counter():
    err = db_configured_or_error()
    if err: return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT value FROM counters WHERE name = %s', ('quote',))
    row = cur.fetchone()
    cur.close(); conn.close()
    return jsonify({'value': row['value'] if row else 142})


@app.route('/api/quote-counter/increment', methods=['POST'])
def increment_quote_counter():
    err = db_configured_or_error()
    if err: return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE counters SET value = value + 1 WHERE name = %s', ('quote',))
    cur.execute('SELECT value FROM counters WHERE name = %s', ('quote',))
    row = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'value': row['value']})


# =====================================================================
# Historial de cotizaciones
# =====================================================================

@app.route('/api/quotes', methods=['POST'])
def create_quote():
    err = db_configured_or_error()
    if err: return err
    q = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO quotes
        (id, client, client_ruc, client_address, issue_date, valid_until, items_json,
         subtotal, disc_pct, disc_amount, igv, total, payment_method, seller, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            client=EXCLUDED.client, client_ruc=EXCLUDED.client_ruc, client_address=EXCLUDED.client_address,
            issue_date=EXCLUDED.issue_date, valid_until=EXCLUDED.valid_until, items_json=EXCLUDED.items_json,
            subtotal=EXCLUDED.subtotal, disc_pct=EXCLUDED.disc_pct, disc_amount=EXCLUDED.disc_amount,
            igv=EXCLUDED.igv, total=EXCLUDED.total, payment_method=EXCLUDED.payment_method,
            seller=EXCLUDED.seller
    ''', (
        q.get('id', ''), q.get('client', ''), q.get('clientRuc', ''), q.get('clientAddress', ''),
        q.get('issueDate', ''), q.get('validUntil', ''), json.dumps(q.get('items', []), ensure_ascii=False),
        q.get('subtotal', 0), q.get('discPct', 0), q.get('discAmount', 0), q.get('igv', 0), q.get('total', 0),
        q.get('paymentMethod', ''), q.get('seller', ''), q.get('status', 'Emitida'),
        datetime.now().isoformat(timespec='seconds'),
    ))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/quotes', methods=['GET'])
def list_quotes():
    err = db_configured_or_error()
    if err: return err
    search = (request.args.get('search') or '').strip().lower()
    status = (request.args.get('status') or '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM quotes ORDER BY created_at DESC')
    rows = cur.fetchall()
    cur.close(); conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['items'] = json.loads(d.pop('items_json') or '[]')
        except ValueError:
            d['items'] = []
        if search and search not in (d.get('client') or '').lower() and search not in (d.get('id') or '').lower():
            continue
        if status and d.get('status') != status:
            continue
        result.append(d)
    return jsonify(result)


@app.route('/api/quotes/<quote_id>/status', methods=['PATCH'])
def update_quote_status(quote_id):
    err = db_configured_or_error()
    if err: return err
    data = request.get_json(force=True) or {}
    status = data.get('status', '')
    if not status:
        return jsonify({'error': 'Falta el nuevo estado.'}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE quotes SET status = %s WHERE id = %s', (status, quote_id))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'ok': True})


# =====================================================================
# Reportes básicos
# =====================================================================

@app.route('/api/reports/summary', methods=['GET'])
def reports_summary():
    err = db_configured_or_error()
    if err: return err
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT client, total, created_at, status FROM quotes')
    rows = cur.fetchall()
    cur.close(); conn.close()

    by_client = defaultdict(float)
    by_month = defaultdict(float)
    by_status = defaultdict(int)
    for r in rows:
        by_client[r['client'] or 'Sin cliente'] += r['total'] or 0
        month = (r['created_at'] or '')[:7]
        if month:
            by_month[month] += r['total'] or 0
        by_status[r['status'] or 'Emitida'] += 1

    top_clients = sorted(by_client.items(), key=lambda x: -x[1])[:10]
    months = sorted(by_month.items())

    return jsonify({
        'total_cotizaciones': len(rows),
        'top_clientes': [{'cliente': c, 'total': round(t, 2)} for c, t in top_clients],
        'por_mes': [{'mes': m, 'total': round(t, 2)} for m, t in months],
        'por_estado': [{'estado': s, 'cantidad': n} for s, n in by_status.items()],
    })


# =====================================================================
# Documentos (catálogo en PDF, brochures, cartas de presentación, etc.)
# =====================================================================

MAX_DOCUMENT_SIZE = 20 * 1024 * 1024  # 20 MB


def ensure_documents_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            filename TEXT NOT NULL,
            content_type TEXT,
            size INTEGER,
            data BYTEA NOT NULL,
            uploaded_at TEXT
        );
    ''')
    conn.commit()
    cur.close(); conn.close()


@app.route('/api/documents', methods=['GET'])
def list_documents():
    err = db_configured_or_error()
    if err: return err
    ensure_documents_table()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name, filename, content_type, size, uploaded_at FROM documents ORDER BY uploaded_at DESC')
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)


@app.route('/api/documents', methods=['POST'])
def upload_document():
    err = db_configured_or_error()
    if err: return err
    ensure_documents_table()

    if 'file' not in request.files:
        return jsonify({'error': 'No se recibió ningún archivo.'}), 400
    file = request.files['file']
    if not file or not file.filename:
        return jsonify({'error': 'No se recibió ningún archivo.'}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Solo se permiten archivos PDF.'}), 400

    data = file.read()
    if len(data) == 0:
        return jsonify({'error': 'El archivo está vacío.'}), 400
    if len(data) > MAX_DOCUMENT_SIZE:
        return jsonify({'error': f'El archivo supera el límite de {MAX_DOCUMENT_SIZE // (1024*1024)} MB.'}), 400

    name = (request.form.get('name') or file.filename).strip()

    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO documents (name, filename, content_type, size, data, uploaded_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, name, filename, content_type, size, uploaded_at
    ''', (name, file.filename, file.content_type or 'application/pdf', len(data), psycopg2.Binary(data),
          datetime.now().isoformat(timespec='seconds')))
    row = dict(cur.fetchone())
    conn.commit()
    cur.close(); conn.close()
    return jsonify(row)


@app.route('/api/documents/<int:doc_id>/download')
def download_document(doc_id):
    err = db_configured_or_error()
    if err: return err
    ensure_documents_table()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT filename, content_type, data FROM documents WHERE id = %s', (doc_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row is None:
        return jsonify({'error': 'No se encontró ese documento.'}), 404
    from flask import Response
    return Response(
        bytes(row['data']),
        mimetype=row['content_type'] or 'application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{row["filename"]}"'}
    )


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
def delete_document(doc_id):
    err = db_configured_or_error()
    if err: return err
    ensure_documents_table()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM documents WHERE id = %s', (doc_id,))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'ok': True})


# =====================================================================
# Consulta de RUC (SUNAT vía Decolecta)
# =====================================================================

@app.route('/api/ruc')
def consultar_ruc():
    numero = request.args.get('numero', '').strip()

    if not (len(numero) == 11 and numero.isdigit()):
        return jsonify({'error': 'RUC inválido. Debe tener 11 dígitos numéricos.'}), 400

    if SUNAT_TOKEN == 'PEGA_AQUI_TU_TOKEN_DE_DECOLECTA' or not SUNAT_TOKEN:
        return jsonify({
            'error': 'Falta configurar el token. Define SUNAT_TOKEN en tu .env o en Render.'
        }), 500

    try:
        resp = requests.get(
            SUNAT_URL,
            params={'numero': numero},
            headers={
                'Authorization': f'Bearer {SUNAT_TOKEN}',
                'Accept': 'application/json',
            },
            timeout=8,
        )
    except requests.RequestException as e:
        return jsonify({'error': f'No se pudo conectar con el servicio de SUNAT: {e}'}), 502

    if resp.status_code in (401, 403):
        return jsonify({'error': 'Token inválido, vencido o sin saldo. Revisa tu cuenta en decolecta.com.'}), 502
    if resp.status_code == 404:
        return jsonify({'error': 'No se encontró ese RUC en SUNAT.'}), 404
    if not resp.ok:
        return jsonify({'error': f'El servicio de SUNAT respondió con un error ({resp.status_code}).'}), 502

    data = resp.json()
    razon = data.get('razonSocial') or data.get('nombre') or data.get('razon_social') or ''
    direccion = data.get('direccion') or ''

    if not razon:
        return jsonify({'error': 'La respuesta no trajo razón social. Verifica el RUC.'}), 502

    return jsonify({'ruc': numero, 'razonSocial': razon, 'direccion': direccion})


@app.route('/api/salud')
def salud():
    """Comprueba que el servidor está corriendo Y que la base de datos realmente responde."""
    token_ok = SUNAT_TOKEN != 'PEGA_AQUI_TU_TOKEN_DE_DECOLECTA' and bool(SUNAT_TOKEN)

    db_ok = False
    db_error = None
    if DATABASE_URL:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute('SELECT 1')
            cur.close()
            conn.close()
            db_ok = True
        except Exception as e:
            db_error = str(e)

    return jsonify({
        'estado': 'ok',
        'token_configurado': token_ok,
        'base_de_datos_configurada': bool(DATABASE_URL),
        'base_de_datos_conectada': db_ok,
        'error_base_de_datos': db_error,
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'\n Backend del cotizador LCS corriendo en el puerto {port}')
    print(' Déjalo abierto mientras uses el cotizador (o súbelo a Render para que corra solo). Ctrl+C para detenerlo.\n')
    app.run(host='0.0.0.0', port=port, debug=True)
