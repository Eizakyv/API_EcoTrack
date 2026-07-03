from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from waitress import serve
import os

app = Flask(__name__)

# ============================================================
# DATABASE CONFIGURATION
# ============================================================
DATABASE_URL = os.environ['DATABASE_URL']

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Thresholds in meters
TRAIL_THRESHOLD_METERS = 15.0
POWER_LINE_THRESHOLD_METERS = 30.0
VALID_TRAIL_TYPES = ('Sendero Actual', 'Sendero', 'Carretera')


@app.route('/check', methods=['POST'])
def check_location():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibió JSON"}), 400

        lat = float(data.get('latitude'))
        lon = float(data.get('longitude'))
        point_wkt = f"POINT({lon} {lat})"

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # ----------------------------------------------------
        # 1. NEAREST TRAIL
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                nombre,
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia
            FROM senderos
            WHERE tipo IN %s
            ORDER BY distancia
            LIMIT 1
        """, (point_wkt, VALID_TRAIL_TYPES))

        trail = cur.fetchone()

        # ----------------------------------------------------
        # 2. NEAREST POWER LINE
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia
            FROM lineas_tension
            ORDER BY distancia
            LIMIT 1
        """, (point_wkt,))

        power_line = cur.fetchone()

        cur.close()
        conn.close()

        # ====================================================
        # DISTANCES AND STATUS CLASSIFICATION
        # ====================================================
        trail_distance = trail['distancia'] if trail else None
        power_line_distance = power_line['distancia'] if power_line else None

        status = "seguro"
        message = "El usuario está seguro dentro del sendero."

        if power_line_distance is not None and power_line_distance < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            message = "Cerca de línea de alta tensión."
        elif trail_distance is not None and trail_distance > TRAIL_THRESHOLD_METERS:
            status = "advertencia"
            message = "Fuera del sendero / Perdido."

        # ====================================================
        # BUILD JSON RESPONSE WITH ENGLISH KEYS
        # ====================================================
        response = {
            "status": status,
            "message": message,
            "location": {
                "latitude": lat,
                "longitude": lon
            },
            "trail": {
                "name": trail['nombre'] if trail else None,
                "distance_meters": round(trail_distance, 2) if trail_distance is not None else None
            },
            "powerLine": {
                "distance_meters": round(power_line_distance, 2) if power_line_distance is not None else None
            }
        }

        print(f"\n📍 Location: {lat}, {lon} | Status: {status} | Trail: {trail['nombre'] if trail else 'N/A'} | Dist: {trail_distance if trail_distance else 'N/A'}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Server error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started with Waitress on http://0.0.0.0:{port}")
    print("📡 Waiting for POST requests on /check ...")
    serve(app, host='0.0.0.0', port=port)from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from waitress import serve
import os

app = Flask(__name__)

# ============================================================
# DATABASE CONFIGURATION
# ============================================================
DATABASE_URL = os.environ['DATABASE_URL']

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Thresholds in meters
TRAIL_THRESHOLD_METERS = 15.0
POWER_LINE_THRESHOLD_METERS = 30.0
VALID_TRAIL_TYPES = ('Sendero Actual', 'Sendero', 'Carretera')


@app.route('/check', methods=['POST'])
def check_location():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibió JSON"}), 400

        lat = float(data.get('latitude'))
        lon = float(data.get('longitude'))
        point_wkt = f"POINT({lon} {lat})"

        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # ----------------------------------------------------
        # 1. NEAREST TRAIL
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                nombre,
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia
            FROM senderos
            WHERE tipo IN %s
            ORDER BY distancia
            LIMIT 1
        """, (point_wkt, VALID_TRAIL_TYPES))

        trail = cur.fetchone()

        # ----------------------------------------------------
        # 2. NEAREST POWER LINE
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia
            FROM lineas_tension
            ORDER BY distancia
            LIMIT 1
        """, (point_wkt,))

        power_line = cur.fetchone()

        cur.close()
        conn.close()

        # ====================================================
        # DISTANCES AND STATUS CLASSIFICATION
        # ====================================================
        trail_distance = trail['distancia'] if trail else None
        power_line_distance = power_line['distancia'] if power_line else None

        status = "seguro"
        message = "El usuario está seguro dentro del sendero."

        if power_line_distance is not None and power_line_distance < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            message = f"Cerca de línea de alta tensión."
        elif trail_distance is not None and trail_distance > TRAIL_THRESHOLD_METERS:
            status = "advertencia"
            message = f"Fuera del sendero / Perdido."

        # ====================================================
        # BUILD JSON RESPONSE WITH ENGLISH KEYS
        # ====================================================
        response = {
            "status": status,                     # "seguro", "advertencia", "peligro"
            "message": message,                   # Texto en español
            "location": {
                "latitude": lat,
                "longitude": lon
            },
            "trail": {
                "name": trail['nombre'] if trail else None,
                "distance_meters": round(trail_distance, 2) if trail_distance is not None else None
            },
            "powerLine": {
                "distance_meters": round(power_line_distance, 2) if power_line_distance is not None else None
            }
        }

        # Log en consola (con nombres en inglés)
        print(f"\n📍 Location: {lat}, {lon} | Status: {status} | Trail: {trail['nombre'] if trail else 'N/A'} | Dist: {trail_distance if trail_distance else 'N/A'}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Server error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started with Waitress on http://0.0.0.0:{port}")
    print("📡 Waiting for POST requests on /check ...")
    serve(app, host='0.0.0.0', port=port)
