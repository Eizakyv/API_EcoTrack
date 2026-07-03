from flask import Flask, request
import psycopg2
import psycopg2.extras
from waitress import serve
import os

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN DE LA BASE DE DATOS (solo variables de entorno)
# ============================================================
DB_CONFIG = {
    "host": os.environ['DB_HOST'],
    "port": int(os.environ['DB_PORT']),
    "database": os.environ['DB_NAME'],
    "user": os.environ['DB_USER'],
    "password": os.environ['DB_PASSWORD']
}

# Umbrales en metros
TRAIL_THRESHOLD_METERS = 15.0
POWER_LINE_THRESHOLD_METERS = 30.0
VALID_TRAIL_TYPES = ('Sendero Actual', 'Sendero', 'Carretera')


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


@app.route('/check', methods=['POST'])
def check_location():
    try:
        data = request.get_json()
        if not data:
            print("❌ Error: No se recibió JSON")
            return "Error: JSON esperado", 400

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
        distancia_trail = trail['distancia'] if trail else float('inf')
        distancia_tension = power_line['distancia'] if power_line else float('inf')

        status = "seguro"
        mensaje = "El usuario está seguro dentro del sendero."

        if distancia_tension < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            mensaje = f"¡PELIGRO CRÍTICO! Cerca de línea de alta tensión ({distancia_tension:.2f} m)."
        elif distancia_trail > TRAIL_THRESHOLD_METERS:
            status = "advertencia"
            mensaje = f"ADVERTENCIA: Fuera del sendero / Perdido ({distancia_trail:.2f} m)."

        # ====================================================
        # SALIDA SIMPLIFICADA EN CONSOLA
        # ====================================================
        print(f"\n=============================================")
        print(f"📍 Ubicación: {lat}, {lon}")

        if status == "seguro":
            print(f"✅ Estado: {status}")
        elif status == "advertencia":
            print(f"⚠️ Estado: {status}")
        else:
            print(f"❌ Estado: {status}")

        print(f"📝 {mensaje}")

        if trail:
            nombre = trail['nombre']
            print(f"🌲 Sendero: '{nombre}'")
            print(f"   Distancia: {distancia_trail:.2f} m")
        else:
            print("🌲 Sendero: No encontrado")
            print("   Distancia: N/A")
        print(f"=============================================")

        print()  # Línea en blanco

        return "OK", 200

    except Exception as e:
        print(f"❌ Error en el servidor: {e}")
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Servidor iniciado con Waitress en http://0.0.0.0:{port}")
    print("📡 Esperando peticiones POST en /check ...")
    serve(app, host='0.0.0.0', port=port)