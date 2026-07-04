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
from datetime import datetime, timedelta
import threading
import time

app = Flask(__name__)

# ============================================================
# DATABASE CONFIGURATION
# ============================================================
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# Thresholds
TRAIL_THRESHOLD_METERS = 15.0
POWER_LINE_THRESHOLD_METERS = 30.0
VALID_TRAIL_TYPES = ('Sendero Actual', 'Sendero', 'Carretera')
LOCATION_EXPIRY_SECONDS = 10

# ============================================================
# CARGA DE CAPAS DESDE POSTGIS
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
    print("✅ Capas cargadas.")
except Exception as e:
    print(f"❌ Error al cargar capas: {e}")

# ============================================================
# ALMACENAMIENTO EN MEMORIA DE UBICACIONES
# ============================================================
user_locations = {}
lock = threading.Lock()

def clean_expired_locations():
    now = datetime.utcnow()
    with lock:
        expired = [uid for uid, data in user_locations.items()
                   if (now - data['timestamp']) > timedelta(seconds=LOCATION_EXPIRY_SECONDS)]
        for uid in expired:
            del user_locations[uid]

def cleaner_thread():
    while True:
        time.sleep(5)
        clean_expired_locations()

threading.Thread(target=cleaner_thread, daemon=True).start()

# ============================================================
# ENDPOINT /login
# ============================================================
@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibió JSON"}), 400

        username = data.get('username')
        password_hash = data.get('password_hash')
        if not username or not password_hash:
            return jsonify({"error": "Faltan credenciales"}), 400
        if len(password_hash) != 64 or not all(c in "0123456789abcdef" for c in password_hash.lower()):
            return jsonify({"error": "Hash inválido"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, password_hash FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row is None:
            return jsonify({"success": False, "message": "Usuario no encontrado"}), 401
        user_id, db_username, role, stored_hash = row
        if password_hash == stored_hash:
            return jsonify({
                "success": True,
                "message": "Login exitoso",
                "user": {"id": user_id, "username": db_username, "role": role}
            }), 200
        else:
            return jsonify({"success": False, "message": "Contraseña incorrecta"}), 401
    except Exception as e:
        print(f"❌ Error en login: {e}")
        return jsonify({"error": str(e)}), 500

# ============================================================
# ENDPOINT /check
# ============================================================
@app.route('/check', methods=['POST'])
def check_location():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibió JSON"}), 400

        lat = float(data.get('latitude'))
        lon = float(data.get('longitude'))
        device_id = data.get('device_id')
        username = data.get('username')  # puede ser None

        if not device_id:
            return jsonify({"error": "Falta device_id"}), 400

        point_wkt = f"POINT({lon} {lat})"
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # Sendero más cercano
        cur.execute("""
            SELECT nombre,
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

        # Línea de alta tensión más cercana
        cur.execute("""
            SELECT ST_DistanceSpheroid(
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

        trail_distance = trail['distancia'] if trail else None
        trail_name = trail['nombre'] if trail else None
        power_line_distance = power_line['distancia'] if power_line else None

        # Distancias a capas geográficas
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

        # Clasificación
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

        # ----------------------------------------------------
        # OBTENER ROL DEL USUARIO (si está logueado)
        # ----------------------------------------------------
        role = None
        if username:
            try:
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute("SELECT role FROM users WHERE username = %s", (username,))
                row2 = cur2.fetchone()
                cur2.close()
                conn2.close()
                if row2:
                    role = row2[0]
            except Exception as e:
                print(f"⚠️ Error al obtener rol: {e}")

        # ----------------------------------------------------
        # GUARDAR UBICACIÓN CON ROL
        # ----------------------------------------------------
        display_name = username if username else "Usuario no logeado"
        with lock:
            user_locations[device_id] = {
                "lat": lat,
                "lon": lon,
                "status": status,
                "trail_name": trail_name,
                "distance_meters": trail_distance,
                "display_name": display_name,
                "role": role,
                "is_inside_park": is_inside_park,
                 "power_line_distance": data.get('power_line_distance'),
                "timestamp": datetime.utcnow()
            }

        response = {
            "status": status,
            "message": message,
            "location": {"latitude": lat, "longitude": lon},
            "trail": {
                "name": trail_name,
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

        print(f"📍 {display_name} - {lat}, {lon} → {status} (dentro: {is_inside_park})")
        return jsonify(response), 200

    except Exception as e:
        print(f"❌ Error en /check: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ============================================================
# ENDPOINT /users/locations (solo admin/guard) – FILTRADO POR PARQUE Y EXCLUYE POR device_id
# ============================================================
@app.route('/users/locations', methods=['GET'])
def get_users_locations():
    try:
        username = request.headers.get('X-Username')
        device_id = request.headers.get('X-DeviceId')
        if not username:
            return jsonify({"error": "Falta identificación"}), 401
        if not device_id:
            return jsonify({"error": "Falta device_id"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row is None:
            return jsonify({"error": "Usuario no encontrado"}), 401
        role = row[0]
        if role not in ('admin', 'guard'):
            return jsonify({"error": "Permiso denegado"}), 403

        now = datetime.utcnow()
        with lock:
            users_list = []
            for uid, data in user_locations.items():
                if (now - data['timestamp']).total_seconds() > LOCATION_EXPIRY_SECONDS:
                    continue
                # EXCLUIR EL DISPOSITIVO ACTUAL POR device_id
                if uid == device_id:
                    continue
                # SOLO SI ESTÁ DENTRO DEL PARQUE
                if not data.get('is_inside_park', False):
                    continue
                users_list.append({
                    "display_name": data['display_name'],
                    "latitude": data['lat'],
                    "longitude": data['lon'],
                    "status": data['status'],
                    "trail_name": data.get('trail_name'),
                    "distance_meters": data.get('distance_meters'),
                    "power_line_distance": power_line_distance,
                    "role": data.get('role')  # <-- DEVOLVEMOS EL ROL
                })
        return jsonify({"users": users_list}), 200

    except Exception as e:
        print(f"❌ Error en /users/locations: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server started on http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port)
