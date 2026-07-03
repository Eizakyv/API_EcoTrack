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
        # 1. SENDERO MÁS CERCANO (fuera de zona de investigación)
        # ----------------------------------------------------
        # Primero obtenemos el polígono de la zona de investigación
        cur.execute("""
            SELECT ST_AsText(geom) AS wkt
            FROM parcela_1ha_pnm
            LIMIT 1
        """)
        research_zone = cur.fetchone()
        research_wkt = research_zone['wkt'] if research_zone else None

        if research_wkt is not None:
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
                  AND NOT ST_Intersects(geom, ST_SetSRID(ST_GeomFromText(%s, 4326), 4326))
                ORDER BY distancia
                LIMIT 1
            """, (point_wkt, VALID_TRAIL_TYPES, research_wkt))
        else:
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

        # ----------------------------------------------------
        # 3. VERIFICAR SI ESTÁ DENTRO DEL PARQUE
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia,
                ST_Contains(geom, ST_SetSRID(ST_GeomFromText(%s, 4326), 4326)) AS dentro
            FROM limites_pnm
            LIMIT 1
        """, (point_wkt, point_wkt))
        park = cur.fetchone()

        # ----------------------------------------------------
        # 4. VERIFICAR SI ESTÁ DENTRO DE ZONA DE INVESTIGACIÓN
        # ----------------------------------------------------
        cur.execute("""
            SELECT 
                ST_DistanceSpheroid(
                    ST_SetSRID(ST_GeomFromText(%s, 4326), 4326),
                    geom,
                    'SPHEROID["WGS 84",6378137,298.257223563]'
                ) AS distancia,
                ST_Contains(geom, ST_SetSRID(ST_GeomFromText(%s, 4326), 4326)) AS dentro
            FROM parcela_1ha_pnm
            LIMIT 1
        """, (point_wkt, point_wkt))
        research = cur.fetchone()

        cur.close()
        conn.close()

        # ====================================================
        # DISTANCIAS Y CLASIFICACIÓN
        # ====================================================
        trail_distance = trail['distancia'] if trail else None
        power_line_distance = power_line['distancia'] if power_line else None

        park_distance = park['distancia'] if park else None
        is_inside_park = park['dentro'] if park else False

        research_distance = research['distancia'] if research else None
        is_inside_research = research['dentro'] if research else False

        # --- Clasificación (prioridad: peligro > advertencia > seguro) ---
        status = "seguro"
        message = "Se encuentra dentro del sendero."

        if power_line_distance is not None and power_line_distance < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            message = "Cerca de línea de alta tensión."
        else:
            # Fuera del parque
            if park is not None and not is_inside_park:
                status = "advertencia"
                message = f"Fuera del parque (distancia: {park_distance:.2f} m)"
            # Dentro de zona de investigación
            elif research is not None and is_inside_research:
                status = "advertencia"
                message = "Dentro de zona de investigación"
            # Fuera del sendero
            elif trail_distance is not None and trail_distance > TRAIL_THRESHOLD_METERS:
                status = "advertencia"
                message = f"Fuera del sendero (distancia: {trail_distance:.2f} m)"

        # ====================================================
        # RESPUESTA JSON
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
            },
            "park": {
                "distance_meters": round(park_distance, 2) if park_distance is not None else None,
                "inside": is_inside_park
            },
            "researchZone": {
                "distance_meters": round(research_distance, 2) if research_distance is not None else None,
                "inside": is_inside_research
            }
        }

        print(f"\n📍 Location: {lat}, {lon} | Status: {status} | Msg: {message}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Server error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started on http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port)
