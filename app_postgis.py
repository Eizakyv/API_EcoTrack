from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from waitress import serve
import os

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN DE LA BASE DE DATOS
# ============================================================
DATABASE_URL = os.environ['DATABASE_URL']

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Umbrales en metros
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
        # 1. SENDERO MÁS CERCANO
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
        # 2. LÍNEA DE ALTA TENSIÓN MÁS CERCANA
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
        # CÁLCULO DE DISTANCIAS Y CLASIFICACIÓN
        # ====================================================
        distancia_trail = trail['distancia'] if trail else None
        distancia_tension = power_line['distancia'] if power_line else None

        status = "seguro"
        mensaje = "El usuario está seguro dentro del sendero."

        if distancia_tension is not None and distancia_tension < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            mensaje = f"¡PELIGRO CRÍTICO! Cerca de línea de alta tensión ({distancia_tension:.2f} m)."
        elif distancia_trail is not None and distancia_trail > TRAIL_THRESHOLD_METERS:
            status = "advertencia"
            mensaje = f"ADVERTENCIA: Fuera del sendero / Perdido ({distancia_trail:.2f} m)."

        # ====================================================
        # CONSTRUIR RESPUESTA JSON
        # ====================================================
        response = {
            "status": status,
            "mensaje": mensaje,
            "ubicacion": {
                "latitud": lat,
                "longitud": lon
            },
            "sendero": {
                "nombre": trail['nombre'] if trail else None,
                "distancia_metros": round(distancia_trail, 2) if distancia_trail is not None else None
            },
            "linea_tension": {
                "distancia_metros": round(distancia_tension, 2) if distancia_tension is not None else None
            }
        }

        # También imprimimos en consola para logs (opcional)
        print(f"\n📍 Ubicación: {lat}, {lon} | Estado: {status} | Sendero: {trail['nombre'] if trail else 'N/A'} | Dist: {distancia_trail if distancia_trail else 'N/A'}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Error en el servidor: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Servidor iniciado con Waitress en http://0.0.0.0:{port}")
    print("📡 Esperando peticiones POST en /check ...")
    serve(app, host='0.0.0.0', port=port)
