from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from waitress import serve
import os
from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.wkt import loads
import pyproj
import hashlib

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

# ============================================================
# CARGA DE CAPAS DESDE POSTGIS (al iniciar)
# ============================================================
wgs84 = pyproj.CRS('EPSG:4326')
utm17n = pyproj.CRS('EPSG:32617')
project_to_meters = pyproj.Transformer.from_crs(wgs84, utm17n, always_xy=True).transform

park_geom = None
research_geom = None
plume_geom = None

def load_geometry_from_db(table_name, geom_column='geom'):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT ST_AsText({geom_column}) FROM {table_name} LIMIT 1;")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            wkt = row[0]
            geom_wgs = loads(wkt)
            if geom_wgs.is_empty:
                print(f"⚠️ Geometría vacía en {table_name}")
                return None
            geom_utm = transform(project_to_meters, geom_wgs)
            return geom_utm
        else:
            print(f"⚠️ No se encontró geometría en {table_name}")
            return None
    except Exception as e:
        print(f"❌ Error al cargar {table_name}: {e}")
        return None

try:
    park_geom = load_geometry_from_db('limites_pnm')
    research_geom = load_geometry_from_db('parcela_1ha_pnm')
    plume_geom = load_geometry_from_db('pluma_grua_pnm')
    print("✅ Capas de límites, parcela de investigación y pluma cargadas desde la base de datos.")
except Exception as e:
    print(f"❌ Error al cargar capas: {e}")

# ============================================================
# ENDPOINT /check
# ============================================================

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibió JSON"}), 400

        username = data.get('username')
        password_hash = data.get('password_hash')  # La app envía el hash SHA-256

        if not username or not password_hash:
            return jsonify({"error": "Faltan credenciales"}), 400

        # Validar que el hash tenga 64 caracteres hexadecimales (opcional)
        if len(password_hash) != 64 or not all(c in "0123456789abcdef" for c in password_hash.lower()):
            return jsonify({"error": "Hash inválido"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row is None:
            return jsonify({"success": False, "message": "Usuario no encontrado"}), 401

        stored_hash = row[0]
        # Comparar directamente los hashes
        if password_hash == stored_hash:
            return jsonify({"success": True, "message": "Login exitoso"}), 200
        else:
            return jsonify({"success": False, "message": "Contraseña incorrecta"}), 401

    except Exception as e:
        print(f"❌ Error en login: {e}")
        return jsonify({"error": str(e)}), 500
        
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
        # 1. SENDERO MÁS CERCANO (SIN EXCLUIR NINGUNO)
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
        # DISTANCIAS Y CLASIFICACIÓN
        # ====================================================
        trail_distance = trail['distancia'] if trail else None
        power_line_distance = power_line['distancia'] if power_line else None

        user_point_wgs = Point(lon, lat)
        user_point_utm = transform(project_to_meters, user_point_wgs)

        park_distance = None
        is_inside_park = False
        if park_geom is not None:
            park_distance = user_point_utm.distance(park_geom)
            is_inside_park = user_point_utm.within(park_geom)
            if is_inside_park:
                park_distance = 0.0

        research_distance = None
        is_inside_research = False
        if research_geom is not None:
            research_distance = user_point_utm.distance(research_geom)
            is_inside_research = user_point_utm.within(research_geom)
            if is_inside_research:
                research_distance = 0.0

        plume_distance = None
        is_inside_plume = False
        if plume_geom is not None:
            plume_distance = user_point_utm.distance(plume_geom)
            is_inside_plume = user_point_utm.within(plume_geom)
            if is_inside_plume:
                plume_distance = 0.0

        status = "seguro"
        message = "Se encuentra dentro del sendero."

        if power_line_distance is not None and power_line_distance < POWER_LINE_THRESHOLD_METERS:
            status = "peligro"
            message = "Cerca de línea de alta tensión."
        else:
            if not is_inside_park and park_distance is not None:
                status = "advertencia"
                message = "Fuera del parque"
            elif is_inside_research and research_distance is not None:
                status = "advertencia"
                message = "Dentro de zona de investigación"
            elif is_inside_plume and plume_distance is not None:
                status = "advertencia"
                message = "Dentro de pluma grúa"
            elif trail_distance is not None and trail_distance > TRAIL_THRESHOLD_METERS:
                status = "advertencia"
                message = "Fuera del sendero"

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
            },
            "plume": {
                "distance_meters": round(plume_distance, 2) if plume_distance is not None else None,
                "inside": is_inside_plume
            }
        }

        print(f"\n📍 Location: {lat}, {lon} | Status: {status} | Msg: {message}")

        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Server error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started on http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port)
